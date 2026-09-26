"""Offline checks for the one-shot mitmproxy pairing addon."""

import base64
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import pair_proxy  # noqa: E402


def jwt_payload(encoded: str) -> dict:
    payload = encoded.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


def fake_token(exp: int = 1_900_000_000) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).rstrip(b"=").decode()
    return f"header.{payload}.signature"


class FakeRequest:
    def __init__(self, method="POST", url=pair_proxy.EXCHANGE_URL, headers=None):
        self.method = method
        self.url = url
        self.headers = {"DPoP": "original-proof", **(headers or {})}


class FakeResponse:
    def __init__(self, token: str, status_code: int = 200, token_type: str = "DPoP"):
        self.status_code = status_code
        self.content = json.dumps(
            {"rData": {"accessToken": token, "tokenType": token_type}}
        ).encode()


class FakeFlow:
    def __init__(self, request=None, response=None):
        self.request = request or FakeRequest()
        self.response = response
        self.metadata = {}


class FakeTimer:
    def __init__(self, callback):
        self.callback = callback
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


class FakeLoop:
    def __init__(self):
        self.delay = None
        self.timer = None

    def call_later(self, delay, callback):
        self.delay = delay
        self.timer = FakeTimer(callback)
        return self.timer


class PairProxyTests(unittest.TestCase):
    def test_only_first_exact_exchange_gets_fresh_proof_without_ath(self):
        addon = pair_proxy.PairProxy()
        for method, url in [
            ("GET", pair_proxy.EXCHANGE_URL),
            ("POST", "https://csg.rocketnow.co.jp/auth/other"),
            ("POST", "https://other.example/auth/exchange_token"),
            ("POST", pair_proxy.EXCHANGE_URL + "?extra=1"),
        ]:
            flow = FakeFlow(FakeRequest(method, url))
            addon.request(flow)
            self.assertEqual(flow.request.headers["DPoP"], "original-proof")
            self.assertFalse(flow.metadata)

        first = FakeFlow()
        addon.request(first)
        self.assertTrue(first.metadata[pair_proxy._FLOW_MARKER])
        proof = first.request.headers["DPoP"]
        self.assertNotEqual(proof, "original-proof")
        header = jwt_payload("x." + proof.split(".")[0] + ".x")
        payload = jwt_payload(proof)
        self.assertEqual(header["alg"], "ES256")
        self.assertEqual(header["typ"], "dpop+jwt")
        self.assertEqual(header["jwk"], addon.signer.public_jwk())
        self.assertEqual(payload["htm"], "POST")
        self.assertEqual(payload["htu"], pair_proxy.EXCHANGE_URL)
        self.assertNotIn("ath", payload)
        self.assertIn("iat", payload)
        self.assertIn("jti", payload)

        second = FakeFlow()
        addon.request(second)
        self.assertEqual(second.request.headers["DPoP"], "original-proof")
        self.assertFalse(second.metadata)

    def test_only_marked_success_saves_same_signer_and_expiry(self):
        addon = pair_proxy.PairProxy()
        token = fake_token()
        unmarked = FakeFlow(response=FakeResponse(token))
        with patch.object(pair_proxy, "save_session") as save:
            addon.response(unmarked)
            save.assert_not_called()

            flow = FakeFlow(response=FakeResponse(token))
            addon.request(flow)
            addon.response(flow)
            save.assert_called_once()
            session, path = save.call_args.args
            self.assertEqual(session.access_token, token)
            self.assertIs(session.signer, addon.signer)
            self.assertEqual(session.expires_at, 1_900_000_000)
            self.assertEqual(path, pair_proxy._SESSION_FILE)
            self.assertTrue(addon.completed)
            addon.response(flow)
            self.assertEqual(save.call_count, 1)

    def test_failed_exchange_does_not_save_or_rewrite_retry(self):
        addon = pair_proxy.PairProxy()
        first = FakeFlow(response=FakeResponse(fake_token(), token_type="Bearer"))
        addon.request(first)
        with patch.object(pair_proxy, "save_session") as save:
            addon.response(first)
            save.assert_not_called()
        retry = FakeFlow()
        addon.request(retry)
        self.assertEqual(retry.request.headers["DPoP"], "original-proof")

    def test_success_copies_available_request_identity_headers(self):
        addon = pair_proxy.PairProxy()
        flow = FakeFlow(
            request=FakeRequest(headers={
                "x-eats-device-id": "captured-device",
                "X-Eats-Pcid": "captured-pcid",
                "x-eats-session-id": "captured-app-session",
                "X-Member-Pcid": "captured-member-pcid",
            }),
            response=FakeResponse(fake_token()),
        )
        addon.request(flow)
        with patch.object(pair_proxy, "save_session") as save:
            addon.response(flow)
        session = save.call_args.args[0]
        self.assertEqual(session.device_id, "captured-device")
        self.assertEqual(session.pcid, "captured-pcid")
        self.assertEqual(session.app_session_id, "captured-app-session")
        self.assertEqual(session.member_pcid, "captured-member-pcid")
        self.assertEqual(len(session.token_binding), 22)

    def test_followup_captures_binding_only_for_issued_token(self):
        addon = pair_proxy.PairProxy()
        loop = FakeLoop()
        token = fake_token()
        exchange = FakeFlow(response=FakeResponse(token))
        addon.request(exchange)
        with (
            patch.object(pair_proxy, "save_session") as save,
            patch.object(pair_proxy.asyncio, "get_running_loop", return_value=loop),
            patch.object(addon, "_shutdown") as shutdown,
        ):
            addon.response(exchange)
            self.assertEqual(save.call_count, 1)
            self.assertEqual(loop.delay, pair_proxy._FOLLOWUP_TIMEOUT_SECONDS)
            shutdown.assert_not_called()

            for url, authorization in (
                ("https://other.example/endpoint/account.me", "DPoP " + token),
                ("https://csg.rocketnow.co.jp/endpoint/account.me", "DPoP other-token"),
            ):
                addon.request(FakeFlow(FakeRequest(url=url, headers={
                    "Authorization": authorization,
                    "X-Coupang-Sec-Token-Binding": "wrong-binding",
                })))
            self.assertEqual(save.call_count, 1)

            addon.request(FakeFlow(FakeRequest(
                method="GET",
                url="https://csg.rocketnow.co.jp/endpoint/account.me",
                headers={
                    "Authorization": "DPoP " + token,
                    "x-coupang-sec-token-binding": "captured-binding",
                    "X-Eats-Device-Id": "later-device",
                    "X-Eats-Pcid": "later-pcid",
                    "X-Eats-Session-Id": "later-session",
                    "X-Member-Pcid": "later-member",
                },
            )))
            self.assertEqual(save.call_count, 2)
            session = save.call_args.args[0]
            self.assertEqual(session.token_binding, "captured-binding")
            self.assertEqual(session.device_id, "later-device")
            self.assertEqual(session.pcid, "later-pcid")
            self.assertEqual(session.app_session_id, "later-session")
            self.assertEqual(session.member_pcid, "later-member")
            self.assertTrue(loop.timer.cancelled)
            shutdown.assert_called_once()

    def test_followup_timeout_stops_proxy_without_second_save(self):
        addon = pair_proxy.PairProxy()
        loop = FakeLoop()
        exchange = FakeFlow(response=FakeResponse(fake_token()))
        addon.request(exchange)
        with (
            patch.object(pair_proxy, "save_session") as save,
            patch.object(pair_proxy.asyncio, "get_running_loop", return_value=loop),
            patch.object(addon, "_shutdown") as shutdown,
        ):
            addon.response(exchange)
            loop.timer.callback()
            self.assertEqual(save.call_count, 1)
            self.assertIsNone(addon.session)
            shutdown.assert_called_once()


if __name__ == "__main__":
    unittest.main()
