"""Ensure the displayed approval summary follows the server preview."""

import unittest

from rocketnow_cli.review import summarize_checkout


class ReviewTests(unittest.TestCase):
    def test_review_uses_server_total_and_selected_card(self):
        preview = {
            "store": {"title": [{"text": "Sample"}, {"text": " Store"}]},
            "items": [{"name": "Dish", "quantity": 2,
                       "subtotalMoney": {"currencyCode": "JPY"}}],
            "requestedAmount": 2400,
        }
        address = {"addressName": "Example", "addressDetail": "1-2-3", "zipCode": "1000000"}
        request = {
            "payMethodCode": "GP_CARD",
            "payMethodList": [{"payMethodCode": "GP_CARD", "payMethodName": "Saved card"}],
            "items": [{"dishId": 7}],
        }
        prepay = {
            "requestedAmount": 2400,
            "payment": {"payMethodId": 42, "maskingPayMethodNumber": "**** 1234"},
            "deliveryType": "SAVER_DELIVERY",
            "deliveryNote": {"noteType": "LEAVE_DOOR_WITHOUT_BELL"},
            "notes": "No utensils",
            "needDisposables": False,
            "isFirstPartyDelivery": False,
        }
        summary = summarize_checkout(preview, address, request, prepay)
        self.assertEqual(summary["store"], "Sample Store")
        self.assertEqual(summary["requestedAmount"], 2400)
        self.assertEqual(summary["currency"], "JPY")
        self.assertEqual(summary["paymentMethod"]["code"], "GP_CARD")
        self.assertEqual(summary["paymentMethod"]["payMethodId"], 42)
        self.assertEqual(summary["paymentMethod"]["maskedNumber"], "**** 1234")
        self.assertEqual(summary["deliveryAddress"]["addressDetail"], "1-2-3")
        self.assertEqual(summary["deliveryType"], "SAVER_DELIVERY")
        self.assertEqual(summary["notes"], "No utensils")

        changed = dict(preview, requestedAmount=2500)
        with self.assertRaisesRegex(ValueError, "amount differs"):
            summarize_checkout(changed, address, request, prepay)


if __name__ == "__main__":
    unittest.main()
