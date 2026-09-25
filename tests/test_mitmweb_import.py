"""Local proxy payment-config import without real captures or credentials."""

import json
import unittest

from rocketnow_cli.mitmweb_import import _payment_config_from_flows, _require_local_mitmweb


class MitmwebImportTests(unittest.TestCase):
    def test_only_loopback_web_ui_is_allowed(self):
        self.assertEqual(_require_local_mitmweb("http://127.0.0.1:8081"), "http://127.0.0.1:8081/")
        with self.assertRaisesRegex(ValueError, "local HTTP loopback"):
            _require_local_mitmweb("https://example.com")

    def test_latest_successful_prepay_supplies_only_setup_fields(self):
        flows = [
            {"id": "old", "request": {"host": "csg.rocketnow.co.jp", "method": "POST",
                "path": "/endpoint/checkout.prepay", "timestamp_start": 1},
             "response": {"status_code": 200}},
            {"id": "new", "request": {"host": "csg.rocketnow.co.jp", "method": "POST",
                "path": "/endpoint/checkout.prepay", "timestamp_start": 2},
             "response": {"status_code": 200}},
        ]
        bodies = {
            ("new", "response"): {"data": {"success": True}, "error": None},
            ("new", "request"): {
                "payment": {"merchantMallKey": "mall", "payMethodId": 42,
                            "maskingPayMethodNumber": "**** 1234"},
                "deviceInfo": {"userAgent": "Rocket Now test", "clientIp": "2001:db8::1"},
                "customerAddressId": 99,
            },
        }
        def get_content(flow_id, side):
            return json.dumps(bodies[(flow_id, side)]).encode()

        config = _payment_config_from_flows(flows, get_content)
        self.assertEqual(config, {
            "merchantMallKey": "mall",
            "deviceUserAgent": "Rocket Now test",
            "preferredPayMethodId": 42,
        })
        self.assertNotIn("clientIp", config)
        self.assertNotIn("maskingPayMethodNumber", config)


if __name__ == "__main__":
    unittest.main()
