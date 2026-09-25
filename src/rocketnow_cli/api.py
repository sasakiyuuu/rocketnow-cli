"""Typed wrappers for observed Rocket Now lookup and preview endpoints.

The transport owns authentication and HTTP details. Keeping it injectable makes
the endpoint mapping testable without contacting Rocket Now.
"""

from __future__ import annotations

from typing import Any, Protocol


JsonObject = dict[str, Any]


class Transport(Protocol):
    def request(
        self,
        method: str,
        path: str,
        *,
        params: JsonObject | None = None,
        json_body: JsonObject | None = None,
    ) -> JsonObject: ...


class RocketNowAPIError(Exception):
    """An API response contained an error."""

    def __init__(self, error: Any) -> None:
        self.error = error
        if isinstance(error, dict):
            message = error.get("message") or error.get("code") or str(error)
        else:
            message = str(error)
        super().__init__(message)


class RocketNowAPI:
    def __init__(self, transport: Transport) -> None:
        self.transport = transport

    def _request(
        self,
        method: str,
        endpoint: str,
        *,
        params: JsonObject | None = None,
        json_body: JsonObject | None = None,
    ) -> Any:
        response = self.transport.request(
            method, f"/endpoint/{endpoint}", params=params, json_body=json_body
        )
        if "error" in response and response["error"] is not None:
            raise RocketNowAPIError(response["error"])
        return response["data"]

    def _get(self, endpoint: str, params: JsonObject | None = None) -> Any:
        return self._request("GET", endpoint, params=params)

    def _post(self, endpoint: str, body: JsonObject) -> Any:
        return self._request("POST", endpoint, json_body=body)

    def autocomplete(self, keyword: str) -> Any:
        return self._get(
            "store.get_auto_complete_keyword_v2", {"keyword": keyword}
        )

    def search(self, keyword: str, latitude: float, longitude: float) -> Any:
        return self._get(
            "store.get_search",
            {"keyWord": keyword, "latitude": latitude, "longitude": longitude},
        )

    def store_with_menu(
        self,
        store_id: str,
        latitude: float,
        longitude: float,
        source_type: str = "SEARCH",
    ) -> Any:
        return self._get(
            "store.get_store_with_menu",
            {
                "storeId": store_id,
                "latitude": latitude,
                "longitude": longitude,
                "sourceType": source_type,
            },
        )

    def dish(self, store_id: str, dish_id: str, delivery_type: str) -> Any:
        return self._get(
            "store.get_dish_v2",
            {"storeId": store_id, "dishId": dish_id, "deliveryType": delivery_type},
        )

    def order_history(self, completed: bool = True, next_token: str = "") -> Any:
        return self._get(
            "account.search_orders", {"completed": completed, "nextToken": next_token}
        )

    def payment_methods(self) -> Any:
        return self._get("checkout.get_payment_methods")

    def default_address(self) -> Any:
        return self._get("account.get_default_address")

    def calculate_cart_price(self, body: JsonObject) -> Any:
        return self._post("checkout.calculate_cart_price", body)

    def checkout_preview(self, body: JsonObject) -> Any:
        return self._post("checkout.display_v4", body)

    def prepay(self, body: JsonObject) -> Any:
        return self._post("checkout.prepay", body)

    def confirm_payment_result(self, body: JsonObject) -> Any:
        return self._post("checkout.confirm_payment_result", body)
