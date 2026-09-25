"""Import payment setup from the account owner's local mitmweb capture."""

from __future__ import annotations

import html
import http.cookiejar
import json
import re
from urllib import error, parse, request

from .order import save_payment_config


def _require_local_mitmweb(url: str) -> str:
    parsed = parse.urlsplit(url)
    if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("mitmweb URL must point to local HTTP loopback")
    if parsed.username or parsed.password or parsed.path not in ("", "/"):
        raise ValueError("mitmweb URL must be its local origin")
    return url.rstrip("/") + "/"


def _open_authenticated(url: str, password: str):
    jar = http.cookiejar.CookieJar()
    opener = request.build_opener(request.HTTPCookieProcessor(jar))
    try:
        page = opener.open(url, timeout=10).read().decode()
    except error.HTTPError as exc:
        if exc.code != 403:
            raise
        page = exc.read().decode()
    match = re.search(r'name="_xsrf" value="([^"]+)', page)
    if not match:
        raise ValueError("mitmweb login form was not found")
    data = parse.urlencode({"token": password, "_xsrf": html.unescape(match.group(1))}).encode()
    try:
        opener.open(request.Request(url, data=data), timeout=10).read()
    except error.HTTPError as exc:
        raise ValueError(f"mitmweb login failed (HTTP {exc.code})") from None
    return opener


def _payment_config_from_flows(flows: list[dict], content_getter) -> dict:
    candidates = sorted(
        (
            flow for flow in flows
            if (flow.get("request") or {}).get("host") == "csg.rocketnow.co.jp"
            and (flow.get("request") or {}).get("method") == "POST"
            and parse.urlsplit((flow.get("request") or {}).get("path") or "").path
            == "/endpoint/checkout.prepay"
            and (flow.get("response") or {}).get("status_code") == 200
        ),
        key=lambda flow: flow["request"]["timestamp_start"],
        reverse=True,
    )
    for flow in candidates:
        try:
            response = json.loads(content_getter(flow["id"], "response"))
            if response.get("error") is not None or not (response.get("data") or {}).get("success"):
                continue
            body = json.loads(content_getter(flow["id"], "request"))
            config = {
                "merchantMallKey": body["payment"]["merchantMallKey"],
                "deviceUserAgent": body["deviceInfo"]["userAgent"],
                "preferredPayMethodId": body["payment"]["payMethodId"],
            }
            if config["merchantMallKey"] and config["deviceUserAgent"] and config["preferredPayMethodId"]:
                return config
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
    raise ValueError("No successful checkout.prepay flow was found in mitmweb")


def import_payment_config(url: str, password: str) -> None:
    base = _require_local_mitmweb(url)
    opener = _open_authenticated(base, password)
    flows = json.load(opener.open(base + "flows", timeout=20))
    if not isinstance(flows, list):
        raise ValueError("mitmweb returned an unexpected flow list")

    def content(flow_id: str, side: str) -> bytes:
        return opener.open(base + f"flows/{flow_id}/{side}/content.data", timeout=20).read()

    save_payment_config(_payment_config_from_flows(flows, content))
