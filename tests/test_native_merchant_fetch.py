import gzip
import runpy
import unittest
import zlib
from pathlib import Path

HELPER = runpy.run_path(str(Path(__file__).resolve().parents[1] / "src/rocketnow_cli/assets/native_merchant_fetch.py"))


class NativeMerchantFetchTests(unittest.TestCase):
    def test_merchant_web_allowlist(self):
        allowed = HELPER["allowed"]
        self.assertTrue(allowed("GET", "https://payment.rocketnow.co.jp/payments/i18n/payment?token=offline"))
        self.assertTrue(allowed("POST", "https://pay.rocketnow.co.jp/wallet-authentication/auth/GMO/PAYPAY"))
        self.assertTrue(allowed("GET", "https://pay.rocketnow.co.jp/api/v1/wallet/order/status-by-token/offline"))
        for method, url in [
            ("POST", "https://csg.rocketnow.co.jp/orders/prepay"),
            ("GET", "https://pay.rocketnow.co.jp.attacker.test/payments/test"),
            ("GET", "https://user:pass@pay.rocketnow.co.jp/payments/test"),
            ("GET", "https://pay.rocketnow.co.jp:444/payments/test"),
            ("POST", "https://pay.rocketnow.co.jp/payments/confirm"),
            ("POST", "https://global.openapi.mul-pay.jp/wallet/startSession"),
        ]:
            with self.subTest(url=url):
                self.assertFalse(allowed(method, url))

    def test_response_encoding(self):
        decode = HELPER["decoded_body"]
        body = b"<html>offline</html>"
        self.assertEqual(decode(gzip.compress(body), "gzip"), body)
        self.assertEqual(decode(zlib.compress(body), "deflate"), body)
        self.assertEqual(decode(body, "identity"), body)
        with self.assertRaises(ValueError):
            decode(body, "unsupported")
