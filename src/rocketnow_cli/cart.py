"""Build a quote request from dish details and a compact cart draft."""

from __future__ import annotations

from typing import Any

from .api import RocketNowAPI


def build_cart_request(api: RocketNowAPI, draft: dict[str, Any]) -> dict[str, Any]:
    """Expand dish IDs and selected option IDs into the observed cart schema."""
    store_id = int(draft["storeId"])
    if draft.get("orderType", "DELIVERY") != "DELIVERY":
        raise ValueError("Only DELIVERY carts have been verified")
    entries = draft.get("items")
    if not isinstance(entries, list) or not entries:
        raise ValueError("Cart must contain at least one item")

    items: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Each cart item must be an object")
        dish_id = int(entry["dishId"])
        quantity = int(entry.get("quantity", 1))
        if quantity < 1:
            raise ValueError("Quantity must be positive")
        dish = api.dish(str(store_id), str(dish_id), "DELIVERY")
        if int(dish["storeId"]) != store_id or int(dish["id"]) != dish_id:
            raise ValueError("Dish does not belong to the requested store")

        selections = entry.get("options", [])
        if not isinstance(selections, list):
            raise ValueError("Options must be a list")
        selected: dict[int, int] = {}
        for selection in selections:
            option_id = int(selection["id"])
            if option_id in selected:
                raise ValueError("Duplicate option item")
            selected[option_id] = int(selection.get("quantity", 1))

        options: list[dict[str, Any]] = []
        seen: set[int] = set()
        for group in dish.get("options") or []:
            group_count = 0
            for option in group.get("optionItems") or []:
                option_id = int(option["id"])
                if option_id not in selected:
                    continue
                if option.get("displayStatus") not in (None, "ON_SALE"):
                    raise ValueError("Selected option is unavailable")
                option_quantity = selected[option_id]
                minimum = int(option.get("minQuantity") or 1)
                maximum = int(option.get("maxQuantity") or option_quantity)
                if not minimum <= option_quantity <= maximum:
                    raise ValueError("Selected option quantity is out of range")
                group_count += 1
                seen.add(option_id)
                options.append({
                    "oitemId": option_id,
                    "oitemName": option["name"],
                    "oitemPrice": option["salePrice"],
                    "oitemQuantity": option_quantity,
                    "isReviewEvent": bool(option.get("isReviewEvent")),
                })
            group_minimum = int(group.get("minSelect") or 0)
            group_maximum = int(group.get("maxSelect") or max(group_count, 1))
            if group_count < group_minimum or group_count > group_maximum:
                raise ValueError("Option group selection count is out of range")
        if seen != set(selected):
            raise ValueError("Unknown option item ID")

        items.append({
            "dishId": dish_id,
            "name": dish["name"],
            "type": dish["type"],
            "salesPrice": dish["salePrice"],
            "quantity": quantity,
            "options": options,
            "promotions": [],
            "priceExpression": [],
            "stampReward": dish.get("stampReward", False),
            "moaExemption": dish.get("moaExemption", False),
            "isReviewEvent": False,
            "isAlcohol": dish.get("isAlcohol", False),
            "hasOption": False,
        })
    return {
        "storeId": store_id,
        "orderType": "DELIVERY",
        "enableStoreMoaCommunication": False,
        "items": items,
    }


def build_checkout_request(
    api: RocketNowAPI,
    cart_request: dict[str, Any],
    *,
    pay_method_code: str | None = None,
    pay_method_id: int | None = None,
) -> dict[str, Any]:
    """Build the observed checkout preview request using saved account settings."""
    address = api.default_address()
    methods = api.payment_methods().get("payMethodList") or []
    if not methods:
        raise ValueError("No payment methods are available")
    if pay_method_id is not None:
        pay_method_id = int(pay_method_id)
    if pay_method_code is None:
        selected = next((method for method in methods if method.get("defaultPayMethod")), None)
        if selected is None:
            raise ValueError("Choose a payment method explicitly")
    else:
        matches = [
            method for method in methods
            if method["payMethodCode"] == pay_method_code
            and (pay_method_id is None or method.get("payMethodId") == pay_method_id)
        ]
        if len(matches) != 1:
            raise ValueError("Choose exactly one payment method ID")
        selected = matches[0]

    items = []
    for item in cart_request["items"]:
        subtotal = (
            item["salesPrice"]
            + sum(option["oitemPrice"] * option["oitemQuantity"] for option in item["options"])
        ) * item["quantity"]
        display_item = item.copy()
        display_item.update({
            "maxSelect": 1,
            "timeRemainingAvailableForOrder": 0,
            "subtotal": int(subtotal),
            "bgGradientStops": [],
            "removable": True,
            "discountedSubtotal": 0,
            "hasOption": True,
        })
        items.append(display_item)

    return {
        "requestedCoupangCash": 0,
        "storeId": cart_request["storeId"],
        "needDishOffProcess": True,
        "payMethodList": [
            {key: method[key] for key in ("payMethodCode", "payMethodName", "payMethodType")}
            for method in methods
        ],
        "payMethodCode": selected["payMethodCode"],
        "needCouponRecommend": True,
        "needStampPopup": True,
        "customerAddressId": address["customerAddressId"],
        "orderType": cart_request["orderType"],
        "coupons": [],
        "remainingCoupangCash": 0,
        "items": items,
        "entryPoint": "store_detail",
    }
