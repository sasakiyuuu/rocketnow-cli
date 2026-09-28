"""Read a small, private catalog snapshot from the owner's local phone proxy.

The proxy supplies already-observed app responses. No app credential or DPoP
proof is copied into the CLI, and only public menu fields are retained.
"""

from __future__ import annotations

from datetime import datetime, timezone
from html.parser import HTMLParser
from http.cookiejar import CookieJar
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable
from urllib import error, parse, request


_BASE = "http://127.0.0.1:8081"
_PATHS = {
    "/endpoint/store.get_clp_categories",
    "/endpoint/store.get_clp",
    "/endpoint/store.get_store_with_menu",
}
_MAX_AGE_SECONDS = 24 * 60 * 60
_SNAPSHOT = Path.home() / "Library" / "Application Support" / "rocketnow-cli" / "catalog-snapshot.json"


class _LoginForm(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.token: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        fields = dict(attrs)
        if tag == "input" and fields.get("name") == "_xsrf":
            self.token = fields.get("value")


def _proxy_password() -> str:
    pids = subprocess.check_output(
        ["lsof", "-t", "-iTCP:8081", "-sTCP:LISTEN"], text=True, timeout=3
    ).splitlines()
    if len(pids) != 1 or not pids[0].isdigit():
        raise RuntimeError("Local phone proxy web interface is unavailable")
    command = subprocess.check_output(
        ["ps", "-p", pids[0], "-o", "command="], text=True, timeout=3
    )
    if "mitmweb" not in command:
        raise RuntimeError("Port 8081 is not the expected local phone proxy")
    supplied = os.environ.get("ROCKETNOW_MITMWEB_PASSWORD")
    if supplied:
        return supplied
    match = re.search(r"--set web_password=(\S+)", command)
    if match:
        return match.group(1)
    if sys.stdin.isatty():
        from getpass import getpass
        supplied = getpass("Local mitmweb web token: ")
        if supplied:
            return supplied
    raise RuntimeError("Set ROCKETNOW_MITMWEB_PASSWORD to the local proxy web token")


def _proxy_client() -> Any:
    client = request.build_opener(request.HTTPCookieProcessor(CookieJar()))
    try:
        response = client.open(_BASE, timeout=5)
    except error.HTTPError as exc:
        response = exc
    with response:
        form = _LoginForm()
        form.feed(response.read().decode("utf-8", errors="replace"))
    if not form.token:
        raise RuntimeError("Local phone proxy login form is unavailable")
    body = parse.urlencode({"token": _proxy_password(), "_xsrf": form.token}).encode()
    with client.open(_BASE, data=body, timeout=5):
        pass
    return client


def _name(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value[:160] if value else None


def _numeric_id(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    value = str(value)
    return value if value.isascii() and value.isdecimal() and len(value) <= 20 else None


def _flow_coordinates(flow: dict[str, Any]) -> tuple[float, float] | None:
    headers = (flow.get("request") or {}).get("headers") or []
    if isinstance(headers, dict):
        headers = headers.items()
    if not isinstance(headers, (list, tuple)) and not hasattr(headers, "__iter__"):
        return None
    raw = next((item[1] for item in headers
                if isinstance(item, (list, tuple)) and len(item) == 2 and
                isinstance(item[0], str) and item[0].lower() == "x-eats-location"), None)
    if not isinstance(raw, str):
        return None
    try:
        location = json.loads(raw)
        latitude, longitude = float(location["latitude"]), float(location["longitude"])
        if (math.isfinite(latitude) and math.isfinite(longitude) and
                -90 <= latitude <= 90 and -180 <= longitude <= 180):
            return latitude, longitude
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        pass
    return None


def _nearby(a: tuple[float, float], b: tuple[float, float]) -> bool:
    """Keep capture groups within roughly 1.5 km without persisting coordinates."""
    midlatitude = math.radians((a[0] + b[0]) / 2)
    north = (a[0] - b[0]) * 111.2
    east = (a[1] - b[1]) * 111.2 * math.cos(midlatitude)
    return math.hypot(north, east) <= 1.5


def _categories(data: dict[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for item in data.get("list") or []:
        if not isinstance(item, dict):
            continue
        identifier, name = _numeric_id(item.get("id")), _name(item.get("name"))
        if identifier and name:
            found.append({"id": int(identifier), "name": name})
    return found[:100]


def _stores(data: dict[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in data.get("entityList") or []:
        if not isinstance(item, dict) or item.get("viewType") != "storeCardWithMenu":
            continue
        raw = (item.get("entity") or {}).get("data") or {}
        if not isinstance(raw, dict):
            continue
        identifier, name = _numeric_id(raw.get("id")), _name(raw.get("name"))
        if not identifier or not name or identifier in seen:
            continue
        seen.add(identifier)
        row: dict[str, Any] = {"id": int(identifier), "name": name}
        if isinstance(raw.get("openStatus"), str):
            row["openStatus"] = raw["openStatus"][:32]
        eta = _name(raw.get("estimatedDeliveryTime"))
        if eta:
            row["estimatedDeliveryTime"] = eta
        rating = raw.get("reviewRating")
        if isinstance(rating, (int, float)) and not isinstance(rating, bool) and 0 <= rating <= 5:
            row["reviewRating"] = rating
        found.append(row)
    return found[:80]


def _menu(data: dict[str, Any]) -> dict[str, Any] | None:
    identifier, name = _numeric_id(data.get("id")), _name(data.get("name"))
    if not identifier or not name:
        return None
    dishes: list[dict[str, Any]] = []
    seen: set[str] = set()
    for group in data.get("menus") or []:
        if not isinstance(group, dict):
            continue
        for raw in group.get("dishes") or []:
            if not isinstance(raw, dict):
                continue
            dish_id, dish_name = _numeric_id(raw.get("id")), _name(raw.get("name"))
            price = raw.get("salePrice")
            if (not dish_id or not dish_name or dish_id in seen or
                    not isinstance(price, (int, float)) or isinstance(price, bool) or
                    price < 0 or price > 50000 or int(price) != price):
                continue
            seen.add(dish_id)
            dishes.append({
                "id": int(dish_id), "name": dish_name, "price": int(price),
                "available": raw.get("displayStatus") == "ON_SALE",
            })
    return {"id": int(identifier), "name": name, "dishes": dishes[:250]}


def catalog_from_flows(
    flows: list[dict[str, Any]],
    read_response: Callable[[str], dict[str, Any]],
    *,
    now: float | None = None,
) -> dict[str, Any]:
    """Extract only public catalog fields from a recent, allowlisted flow set."""
    now = time.time() if now is None else now
    latest_categories: tuple[float, str] | None = None
    latest_clp: dict[str, tuple[float, str]] = {}
    latest_menu: dict[str, tuple[float, str]] = {}
    anchor: tuple[float, tuple[float, float]] | None = None
    for flow in flows:
        if not isinstance(flow, dict):
            continue
        req, resp = flow.get("request") or {}, flow.get("response") or {}
        timestamp = req.get("timestamp_start")
        if (req.get("host") != "csg.rocketnow.co.jp" or
                req.get("method") != "GET" or resp.get("status_code") != 200 or
                not isinstance(timestamp, (int, float)) or
                not now - _MAX_AGE_SECONDS <= timestamp <= now + 60 or
                parse.urlsplit(req.get("path") or "").path != "/endpoint/store.get_clp"):
            continue
        coordinates = _flow_coordinates(flow)
        if coordinates and (anchor is None or timestamp > anchor[0]):
            anchor = (timestamp, coordinates)

    for flow in flows:
        if not isinstance(flow, dict):
            continue
        req, resp = flow.get("request") or {}, flow.get("response") or {}
        timestamp = req.get("timestamp_start")
        if (req.get("host") != "csg.rocketnow.co.jp" or
                req.get("method") != "GET" or resp.get("status_code") != 200 or
                not isinstance(timestamp, (int, float)) or
                not now - _MAX_AGE_SECONDS <= timestamp <= now + 60):
            continue
        parsed = parse.urlsplit(req.get("path") or "")
        if parsed.path not in _PATHS:
            continue
        flow_id = flow.get("id")
        if not isinstance(flow_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", flow_id):
            continue
        params = parse.parse_qs(parsed.query)
        if parsed.path.endswith("get_clp_categories"):
            if latest_categories is None or timestamp > latest_categories[0]:
                latest_categories = (timestamp, flow_id)
        elif parsed.path.endswith("get_clp"):
            if anchor and (not (coordinates := _flow_coordinates(flow)) or
                           not _nearby(anchor[1], coordinates)):
                continue
            cid = _numeric_id((params.get("categoryId") or [None])[0])
            if cid and (cid not in latest_clp or timestamp > latest_clp[cid][0]):
                latest_clp[cid] = (timestamp, flow_id)
        else:
            if anchor and (not (coordinates := _flow_coordinates(flow)) or
                           not _nearby(anchor[1], coordinates)):
                continue
            sid = _numeric_id((params.get("storeId") or [None])[0])
            if sid and (sid not in latest_menu or timestamp > latest_menu[sid][0]):
                latest_menu[sid] = (timestamp, flow_id)

    if latest_categories is None and not latest_clp:
        raise RuntimeError("No recent app category traffic was captured")

    def content(flow_id: str) -> dict[str, Any]:
        response = read_response(flow_id)
        if not isinstance(response, dict) or not isinstance(response.get("data"), dict):
            return {}
        return response["data"]

    categories = _categories(content(latest_categories[1])) if latest_categories else []
    names = {str(item["id"]): item["name"] for item in categories}
    category_stores = {}
    newest = latest_categories[0] if latest_categories else 0.0
    for cid, (timestamp, flow_id) in latest_clp.items():
        newest = max(newest, timestamp)
        category_stores[cid] = {
            "name": names.get(cid), "stores": _stores(content(flow_id)),
            "capturedAt": datetime.fromtimestamp(timestamp, timezone.utc).isoformat(),
        }
    menus = {}
    for sid, (timestamp, flow_id) in latest_menu.items():
        parsed_menu = _menu(content(flow_id))
        if parsed_menu and str(parsed_menu["id"]) == sid:
            menus[sid] = parsed_menu
    return {
        "categories": categories, "categoryStores": category_stores,
        "menuByStore": menus, "capturedAt": datetime.fromtimestamp(newest, timezone.utc).isoformat(),
        "source": "app_proxy_live",
    }


def _save_snapshot(snapshot: dict[str, Any]) -> None:
    _SNAPSHOT.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _SNAPSHOT.parent.chmod(0o700)
    fd, name = tempfile.mkstemp(prefix=".catalog-", dir=_SNAPSHOT.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(snapshot, output, ensure_ascii=False)
        os.replace(name, _SNAPSHOT)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def load_app_catalog() -> dict[str, Any]:
    """Read current app traffic from the local proxy, with a clearly labeled cache."""
    try:
        client = _proxy_client()
        with client.open(_BASE + "/flows", timeout=12) as response:
            flows = json.load(response)
        if not isinstance(flows, list):
            raise RuntimeError("Unexpected proxy flow listing")

        def read_response(flow_id: str) -> dict[str, Any]:
            with client.open(_BASE + "/flows/" + flow_id + "/response/content.data", timeout=5) as response:
                return json.load(response)

        snapshot = catalog_from_flows(flows, read_response)
        _save_snapshot(snapshot)
        return snapshot
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, error.URLError, json.JSONDecodeError):
        if _SNAPSHOT.is_file():
            cached = json.loads(_SNAPSHOT.read_text(encoding="utf-8"))
            cached["source"] = "app_proxy_cache"
            return cached
        raise RuntimeError("No app catalog capture is available; open a category on the iPhone's 8080 proxy") from None
