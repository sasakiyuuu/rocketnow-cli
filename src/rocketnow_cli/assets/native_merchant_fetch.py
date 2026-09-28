"""Private stdin/stdout transport for merchant web pages only; no redirects."""
from __future__ import annotations

import base64
import gzip
import json
from http.cookies import SimpleCookie
import sys
import urllib.error
import urllib.request
import zlib
from pathlib import Path
from urllib.parse import urlsplit

sys.path[:0] = [str(Path(__file__).resolve().parents[2]), str(Path(__file__).resolve().parents[3] / ".deps")]


def allowed(method: str, url: str) -> bool:
    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError:
        return False
    if parsed.scheme != "https" or parsed.hostname not in {"pay.rocketnow.co.jp", "payment.rocketnow.co.jp"}:
        return False
    if port not in {None, 443} or parsed.username or parsed.password or method not in {"GET", "POST"}:
        return False
    p = parsed.path
    if any(segment.lower() in {"prepay", "confirm", "create", "submit"} for segment in p.split("/")):
        return False
    if p.startswith(("/payments/", "/wallet-authentication/")):
        return True
    return method == "GET" and (p == "/registration/pay-type/selection" or p.startswith("/api/v1/wallet/order/status-by-token/")
        or p.startswith(("/resources/", "/static/", "/assets/"))
        or p.endswith((".js", ".css", ".png", ".jpg", ".svg", ".ico", ".woff", ".woff2")))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def decoded_body(body: bytes, encoding: str) -> bytes:
    if not encoding or encoding.lower() == "identity":
        return body
    if encoding.lower() == "gzip":
        return gzip.decompress(body)
    if encoding.lower() == "deflate":
        try:
            return zlib.decompress(body)
        except zlib.error:
            return zlib.decompress(body, -zlib.MAX_WBITS)
    raise ValueError("Unsupported merchant response encoding")


def main() -> None:
    config = json.loads(sys.stdin.buffer.read(2 * 1024 * 1024))
    method, url = config["method"].upper(), config["url"]
    if not allowed(method, url):
        raise ValueError("Merchant web request rejected")
    from rocketnow_cli.auth import load_session
    session = load_session()
    headers = {}
    for key, value in config.get("headers", {}).items():
        lower = key.lower()
        if lower in {"authorization", "dpop", "host", "content-length", "connection", "accept-encoding"} or lower.startswith("sec-ch-ua"):
            continue
        if (lower.startswith("sec-fetch-") and urlsplit(url).path != "/payments/i18n/payment") or method == "GET" and lower in {"origin", "referer"}:
            continue
        headers[key] = value
    headers["Accept-Encoding"] = "identity"
    headers["Authorization"] = "DPoP " + session.access_token
    headers["DPoP"] = session.signer.proof(method, url, session.access_token)
    if getattr(session, "sso_auth_header", None):
        headers = {k: v for k, v in headers.items() if k.lower() != "x-sso-auth"}
        headers["X-SSO-Auth"] = session.sso_auth_header
    if getattr(session, "access_token_hash", None):
        cookie = SimpleCookie()
        if not (method == "GET" and urlsplit(url).path == "/payments/i18n/payment"):
            cookie.load(next((v for k, v in headers.items() if k.lower() == "cookie"), ""))
        cookie["CT_ATH"] = session.access_token_hash
        headers = {k: v for k, v in headers.items() if k.lower() != "cookie"}
        headers["Cookie"] = "; ".join(m.OutputString() for m in cookie.values())
    body = base64.b64decode(config["bodyBase64"], validate=True) if config.get("bodyBase64") is not None else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        response = urllib.request.build_opener(NoRedirect()).open(req, timeout=30)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        raw = decoded_body(response.read(), response.headers.get("Content-Encoding", ""))
        result = {"status": response.status, "headers": list(response.headers.items()),
            "setCookies": response.headers.get_all("Set-Cookie", []),
            "bodyBase64": base64.b64encode(raw).decode("ascii")}
    sys.stdout.write(json.dumps(result))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        sys.stderr.write("Native merchant fetch failed; private request was not logged.\n")
        sys.exit(1)
