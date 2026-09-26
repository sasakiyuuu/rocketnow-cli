"""Interactive OAuth/PKCE login and local credential storage."""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import tempfile
import time
from urllib import error, parse, request
import uuid

from .dpop import DPoPSigner


API_ORIGIN = "https://csg.rocketnow.co.jp"
SSO_ORIGIN = "https://member.rocketnow.co.jp"
CLIENT_ID = "236b19de-7744-4d6e-a5c4-06331680902b"
REDIRECT_URI = "rocketnowauth:/oauth2redirect"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def jwt_exp(access_token: str) -> int:
    """Read the signed token's expiry for scheduling; this does not validate it."""
    payload = access_token.split(".")[1]
    decoded = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
    return int(json.loads(decoded)["exp"])


class LoginFlow:
    def __init__(self) -> None:
        self.verifier = _b64url(secrets.token_bytes(48))
        self.state = secrets.token_hex(32)
        self.nonce = _b64url(secrets.token_bytes(16))
        self.signer = DPoPSigner.generate()
        self.device_id = secrets.token_urlsafe(41)
        self.pcid = str(uuid.uuid4())
        self.app_uuid = str(uuid.uuid4())
        self.member_pcid = str(secrets.randbelow(9 * 10**22) + 10**22)
        self.app_session_id = str(uuid.uuid4())

    def landing_url(self) -> str:
        challenge = _b64url(hashlib.sha256(self.verifier.encode()).digest())
        ios_version = os.environ.get("ROCKETNOW_IOS_VERSION", "26.6.1")
        app_version = os.environ.get("ROCKETNOW_APP_VERSION", "1.15.0")
        app_build = int(os.environ.get("ROCKETNOW_APP_BUILD", "328257"))
        client_info = {
            "deviceId": self.device_id,
            "timeZone": "Asia/Tokyo",
            "pcid": self.pcid,
            "autoLogin": "Y",
            "uuid": self.app_uuid,
            "appVersion": app_version,
            "appName": "eats",
            "networkType": "WiFi",
            "deviceDensity": "X3",
            "osType": "iOS",
            "resolutionType": "402x874",
            "memberPcid": self.member_pcid,
            "deviceModel": "iPhone",
            "osVersion": ios_version,
        }
        event_time = datetime.now(timezone(timedelta(hours=9))).isoformat(timespec="milliseconds")
        log_info = {
            "common": {
                "eventTime": event_time,
                "pcid": self.pcid,
                "libraryVersion": "2.0.0",
                "appCode": "rocketnow",
                "market": "JP",
                "appId": "com.cpone.customer",
                "platform": "ios",
                "systemLanguage": "ja",
                "lang": "ja",
                "resolution": "1206x2622",
                "app": {
                    "appVersionName": app_version,
                    "osVersion": ios_version,
                    "appVersionCode": app_build,
                    "model": "iPhone18,1",
                    "uuid": str(uuid.uuid4()),
                },
            },
            "extra": {
                "appSessionId": self.app_session_id,
                "app_lang": "system_default",
                "userBenefitType": "NON_LOGIN",
            },
            "appCode": "rocketnow",
            "market": "JP",
            "apiVersion": 1,
            "mode": "production",
        }
        params = [
            ("response_type", "code"),
            ("log_info", json.dumps(log_info, separators=(",", ":"))),
            ("state", self.state),
            ("nonce", self.nonce),
            ("client_info", json.dumps(client_info, separators=(",", ":"))),
            ("redirect_uri", REDIRECT_URI),
            ("extra_parameters", json.dumps(
                {"entryType": "signup", "loginFlow": "unified_api"}, separators=(",", ":")
            )),
            ("market", "JP"),
            ("audience", "https://www.rocketnow.co.jp"),
            ("locale", "ja-JP"),
            ("scope", "offline openid eats core-shared pay"),
            ("service", "eats-app"),
            ("max_age", "14400"),
            ("client_id", CLIENT_ID),
            ("platform", "IOS_APP"),
            ("code_challenge", challenge),
            ("code_challenge_method", "S256"),
        ]
        return SSO_ORIGIN + "/sso/v2/landing.pang?" + parse.urlencode(params, quote_via=parse.quote)

    def exchange(self, code: str, state: str) -> "Session":
        if state != self.state:
            raise ValueError("OAuth state mismatch")
        url = API_ORIGIN + "/auth/exchange_token"
        body = json.dumps(
            {"state": state, "code": code, "codeVerifier": self.verifier},
            separators=(",", ":"),
        ).encode()
        req = request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "DPoP": self.signer.proof("POST", url),
            },
        )
        try:
            with request.urlopen(req, timeout=30) as response:
                result = json.load(response)
        except error.HTTPError as exc:
            raise RuntimeError(f"Token exchange returned HTTP {exc.code}") from None
        data = result.get("rData") or {}
        if not data.get("accessToken") or data.get("tokenType") != "DPoP":
            raise RuntimeError("Token exchange failed: " + str(result.get("rMessage", "unknown error")))
        return Session(
            data["accessToken"], self.signer, jwt_exp(data["accessToken"]),
            self.device_id, self.pcid,
        )


class Session:
    def __init__(
        self,
        access_token: str,
        signer: DPoPSigner,
        expires_at: int,
        device_id: str | None = None,
        pcid: str | None = None,
    ) -> None:
        self.access_token = access_token
        self.signer = signer
        self.expires_at = expires_at
        self.device_id = device_id or secrets.token_urlsafe(41)
        self.pcid = pcid or str(uuid.uuid4())

    @property
    def expired(self) -> bool:
        return time.time() >= self.expires_at - 60


def session_path() -> Path:
    override = os.environ.get("ROCKETNOW_SESSION_FILE")
    if override:
        return Path(override).expanduser()
    return Path.home() / "Library" / "Application Support" / "rocketnow-cli" / "session.json"


def save_session(session: Session, path: Path | None = None) -> Path:
    target = path or session_path()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    target.parent.chmod(0o700)
    data = {
        "access_token": session.access_token,
        "private_key_pem": session.signer.private_pem().decode(),
        "expires_at": session.expires_at,
        "device_id": session.device_id,
        "pcid": session.pcid,
    }
    fd, temp_name = tempfile.mkstemp(prefix=".session-", dir=target.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(data, output)
        os.replace(temp_name, target)
    except BaseException:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
        raise
    return target


def load_session(path: Path | None = None) -> Session:
    target = path or session_path()
    with target.open(encoding="utf-8") as source:
        data = json.load(source)
    session = Session(
        data["access_token"],
        DPoPSigner.from_pem(data["private_key_pem"].encode()),
        int(data["expires_at"]),
        data.get("device_id"),
        data.get("pcid"),
    )
    if not data.get("device_id") or not data.get("pcid"):
        save_session(session, target)
    return session
