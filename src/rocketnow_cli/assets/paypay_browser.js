#!/usr/bin/env node
"use strict";

// Private configuration arrives on stdin. Stdout contains only sanitized events.
const crypto = require("node:crypto");
const fs = require("node:fs/promises");
const path = require("node:path");
const os = require("node:os");
const { spawn } = require("node:child_process");
const { createRequire } = require("node:module");
process.umask(0o077);
const MERCHANTS = new Set(["pay.rocketnow.co.jp", "payment.rocketnow.co.jp"]);
const b64 = (value) => Buffer.from(value).toString("base64url");

function signer(pem) {
  const key = crypto.createPrivateKey(pem);
  const exported = crypto.createPublicKey(key).export({ format: "jwk" });
  if (exported.kty !== "EC" || exported.crv !== "P-256") throw new Error("Expected P-256 key");
  const jwk = { crv: exported.crv, kty: exported.kty, x: exported.x, y: exported.y };
  return {
    jwk,
    thumbprint: b64(crypto.createHash("sha256").update(JSON.stringify(jwk)).digest()),
    proof(method, address, token) {
      const url = new URL(address);
      if (!isMerchant(url)) throw new Error("Proof requires merchant HTTPS URL");
      const header = { alg: "ES256", jwk, typ: "dpop+jwt" };
      const payload = {
        htm: method.toUpperCase(), htu: url.origin + url.pathname,
        iat: Math.floor(Date.now() / 1000), jti: crypto.randomUUID(),
        ath: b64(crypto.createHash("sha256").update(token).digest()),
      };
      const input = b64(JSON.stringify(header)) + "." + b64(JSON.stringify(payload));
      const signature = crypto.sign("sha256", Buffer.from(input), { key, dsaEncoding: "ieee-p1363" });
      return input + "." + b64(signature);
    },
  };
}

function isMerchant(url) {
  return url.protocol === "https:" && MERCHANTS.has(url.hostname) && (!url.port || url.port === "443");
}

function approvedGatewayStart(address) {
  try {
    const url = new URL(address);
    return url.protocol === "https:" && url.hostname === "global.openapi.mul-pay.jp"
      && (!url.port || url.port === "443") && !url.username && !url.password
      && url.pathname === "/wallet/startSession";
  } catch (_) { return false; }
}

function merchantGatewayStart(html) {
  const found = /<textarea\b[^>]*\bid=["']redirectUrl["'][^>]*>([^<]*)<\/textarea>/i.exec(html);
  if (!found) return null;
  let address;
  try {
    address = found[1].replace(/&(?:#x([0-9a-f]+)|#([0-9]+)|amp|quot|apos|lt|gt);/gi, (entity, hex, decimal) => {
      if (hex) return String.fromCodePoint(parseInt(hex, 16));
      if (decimal) return String.fromCodePoint(parseInt(decimal, 10));
      return {"&amp;":"&","&quot;":"\"","&apos;":"'","&lt;":"<","&gt;":">"}[entity.toLowerCase()] || entity;
    });
  } catch (_) { return null; }
  if (!approvedGatewayStart(address)) return null;
  const url = new URL(address);
  const keys = [...url.searchParams.keys()];
  if (keys.length !== 1 || keys[0] !== "p" || !url.searchParams.get("p")) return null;
  // The fragment is local to the browser. HTTP requests and DPoP targets omit it.
  url.hash = "";
  return url.href;
}

function resolvedGatewayStart(address, approvedAddress) {
  if (!approvedGatewayStart(address) || !approvedAddress) return address;
  const url = new URL(address);
  const keys = [...url.searchParams.keys()];
  return keys.length === 1 && keys[0] === "success" && url.searchParams.get("success") === "true"
    && merchantGatewayStart(`<textarea id="redirectUrl">${approvedAddress}</textarea>`) === approvedAddress
    ? approvedAddress : address;
}

function approvedPaypayCashier(address) {
  try { const u=new URL(address);return u.protocol==="https:"&&u.hostname==="www.paypay.ne.jp"&&u.pathname==="/app/cashier"&&(!u.port||u.port==="443")&&!u.username&&!u.password; } catch(_){return false;}
}

function nativeReturn(html, expectedOrderId) {
  if (!/^\d+$/.test(String(expectedOrderId || ""))) return null;
  const match = /parent\.location\.href\s*=\s*(['"])([^\r\n]*?)\1/.exec(html);
  if (!match) return null;
  // The gateway escapes URL separators in its JavaScript string. Decode only
  // those separators; never evaluate gateway code or decode '+' as a space.
  const address = match[2].replace(/\\+([/?&=])/g, "$1");
  if (address.includes("\\")) return null;
  try {
    const native = new URL(address);
    if (native.protocol !== "rocketnowcustomer:" || native.hostname !== "redirectPayment"
        && native.hostname !== "redirectpayment") return null;
    if (native.username || native.password || native.port || !["", "/"].includes(native.pathname)) return null;
    const fields = {};
    for (const pair of native.search.slice(1).split("&")) {
      const at = pair.indexOf("=");
      if (at < 1) return null;
      const key = decodeURIComponent(pair.slice(0, at));
      if (Object.hasOwn(fields, key)) return null;
      fields[key] = decodeURIComponent(pair.slice(at + 1));
    }
    if (fields.orderId !== String(expectedOrderId) || !fields.p || fields.p.length > 1024 || /\s/.test(fields.p)) return null;
    const target = new URL(fields.returnUrl);
    const prefix = /^\/wallet-authentication\/auth-result\/GMO\/PAYPAY\/\d+\/EATS\/EATS\/(\d+)$/.exec(target.pathname);
    if (!isMerchant(target) || target.hostname !== "pay.rocketnow.co.jp" || target.username || target.password
        || !prefix || prefix[1] !== String(expectedOrderId) || target.hash) return null;
    target.searchParams.set("p", fields.p);
    return target.href;
  } catch (_) { return null; }
}

function stage(url) {
  if (url.hostname === "global.openapi.mul-pay.jp" && url.pathname === "/wallet/callbackSession") return "gateway_callback";
  if (isMerchant(url)) return "merchant";
  if (url.hostname === "paypay.ne.jp" || url.hostname.endsWith(".paypay.ne.jp")) return "paypay";
  if (url.hostname === "mul-pay.jp" || url.hostname.endsWith(".mul-pay.jp")) return "gateway";
  return "other";
}

function safeLocation(address) {
  const url = new URL(address);
  // Never expose query, fragments, or path components that can be opaque tokens.
  const knownPaths = new Set(["/wallet/startSession", "/wallet/callbackSession", "/app/cashier", "/payments/i18n/payment", "/"]);
  return { host: url.hostname, path: knownPaths.has(url.pathname) ? url.pathname : "/[redacted]", stage: stage(url) };
}

function merchantFailure(address, status) {
  const url = new URL(address);
  if (!isMerchant(url) || url.pathname !== "/payments/i18n/payment") return null;
  if (status === 401) return "merchant_auth_rejected";
  if (status === 403) return "merchant_access_denied";
  return null;
}

function requestHeaders(original, captured, address, method, token, dpop) {
  const headers = {};
  const url = new URL(address);
  const capturedLower = Object.fromEntries(Object.entries(captured || {}).map(([key, value]) => [key.toLowerCase(), String(value)]));
  const ownProof = (value) => {
    try {
      const jwk = JSON.parse(Buffer.from(value.split(".")[0], "base64url")).jwk;
      return jwk && ["kty", "crv", "x", "y"].every((key) => jwk[key] === dpop.jwk[key]);
    } catch (_) { return false; }
  };
  const capturedPrivate = new Set(["x-sso-auth", "x-coupang-sec-token-binding", "rocketpay-app"]);
  for (const [name, value] of Object.entries(original)) {
    const lower = name.toLowerCase();
    if (lower === "authorization" && (isMerchant(url) || value === "DPoP " + token)) continue;
    if (lower === "dpop" && (isMerchant(url) || ownProof(value))) continue;
    if (!isMerchant(url) && capturedPrivate.has(lower) && value === capturedLower[lower]) continue;
    if (lower === "cookie" && !isMerchant(url)) {
      headers[lower] = value.split(";").filter((pair) => !/^\s*CT_ATH\s*=/i.test(pair)).join(";");
    } else headers[lower] = value;
  }
  if (isMerchant(url)) {
    for (const [name, value] of Object.entries(captured || {})) {
      const lower = name.toLowerCase();
      // Cookies belong in the cookie jar; transport headers belong to Chromium.
      if (!["authorization", "dpop", "cookie", "host", "content-length", "connection", "origin", "referer"].includes(lower)
          && !lower.startsWith(":")) headers[lower] = String(value);
    }
    headers.authorization = "DPoP " + token;
    headers.dpop = dpop.proof(method, address, token);
    for (const name of Object.keys(headers)) {
      if (name.startsWith("sec-ch-ua") || method.toUpperCase() === "GET"
          && (name === "referer" || name === "origin" || name.startsWith("sec-fetch-"))) delete headers[name];
    }
  }
  return headers;
}

async function privateWrite(directory, name, value) {
  const target = path.join(directory, name);
  await fs.writeFile(target, value, { mode: 0o600 });
  await fs.chmod(target, 0o600);
}

function nativeFetch(executable, request, helper = "native_merchant_fetch.py") {
  if (!path.isAbsolute(executable || "")) return Promise.reject(new Error("Absolute Python executable required"));
  return new Promise((resolve, reject) => {
    const child = spawn(executable, [path.join(__dirname, helper)], { stdio: ["pipe", "pipe", "pipe"] });
    let output = "";
    child.stdout.on("data", (chunk) => { output += chunk; });
    child.stderr.resume(); // Never forward child diagnostics that might contain request data.
    child.on("error", () => reject(new Error("Native merchant process failed")));
    child.on("close", (code) => {
      if (code !== 0) return reject(new Error("Native merchant fetch failed"));
      try { resolve(JSON.parse(output)); } catch (_) { reject(new Error("Invalid native merchant response")); }
    });
    child.stdin.on("error", () => {});
    child.stdin.end(JSON.stringify(request));
  });
}

function responseCookies(values, address, rootDomain = "rocketnow.co.jp") {
  const url = new URL(address);
  const cookies = [];
  for (const line of values || []) {
    const fields = line.split(";");
    const at = fields[0].indexOf("=");
    if (at < 1) continue;
    const cookie = { name: fields[0].slice(0, at).trim(), value: fields[0].slice(at + 1).trim(),
      domain: url.hostname, path: "/", secure: true };
    for (const field of fields.slice(1)) {
      const split = field.indexOf("=");
      const key = (split < 0 ? field : field.slice(0, split)).trim().toLowerCase();
      const value = split < 0 ? "" : field.slice(split + 1).trim();
      if (key === "domain") cookie.domain = value;
      if (key === "path") cookie.path = value;
      if (key === "httponly") cookie.httpOnly = true;
      if (key === "samesite" && /^(strict|lax|none)$/i.test(value)) cookie.sameSite = value[0].toUpperCase() + value.slice(1).toLowerCase();
      if (key === "expires" && Number.isFinite(Date.parse(value))) cookie.expires = Math.floor(Date.parse(value) / 1000);
      if (key === "max-age" && /^-?\d+$/.test(value)) cookie.expires = Math.max(1, Math.floor(Date.now() / 1000) + Number(value));
    }
    const domain = cookie.domain.replace(/^\./, "");
    if ((domain === url.hostname || domain === rootDomain) && cookie.path.startsWith("/")) cookies.push(cookie);
  }
  return cookies;
}

async function main() {
  let input = "";
  for await (const chunk of process.stdin) {
    input += chunk;
    if (input.length > 1024 * 1024) throw new Error("Configuration too large");
  }
  const config = JSON.parse(input);
  if (config.expectedAmount !== 1000) throw new Error("Expected authorized amount of 1000 JPY");
  if (!isMerchant(new URL(config.paymentUrl))) throw new Error("Expected merchant payment URL");
  if (typeof config.accessToken !== "string" || !config.accessToken) throw new Error("Missing access token");
  if (!path.isAbsolute(config.outputDir || "")) throw new Error("Private outputDir is required");
  await fs.mkdir(config.outputDir, { recursive: true, mode: 0o700 });
  const directory = await fs.realpath(config.outputDir);
  if (directory.startsWith("/Volumes/") || !directory.startsWith(os.homedir() + path.sep)) {
    throw new Error("Private outputDir must be under internal home directory");
  }
  await fs.chmod(directory, 0o700);
  const dpop = signer(config.privateKeyPem);
  const load = process.env.ROCKETNOW_PLAYWRIGHT_ROOT
    ? createRequire(path.join(path.resolve(process.env.ROCKETNOW_PLAYWRIGHT_ROOT), "package.json")) : require;
  const { chromium } = load("playwright");
  const profilePath = config.profileDir || path.join(directory, "browser-profile");
  await fs.mkdir(profilePath, { recursive: true, mode: 0o700 });
  const profileDirectory = await fs.realpath(profilePath);
  if (config.profileDir && !profileDirectory.startsWith(path.join(os.homedir(), "Library", "Application Support", "rocketnow-cli", "payment-browser") + path.sep)) {
    throw new Error("Reused profile must belong to the private payment browser");
  }
  const context = await chromium.launchPersistentContext(profileDirectory, {
    channel: "chrome", headless: config.headless === true, locale: "ja-JP", timezoneId: "Asia/Tokyo",
    viewport: { width: 440, height: 850 }, serviceWorkers: "block",
  });
  await fs.chmod(profileDirectory, 0o700);
  const emit = (event) => process.stdout.write(JSON.stringify(event) + "\n");
  const openedGatewayUrls = new Set();
  await context.exposeBinding("__rocketnowOpenGateway", async (source, address) => {
    if (!isMerchant(new URL(source.frame.url())) || !approvedGatewayStart(address)) {
      emit({ event: "external_bridge_rejected" });
      return;
    }
    if (config.stopAfterInitialNavigation === true) {
      emit({ event: "external_bridge_suppressed_for_dry_run" });
      return;
    }
    if (openedGatewayUrls.has(address)) return;
    openedGatewayUrls.add(address);
    const externalPage = await context.newPage();
    emit({ event: "gateway_tab_opened", ...safeLocation(address) });
    await externalPage.goto(address, { waitUntil: "domcontentloaded", timeout: 60000 })
      .catch(() => emit({ event: "gateway_navigation_incomplete" }));
  });
  await context.addInitScript(() => {
    if (location.protocol !== "https:" || !["pay.rocketnow.co.jp", "payment.rocketnow.co.jp"].includes(location.hostname)
        || (location.port && location.port !== "443")) return;
    const webkit = window.webkit || (window.webkit = {});
    const handlers = webkit.messageHandlers || (webkit.messageHandlers = {});
    handlers.openExternalHTTP = {
      postMessage(address) { window.__rocketnowOpenGateway(address).catch(() => {}); },
    };
  });
  const cookies = config.cookies || [];
  if (!Array.isArray(cookies)) throw new Error("cookies must be an array");
  for (const cookie of cookies) {
    const domain = String(cookie.domain || "").replace(/^\./, "");
    const cookieUrl = cookie.url ? new URL(cookie.url) : null;
    if (!MERCHANTS.has(domain) && domain !== "rocketnow.co.jp" && !(cookieUrl && isMerchant(cookieUrl))) {
      throw new Error("Captured cookies must belong to Rocket Now");
    }
  }
  if (config.cookieHeader) {
    for (const pair of config.cookieHeader.split(";")) {
      const split = pair.indexOf("=");
      if (split <= 0) continue;
      cookies.push({ name: pair.slice(0, split).trim(), value: pair.slice(split + 1).trim(),
        url: new URL(config.paymentUrl).origin, secure: true });
    }
  }
  if (cookies.length) await context.addCookies(cookies);
  let replayInitial = null;
  if (config.initialResponseFile) {
    const replayPath = await fs.realpath(config.initialResponseFile);
    const privateRoot = path.join(os.homedir(), "Library", "Application Support", "rocketnow-cli", "payment-browser") + path.sep;
    if (!replayPath.startsWith(privateRoot)) throw new Error("Initial replay must belong to private payment captures");
    replayInitial = JSON.parse(await fs.readFile(replayPath, "utf8"));
    const html = Buffer.from(replayInitial.bodyBase64 || "", "base64").toString("utf8");
    if (replayInitial.url !== config.paymentUrl || replayInitial.status !== 200
        || Date.now() - replayInitial.capturedAt > 300000 || !html.includes("paymentInit") || !html.includes("authUrl")) {
      throw new Error("Initial replay is stale or is not a payment initialization page");
    }
  }
  let gatewayReplay = null;
  if (config.gatewayResponseFile) {
    const gatewayPath = await fs.realpath(config.gatewayResponseFile);
    if (!gatewayPath.startsWith(path.join(os.homedir(), "Library", "Application Support", "rocketnow-cli") + path.sep)) throw new Error("Gateway replay must be private");
    gatewayReplay = JSON.parse(await fs.readFile(gatewayPath, "utf8"));
    if (!approvedGatewayStart(gatewayReplay.url) || gatewayReplay.url !== config.startUrl || gatewayReplay.status !== 200
        || Date.now() - gatewayReplay.capturedAt > 300000 || (gatewayReplay.setCookies || []).length) throw new Error("Gateway replay is invalid or stale");
  }
  let resumePaypayApproved = false;
  if (config.paypayResumeFile) {
    const resumePath = await fs.realpath(config.paypayResumeFile);
    if (!resumePath.startsWith(path.join(os.homedir(), "Library", "Application Support", "rocketnow-cli", "payment-browser") + path.sep)) throw new Error("PayPay resume proof must be private");
    const proof=JSON.parse(await fs.readFile(resumePath,"utf8")),source=new URL(proof.url);
    const html=Buffer.from(proof.bodyBase64||"","base64").toString("utf8").replaceAll("&amp;","&");
    resumePaypayApproved = proof.status===200&&source.hostname==="p01.mul-pay.jp"&&source.pathname==="/payment/PaypayStart.idPass"
      &&Date.now()-proof.capturedAt<300000&&approvedPaypayCashier(config.startUrl)&&html.includes(config.startUrl);
    if (!resumePaypayApproved) throw new Error("PayPay resume proof is stale or does not match");
  }
  if (config.startUrl && !approvedGatewayStart(config.startUrl) && !resumePaypayApproved) throw new Error("Only a linked gateway or verified PayPay start can resume this browser");
  let approvedWalletGatewayUrl = null;
  let gatewayStartSelected = false;
  await context.route("**/*", async (route) => {
    const request = route.request();
    if (new URL(request.url()).hostname === "csg.rocketnow.co.jp"
        && !["GET", "HEAD", "OPTIONS"].includes(request.method())) {
      await route.abort();
      emit({ event: "order_api_mutation_blocked", ...safeLocation(request.url()) });
      return;
    }
    const headers = requestHeaders(await request.allHeaders(), config.headers, request.url(), request.method(), config.accessToken, dpop);
    if (config.nativeMerchantFetch === true && request.method() === "GET"
        && new URL(request.url()).pathname === "/payments/i18n/payment" && isMerchant(new URL(request.url()))) {
      for (const [name,value] of Object.entries(config.headers || {})) {
        if (name.toLowerCase().startsWith("sec-fetch-")) headers[name.toLowerCase()]=String(value);
      }
    }
    const host = new URL(request.url()).hostname;
    if (["global.openapi.mul-pay.jp", "p01.mul-pay.jp"].includes(host) && config.gatewayUserAgent) headers["user-agent"] = config.gatewayUserAgent;
    if (config.nativeGatewayFetch === true && (approvedGatewayStart(request.url()) && request.method() === "GET"
        || host === "p01.mul-pay.jp" && new URL(request.url()).protocol === "https:"
          && new URL(request.url()).pathname === "/payment/PaypayStart.idPass" && request.method() === "POST")) {
      try {
        const body = request.postDataBuffer();
        const providerHeaders = {"User-Agent":config.gatewayUserAgent || config.headers["User-Agent"] || config.headers["user-agent"]};
        for (const name of ["accept","accept-language","content-type","cookie"]) if (headers[name]) providerHeaders[name]=headers[name];
        const effectiveUrl = gatewayStartSelected ? request.url() : resolvedGatewayStart(request.url(), approvedWalletGatewayUrl);
        if (effectiveUrl !== request.url()) {
          gatewayStartSelected = true;
          emit({event:"merchant_gateway_start_selected",...safeLocation(request.url())});
        }
        const gatewayRequest={url:effectiveUrl,method:request.method(),headers:providerHeaders,bodyBase64:body===null?null:body.toString("base64")};
        await privateWrite(directory,"gateway-attempt.json",JSON.stringify(gatewayRequest));
        const response = await nativeFetch(config.pythonExecutable,gatewayRequest,"native_gateway_fetch.py");
        const cookies = responseCookies(response.setCookies,request.url(),"mul-pay.jp");
        if (cookies.length) await context.addCookies(cookies);
        await privateWrite(directory, "gateway-"+request.method().toLowerCase()+"-response.json", JSON.stringify({url:request.url(),capturedAt:Date.now(),...response}));
        const responseHeaders = {};
        for (const [name,value] of response.headers) if (!["content-length","content-encoding","transfer-encoding","connection","set-cookie"].includes(name.toLowerCase())) responseHeaders[name]=value;
        await route.fulfill({status:response.status,headers:responseHeaders,body:Buffer.from(response.bodyBase64,"base64")});
        return;
      } catch (_) { await route.abort(); emit({event:"gateway_request_failed",code:_.name,...safeLocation(request.url())}); return; }
    }
    if (gatewayReplay && request.method() === "GET" && request.url() === gatewayReplay.url) {
      const response = gatewayReplay;
      gatewayReplay = null;
      const responseHeaders = {};
      for (const [name,value] of response.headers) if (!["content-length","content-encoding","transfer-encoding","connection","set-cookie"].includes(name.toLowerCase())) responseHeaders[name]=value;
      await route.fulfill({status:200,headers:responseHeaders,body:Buffer.from(response.bodyBase64,"base64")});
      emit({event:"gateway_start_replayed",...safeLocation(request.url())});
      return;
    }
    // route.continue overrides can survive redirects. Fetch each merchant hop
    // with redirects disabled, then let the browser follow it through this route.
    if (isMerchant(new URL(request.url()))) {
      try {
        if (config.stopAfterInitialNavigation === true && request.isNavigationRequest()) {
          await privateWrite(directory, "merchant-request.json", JSON.stringify({ url: request.url(), method: request.method(), headers }));
        }
        if (config.nativeMerchantFetch === true) {
          const body = request.postDataBuffer();
          if (request.method() === "POST" && new URL(request.url()).pathname === "/wallet-authentication/GMO/PAYPAY") {
            await privateWrite(directory,"wallet-auth-request.json",JSON.stringify({url:request.url(),method:request.method(),headers,bodyBase64:body===null?null:body.toString("base64")}));
          }
          let response;
          if (replayInitial && request.method() === "GET" && request.url() === config.paymentUrl) {
            response = replayInitial;
            replayInitial = null;
            emit({ event: "initial_page_replayed", ...safeLocation(request.url()) });
          } else response = await nativeFetch(config.pythonExecutable, { url: request.url(), method: request.method(), headers,
            bodyBase64: body === null ? null : body.toString("base64") });
          if (request.method() === "POST" && new URL(request.url()).pathname === "/wallet-authentication/GMO/PAYPAY" && response.status === 200) {
            approvedWalletGatewayUrl = merchantGatewayStart(Buffer.from(response.bodyBase64,"base64").toString("utf8"));
            if (approvedWalletGatewayUrl) emit({event:"merchant_gateway_url_verified",...safeLocation(approvedWalletGatewayUrl)});
          }
          if (request.method() === "GET" && request.url() === config.paymentUrl) {
            await privateWrite(directory, "native-initial-response.json", JSON.stringify({
              url: request.url(), capturedAt: Date.now(), ...response,
            }));
          }
          const cookies = responseCookies(response.setCookies, request.url());
          if (cookies.length) await context.addCookies(cookies);
          const responseHeaders = {};
          for (const [name, value] of response.headers) {
            if (!["set-cookie", "content-length", "content-encoding", "transfer-encoding", "connection"].includes(name.toLowerCase())) responseHeaders[name] = value;
          }
          await route.fulfill({ status: response.status, headers: responseHeaders, body: Buffer.from(response.bodyBase64, "base64") });
        } else {
          const response = await route.fetch({ headers, maxRedirects: 0 });
          await route.fulfill({ response });
        }
      } catch (_) { await route.abort(); emit({ event: "merchant_request_failed", ...safeLocation(request.url()) }); }
    } else await route.continue({ headers });
  });
  let sequence = 0;
  const resumedCallbacks = new Set();
  const watch = (page) => {
    page.on("framenavigated", (frame) => {
      if (frame === page.mainFrame() && /^https?:/.test(frame.url())) emit({ event: "navigation", ...safeLocation(frame.url()) });
    });
    page.on("response", async (response) => {
      if (!response.request().isNavigationRequest()) return;
      const address = response.url();
      if (!/^https?:/.test(address)) return;
      emit({ event: "navigation_response", status: response.status(), ...safeLocation(address) });
      const failure = merchantFailure(address, response.status());
      if (failure) emit({ event: failure, status: response.status(), ...safeLocation(address) });
      if (stage(new URL(address)) === "gateway_callback" || isMerchant(new URL(address))
          || approvedGatewayStart(address) || (new URL(address).hostname === "p01.mul-pay.jp" && new URL(address).pathname === "/payment/PaypayStart.idPass")) {
        try {
          const number = String(++sequence).padStart(3, "0");
          const body = await response.body();
          await privateWrite(directory, `return-${number}.html`, body);
          await privateWrite(directory, `return-${number}.json`, JSON.stringify({ url: address, status: response.status(), headers: await response.headersArray() }));
          await privateWrite(directory, "browser-state.json", JSON.stringify(await context.storageState()));
          if (stage(new URL(address)) === "gateway_callback" && !resumedCallbacks.has(address)) {
            const returnUrl = nativeReturn(body.toString("utf8"), config.expectedOrderId);
            if (returnUrl && config.stopAfterInitialNavigation !== true) {
              resumedCallbacks.add(address);
              emit({ event: "native_return_resumed", ...safeLocation(returnUrl) });
              await page.goto(returnUrl, { waitUntil: "domcontentloaded", timeout: 60000 })
                .catch(() => emit({ event: "native_return_navigation_incomplete" }));
            } else emit({ event: "native_return_unverified" });
          }
        } catch (_) { emit({ event: "private_capture_failed", ...safeLocation(address) }); }
      }
    });
  };
  context.on("page", watch);
  for (const page of context.pages()) watch(page);
  const page = await context.newPage();
  emit({ event: "browser_ready", expectedAmount: config.expectedAmount });
  emit({ event: "amount_unverified", expectedAmount: config.expectedAmount, requiresUserConfirmation: true });
  await page.goto(config.startUrl || config.paymentUrl, { waitUntil: "domcontentloaded", timeout: 60000 }).catch((error) => emit({ event: "initial_navigation_incomplete", code: /ERR_[A-Z_]+/.exec(String(error.message))?.[0] || error.name }));
  if (config.stopAfterInitialNavigation === true) {
    await new Promise((resolve) => setTimeout(resolve, 1000));
    emit({ event: "initial_navigation_complete", ...safeLocation(page.url()) });
    await context.close();
    emit({ event: "browser_closed" });
    return;
  }
  // Closing the browser is the only automatic stop; callbacks never close it.
  await new Promise((resolve) => context.on("close", resolve));
  emit({ event: "browser_closed" });
}

module.exports = { signer, isMerchant, approvedGatewayStart, nativeReturn, safeLocation, requestHeaders, responseCookies, merchantFailure, nativeFetch, merchantGatewayStart, resolvedGatewayStart };
if (require.main === module) main().catch(() => {
  process.stderr.write("Payment browser failed; private configuration and URLs were not logged.\n");
  process.exitCode = 1;
});
