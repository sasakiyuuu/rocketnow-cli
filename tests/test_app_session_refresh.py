"""Offline checks for importing only the owner's matching app rotation."""

import base64
from io import BytesIO
import json
import time
import unittest
from unittest.mock import patch

from rocketnow_cli.app_session_refresh import candidate_tokens, local_proxy_candidates
from rocketnow_cli.auth import Session
from rocketnow_cli.dpop import DPoPSigner


def token(claims):
    encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return "header." + encoded + ".signature"


def flow(authorization, session, now, **changes):
    value = {
        "id": "flow-1",
        "request": {
            "host": "csg.rocketnow.co.jp", "method": "GET",
            "path": "/endpoint/account.member_extra_info", "timestamp_start": now - 5,
            "headers": [["X-Eats-Device-Id", session.device_id], ["X-Eats-Pcid", session.pcid]],
        },
        "response": {"status_code": 200, "headers": [["Authorization", authorization]]},
    }
    for key, item in changes.items():
        section, field = key.split("__", 1)
        value[section][field] = item
    return value


class ProxyRefreshTests(unittest.TestCase):
    def setUp(self):
        self.now = int(time.time())
        self.claims = {"sub": "account-A", "auth_time": self.now - 1000,
                       "exp": self.now + 100, "aud": ["https://www.rocketnow.co.jp"]}
        self.session = Session(token(self.claims), DPoPSigner.generate(), self.claims["exp"],
                               "device-A", "pcid-A")
        self.rotated = token(dict(self.claims, exp=self.now + 14_400))

    def test_only_matching_owner_device_and_endpoint_is_selected(self):
        good = flow("DPoP " + self.rotated, self.session, self.now)
        ignored = [
            flow("DPoP " + self.rotated, self.session, self.now, request__host="other.example"),
            flow("DPoP " + self.rotated, self.session, self.now, request__method="POST"),
            flow("DPoP " + self.rotated, self.session, self.now, request__path="/endpoint/account.member"),
            flow("DPoP " + self.rotated, self.session, self.now, response__status_code=401),
            flow("Bearer " + self.rotated, self.session, self.now),
            flow("DPoP " + token(dict(self.claims, sub="account-B", exp=self.now + 14_400)),
                 self.session, self.now),
            flow("DPoP " + token(dict(self.claims, auth_time=self.now - 999, exp=self.now + 14_400)),
                 self.session, self.now),
            flow("DPoP " + token(dict(self.claims, exp=self.now + 30)), self.session, self.now),
        ]
        wrong_device = flow("DPoP " + self.rotated, self.session, self.now)
        wrong_device["request"]["headers"][0][1] = "device-B"
        ignored.append(wrong_device)
        wrong_pcid = flow("DPoP " + self.rotated, self.session, self.now)
        wrong_pcid["request"]["headers"][1][1] = "pcid-B"
        ignored.append(wrong_pcid)
        self.assertEqual(candidate_tokens([*ignored, good], self.session, now=self.now),
                         ["DPoP " + self.rotated])

    def test_loopback_proxy_reads_only_flow_metadata(self):
        good = flow("DPoP " + self.rotated, self.session, self.now)

        class Client:
            def __init__(self):
                self.urls = []

            def open(self, url, *, timeout):
                self.urls.append(url)
                return BytesIO(json.dumps([good]).encode())

        client = Client()
        with patch("rocketnow_cli.app_session_refresh._proxy_client", return_value=client):
            self.assertEqual(local_proxy_candidates(self.session), ["DPoP " + self.rotated])
        self.assertEqual(client.urls, ["http://127.0.0.1:8081/flows"])


if __name__ == "__main__":
    unittest.main()
