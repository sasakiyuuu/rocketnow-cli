"""Purchase body construction checks; these never place an order."""

import unittest
from pathlib import Path
import tempfile
from unittest.mock import patch

from rocketnow_cli.order import (
    build_prepay_request,
    create_pending_order,
    load_pending_order,
    resolve_search_tracking,
    update_pending_order,
)


class FakeAPI:
    def payment_methods(self):
        return {"payMethodList": [{
            "payMethodCode": "GP_CARD",
            "payMethodId": 42,
            "maskingPayMethodNumber": "**** 1234",
            "payMethodName": "Saved card",
            "payMethodType": "GP_CARD",
        }]}

    def default_address(self):
        return {"latitude": 35.0, "longitude": 139.0}

    def search(self, keyword, latitude, longitude):
        assert (keyword, latitude, longitude) == ("sushi", 35.0, 139.0)
        return {"entityList": [
            {"entity": {"data": {"storeId": 10, "logging": {
                "searchId": "search-1", "searchJourneyId": "journey-1"
            }}}}
        ]}


class FakeSession:
    pcid = "fake-device-uuid"


class PrepayBuilderTests(unittest.TestCase):
    def setUp(self):
        self.checkout_request = {
            "orderType": "DELIVERY",
            "payMethodCode": "GP_CARD",
            "items": [{"dishId": 1, "quantity": 2}],
            "coupons": [],
            "storeId": 10,
            "customerAddressId": 20,
            "requestedCoupangCash": 0,
        }
        self.preview = {
            "requestedAmount": 2400.0,
            "deliveryTypes": {"selectedDeliveryType": "SAVER_DELIVERY"},
        }
        self.draft = {"searchId": "search-1", "searchJourneyId": "journey-1"}
        self.config = {
            "merchantMallKey": "mall-key",
            "clientIp": "2001:db8::1",
            "deviceUserAgent": "Rocket Now test device",
        }

    def build(self):
        return build_prepay_request(
            FakeAPI(), FakeSession(), self.checkout_request,
            self.preview, self.draft, self.config,
        )

    def test_builds_saved_card_request_from_reviewed_total(self):
        body = self.build()
        self.assertEqual(body["requestedAmount"], 2400)
        self.assertEqual(body["deliveryType"], "SAVER_DELIVERY")
        self.assertEqual(body["payment"]["payMethodId"], 42)
        self.assertEqual(body["payment"]["payMethodTypeCode"], "GP_CARD")
        self.assertEqual(body["deviceInfo"]["uuid"], "fake-device-uuid")
        self.assertEqual(body["items"], self.checkout_request["items"])

    def test_missing_search_tracking_is_rejected(self):
        del self.draft["searchId"]
        with self.assertRaisesRegex(ValueError, "searchId"):
            self.build()

    def test_changed_fractional_total_is_rejected(self):
        self.preview["requestedAmount"] = 2400.5
        with self.assertRaisesRegex(ValueError, "whole-yen"):
            self.build()

    def test_apple_pay_is_rejected(self):
        self.checkout_request["payMethodCode"] = "APPLEPAY"
        with self.assertRaisesRegex(ValueError, "saved-card"):
            self.build()

    def test_search_tracking_can_be_resolved_from_keyword(self):
        draft = resolve_search_tracking(FakeAPI(), {"storeId": 10, "keyword": "sushi"})
        self.assertEqual(draft["searchId"], "search-1")
        self.assertEqual(draft["searchJourneyId"], "journey-1")

    def test_pending_order_prevents_duplicate_purchase_attempt(self):
        review_hash = "a" * 64
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "payment-config.json"
            with patch("rocketnow_cli.order.payment_config_path", return_value=config_path):
                path = create_pending_order(review_hash, {"requestedAmount": 2400})
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                with self.assertRaises(FileExistsError):
                    create_pending_order(review_hash, {"requestedAmount": 2400})
                state = load_pending_order(review_hash)
                self.assertEqual(state["status"], "submitting")
                state["status"] = "awaiting_payment"
                update_pending_order(review_hash, state)
                self.assertEqual(load_pending_order(review_hash)["status"], "awaiting_payment")


if __name__ == "__main__":
    unittest.main()
