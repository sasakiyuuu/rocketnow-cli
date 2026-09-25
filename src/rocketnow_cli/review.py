"""Concise purchase review derived from the server's checkout preview."""

from __future__ import annotations

from typing import Any


def summarize_checkout(
    preview: dict[str, Any],
    address: dict[str, Any],
    checkout_request: dict[str, Any],
    prepay_request: dict[str, Any] | None = None,
) -> dict[str, Any]:
    title_parts = (preview.get("store") or {}).get("title") or []
    store_title = "".join(
        str(part.get("text") or "") for part in title_parts if isinstance(part, dict)
    )
    items = [
        {"name": item.get("name"), "quantity": item.get("quantity")}
        for item in preview.get("items") or []
    ]
    currencies = [
        ((item.get("subtotalMoney") or {}).get("currencyCode"))
        for item in preview.get("items") or []
    ]
    currency = next((value for value in currencies if value), None)
    payment_code = checkout_request["payMethodCode"]
    method = next(
        method for method in checkout_request["payMethodList"]
        if method["payMethodCode"] == payment_code
    )
    payment = (prepay_request or {}).get("payment") or {}
    if prepay_request and prepay_request.get("requestedAmount") != preview.get("requestedAmount"):
        raise ValueError("Prepay amount differs from the checkout preview")
    return {
        "store": store_title,
        "items": items,
        "requestedAmount": preview.get("requestedAmount"),
        "currency": currency,
        "deliveryAddress": {
            "addressName": address.get("addressName"),
            "addressDetail": address.get("addressDetail"),
            "buildingName": address.get("buildingName"),
            "roomNumber": address.get("roomNumber"),
            "zipCode": address.get("zipCode"),
        },
        "paymentMethod": {
            "code": payment_code,
            "name": method.get("payMethodName"),
            "payMethodId": payment.get("payMethodId"),
            "maskedNumber": payment.get("maskingPayMethodNumber"),
        },
        "deliveryType": (prepay_request or {}).get("deliveryType"),
        "deliveryNoteType": ((prepay_request or {}).get("deliveryNote") or {}).get("noteType"),
        "notes": (prepay_request or {}).get("notes"),
        "needDisposables": (prepay_request or {}).get("needDisposables"),
        "isFirstPartyDelivery": (prepay_request or {}).get("isFirstPartyDelivery"),
    }
