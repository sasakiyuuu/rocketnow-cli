import base64
import gzip
import runpy
import unittest
import zlib
from pathlib import Path

HELPER = runpy.run_path(str(Path(__file__).resolve().parents[1] / "src/rocketnow_cli/assets/native_gateway_fetch.py"))


class NativeGatewayFetchTests(unittest.TestCase):
    def config(self, method="GET", url="https://global.openapi.mul-pay.jp/wallet/startSession?p=offline", body=None):
        return {"method": method, "url": url, "headers": {"User-Agent": "offline", "Content-Type": "application/x-www-form-urlencoded"},
            "bodyBase64": None if body is None else base64.b64encode(body).decode()}

    def test_exact_endpoints_and_form(self):
        validate = HELPER["validated_request"]
        validate(self.config())
        validate(self.config("POST", "https://p01.mul-pay.jp/payment/PaypayStart.idPass", b"AccessID=offline&Token=offline"))
        for config in [
            self.config(url="https://global.openapi.mul-pay.jp.attacker.test/wallet/startSession?p=offline"),
            self.config(url="https://user:password@global.openapi.mul-pay.jp/wallet/startSession?p=offline"),
            self.config(url="https://global.openapi.mul-pay.jp:444/wallet/startSession?p=offline"),
            self.config(url="https://global.openapi.mul-pay.jp/wallet/startSession?p=one&p=two"),
            self.config(url="https://global.openapi.mul-pay.jp/wallet/callbackSession?p=offline"),
            self.config("GET", "https://p01.mul-pay.jp/payment/PaypayStart.idPass"),
            self.config("POST", "https://p01.mul-pay.jp/payment/PaypayStart.idPass", b"AccessID=a&Token=b&pay=true"),
            self.config("POST", "https://p01.mul-pay.jp/payment/PaypayStart.idPass", b"AccessID=a&Token=b&Token=c"),
        ]:
            with self.subTest(url=config["url"]):
                with self.assertRaises(ValueError): validate(config)

    def test_credentials_and_browser_hints(self):
        validate = HELPER["validated_request"]
        for name in HELPER["FORBIDDEN_HEADERS"]:
            config = self.config()
            config["headers"][name] = "private"
            with self.assertRaises(ValueError): validate(config)
        config = self.config()
        config["headers"]["Cookie"] = "provider=keep; CT_ATH=private"
        with self.assertRaises(ValueError): validate(config)
        config["headers"] = {"Cookie": "provider=keep", "User-Agent": "native", "Accept-Encoding": "gzip", "Sec-CH-UA": "chrome", "Origin": "merchant"}
        headers = validate(config)[2]
        self.assertEqual(headers, {"cookie": "provider=keep", "user-agent": "native", "accept-encoding": "identity"})

    def test_encoding(self):
        decode = HELPER["decoded_body"]
        body = b"<html>offline</html>"
        self.assertEqual(decode(gzip.compress(body), "gzip"), body)
        self.assertEqual(decode(zlib.compress(body), "deflate"), body)
        self.assertEqual(decode(body, "identity"), body)
