"""Authenticated JSON transport for the observed customer API."""

from __future__ import annotations

import json
import os
from urllib import error, parse, request

from .auth import API_ORIGIN, Session


class RocketNowHTTPError(RuntimeError):
    def __init__(self, status: int) -> None:
        self.status = status
        super().__init__(f"Rocket Now returned HTTP {status}")


class HTTPTransport:
    def __init__(self, session: Session) -> None:
        self.session = session
        self._location: dict | None = None

    @property
    def location_address_id(self) -> int | None:
        return None if self._location is None else self._location.get("addressId")

    def _location_header(self, params: dict | None) -> str:
        if self._location is None:
            response = self.request("GET", "/endpoint/account.get_default_address")
            address = response.get("data")
            if not isinstance(address, dict):
                raise RuntimeError("No default delivery address is available")
            self._location = {
                "regionId": address["regionId"],
                "latitude": address["latitude"],
                "siGunGu": parse.quote(address["siGunGu"], safe=""),
                "siDo": parse.quote(address["siDo"], safe=""),
                "zipcode": address["zipCode"],
                "addressId": address["customerAddressId"],
                "longitude": address["longitude"],
            }
        location = self._location.copy()
        if params:
            for coordinate in ("latitude", "longitude"):
                if coordinate in params:
                    location[coordinate] = params[coordinate]
        return json.dumps(location, separators=(",", ":"))

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
    ) -> dict:
        if self.session.expired:
            raise RuntimeError("Session expired; run `rocketnow auth login` again")
        if not path.startswith("/endpoint/"):
            raise ValueError("Only observed customer endpoints are supported")
        method = method.upper()
        if method not in ("GET", "POST"):
            raise ValueError("Unsupported HTTP method")
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
        if body is not None:
            headers["Content-Type"] = "application/json"
        if path.startswith(("/endpoint/store.", "/endpoint/checkout.")):
            headers["X-Eats-Location"] = self._location_header(params)
        req = request.Request(url, data=body, method=method, headers=headers)
        try:
            with request.urlopen(req, timeout=30) as response:
                value = json.load(response)
        except error.HTTPError as exc:
            raise RocketNowHTTPError(exc.code) from None
        if not isinstance(value, dict):
            raise RuntimeError("Rocket Now returned an unexpected JSON response")
        return value
