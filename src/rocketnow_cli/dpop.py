"""Proof of possession signing for the observed Rocket Now API requests."""

from __future__ import annotations

import base64
import hashlib
import json
import time
import uuid
from urllib.parse import urlsplit, urlunsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _json_b64(value: dict) -> str:
    return _b64url(json.dumps(value, separators=(",", ":"), sort_keys=True).encode())


class DPoPSigner:
    """Generate ES256 DPoP proofs using a P-256 private key."""

    def __init__(self, private_key: ec.EllipticCurvePrivateKey) -> None:
        if not isinstance(private_key.curve, ec.SECP256R1):
            raise ValueError("DPoP requires a P-256 private key")
        self.private_key = private_key

    @classmethod
    def generate(cls) -> "DPoPSigner":
        return cls(ec.generate_private_key(ec.SECP256R1()))

    @classmethod
    def from_pem(cls, pem: bytes) -> "DPoPSigner":
        key = serialization.load_pem_private_key(pem, password=None)
        if not isinstance(key, ec.EllipticCurvePrivateKey):
            raise ValueError("DPoP key must be an EC private key")
        return cls(key)

    def private_pem(self) -> bytes:
        return self.private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )

    def public_jwk(self) -> dict[str, str]:
        numbers = self.private_key.public_key().public_numbers()
        return {
            "crv": "P-256",
            "kty": "EC",
            "x": _b64url(numbers.x.to_bytes(32, "big")),
            "y": _b64url(numbers.y.to_bytes(32, "big")),
        }

    def proof(
        self,
        method: str,
        url: str,
        access_token: str | None = None,
        *,
        issued_at: int | None = None,
        jti: str | None = None,
    ) -> str:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.netloc or not parsed.path:
            raise ValueError("DPoP URL must be an absolute HTTPS URL with a path")
        htu = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
        header = {"alg": "ES256", "jwk": self.public_jwk(), "typ": "dpop+jwt"}
        payload = {
            "htm": method.upper(),
            "htu": htu,
            "iat": int(time.time()) if issued_at is None else issued_at,
            "jti": str(uuid.uuid4()) if jti is None else jti,
        }
        if access_token is not None:
            payload["ath"] = _b64url(hashlib.sha256(access_token.encode()).digest())
        signing_input = f"{_json_b64(header)}.{_json_b64(payload)}".encode()
        der = self.private_key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(der)
        signature = r.to_bytes(32, "big") + s.to_bytes(32, "big")
        return signing_input.decode() + "." + _b64url(signature)
