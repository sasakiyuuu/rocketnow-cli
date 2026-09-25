"""Local purchase approval tests; these never contact Rocket Now."""

import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from rocketnow_cli.approval import (
    consume_review,
    create_review,
    intent_digest,
    review_tracking,
    verify_review,
)


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        directory = Path(self.temporary.name) / "reviews"
        self.directory = directory
        patcher = patch("rocketnow_cli.approval.review_directory", return_value=directory)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.intent = {
            "storeId": 10,
            "items": [{"dishId": 7, "quantity": 2, "options": []}],
            "coupons": [],
            "deliveryType": "SAVER_DELIVERY",
            "deliveryNote": {"noteType": "LEAVE_DOOR_WITHOUT_BELL"},
            "notes": "Leave by door",
            "customerAddressId": 20,
            "requestedAmount": 2400,
            "payment": {
                "payMethodCode": "GP_CARD",
                "payMethodId": 42,
                "maskingPayMethodNumber": "**** 1234",
            },
            "searchIds": "search-one",
            "searchJourneyIds": "journey-one",
            "deviceInfo": {"uuid": "device-one"},
        }

    def test_review_binds_all_purchase_details_and_ignores_only_volatile_fields(self):
        review_hash = create_review(self.intent)
        verify_review(review_hash, self.intent)
        self.assertEqual(
            review_tracking(review_hash),
            {"searchId": "search-one", "searchJourneyId": "journey-one"},
        )
        for field, changed_value in (
            ("notes", "Ring the bell"),
            ("requestedAmount", 2500),
            ("customerAddressId", 21),
            ("deliveryType", "STANDARD_DELIVERY"),
            ("items", [{"dishId": 8, "quantity": 2, "options": []}]),
        ):
            with self.subTest(field=field):
                changed = copy.deepcopy(self.intent)
                changed[field] = changed_value
                with self.assertRaisesRegex(ValueError, "intent changed"):
                    verify_review(review_hash, changed)
        for field, changed_value in (
            ("payMethodId", 43),
            ("maskingPayMethodNumber", "**** 9876"),
        ):
            with self.subTest(field=field):
                changed = copy.deepcopy(self.intent)
                changed["payment"][field] = changed_value
                with self.assertRaisesRegex(ValueError, "intent changed"):
                    verify_review(review_hash, changed)

        volatile_only = copy.deepcopy(self.intent)
        volatile_only.update(searchIds="search-two", searchJourneyIds="journey-two")
        volatile_only["deviceInfo"] = {"uuid": "device-two"}
        self.assertEqual(intent_digest(self.intent), intent_digest(volatile_only))
        verify_review(review_hash, volatile_only)

    def test_expires_and_rejects_future_timestamp(self):
        with patch("rocketnow_cli.approval.time.time", return_value=1000.0):
            review_hash = create_review(self.intent)
        with patch("rocketnow_cli.approval.time.time", return_value=1600.0):
            verify_review(review_hash, self.intent)
        with patch("rocketnow_cli.approval.time.time", return_value=1600.1):
            with self.assertRaisesRegex(ValueError, "expired"):
                verify_review(review_hash, self.intent)
        with patch("rocketnow_cli.approval.time.time", return_value=999.0):
            with self.assertRaisesRegex(ValueError, "expired"):
                verify_review(review_hash, self.intent)

    def test_consumption_blocks_replay_and_does_not_reset(self):
        review_hash = create_review(self.intent)
        consume_review(review_hash)
        with self.assertRaisesRegex(ValueError, "already been submitted"):
            verify_review(review_hash, self.intent)
        with self.assertRaisesRegex(ValueError, "already been submitted"):
            consume_review(review_hash)
        another_hash = create_review(self.intent)
        self.assertNotEqual(review_hash, another_hash)
        verify_review(another_hash, self.intent)

    def test_review_and_lock_files_are_private(self):
        review_hash = create_review(self.intent)
        review_path = self.directory / f"{review_hash}.json"
        self.assertEqual(self.directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual(review_path.stat().st_mode & 0o777, 0o600)
        consume_review(review_hash)
        self.assertEqual(review_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(review_path.with_suffix(".lock").stat().st_mode & 0o777, 0o600)

    def test_missing_or_malformed_hash_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "not found"):
            verify_review("a" * 64, self.intent)
        with self.assertRaisesRegex(ValueError, "Invalid review hash"):
            consume_review("../bad")


if __name__ == "__main__":
    unittest.main()
