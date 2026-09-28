"""Payment browser credential boundary checks, without payment requests."""

from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from rocketnow_cli.auth import Session
from rocketnow_cli.dpop import DPoPSigner
from rocketnow_cli.payment_browser import browser_config, refresh_web_session


class PaymentBrowserTests(unittest.TestCase):
    def test_web_session_refresh_replaces_stale_header_without_changing_token(self):
        session = Session("fake-token", DPoPSigner.generate(), 1_900_000_000, sso_auth_header="stale")
        api = Mock()
        api.web_session.return_value = {"ssoAuthHeader": "fresh"}
        with patch("rocketnow_cli.payment_browser.save_session") as save:
            refresh_web_session(api, session)
        self.assertEqual(session.sso_auth_header, "fresh")
        self.assertEqual(session.access_token, "fake-token")
        save.assert_called_once_with(session)

    def test_missing_web_credentials_stops_before_browser_setup(self):
        session = Session("fake", DPoPSigner.generate(), 1_900_000_000)
        with self.assertRaisesRegex(ValueError, "pair the CLI again"):
            browser_config(session, "https://payment.rocketnow.co.jp/payments/i18n/payment", 1000, Path("/tmp/private"), "Mozilla/5.0 (iPhone) AppleWebKit/605.1.15 RocketNow/1.15.1")

    def test_native_app_user_agent_cannot_be_used_for_webview(self):
        session = Session("fake", DPoPSigner.generate(), 1_900_000_000,
                          access_token_hash="fake-hash", sso_auth_header="fake-sso")
        with self.assertRaisesRegex(ValueError, "separate from deviceUserAgent"):
            browser_config(session, "https://payment.rocketnow.co.jp/payments/i18n/payment", 1000,
                           Path("/tmp/private"), "Rocket Now/1.15.1 (iPhone; iOS 26.6.1; Scale/3.00)")

    def test_captured_web_credentials_use_only_merchant_cookie_domain(self):
        session = Session("fake", DPoPSigner.generate(), 1_900_000_000,
                          access_token_hash="fake-hash", sso_auth_header="fake-sso")
        config = browser_config(session, "https://payment.rocketnow.co.jp/payments/i18n/payment", 1000, Path("/tmp/private"), "Mozilla/5.0 (iPhone) AppleWebKit/605.1.15 RocketNow/1.15.1")
        self.assertEqual(config["headers"]["X-Sso-Auth"], "fake-sso")
        self.assertEqual(config["cookies"][0]["domain"], ".rocketnow.co.jp")
        self.assertEqual(config["cookies"][0]["value"], "fake-hash")
        self.assertTrue(config["cookies"][0]["secure"])
        self.assertTrue(config["cookies"][0]["httpOnly"])

    def test_reviewed_non_thousand_yen_amount_is_supported(self):
        session = Session("fake", DPoPSigner.generate(), 1_900_000_000,
                          access_token_hash="fake-hash", sso_auth_header="fake-sso")
        config = browser_config(session, "https://payment.rocketnow.co.jp/payments/i18n/payment",
                                936, Path("/tmp/private"),
                                "Mozilla/5.0 (iPhone) AppleWebKit/605.1.15 RocketNow/1.15.1")
        self.assertEqual(config["expectedAmount"], 936)
        with self.assertRaisesRegex(ValueError, "reviewed whole-yen"):
            browser_config(session, "https://payment.rocketnow.co.jp/payments/i18n/payment",
                           0, Path("/tmp/private"),
                           "Mozilla/5.0 (iPhone) AppleWebKit/605.1.15 RocketNow/1.15.1")


if __name__ == "__main__":
    unittest.main()
