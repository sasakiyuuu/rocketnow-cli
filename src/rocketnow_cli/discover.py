"""Read-only, paced discovery of inexpensive dishes in category store menus."""

from __future__ import annotations

import math
import time
from typing import Any

from .api import RocketNowAPIError


def _is_auth_error(exc: Exception) -> bool:
    """Recognize authentication failures without recording sensitive error text."""
    values: list[Any] = [getattr(exc, "status", None), getattr(exc, "status_code", None)]
    if isinstance(exc, RocketNowAPIError):
        error = exc.error
        if isinstance(error, dict):
            values.extend(error.get(key) for key in ("status", "statusCode", "code", "message"))
        else:
            values.append(error)
    values.append(str(exc))
    joined = " ".join(str(value).lower() for value in values if value is not None)
    return any(
        word in joined
        for word in ("401", "403", "unauthorized", "unauthenticated", "authentication",
                     "auth expired", "session expired", "token expired", "invalid token", "login required")
    )


def _store_cards(response: Any) -> list[dict[str, Any]]:
    if not isinstance(response, dict) or not isinstance(response.get("entityList"), list):
        raise ValueError("Unexpected category response")
    cards = []
    for item in response["entityList"]:
        if not isinstance(item, dict) or item.get("viewType") != "storeCardWithMenu":
            continue
        entity = item.get("entity")
        data = entity.get("data") if isinstance(entity, dict) else None
        if isinstance(data, dict) and data.get("id") is not None and isinstance(data.get("name"), str):
            cards.append(data)
    return cards


def _menu_dishes(response: Any) -> list[dict[str, Any]]:
    if not isinstance(response, dict) or not isinstance(response.get("menus"), list):
        raise ValueError("Unexpected store menu response")
    dishes = []
    for menu in response["menus"]:
        if not isinstance(menu, dict) or not isinstance(menu.get("dishes"), list):
            continue
        dishes.extend(dish for dish in menu["dishes"] if isinstance(dish, dict))
    return dishes


def scan_catalog(
    api: Any,
    latitude: float,
    longitude: float,
    category_ids: list[int],
    max_price: int = 1000,
    limit_stores: int = 100,
    delay_seconds: float = 0.3,
) -> dict[str, Any]:
    """Scan up to 100 unique stores; never invoke checkout or order endpoints.

    The maximum price applies to the individual dish. Delivery and small order
    fees require a separate checkout preview and are not included here.
    """
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        raise ValueError("Invalid delivery coordinates")
    if not isinstance(max_price, int) or isinstance(max_price, bool) or max_price < 0:
        raise ValueError("max_price must be a nonnegative integer")
    if not isinstance(limit_stores, int) or isinstance(limit_stores, bool) or limit_stores < 1:
        raise ValueError("limit_stores must be a positive integer")
    if not isinstance(delay_seconds, (int, float)) or not math.isfinite(delay_seconds) or delay_seconds < 0:
        raise ValueError("delay_seconds must be nonnegative and finite")
    if not isinstance(category_ids, list) or any(not isinstance(cid, int) or isinstance(cid, bool) for cid in category_ids):
        raise ValueError("category_ids must be a list of integers")

    category_ids = list(dict.fromkeys(category_ids))
    limit_stores = min(limit_stores, 100)
    last_call: float | None = None

    def paced_call(method: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal last_call
        if last_call is not None:
            wait = delay_seconds - (time.monotonic() - last_call)
            if wait > 0:
                time.sleep(wait)
        # Mark the start, so consecutive request starts stay at least this far apart.
        last_call = time.monotonic()
        return method(*args, **kwargs)

    failures: list[dict[str, Any]] = []
    by_category: list[tuple[int, list[dict[str, Any]]]] = []
    for category_id in category_ids:
        try:
            cards = _store_cards(paced_call(api.category_stores, category_id))
        except Exception as exc:
            if _is_auth_error(exc):
                raise
            failures.append({"stage": "category", "categoryId": category_id,
                             "errorType": type(exc).__name__})
            continue
        by_category.append((category_id, cards))

    selected: list[dict[str, Any]] = []
    seen: dict[str, dict[str, Any]] = {}
    offset = 0
    while len(selected) < limit_stores and any(offset < len(cards) for _, cards in by_category):
        for category_id, cards in by_category:
            if offset >= len(cards):
                continue
            card = cards[offset]
            key = str(card["id"])
            if key in seen:
                if category_id not in seen[key]["categoryIds"]:
                    seen[key]["categoryIds"].append(category_id)
            elif len(selected) < limit_stores:
                row = {
                    "storeId": card["id"], "store": card["name"],
                    "categoryIds": [category_id],
                    "estimatedDeliveryTime": card.get("estimatedDeliveryTime"),
                    "openStatus": card.get("openStatus"),
                }
                seen[key] = row
                selected.append(row)
        offset += 1

    products: list[dict[str, Any]] = []
    stores_scanned = 0
    for store in selected:
        try:
            dishes = _menu_dishes(paced_call(
                api.store_with_menu, str(store["storeId"]), latitude, longitude,
                source_type="SEARCH",
            ))
        except Exception as exc:
            if _is_auth_error(exc):
                raise
            failures.append({"stage": "store", "storeId": store["storeId"],
                             "errorType": type(exc).__name__})
            continue
        stores_scanned += 1
        seen_dish_ids: set[str] = set()
        for dish in dishes:
            price = dish.get("salePrice")
            if isinstance(price, bool) or not isinstance(price, (int, float)) or not math.isfinite(price):
                continue
            if not float(price).is_integer() or price < 0 or price > max_price:
                continue
            if dish.get("displayStatus") != "ON_SALE" or dish.get("id") is None or not isinstance(dish.get("name"), str):
                continue
            dish_key = str(dish["id"])
            if dish_key in seen_dish_ids:
                continue
            seen_dish_ids.add(dish_key)
            products.append({
                "storeId": store["storeId"], "store": store["store"],
                "categoryIds": list(store["categoryIds"]),
                "estimatedDeliveryTime": store["estimatedDeliveryTime"],
                "openStatus": store["openStatus"],
                "id": dish["id"], "name": dish["name"], "price": int(price),
                "hasOptions": dish.get("hasOptions") is True,
            })

    products.sort(key=lambda item: (item["price"], str(item["store"]), str(item["name"]),
                                    str(item["storeId"]), str(item["id"])))
    return {"storesDiscovered": len(selected), "storesScanned": stores_scanned,
            "candidateProducts": products, "failures": failures}
