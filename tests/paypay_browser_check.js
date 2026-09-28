"use strict";
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const { signer, requestHeaders, safeLocation, approvedGatewayStart, nativeReturn, responseCookies } = require("../src/rocketnow_cli/assets/paypay_browser.js");
const { privateKey, publicKey } = crypto.generateKeyPairSync("ec", { namedCurve: "prime256v1" });
const dpop = signer(privateKey.export({ type: "pkcs8", format: "pem" }));
const token = "offline-test-token";
const address = "https://payment.rocketnow.co.jp/payments/i18n/payment?paymentToken=secret";
const proof = dpop.proof("GET", address, token);
const [header, body, signature] = proof.split(".");
const decoded = JSON.parse(Buffer.from(body, "base64url"));
assert.equal(decoded.htu, "https://payment.rocketnow.co.jp/payments/i18n/payment");
assert.equal(decoded.ath, crypto.createHash("sha256").update(token).digest("base64url"));
assert.equal(decoded.htm, "GET");
assert.ok(Math.abs(decoded.iat - Date.now() / 1000) < 2);
assert.notEqual(proof, dpop.proof("GET", address, token));
assert.ok(crypto.verify("sha256", Buffer.from(header + "." + body), { key: publicKey, dsaEncoding: "ieee-p1363" }, Buffer.from(signature, "base64url")));
assert.equal(dpop.thumbprint, crypto.createHash("sha256").update(JSON.stringify(dpop.jwk)).digest("base64url"));
const original = { Authorization: "DPoP " + token, DPoP: proof, "X-SSO-Auth": "secret", "Rocketpay-App": "secret", "X-Coupang-Sec-Token-Binding": "secret", cookie: "CT_ATH=secret; gateway_session=keep" };
const captured = { "X-SSO-Auth": "secret", "Rocketpay-App": "secret", "X-Coupang-Sec-Token-Binding": "secret" };
const external = requestHeaders(original, captured, "https://global.openapi.mul-pay.jp/wallet/callbackSession", "POST", token, dpop);
assert.deepEqual(external, { cookie: " gateway_session=keep" });
const merchant = requestHeaders(original, { "X-SSO-Auth": "captured-secret" }, address, "GET", token, dpop);
assert.equal(merchant.authorization, "DPoP " + token);
assert.equal(merchant["x-sso-auth"], "captured-secret");
assert.ok(merchant.dpop);
const lookalike = requestHeaders(original, {}, "https://payment.rocketnow.co.jp.attacker.example/", "GET", token, dpop);
assert.equal(lookalike.authorization, undefined);
const provider = requestHeaders({ Authorization: "Bearer provider-token", DPoP: "provider-proof", "X-SSO-Auth": "provider-owned" }, captured,
  "https://www.paypay.ne.jp/api/payment", "POST", token, dpop);
assert.deepEqual(provider, { authorization: "Bearer provider-token", dpop: "provider-proof", "x-sso-auth": "provider-owned" });
assert.equal(safeLocation("https://example.test/wallet/status-by-token/secret?secret=more").path, "/[redacted]");
assert.equal(safeLocation("https://global.openapi.mul-pay.jp/wallet/callbackSession?token=secret").stage, "gateway_callback");
assert.ok(approvedGatewayStart("https://global.openapi.mul-pay.jp/wallet/startSession?secret=redacted"));
for (const address of ["http://global.openapi.mul-pay.jp/wallet/startSession", "https://global.openapi.mul-pay.jp.attacker.test/wallet/startSession",
  "https://global.openapi.mul-pay.jp/wallet/callbackSession", "https://global.openapi.mul-pay.jp:444/wallet/startSession",
  "https://user:pass@global.openapi.mul-pay.jp/wallet/startSession", "javascript:alert(1)"]) assert.equal(approvedGatewayStart(address), false);
const returnBase = "https://pay.rocketnow.co.jp/wallet-authentication/auth-result/GMO/PAYPAY/123/EATS/EATS/456";
const callback = (base = returnBase, orderId = "456", p = "proof+literal") =>
  `<script>parent.location.href='rocketnowcustomer://redirectPayment\\?returnUrl=${encodeURIComponent(base)}&orderId=${orderId}&p=${p}';</script>`;
const resumed = new URL(nativeReturn(callback(), "456"));
assert.equal(resumed.searchParams.get("p"), "proof+literal");
assert.equal(nativeReturn(callback(), undefined), null);
assert.equal(nativeReturn(callback(), "999"), null);
assert.equal(nativeReturn(callback(returnBase.replace("/456", "/999")), "456"), null);
assert.equal(nativeReturn(callback(returnBase.replace("pay.rocketnow.co.jp", "pay.rocketnow.co.jp.attacker.test")), "456"), null);
assert.equal(nativeReturn(callback().replace("rocketnowcustomer:", "attacker:"), "456"), null);
const merchantGet = requestHeaders({ "sec-ch-ua": "chrome", "sec-fetch-site": "same-origin", origin: "chrome", referer: "chrome" },
  { "sec-fetch-mode": "navigate" }, address, "GET", token, dpop);
for (const key of ["sec-ch-ua", "sec-fetch-site", "sec-fetch-mode", "origin", "referer"]) assert.equal(merchantGet[key], undefined);
const merchantPost = requestHeaders({ "sec-ch-ua": "chrome", origin: "merchant", referer: "merchant" }, {}, address, "POST", token, dpop);
assert.equal(merchantPost.origin, "merchant");
assert.equal(merchantPost.referer, "merchant");
assert.equal(merchantPost["sec-ch-ua"], undefined);
const externalFetch = requestHeaders({ "sec-ch-ua": "chrome", origin: "provider", referer: "provider" }, {}, "https://www.paypay.ne.jp/", "GET", token, dpop);
assert.equal(externalFetch["sec-ch-ua"], "chrome");
const seeded = responseCookies(["merchant=value; Domain=.rocketnow.co.jp; Path=/; HttpOnly; Secure; SameSite=Lax", "bad=value; Domain=paypay.ne.jp; Path=/"], address);
assert.equal(seeded.length, 1);
assert.equal(seeded[0].domain, ".rocketnow.co.jp");
assert.equal(seeded[0].httpOnly, true);
assert.equal(seeded[0].sameSite, "Lax");
console.log("PayPay browser offline signing and credential isolation checks passed");

const {merchantFailure}=require("../src/rocketnow_cli/assets/paypay_browser.js");
assert.equal(merchantFailure("https://payment.rocketnow.co.jp/payments/i18n/payment?token=offline",401),"merchant_auth_rejected");
assert.equal(merchantFailure("https://payment.rocketnow.co.jp/payments/i18n/payment",403),"merchant_access_denied");
assert.equal(merchantFailure("https://payment.rocketnow.co.jp/payments/i18n/payment",200),null);
assert.equal(merchantFailure("https://www.paypay.ne.jp/api/login",401),null);

const {merchantGatewayStart,resolvedGatewayStart}=require("../src/rocketnow_cli/assets/paypay_browser.js");
const authorized="https://global.openapi.mul-pay.jp/wallet/startSession?p=opaque123";
assert.equal(merchantGatewayStart(`<textarea id="redirectUrl">${authorized}</textarea>`),authorized);
assert.equal(merchantGatewayStart('<textarea id="redirectUrl">https://global.openapi.mul-pay.jp/wallet/startSession?success=true</textarea>'),null);
assert.equal(resolvedGatewayStart('https://global.openapi.mul-pay.jp/wallet/startSession?success=true',authorized),authorized);
assert.equal(resolvedGatewayStart('https://global.openapi.mul-pay.jp/wallet/startSession?success=false',authorized),'https://global.openapi.mul-pay.jp/wallet/startSession?success=false');
assert.equal(resolvedGatewayStart('https://global.openapi.mul-pay.jp.attacker.test/wallet/startSession?success=true',authorized),'https://global.openapi.mul-pay.jp.attacker.test/wallet/startSession?success=true');

assert.equal(merchantGatewayStart(`<textarea id="redirectUrl">${authorized}#browser-state</textarea>`),authorized);
assert.equal(resolvedGatewayStart('https://global.openapi.mul-pay.jp/wallet/startSession?success=true',merchantGatewayStart(`<textarea id="redirectUrl">${authorized}#browser-state</textarea>`)),authorized);

assert.equal(merchantGatewayStart('<textarea id="redirectUrl">https://global.openapi.mul-pay.jp/wallet/startSession?p=&#65;opaque123</textarea>'),'https://global.openapi.mul-pay.jp/wallet/startSession?p=Aopaque123');
