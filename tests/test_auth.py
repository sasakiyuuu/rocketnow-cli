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

    def test_exchange_keeps_login_device_identity(self):
        flow = LoginFlow()
        token = "h." + b64url(json.dumps({"exp": 1_900_000_000}).encode()) + ".s"
        response = {"rData": {"accessToken": token, "tokenType": "DPoP"}}
        with patch("rocketnow_cli.auth.request.urlopen", return_value=BytesIO(json.dumps(response).encode())):
            session = flow.exchange("example-code", flow.state)
        self.assertEqual(session.device_id, flow.device_id)
        self.assertEqual(session.pcid, flow.pcid)


if __name__ == "__main__":
    unittest.main()
