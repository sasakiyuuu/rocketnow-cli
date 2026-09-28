"""Offline transport checks for signed requests and endpoint allowlisting."""

import base64
import hashlib
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
    def __init__(self, body, headers=None):
        super().__init__(body)
        self.headers = headers or {}


def token(payload):
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return "eyJhbGciOiJFUzI1NiJ9." + encoded + ".signature"


def thumbprint(signer):
    jwk = json.dumps(signer.public_jwk(), sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(hashlib.sha256(jwk).digest()).rstrip(b"=").decode()


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
        self.assertEqual(req.get_header("X-coupang-sec-token-binding"), self.session.token_binding)
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

    def test_refresh_skips_network_until_token_expires(self):
        with patch("rocketnow_cli.transport.request.urlopen") as send:
            self.assertFalse(self.transport.refresh())
            send.assert_not_called()

    def test_prepay_carries_observed_device_cookie_and_json_charset(self):
        self.transport._location = {"regionId": 1, "latitude": 35.0, "longitude": 139.0}
        with patch("rocketnow_cli.transport.request.urlopen", side_effect=lambda *_args, **_kwargs: Response(b'{"data":{}}')) as send:
            self.transport.request("POST", "/endpoint/checkout.prepay", json_body={"requestedAmount": 1000})
        req = send.call_args.args[0]
        self.assertEqual(req.get_header("Cookie"), "x-eats-uuid=" + self.session.device_id)
        self.assertEqual(req.get_header("Content-type"), "application/json; charset=utf-8")

    def test_payment_methods_read_carries_device_cookie(self):
        self.transport._location = {"regionId": 1, "latitude": 35.0, "longitude": 139.0}
        with patch("rocketnow_cli.transport.request.urlopen", return_value=Response(b'{"data":{"payMethodList":[]}}')) as send:
            self.transport.request("GET", "/endpoint/checkout.get_payment_methods")
        self.assertEqual(send.call_args.args[0].get_header("Cookie"), "x-eats-uuid=" + self.session.device_id)

    def test_expired_session_tries_harmless_refresh_then_stops_without_rotation(self):
        self.session.expires_at = int(time.time()) - 1
        with patch("rocketnow_cli.transport.request.urlopen", side_effect=lambda *_args, **_kwargs: Response(b'{"data":{}}')) as send:
            with self.assertRaisesRegex(RuntimeError, "renewal was not offered"):
                self.transport.request("GET", "/endpoint/account.member")
            self.assertEqual(send.call_count, 2)
            self.assertTrue(send.call_args_list[0].args[0].full_url.endswith("/endpoint/account.member_extra_info"))
            self.assertTrue(send.call_args_list[1].args[0].full_url.endswith("/endpoint/account.get_default_address"))

    def test_expired_session_rotates_before_the_requested_endpoint(self):
        now = int(time.time())
        old_claims = {
            "iss": "https://mauth.jp.coupang.net/", "aud": ["https://www.rocketnow.co.jp"],
            "sub": "account", "client_id": "client", "cnf": {"jkt": thumbprint(self.session.signer)},
            "auth_time": now - 14_500, "scp": ["offline", "eats"],
            "iat": now - 14_410, "exp": now - 10,
        }
        new_claims = dict(old_claims, iat=now, exp=now + 14_400)
        old_token, new_token = token(old_claims), token(new_claims)
        self.session.access_token = old_token
        self.session.expires_at = old_claims["exp"]
        responses = [
            Response(b'{"data":{}}', {"Authorization": "DPoP " + new_token}),
            Response(b'{"data":{"ok":true}}'),
        ]
        with patch("rocketnow_cli.transport.request.urlopen", side_effect=responses) as send, \
             patch("rocketnow_cli.transport.save_session") as save:
            result = self.transport.request("GET", "/endpoint/account.member")
        self.assertEqual(result["data"], {"ok": True})
        self.assertEqual(send.call_count, 2)
        self.assertTrue(send.call_args_list[0].args[0].full_url.endswith("/endpoint/account.member_extra_info"))
        self.assertTrue(send.call_args_list[1].args[0].full_url.endswith("/endpoint/account.member"))
        self.assertEqual(send.call_args_list[1].args[0].get_header("Authorization"), "DPoP " + new_token)
        self.assertEqual(self.session.expires_at, now + 14_400)
        save.assert_called_once_with(self.session)

    def test_rotation_rejects_token_bound_to_another_key(self):
        now = int(time.time())
        claims = {
            "iss": "issuer", "aud": ["audience"], "sub": "account", "client_id": "client",
            "auth_time": now - 14_500, "scp": ["offline", "eats"],
            "cnf": {"jkt": thumbprint(self.session.signer)}, "iat": now - 14_410, "exp": now - 10,
        }
        self.session.access_token = token(claims)
        self.session.expires_at = claims["exp"]
        wrong = token(dict(claims, cnf={"jkt": "wrong-key"}, iat=now, exp=now + 14_400))
        with patch("rocketnow_cli.transport.request.urlopen", side_effect=lambda *_args, **_kwargs: Response(
            b'{"data":{}}', {"Authorization": "DPoP " + wrong}
        )), patch("rocketnow_cli.transport.save_session") as save:
            with self.assertRaisesRegex(RuntimeError, "renewal was not offered"):
                self.transport.request("GET", "/endpoint/account.member")
        save.assert_not_called()

    def test_refresh_tries_observed_splash_endpoint_when_address_does_not_rotate(self):
        now = int(time.time())
        claims = {
            "iss": "issuer", "aud": ["audience"], "sub": "account", "client_id": "client",
            "auth_time": now - 14_500, "scp": ["offline", "eats"],
            "cnf": {"jkt": thumbprint(self.session.signer)}, "iat": now - 14_410, "exp": now - 10,
        }
        self.session.access_token = token(claims)
        self.session.expires_at = claims["exp"]
        newer = token(dict(claims, iat=now, exp=now + 14_400))
        address = {"data": {"regionId": 1, "latitude": 35.0, "longitude": 139.0,
                            "siDo": "東京都", "siGunGu": "千代田区", "zipCode": "1000000",
                            "customerAddressId": 2}}
        responses = [
            Response(b'{"data":{}}'),
            Response(json.dumps(address).encode()),
            Response(b'{"data":{}}', {"Authorization": "DPoP " + newer}),
            Response(b'{"data":{"ok":true}}'),
        ]
        with patch("rocketnow_cli.transport.request.urlopen", side_effect=responses) as send, \
             patch("rocketnow_cli.transport.save_session"):
            self.assertTrue(self.transport.request("GET", "/endpoint/account.member")["data"]["ok"])
        self.assertEqual(send.call_count, 4)
        self.assertTrue(send.call_args_list[0].args[0].full_url.endswith("/endpoint/account.member_extra_info"))
        splash = send.call_args_list[2].args[0]
        self.assertTrue(splash.full_url.endswith("/endpoint/ads.splash_screen"))
        self.assertEqual(json.loads(splash.data), {"regionId": 1})
        self.assertEqual(json.loads(splash.get_header("X-eats-location"))["regionId"], 1)
        self.assertEqual(send.call_args_list[3].args[0].get_header("Authorization"), "DPoP " + newer)

    def test_pre_expiry_probe_without_rotation_allows_purchase_once_and_backs_off(self):
        self.session.expires_at = int(time.time()) + 8 * 60
        self.transport._location = {"regionId": 1, "latitude": 35.0, "longitude": 139.0}
        with patch("rocketnow_cli.transport.request.urlopen", side_effect=lambda *_args, **_kwargs: Response(b'{"data":{}}')) as send:
            self.transport.request("POST", "/endpoint/checkout.prepay", json_body={"requestedAmount": 1000})
            self.transport.request("GET", "/endpoint/account.member")
        paths = [call.args[0].full_url.rsplit("/", 1)[-1] for call in send.call_args_list]
        self.assertEqual(paths, ["account.member_extra_info", "checkout.prepay", "account.member"])
        self.assertEqual(sum(call.args[0].get_method() == "POST" for call in send.call_args_list), 1)

    def test_expired_session_falls_back_after_member_probe_error_and_never_sends_purchase(self):
        self.session.expires_at = int(time.time()) - 1
        self.transport._location = {"regionId": 1, "latitude": 35.0, "longitude": 139.0}
        from rocketnow_cli.transport import RocketNowHTTPError
        with patch("rocketnow_cli.transport.HTTPTransport._send", side_effect=[
            RocketNowHTTPError(401), {"data": {}},
        ]) as send:
            with self.assertRaisesRegex(RuntimeError, "renewal failed"):
                self.transport.request("POST", "/endpoint/checkout.prepay", json_body={"requestedAmount": 1000})
        self.assertEqual([call.args[:2] for call in send.call_args_list], [
            ("GET", "/endpoint/account.member_extra_info"),
            ("GET", "/endpoint/account.get_default_address"),
        ])


if __name__ == "__main__":
    unittest.main()
