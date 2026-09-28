"use strict";
// Offline process dispatch check: gateway traffic must never use the merchant
// helper, which intentionally rejects GMO hosts.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { EventEmitter } = require("node:events");
const target = path.resolve(__dirname, "../src/rocketnow_cli/assets/paypay_browser.js");
const calls = [];
const fakeSpawn = (executable, args) => {
  const child = new EventEmitter();
  child.stdout = new EventEmitter();
  child.stderr = { resume() {} };
  child.stdin = new EventEmitter();
  child.stdin.end = (input) => {
    calls.push({ executable, args, request: JSON.parse(input) });
    queueMicrotask(() => {
      child.stdout.emit("data", JSON.stringify({ status: 200, headers: [], setCookies: [], bodyBase64: "" }));
      child.emit("close", 0);
    });
  };
  return child;
};
const moduleObject = { exports: {} };
const fakeRequire = (name) => name === "node:child_process" ? { spawn: fakeSpawn } : require(name);
fakeRequire.main = null;
vm.runInNewContext(fs.readFileSync(target, "utf8"), {
  require: fakeRequire, module: moduleObject, __dirname: path.dirname(target), Buffer, URL,
  process: { umask() {} }, setTimeout, clearTimeout,
}, { filename: target });
(async () => {
  const request = { method: "GET", url: "https://global.openapi.mul-pay.jp/wallet/startSession?p=offline",
    headers: { "User-Agent": "native" }, bodyBase64: null };
  const result = await moduleObject.exports.nativeFetch("/offline/python", request, "native_gateway_fetch.py");
  assert.equal(result.status, 200);
  assert.equal(path.basename(calls[0].args[0]), "native_gateway_fetch.py");
  assert.deepEqual(calls[0].request, request);
  await moduleObject.exports.nativeFetch("/offline/python", { ...request, url: "https://pay.rocketnow.co.jp/payments/test" });
  assert.equal(path.basename(calls[1].args[0]), "native_merchant_fetch.py");
  console.log("Native merchant/gateway helper dispatch checks passed");
})().catch((error) => { console.error(error); process.exitCode = 1; });
