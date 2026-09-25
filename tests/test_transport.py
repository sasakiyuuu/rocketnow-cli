"""Offline transport checks for signed requests and endpoint allowlisting."""

import base64
from io import BytesIO
import json
import time
import unittest
from unittest.mock import patch

from rocketnow_cli.auth import Session
from rocketnow_cli.dpop import DPoPSigner
from rocketnow_cli.transport import HTTPTransport


def decode_part(value):
    return json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))


class Response(BytesIO):
    pass


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.session = Session("fake-access-token", DPoPSigner.generate(), int(time.time()) + 3600)
        self.transport = HTTPTransport(self.session)

    def test_get_signs_observed_endpoint_and_excludes_query_from_htu(self):
        address = {"data": {"regionId": 1, "latitude": 35.0, "longitude": 139.0,
                            "siDo": "東京都", "siGunGu": "千代田区", "zipCode": "1000000",
                            "customerAddressId": 2}, "error": None}
        with patch("rocketnow_cli.transport.request.urlopen", side_effect=[
            Response(json.dumps(address).encode()), Response(b'{"data":{},"error":null}')
        ]) as send:
            self.transport.request("GET", "/endpoint/store.get_search", params={"keyWord": "寿司"})
        self.assertEqual(send.call_count, 2)
        req = send.call_args.args[0]
        self.assertIn("keyWord=", req.full_url)
        self.assertEqual(req.get_header("Authorization"), "DPoP fake-access-token")
        proof = req.get_header("Dpop") or req.get_header("DPoP")
        payload = decode_part(proof.split(".")[1])
        self.assertEqual(payload["htm"], "GET")
        self.assertEqual(payload["htu"], "https://csg.rocketnow.co.jp/endpoint/store.get_search")
        self.assertIn("ath", payload)
        location = json.loads(req.get_header("X-eats-location"))
        self.assertEqual(location["addressId"], 2)
        self.assertEqual(location["siDo"], "%E6%9D%B1%E4%BA%AC%E9%83%BD")

    def test_non_endpoint_path_is_rejected_before_network(self):
        with patch("rocketnow_cli.transport.request.urlopen") as send:
            with self.assertRaises(ValueError):
                self.transport.request("POST", "/auth/exchange_token", json_body={})
            send.assert_not_called()

    def test_expired_session_is_rejected_before_network(self):
        self.session.expires_at = int(time.time()) - 1
        with patch("rocketnow_cli.transport.request.urlopen") as send:
            with self.assertRaisesRegex(RuntimeError, "Session expired"):
                self.transport.request("GET", "/endpoint/account.member")
            send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
