#!/usr/bin/env node
"use strict";

// Browser-side SSO handles the vendor's cookies and device checks. Only the
// final authorization code leaves this helper, through stdout to the CLI.
const path = require("node:path");
const { createRequire } = require("node:module");

async function main() {
  const playwrightRoot = process.env.ROCKETNOW_PLAYWRIGHT_ROOT;
  const load = playwrightRoot
    ? createRequire(path.join(path.resolve(playwrightRoot), "package.json"))
    : require;
  let chromium, webkit;
  try {
    ({ chromium, webkit } = load("playwright"));
  } catch (error) {
    if (error.code === "MODULE_NOT_FOUND") {
      throw new Error("Playwright not found; run npm install and set ROCKETNOW_PLAYWRIGHT_ROOT to the project directory");
    }
    throw error;
  }
  const landingUrl = process.argv[2];
  if (!landingUrl || !landingUrl.startsWith("https://member.rocketnow.co.jp/sso/v2/landing.pang?")) {
    throw new Error("Expected a Rocket Now SSO landing URL");
  }
  const browserKind = process.env.ROCKETNOW_BROWSER === "webkit" ? "webkit" : "chrome";
  const browser = browserKind === "webkit"
    ? await webkit.launch({ headless: process.env.ROCKETNOW_HEADLESS === "1" })
    : await chromium.launch({ channel: "chrome", headless: process.env.ROCKETNOW_HEADLESS === "1" });
  const context = await browser.newContext({
    locale: "ja-JP",
    timezoneId: "Asia/Tokyo",
    viewport: { width: 402, height: 874 },
    deviceScaleFactor: 3,
    isMobile: true,
    hasTouch: true,
    userAgent: "Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.6.1 Mobile/15E148 Safari/604.1",
  });
  const page = await context.newPage();
  let settled = false;
  let timer;

  const result = new Promise((resolve, reject) => {
    timer = setTimeout(() => reject(new Error("Login timed out")), 15 * 60 * 1000);
    page.on("response", async (response) => {
      const responseUrl = new URL(response.url());
      if (responseUrl.hostname === "member.rocketnow.co.jp"
          && responseUrl.pathname === "/sso/smsVerification.pang"
          && response.status() >= 400) {
        reject(new Error(`SSO denied the SMS page (HTTP ${response.status()}); use auth pair-proxy`));
        return;
      }
      if (!response.url().startsWith("https://member.rocketnow.co.jp/login/v2/signin")) return;
      try {
        const body = await response.json();
        const redirect = body?.data?.authRedirectUrl;
        if (!redirect) {
          if (body?.success === false && !body?.data?.redirectPageUrl) {
            reject(new Error(`SSO signin failed (${body?.resultTypeCode || "unknown"})`));
          }
          return;
        }
        const url = new URL(redirect);
        if (url.protocol !== "rocketnowauth:" || url.pathname !== "/oauth2redirect") return;
        const code = url.searchParams.get("code");
        const state = url.searchParams.get("state");
        if (!code || !state) return;
        settled = true;
        clearTimeout(timer);
        resolve({ code, state });
      } catch (error) {
        // Intermediate or failed signin responses are expected during MFA.
      }
    });
    page.on("close", () => {
      if (!settled) reject(new Error("Login window closed before completion"));
    });
  });

  try {
    const landingResponse = await page.goto(landingUrl, { waitUntil: "domcontentloaded" });
    if (!landingResponse || landingResponse.status() >= 400) {
      throw new Error(`SSO landing returned HTTP ${landingResponse?.status() ?? "unknown"}`);
    }
    const authCode = await result;
    process.stdout.write(JSON.stringify(authCode) + "\n");
  } finally {
    settled = true;
    clearTimeout(timer);
    await browser.close();
  }
}

main().catch((error) => {
  const summary = String(error.message).split("\n")[0].split(" at https://")[0];
  process.stderr.write(`Login failed: ${summary}\n`);
  process.exitCode = 1;
});
