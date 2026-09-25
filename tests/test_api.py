"""Endpoint mapping checks that never make network requests."""

import unittest

from rocketnow_cli.api import RocketNowAPI, RocketNowAPIError


class RecordingTransport:
    def __init__(self, response=None):
        self.response = response if response is not None else {"data": {"ok": True}}
        self.calls = []

    def request(self, method, path, *, params=None, json_body=None):
        self.calls.append((method, path, params, json_body))
        return self.response


class RocketNowAPITests(unittest.TestCase):
    def setUp(self):
        self.transport = RecordingTransport()
        self.api = RocketNowAPI(self.transport)

    def assert_call(self, endpoint, params):
        self.assertEqual(
            self.transport.calls,
            [("GET", f"/endpoint/{endpoint}", params, None)],
        )

    def test_autocomplete(self):
        self.assertEqual(self.api.autocomplete("ピザ"), {"ok": True})
        self.assert_call("store.get_auto_complete_keyword_v2", {"keyword": "ピザ"})

    def test_search(self):
        self.api.search("寿司", 35.6, 139.7)
        self.assert_call(
            "store.get_search",
            {"keyWord": "寿司", "latitude": 35.6, "longitude": 139.7},
        )

    def test_store_with_menu_defaults_source_type(self):
        self.api.store_with_menu("s1", 35.6, 139.7)
        self.assert_call(
            "store.get_store_with_menu",
            {
                "storeId": "s1",
                "latitude": 35.6,
                "longitude": 139.7,
                "sourceType": "SEARCH",
            },
        )

    def test_store_with_menu_accepts_source_type(self):
        self.api.store_with_menu("s1", 35.6, 139.7, "HOME")
        self.assertEqual(self.transport.calls[0][2]["sourceType"], "HOME")

    def test_dish(self):
        self.api.dish("s1", "d2", "DELIVERY")
        self.assert_call(
            "store.get_dish_v2",
            {"storeId": "s1", "dishId": "d2", "deliveryType": "DELIVERY"},
        )

    def test_order_history_defaults_to_completed(self):
        self.api.order_history()
        self.assert_call("account.search_orders", {"completed": True, "nextToken": ""})

    def test_order_history_accepts_incomplete(self):
        self.api.order_history(False)
        self.assert_call("account.search_orders", {"completed": False, "nextToken": ""})

    def test_payment_methods(self):
        self.api.payment_methods()
        self.assert_call("checkout.get_payment_methods", None)

    def test_default_address(self):
        self.assertEqual(self.api.default_address(), {"ok": True})
        self.assert_call("account.get_default_address", None)

    def test_calculate_cart_price_sends_body(self):
        body = {"storeId": "s1", "items": [{"dishId": "d2", "quantity": 2}]}
        self.assertEqual(self.api.calculate_cart_price(body), {"ok": True})
        self.assertEqual(
            self.transport.calls,
            [("POST", "/endpoint/checkout.calculate_cart_price", None, body)],
        )

    def test_checkout_preview_sends_body(self):
        body = {"storeId": "s1", "addressId": "a3"}
        self.assertEqual(self.api.checkout_preview(body), {"ok": True})
        self.assertEqual(
            self.transport.calls,
            [("POST", "/endpoint/checkout.display_v4", None, body)],
        )

    def test_prepay_uses_the_observed_purchase_endpoint(self):
        body = {"requestedAmount": 2400}
        self.api.prepay(body)
        self.assertEqual(
            self.transport.calls,
            [("POST", "/endpoint/checkout.prepay", None, body)],
        )

    def test_payment_confirmation_uses_the_observed_endpoint(self):
        body = {"orderId": 1}
        self.api.confirm_payment_result(body)
        self.assertEqual(
            self.transport.calls,
            [("POST", "/endpoint/checkout.confirm_payment_result", None, body)],
        )

    def test_post_error_uses_same_envelope_handling(self):
        self.transport.response = {
            "error": {"code": "INVALID_CART", "message": "Cart is invalid"},
            "data": {"price": 0},
        }
        with self.assertRaisesRegex(RocketNowAPIError, "Cart is invalid") as raised:
            self.api.calculate_cart_price({"items": []})
        self.assertEqual(raised.exception.error["code"], "INVALID_CART")

    def test_api_error_is_raised_instead_of_returned_as_data(self):
        self.transport.response = {
            "error": {"code": "UNAUTHORIZED", "message": "Sign in required"},
            "data": {"ok": False},
        }
        with self.assertRaisesRegex(RocketNowAPIError, "Sign in required") as raised:
            self.api.order_history()
        self.assertEqual(raised.exception.error["code"], "UNAUTHORIZED")

    def test_empty_error_object_is_still_an_error(self):
        self.transport.response = {"error": {}, "data": None}
        with self.assertRaises(RocketNowAPIError):
            self.api.payment_methods()


if __name__ == "__main__":
    unittest.main()
