"""Authenticated JSON transport for the observed customer API."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from urllib import error, parse, request

from .auth import API_ORIGIN, Session, save_session


class RocketNowHTTPError(RuntimeError):
    def __init__(self, status: int) -> None:
        self.status = status
        super().__init__(f"Rocket Now returned HTTP {status}")


class HTTPTransport:
    _EARLY_REFRESH_WINDOW = 10 * 60
    _EARLY_REFRESH_BACKOFF = 60

    def __init__(self, session: Session) -> None:
        self.session = session
        self._location: dict | None = None
        self._next_early_refresh_at = 0.0

    @property
    def location_address_id(self) -> int | None:
        return None if self._location is None else self._location.get("addressId")

    def _location_header(self, params: dict | None) -> str:
        if self._location is None:
            response = self.request("GET", "/endpoint/account.get_default_address")
            address = response.get("data")
            if not isinstance(address, dict):
                raise RuntimeError("No default delivery address is available")
            self._set_location(address)
        location = self._location.copy()
        if params:
            for coordinate in ("latitude", "longitude"):
                if coordinate in params:
                    location[coordinate] = params[coordinate]
        return json.dumps(location, separators=(",", ":"))

    def _set_location(self, address: dict) -> None:
        self._location = {
            "regionId": address["regionId"],
            "latitude": address["latitude"],
            "siGunGu": parse.quote(address["siGunGu"], safe=""),
            "siDo": parse.quote(address["siDo"], safe=""),
            "zipcode": address["zipCode"],
            "addressId": address["customerAddressId"],
            "longitude": address["longitude"],
        }

    def _accept_rotated_token(self, authorization: str | None) -> bool:
        """Keep a newer token returned by the authenticated API response."""
        if not authorization or not authorization.startswith("DPoP "):
            return False
        candidate = authorization[5:]
        try:
            old_payload = _jwt_payload(self.session.access_token)
            new_payload = _jwt_payload(candidate)
            jwk = self.session.signer.public_jwk()
            thumbprint = _b64url(hashlib.sha256(
                json.dumps(jwk, separators=(",", ":"), sort_keys=True).encode()
            ).digest())
            if any(new_payload.get(key) != old_payload.get(key) for key in (
                "iss", "aud", "sub", "client_id", "auth_time", "scp",
            )):
                return False
            if new_payload.get("cnf", {}).get("jkt") != thumbprint:
                return False
            new_exp = int(new_payload["exp"])
            if new_exp <= max(self.session.expires_at, time.time() + 60):
                return False
        except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return False
        self.session.access_token = candidate
        self.session.expires_at = new_exp
        save_session(self.session)
        return True

    def refresh(self) -> bool:
        """Ask a harmless account endpoint to rotate the bound access token."""
        now = time.time()
        expired = now >= self.session.expires_at
        if not expired and (self.session.expires_at - now > self._EARLY_REFRESH_WINDOW
                            or now < self._next_early_refresh_at):
            return False
        old_token = self.session.access_token
        probe_error: RocketNowHTTPError | error.URLError | None = None
        try:
            self._send("GET", "/endpoint/account.member_extra_info")
        except (RocketNowHTTPError, error.URLError) as exc:
            probe_error = exc
        if self.session.access_token != old_token:
            return True
        if not expired:
            # A normal authenticated request can still rotate the token. Avoid
            # probing on every request if this early attempt did not offer one.
            self._next_early_refresh_at = now + self._EARLY_REFRESH_BACKOFF
            return False

        try:
            result = self._send("GET", "/endpoint/account.get_default_address")
        except (RocketNowHTTPError, error.URLError) as exc:
            probe_error = exc
        else:
            if self.session.access_token != old_token:
                return True
            address = result.get("data")
            if isinstance(address, dict) and "regionId" in address:
                try:
                    self._set_location(address)
                except (KeyError, TypeError, ValueError):
                    pass
                else:
                    try:
                        self._send(
                            "POST", "/endpoint/ads.splash_screen",
                            json_body={"regionId": address["regionId"]},
                        )
                    except (RocketNowHTTPError, error.URLError) as exc:
                        probe_error = exc
        if self.session.access_token != old_token:
            return True
        if isinstance(probe_error, RocketNowHTTPError):
            raise RuntimeError(f"Session renewal failed (HTTP {probe_error.status}); run `rocketnow auth pair-proxy`") from None
        if isinstance(probe_error, error.URLError):
            raise RuntimeError("Session renewal unavailable; check the network and retry") from None
        raise RuntimeError("Session renewal was not offered; run `rocketnow auth pair-proxy`")

    def _send(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
    ) -> dict:
        url = API_ORIGIN + path
        if params:
            normalized_params = {
                key: str(value).lower() if isinstance(value, bool) else value
                for key, value in params.items()
            }
            url += "?" + parse.urlencode(normalized_params)
        body = None if json_body is None else json.dumps(json_body, separators=(",", ":")).encode()
        proof = self.session.signer.proof(method, url, self.session.access_token)
        headers = {
            "Accept": "*/*",
            "Accept-Language": "ja;q=1.0, en-US;q=0.9, en;q=0.8",
            "Authorization": "DPoP " + self.session.access_token,
            "DPoP": proof,
            "User-Agent": (
                "Rocket Now/" + os.environ.get("ROCKETNOW_APP_VERSION", "1.15.0")
                + " (com.cpone.customer; build:328257; iOS "
                + os.environ.get("ROCKETNOW_IOS_VERSION", "26.6.1")
                + ") Alamofire/5.9.1"
            ),
            "X-Eats-App-Version": os.environ.get("ROCKETNOW_APP_VERSION", "1.15.0"),
            "X-Eats-Device-Id": self.session.device_id,
            "X-Eats-Pcid": self.session.pcid,
            "X-Eats-OS-Type": "iOS",
            "X-Eats-OS-Version": os.environ.get("ROCKETNOW_IOS_VERSION", "26.6.1"),
            "X-Eats-Device-Density": "X3",
            "X-Eats-Device-Model": "iPhone",
            "X-Eats-Resolution-Type": "402x874",
            "X-Eats-Network-Type": "WiFi",
            "X-Eats-Time-Zone": "Asia/Tokyo",
            "X-Eats-Locale": "ja",
        }
        if self.session.token_binding:
            headers["X-Coupang-Sec-Token-Binding"] = self.session.token_binding
        if self.session.app_session_id:
            headers["X-Eats-Session-Id"] = self.session.app_session_id
        if self.session.member_pcid:
            headers["X-Member-Pcid"] = self.session.member_pcid
        if body is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
        if path in ("/endpoint/checkout.get_payment_methods", "/endpoint/checkout.prepay", "/endpoint/checkout.confirm_payment_result"):
            headers["Cookie"] = "x-eats-uuid=" + self.session.device_id
        if path.startswith(("/endpoint/store.", "/endpoint/checkout.", "/endpoint/ads.splash_screen")):
            headers["X-Eats-Location"] = self._location_header(params)
        req = request.Request(url, data=body, method=method, headers=headers)
        try:
            with request.urlopen(req, timeout=30) as response:
                self._accept_rotated_token(response.headers.get("Authorization"))
                value = json.load(response)
        except error.HTTPError as exc:
            raise RocketNowHTTPError(exc.code) from None
        if not isinstance(value, dict):
            raise RuntimeError("Rocket Now returned an unexpected JSON response")
        return value

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
    ) -> dict:
        if not path.startswith("/endpoint/"):
            raise ValueError("Only observed customer endpoints are supported")
        method = method.upper()
        if method not in ("GET", "POST"):
            raise ValueError("Unsupported HTTP method")
        if self.session.expires_at - time.time() <= self._EARLY_REFRESH_WINDOW:
            self.refresh()
        return self._send(method, path, params=params, json_body=json_body)


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _jwt_payload(token: str) -> dict:
    payload = token.split(".")[1]
    decoded = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
    value = json.loads(decoded)
    if not isinstance(value, dict):
        raise ValueError("Invalid JWT payload")
    return value
