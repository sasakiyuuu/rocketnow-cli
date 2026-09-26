"""Offline checks for PKCE binding and private session persistence."""

import base64
import hashlib
from io import BytesIO
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from rocketnow_cli.auth import LoginFlow, Session, load_session, save_session


def b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


class AuthTests(unittest.TestCase):
    def test_landing_url_binds_pkce_and_state(self):
        flow = LoginFlow()
        url = flow.landing_url()
        parsed = urlsplit(url)
        params = parse_qs(parsed.query)
        self.assertEqual(parsed.hostname, "member.rocketnow.co.jp")
        self.assertEqual(params["state"], [flow.state])
        self.assertRegex(flow.state, re.compile(r"^[0-9a-f]{64}$"))
        self.assertEqual(params["nonce"], [flow.nonce])
        self.assertEqual(
            params["code_challenge"],
            [b64url(hashlib.sha256(flow.verifier.encode()).digest())],
        )
        self.assertEqual(params["code_challenge_method"], ["S256"])
        client_info = json.loads(params["client_info"][0])
        self.assertEqual(client_info["deviceId"], flow.device_id)
        self.assertEqual(client_info["pcid"], flow.pcid)

    def test_state_mismatch_stops_before_network(self):
        with self.assertRaisesRegex(ValueError, "state mismatch"):
            LoginFlow().exchange("unused-code", "wrong-state")

    def test_session_round_trip_keeps_key_and_file_private(self):
        flow = LoginFlow()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.json"
            saved = Session("fake-token", flow.signer, 1_900_000_000)
            save_session(saved, path)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            loaded = load_session(path)
            self.assertEqual(loaded.access_token, "fake-token")
            self.assertEqual(loaded.signer.public_jwk(), flow.signer.public_jwk())
            self.assertEqual(loaded.expires_at, 1_900_000_000)
            self.assertEqual(loaded.token_binding, saved.token_binding)
            self.assertRegex(loaded.token_binding, re.compile(r"^[A-Za-z0-9_-]{22}$"))
            self.assertIsNone(loaded.app_session_id)
            self.assertIsNone(loaded.member_pcid)

    def test_legacy_session_load_generates_and_persists_binding(self):
        flow = LoginFlow()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.json"
            path.write_text(json.dumps({
                "access_token": "fake-token",
                "private_key_pem": flow.signer.private_pem().decode(),
                "expires_at": 1_900_000_000,
                "device_id": "existing-device",
                "pcid": "existing-pcid",
            }))
            first = load_session(path)
            second = load_session(path)
            self.assertEqual(first.token_binding, second.token_binding)
            self.assertEqual(first.device_id, "existing-device")
            self.assertEqual(first.pcid, "existing-pcid")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(path.read_text())["token_binding"], first.token_binding)

    def test_new_session_metadata_round_trip(self):
        flow = LoginFlow()
        session = Session(
            "fake-token", flow.signer, 1_900_000_000,
            "device", "pcid", "binding", "app-session", "member-pcid",
        )
        with tempfile.TemporaryDirectory() as directory:
            loaded = load_session(save_session(session, Path(directory) / "session.json"))
        self.assertEqual(loaded.token_binding, "binding")
        self.assertEqual(loaded.app_session_id, "app-session")
        self.assertEqual(loaded.member_pcid, "member-pcid")

    def test_exchange_keeps_login_device_identity(self):
        flow = LoginFlow()
        token = "h." + b64url(json.dumps({"exp": 1_900_000_000}).encode()) + ".s"
        response = {"rData": {"accessToken": token, "tokenType": "DPoP"}}
        with patch("rocketnow_cli.auth.request.urlopen", return_value=BytesIO(json.dumps(response).encode())):
            session = flow.exchange("example-code", flow.state)
        self.assertEqual(session.device_id, flow.device_id)
        self.assertEqual(session.pcid, flow.pcid)
        self.assertEqual(session.app_session_id, flow.app_session_id)
        self.assertEqual(session.member_pcid, flow.member_pcid)


if __name__ == "__main__":
    unittest.main()
