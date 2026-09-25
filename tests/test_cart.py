"""Cart expansion checks with realistic option constraints."""

import unittest

from rocketnow_cli.cart import build_cart_request, build_checkout_request


class FakeAPI:
    def dish(self, store_id, dish_id, delivery_type):
        assert (store_id, dish_id, delivery_type) == ("10", "20", "DELIVERY")
        return {
            "id": 20,
            "storeId": 10,
            "name": "Sample dish",
            "type": "FOOD",
            "salePrice": 900,
            "stampReward": False,
            "moaExemption": False,
            "options": [{
                "minSelect": 1,
                "maxSelect": 1,
                "optionItems": [
                    {"id": 30, "name": "Size A", "salePrice": 100, "displayStatus": "ON_SALE", "minQuantity": 1, "maxQuantity": 1},
                    {"id": 31, "name": "Size B", "salePrice": 200, "displayStatus": "ON_SALE", "minQuantity": 1, "maxQuantity": 1},
                ],
            }],
        }

    def default_address(self):
        return {"customerAddressId": 77}

    def payment_methods(self):
        return {"payMethodList": [
            {"payMethodCode": "CARD", "payMethodName": "Card", "payMethodType": "CARD", "defaultPayMethod": True},
            {"payMethodCode": "CASH", "payMethodName": "Cash", "payMethodType": "CASH", "defaultPayMethod": False},
        ]}


class CartTests(unittest.TestCase):
    def test_required_option_builds_observed_quote_shape(self):
        request = build_cart_request(FakeAPI(), {
            "storeId": 10,
            "items": [{"dishId": 20, "quantity": 2, "options": [{"id": 31}]}],
        })
        self.assertEqual(request["storeId"], 10)
        self.assertEqual(request["orderType"], "DELIVERY")
        item = request["items"][0]
        self.assertEqual(item["dishId"], 20)
        self.assertEqual(item["salesPrice"], 900)
        self.assertEqual(item["quantity"], 2)
        self.assertEqual(item["options"], [{
            "oitemId": 31,
            "oitemName": "Size B",
            "oitemPrice": 200,
            "oitemQuantity": 1,
            "isReviewEvent": False,
        }])

    def test_missing_required_option_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "selection count"):
            build_cart_request(FakeAPI(), {"storeId": 10, "items": [{"dishId": 20}]})

    def test_unknown_option_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unknown option"):
            build_cart_request(FakeAPI(), {
                "storeId": 10,
                "items": [{"dishId": 20, "options": [{"id": 30}, {"id": 999}]}],
            })

    def test_checkout_request_uses_saved_address_and_default_payment(self):
        cart = build_cart_request(FakeAPI(), {
            "storeId": 10,
            "items": [{"dishId": 20, "quantity": 2, "options": [{"id": 31}]}],
        })
        checkout = build_checkout_request(FakeAPI(), cart)
        self.assertEqual(checkout["customerAddressId"], 77)
        self.assertEqual(checkout["payMethodCode"], "CARD")
        self.assertEqual(checkout["items"][0]["subtotal"], 2200)
        self.assertEqual(checkout["items"][0]["discountedSubtotal"], 0)

    def test_checkout_rejects_unknown_payment_method(self):
        cart = build_cart_request(FakeAPI(), {
            "storeId": 10,
            "items": [{"dishId": 20, "options": [{"id": 30}]}],
        })
        with self.assertRaisesRegex(ValueError, "exactly one payment"):
            build_checkout_request(FakeAPI(), cart, pay_method_code="NOT_FOUND")

    def test_multiple_saved_cards_require_an_exact_id(self):
        class TwoCards(FakeAPI):
            def payment_methods(self):
                return {"payMethodList": [
                    {"payMethodCode": "GP_CARD", "payMethodId": 1, "payMethodName": "Card A", "payMethodType": "GP_CARD"},
                    {"payMethodCode": "GP_CARD", "payMethodId": 2, "payMethodName": "Card B", "payMethodType": "GP_CARD"},
                ]}

        cart = build_cart_request(FakeAPI(), {
            "storeId": 10,
            "items": [{"dishId": 20, "options": [{"id": 30}]}],
        })
        with self.assertRaisesRegex(ValueError, "exactly one payment"):
            build_checkout_request(TwoCards(), cart, pay_method_code="GP_CARD")
        checkout = build_checkout_request(
            TwoCards(), cart, pay_method_code="GP_CARD", pay_method_id=2
        )
        self.assertEqual(checkout["payMethodCode"], "GP_CARD")


if __name__ == "__main__":
    unittest.main()
