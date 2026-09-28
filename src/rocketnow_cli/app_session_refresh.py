"""Find a bound access-token rotation in the owner's local app proxy."""

from __future__ import annotations

import base64
import json
import re
import time
from urllib import parse

from .auth import Session
from .proxy_catalog import _BASE, _proxy_client


_HOST = "csg.rocketnow.co.jp"
_PATH = "/endpoint/account.member_extra_info"
_MAX_FLOW_LIST_BYTES = 32 * 1024 * 1024
_MAX_CANDIDATES = 100
_FLOW_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")


def _header(headers: object, name: str) -> str | None:
    if isinstance(headers, dict):
        pairs = headers.items()
    elif isinstance(headers, list):
        pairs = headers
    else:
        return None
    for pair in pairs:
        if (isinstance(pair, (list, tuple)) and len(pair) == 2
                and isinstance(pair[0], str) and pair[0].lower() == name):
            return pair[1] if isinstance(pair[1], str) else None
    return None


def _claims(token: str) -> dict:
    part = token.split(".")[1]
    value = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    if not isinstance(value, dict):
        raise ValueError("Invalid token claims")
    return value


def candidate_tokens(flows: object, session: Session, *, now: float | None = None) -> list[str]:
    """Return only recent matching response headers; final trust is checked by transport."""
    if not isinstance(flows, list):
        return []
    now = time.time() if now is None else now
    try:
        current = _claims(session.access_token)
    except (IndexError, TypeError, ValueError, json.JSONDecodeError):
        return []
    matching: list[tuple[float, str]] = []
    for flow in flows:
        if not isinstance(flow, dict):
            continue
        req, resp = flow.get("request") or {}, flow.get("response") or {}
        if not isinstance(req, dict) or not isinstance(resp, dict):
            continue
        path = req.get("path")
        timestamp = req.get("timestamp_start")
        flow_id = flow.get("id")
        if (req.get("host") != _HOST or req.get("method") != "GET"
                or not isinstance(path, str) or parse.urlsplit(path).path != _PATH
                or parse.urlsplit(path).query or resp.get("status_code") != 200
                or not isinstance(timestamp, (int, float)) or isinstance(timestamp, bool)
                or timestamp > now + 60 or not isinstance(flow_id, str)
                or not _FLOW_ID.fullmatch(flow_id)):
            continue
        headers = req.get("headers")
        if (_header(headers, "x-eats-device-id") != session.device_id
                or _header(headers, "x-eats-pcid") != session.pcid):
            continue
        authorization = _header(resp.get("headers"), "authorization")
        if not authorization or not authorization.startswith("DPoP "):
            continue
        candidate = authorization[5:]
        try:
            claims = _claims(candidate)
            if (claims.get("sub") != current.get("sub")
                    or claims.get("auth_time") != current.get("auth_time")
                    or int(claims["exp"]) <= max(now + 60, session.expires_at)):
                continue
        except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
        matching.append((timestamp, authorization))
    matching.sort(key=lambda item: item[0], reverse=True)
    return [authorization for _, authorization in matching[:_MAX_CANDIDATES]]


def local_proxy_candidates(session: Session) -> list[str]:
    """Read only mitmweb's loopback flow metadata, never response bodies."""
    client = _proxy_client()
    with client.open(_BASE + "/flows", timeout=12) as response:
        content = response.read(_MAX_FLOW_LIST_BYTES + 1)
    if len(content) > _MAX_FLOW_LIST_BYTES:
        raise RuntimeError("Local proxy flow listing is too large")
    return candidate_tokens(json.loads(content), session)
