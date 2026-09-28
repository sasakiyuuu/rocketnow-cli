"""Private relay for two GMO handoff endpoints, without Rocket Now credentials."""
from __future__ import annotations

import base64
import gzip
import json
import sys
import urllib.error
import urllib.request
import zlib
from urllib.parse import parse_qs, urlsplit

ALLOWED_HEADERS = {"user-agent", "accept", "accept-language", "content-type", "cookie"}
FORBIDDEN_HEADERS = {"authorization", "dpop", "x-sso-auth", "x-coupang-sec-token-binding", "rocketpay-app"}


def validated_request(config: dict) -> tuple[str, str, dict, bytes | None]:
    method, url = config["method"].upper(), config["url"]
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in {None, 443} or parsed.fragment:
        raise ValueError("Gateway URL rejected")
    body = base64.b64decode(config["bodyBase64"], validate=True) if config.get("bodyBase64") is not None else None
    incoming = config.get("headers", {})
    headers = {}
    for key, value in incoming.items():
        lower = key.lower()
        if lower in FORBIDDEN_HEADERS:
            raise ValueError("Merchant credential rejected")
        if lower == "cookie" and any(pair.split("=", 1)[0].strip().lower() == "ct_ath" for pair in value.split(";")):
            raise ValueError("Merchant cookie rejected")
        if lower in ALLOWED_HEADERS:
            if lower in headers:
                raise ValueError("Duplicate header rejected")
            headers[lower] = value
    headers["accept-encoding"] = "identity"
    if method == "GET" and parsed.hostname == "global.openapi.mul-pay.jp" and parsed.path == "/wallet/startSession":
        fields = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
        if set(fields) != {"p"} or len(fields["p"]) != 1 or not fields["p"][0] or body not in {None, b""}:
            raise ValueError("Gateway start parameters rejected")
    elif method == "POST" and parsed.hostname == "p01.mul-pay.jp" and parsed.path == "/payment/PaypayStart.idPass":
        if parsed.query or body is None or headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/x-www-form-urlencoded":
            raise ValueError("Gateway form rejected")
        fields = parse_qs(body.decode("ascii"), keep_blank_values=True, strict_parsing=True)
        if set(fields) != {"AccessID", "Token"} or any(len(v) != 1 or not v[0] for v in fields.values()):
            raise ValueError("Gateway form fields rejected")
    else:
        raise ValueError("Gateway endpoint rejected")
    return method, url, headers, body


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
    raise ValueError("Unsupported gateway encoding")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def main() -> None:
    config = json.loads(sys.stdin.buffer.read(2 * 1024 * 1024))
    method, url, headers, body = validated_request(config)
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        response = urllib.request.build_opener(NoRedirect()).open(req, timeout=30)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        raw = decoded_body(response.read(), response.headers.get("Content-Encoding", ""))
        result = {"status": response.status, "headers": list(response.headers.items()),
            "setCookies": response.headers.get_all("Set-Cookie", []), "bodyBase64": base64.b64encode(raw).decode("ascii")}
    sys.stdout.write(json.dumps(result))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        sys.stderr.write("Native gateway fetch failed; private request was not logged.\n")
        sys.exit(1)
