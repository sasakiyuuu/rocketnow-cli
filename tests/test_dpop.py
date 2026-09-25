"""Verify the generated proof, including the signature and URL binding."""

import base64
import hashlib
import json
import unittest

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from rocketnow_cli.dpop import DPoPSigner


def decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class DPoPTests(unittest.TestCase):
    def test_proof_binds_method_path_and_access_token(self):
        signer = DPoPSigner.generate()
        proof = signer.proof(
            "get",
            "https://csg.rocketnow.co.jp/endpoint/store.get_search?keyWord=pizza",
            "sample-access-token",
            issued_at=1_700_000_000,
            jti="test-jti",
        )
        header_part, payload_part, signature_part = proof.split(".")
        header = json.loads(decode(header_part))
        payload = json.loads(decode(payload_part))
        self.assertEqual(header["alg"], "ES256")
        self.assertEqual(header["typ"], "dpop+jwt")
        self.assertEqual(header["jwk"], signer.public_jwk())
        self.assertEqual(payload["htm"], "GET")
        self.assertEqual(payload["htu"], "https://csg.rocketnow.co.jp/endpoint/store.get_search")
        self.assertEqual(payload["iat"], 1_700_000_000)
        self.assertEqual(payload["jti"], "test-jti")
        expected_ath = base64.urlsafe_b64encode(
            hashlib.sha256(b"sample-access-token").digest()
        ).rstrip(b"=").decode()
        self.assertEqual(payload["ath"], expected_ath)
        signature = decode(signature_part)
        self.assertEqual(len(signature), 64)
        der = encode_dss_signature(
            int.from_bytes(signature[:32], "big"),
            int.from_bytes(signature[32:], "big"),
        )
        signer.private_key.public_key().verify(
            der, f"{header_part}.{payload_part}".encode(), ec.ECDSA(hashes.SHA256())
        )

    def test_pem_round_trip_preserves_public_key(self):
        signer = DPoPSigner.generate()
        loaded = DPoPSigner.from_pem(signer.private_pem())
        self.assertEqual(loaded.public_jwk(), signer.public_jwk())

    def test_http_url_is_rejected(self):
        with self.assertRaises(ValueError):
            DPoPSigner.generate().proof("GET", "http://example.com/path", "token")

    def test_token_exchange_proof_has_no_access_token_hash(self):
        proof = DPoPSigner.generate().proof(
            "POST", "https://csg.rocketnow.co.jp/auth/exchange_token"
        )
        payload = json.loads(decode(proof.split(".")[1]))
        self.assertNotIn("ath", payload)


if __name__ == "__main__":
    unittest.main()
