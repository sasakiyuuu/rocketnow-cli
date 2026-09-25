"""Offline checks for PKCE binding and private session persistence."""

import base64
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
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
        self.assertEqual(params["nonce"], [flow.nonce])
        self.assertEqual(
            params["code_challenge"],
            [b64url(hashlib.sha256(flow.verifier.encode()).digest())],
        )
        self.assertEqual(params["code_challenge_method"], ["S256"])

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


if __name__ == "__main__":
    unittest.main()
