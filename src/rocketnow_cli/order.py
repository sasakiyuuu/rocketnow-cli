"""Construct a purchase request from a reviewed checkout; no network calls here."""

from __future__ import annotations

from ipaddress import ip_address
import json
import os
from pathlib import Path
import re
import socket
import tempfile
import time
from typing import Any

from .api import RocketNowAPI
from .auth import Session


def resolve_search_tracking(api: RocketNowAPI, draft: dict[str, Any]) -> dict[str, Any]:
    """Find tracking IDs for a selected store from a fresh search result."""
    if draft.get("searchId") and draft.get("searchJourneyId"):
        return draft
    keyword = draft.get("keyword")
    if not isinstance(keyword, str) or not keyword:
        raise ValueError("Include searchId/searchJourneyId or a keyword in the draft")
    address = api.default_address()
    result = api.search(keyword, address["latitude"], address["longitude"])
    store_id = int(draft["storeId"])
    for entry in result.get("entityList") or []:
        data = (entry.get("entity") or {}).get("data") or {}
        if not isinstance(data, dict) or data.get("storeId") is None:
            continue
        if int(data["storeId"]) != store_id:
            continue
        logging = data.get("logging") or {}
        if logging.get("searchId") and logging.get("searchJourneyId"):
            return dict(
                draft,
                searchId=logging["searchId"],
                searchJourneyId=logging["searchJourneyId"],
            )
    raise ValueError("Selected store was not found in the keyword search results")


def payment_config_path() -> Path:
    return Path.home() / "Library" / "Application Support" / "rocketnow-cli" / "payment-config.json"


def save_payment_config(config: dict[str, Any], path: Path | None = None) -> Path:
    if not config.get("merchantMallKey") or not config.get("deviceUserAgent"):
        raise ValueError("Payment config needs merchantMallKey and deviceUserAgent")
    target = path or payment_config_path()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    target.parent.chmod(0o700)
    fd, temp_name = tempfile.mkstemp(prefix=".payment-config-", dir=target.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(config, output)
        os.replace(temp_name, target)
    except BaseException:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
        raise
    return target


def load_payment_config(path: Path | None = None) -> dict[str, Any]:
    target = path or payment_config_path()
    with target.open(encoding="utf-8") as source:
        config = json.load(source)
    if not isinstance(config, dict):
        raise ValueError("Payment config is not a JSON object")
    return config


def pending_order_path(review_hash: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{64}", review_hash):
        raise ValueError("Invalid review hash")
    return payment_config_path().parent / "pending-orders" / (review_hash + ".json")


def create_pending_order(review_hash: str, review: dict[str, Any]) -> Path:
    path = pending_order_path(review_hash)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    now = int(time.time())
    payload = {"status": "submitting", "createdAt": now, "review": review}
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        json.dump(payload, output, ensure_ascii=False)
    return path


def load_pending_order(review_hash: str) -> dict[str, Any]:
    with pending_order_path(review_hash).open(encoding="utf-8") as source:
        value = json.load(source)
    if not isinstance(value, dict):
        raise ValueError("Pending order file is invalid")
    return value


def update_pending_order(review_hash: str, value: dict[str, Any]) -> None:
    path = pending_order_path(review_hash)
    if not path.exists():
        raise FileNotFoundError(path)
    fd, temp_name = tempfile.mkstemp(prefix=".pending-order-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False)
        os.replace(temp_name, path)
    except BaseException:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
        raise


def current_client_ip() -> str:
    """Choose this machine's routed IPv6 address without sending a packet."""
    sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
    try:
        sock.connect(("2001:4860:4860::8888", 443))
        address = sock.getsockname()[0]
    except OSError as exc:
        raise ValueError("No routed IPv6 address; set clientIp in payment config") from exc
    finally:
        sock.close()
    if not ip_address(address).is_global:
        raise ValueError("No global IPv6 address; set clientIp in payment config")
    return address


def build_prepay_request(
    api: RocketNowAPI,
    session: Session,
    checkout_request: dict[str, Any],
    preview: dict[str, Any],
    draft: dict[str, Any],
    payment_config: dict[str, Any],
) -> dict[str, Any]:
    """Build the observed GP_CARD prepay body after a fresh checkout preview."""
    if checkout_request.get("orderType") != "DELIVERY":
        raise ValueError("Only DELIVERY orders have been observed")
    if checkout_request.get("payMethodCode") != "GP_CARD":
        raise ValueError("Only saved-card payment has been observed")

    amount = preview.get("requestedAmount")
    if not isinstance(amount, (int, float)) or amount <= 0 or int(amount) != amount:
        raise ValueError("Checkout preview has no valid whole-yen amount")
    delivery_types = preview.get("deliveryTypes") or {}
    delivery_type = delivery_types.get("selectedDeliveryType")
    if not isinstance(delivery_type, str) or not delivery_type:
        raise ValueError("Checkout preview has no selected delivery type")

    search_id = draft.get("searchId")
    journey_id = draft.get("searchJourneyId")
    if not isinstance(search_id, str) or not search_id:
        raise ValueError("A searchId from the selected store result is required")
    if not isinstance(journey_id, str) or not journey_id:
        raise ValueError("A searchJourneyId from the selected store result is required")

    merchant_key = payment_config.get("merchantMallKey")
    client_ip = payment_config.get("clientIp") or current_client_ip()
    device_user_agent = payment_config.get("deviceUserAgent")
    if not merchant_key or not client_ip or not device_user_agent:
        raise ValueError("Payment setup is incomplete")
    ip_address(client_ip)

    methods = api.payment_methods().get("payMethodList") or []
    selected_id = draft.get("payMethodId")
    if selected_id is not None:
        selected_id = int(selected_id)
    matches = [
        item for item in methods
        if item.get("payMethodCode") == "GP_CARD"
        and (selected_id is None or item.get("payMethodId") == selected_id)
    ]
    if len(matches) != 1 or not matches[0].get("payMethodId") or not matches[0].get("maskingPayMethodNumber"):
        raise ValueError("No saved card is available")
    method = matches[0]

    return {
        "searchIds": search_id,
        "payment": {
            "payMethodId": method["payMethodId"],
            "savePaymentOption": True,
            "payMethodCorporateCode": method["payMethodCode"],
            "merchantMallKey": merchant_key,
            "serviceTypeCode": "DELIVERY",
            "maskingPayMethodNumber": method["maskingPayMethodNumber"],
            "payMethodName": method["payMethodName"],
            "payMethodTypeCode": method["payMethodType"],
        },
        "items": checkout_request["items"],
        "pickupOrder": False,
        "deliveryType": delivery_type,
        "coupons": checkout_request["coupons"],
        "deviceInfo": {
            "uuid": session.pcid,
            "deviceType": "IOS_APP",
            "userAgent": device_user_agent,
            "clientIp": client_ip,
        },
        "needDisposables": bool(draft.get("needDisposables", False)),
        "storeId": checkout_request["storeId"],
        "isFirstPartyDelivery": bool(draft.get("isFirstPartyDelivery", False)),
        "customerAddressId": checkout_request["customerAddressId"],
        "notes": str(draft.get("notes", "")),
        "deliveryNote": {
            "noteType": draft.get("deliveryNoteType", "LEAVE_DOOR_WITHOUT_BELL")
        },
        "requestedAmount": int(amount),
        "searchJourneyIds": journey_id,
        "requestedCoupangCash": checkout_request["requestedCoupangCash"],
    }
