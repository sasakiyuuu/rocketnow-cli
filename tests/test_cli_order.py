"""Purchase approval and payment confirmation gates, with no network calls."""

import argparse
import unittest
from unittest.mock import Mock, patch

from rocketnow_cli.cli import _checked_address, _confirm_order, _submit_order, run


class OrderCommandTests(unittest.TestCase):
    def setUp(self):
        self.api = Mock()
        self.args = argparse.Namespace(
            draft_file="draft.json", approve_hash="a" * 64, approve_amount=2400
        )
        self.review = {"reviewHash": "a" * 64, "requestedAmount": 2400.0}

    def test_changed_review_stops_before_purchase(self):
        with patch("rocketnow_cli.cli._json_file", return_value={"searchId": "s", "searchJourneyId": "j"}), \
             patch("rocketnow_cli.cli._build_purchase_review", return_value=({}, self.review)):
            self.args.approve_amount = 2500
            with self.assertRaisesRegex(ValueError, "Checkout changed"):
                _submit_order(self.api, object(), self.args)
        self.api.prepay.assert_not_called()

    def test_approved_review_creates_pending_state_once(self):
        self.api.prepay.return_value = {
            "success": True, "amount": 2400, "orderId": 12,
            "paymentAuthToken": "auth", "paymentUrl": "https://example.com/pay",
            "processType": "PREPAY",
        }
        with patch("rocketnow_cli.cli._json_file", return_value={"storeId": 10, "searchId": "s", "searchJourneyId": "j"}), \
             patch("rocketnow_cli.cli._build_purchase_review", return_value=({"requestedAmount": 2400}, self.review)), \
             patch("rocketnow_cli.cli.verify_review") as verify, \
             patch("rocketnow_cli.cli.create_pending_order") as create, \
             patch("rocketnow_cli.cli.consume_review") as consume, \
             patch("rocketnow_cli.cli.load_pending_order", return_value={"status": "submitting"}), \
             patch("rocketnow_cli.cli.update_pending_order") as update:
            result = _submit_order(self.api, object(), self.args)
        create.assert_called_once_with("a" * 64, self.review)
        verify.assert_called_once_with("a" * 64, {"requestedAmount": 2400})
        consume.assert_called_once_with("a" * 64)
        self.api.prepay.assert_called_once_with({"requestedAmount": 2400})
        self.assertEqual(update.call_args.args[1]["status"], "awaiting_payment")
        self.assertEqual(result["status"], "awaiting_payment")
        self.assertTrue(result["paymentPageAvailable"])

    def test_changed_server_amount_is_not_exposed_as_a_payment_url(self):
        self.api.prepay.return_value = {
            "success": True, "amount": 2500, "orderId": 12,
            "paymentAuthToken": "auth", "paymentUrl": "https://example.com/pay",
            "processType": "PREPAY",
        }
        with patch("rocketnow_cli.cli._json_file", return_value={"searchId": "s", "searchJourneyId": "j"}), \
             patch("rocketnow_cli.cli._build_purchase_review", return_value=({"requestedAmount": 2400}, self.review)), \
             patch("rocketnow_cli.cli.verify_review"), \
             patch("rocketnow_cli.cli.create_pending_order"), \
             patch("rocketnow_cli.cli.consume_review"), \
             patch("rocketnow_cli.cli.load_pending_order", return_value={"status": "submitting"}), \
             patch("rocketnow_cli.cli.update_pending_order") as update:
            with self.assertRaisesRegex(RuntimeError, "differs from approval"):
                _submit_order(self.api, object(), self.args)
        self.assertEqual(update.call_args.args[1]["status"], "requires_reconciliation")

    def test_confirmation_uses_only_prepay_response_values(self):
        state = {"status": "awaiting_payment", "prepay": {
            "transactionToken": None,
            "paymentAuthToken": "auth-token",
            "orderId": 123,
            "amount": 2400,
        }}
        self.api.confirm_payment_result.return_value = {"status": "PAYMENT_APPROVED"}
        with patch("rocketnow_cli.cli.load_pending_order", return_value=state), \
             patch("rocketnow_cli.cli.update_pending_order") as update:
            result = _confirm_order(self.api, "a" * 64)
        self.api.confirm_payment_result.assert_called_once_with({
            "transactionToken": None,
            "paymentAuthToken": "auth-token",
            "orderId": 123,
            "amount": 2400,
            "isLegacyPayment": True,
        })
        self.assertEqual(result["paymentStatus"], "PAYMENT_APPROVED")
        self.assertEqual(update.call_count, 2)

    def test_unrecognized_payment_status_requires_reconciliation(self):
        state = {"status": "awaiting_payment", "prepay": {
            "transactionToken": None, "paymentAuthToken": "auth-token",
            "orderId": 123, "amount": 2400,
        }}
        self.api.confirm_payment_result.return_value = {"status": "PENDING"}
        with patch("rocketnow_cli.cli.load_pending_order", return_value=state), \
             patch("rocketnow_cli.cli.update_pending_order"):
            result = _confirm_order(self.api, "a" * 64)
        self.assertEqual(result["status"], "requires_reconciliation")

    def test_address_change_stops_purchase_review(self):
        self.api.default_address.return_value = {"customerAddressId": 21}
        self.api.transport.location_address_id = 20
        with self.assertRaisesRegex(ValueError, "address changed"):
            _checked_address(self.api, {"customerAddressId": 20})
        self.api.default_address.return_value = {"customerAddressId": 20}
        self.api.transport.location_address_id = 21
        with self.assertRaisesRegex(ValueError, "location changed"):
            _checked_address(self.api, {"customerAddressId": 20})

    def test_payment_url_is_hidden_when_opened(self):
        args = argparse.Namespace(
            command="order", action="payment-url", pending_id="a" * 64, reveal=False
        )
        state = {"status": "awaiting_payment", "prepay": {
            "paymentUrl": "https://payment.example/secret-token"
        }}
        with patch("rocketnow_cli.cli.load_session"), \
             patch("rocketnow_cli.cli.load_pending_order", return_value=state), \
             patch("webbrowser.open", return_value=True) as open_browser:
            result = run(args)
        self.assertTrue(result["opened"])
        self.assertNotIn("paymentUrl", result)
        open_browser.assert_called_once_with("https://payment.example/secret-token")


if __name__ == "__main__":
    unittest.main()
