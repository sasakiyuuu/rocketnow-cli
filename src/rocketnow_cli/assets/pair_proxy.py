"""One-shot mitmproxy addon for pairing an app login with this CLI's DPoP key.

The CLI launches this packaged addon in a dedicated proxy process.

Only the first exact token-exchange request is modified. No token or key is logged.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit


# mitmdump may use a different Python environment from the CLI install.
# Make the sibling rocketnow_cli package importable from this installed asset.
_package_root = Path(__file__).resolve().parents[2]
if str(_package_root) not in sys.path:
    sys.path.insert(0, str(_package_root))

from rocketnow_cli.auth import (  # noqa: E402
    Session, exchange_session_metadata, jwt_exp, save_session, session_path,
)
from rocketnow_cli.dpop import DPoPSigner  # noqa: E402


EXCHANGE_URL = "https://csg.rocketnow.co.jp/auth/exchange_token"
_FLOW_MARKER = "rocketnow_cli_pair_exchange"
_SESSION_FILE = session_path()  # Compatibility for callers inspecting the default path.
_FOLLOWUP_TIMEOUT_SECONDS = 10


def _request_header(headers: object, name: str) -> str | None:
    """Read a request header from mitmproxy or a plain mapping."""
    value = headers.get(name)  # type: ignore[attr-defined]
    if value is None:
        value = next(
            (item for key, item in headers.items() if key.lower() == name.lower()),  # type: ignore[attr-defined]
            None,
        )
    return value if isinstance(value, str) and value else None


class PairProxy:
    """Bind one authorized app token exchange to a fresh local P-256 key."""

    def __init__(self) -> None:
        self.attempted = False
        self.completed = False
        self.signer: DPoPSigner | None = None
        self.session: Session | None = None
        self._timeout_handle: asyncio.TimerHandle | None = None

    def request(self, flow: object) -> None:
        request = flow.request
        if self.completed and self.session is not None:
            self._capture_followup(request)
            return
        if self.attempted or request.method.upper() != "POST" or request.url != EXCHANGE_URL:
            return

        # Claim the single attempt before modifying the request. A retry must not
        # silently issue another token bound to a different key.
        self.attempted = True
        self.signer = DPoPSigner.generate()
        request.headers["DPoP"] = self.signer.proof("POST", EXCHANGE_URL)
        flow.metadata[_FLOW_MARKER] = True
        print("Rocket Now pairing: forwarding the first token exchange")

    def response(self, flow: object) -> None:
        if self.completed or not flow.metadata.get(_FLOW_MARKER) or self.signer is None:
            return
        response = flow.response
        if response is None or response.status_code != 200:
            print("Rocket Now pairing: token exchange did not succeed")
            self._shutdown()
            return
        try:
            result = json.loads(response.content)
            data = result["rData"]
            token = data["accessToken"]
            if data.get("tokenType") != "DPoP" or not isinstance(token, str) or not token:
                raise ValueError("response is not a DPoP token exchange")
            headers = flow.request.headers
            session = Session(
                token,
                self.signer,
                jwt_exp(token),
                device_id=_request_header(headers, "X-Eats-Device-Id"),
                pcid=_request_header(headers, "X-Eats-Pcid"),
                app_session_id=_request_header(headers, "X-Eats-Session-Id"),
                member_pcid=_request_header(headers, "X-Member-Pcid"),
                **exchange_session_metadata(data),
            )
            save_session(session, session_path())
        except (KeyError, TypeError, ValueError, UnicodeDecodeError, OSError) as exc:
            print(f"Rocket Now pairing: could not save session ({type(exc).__name__})")
            self._shutdown()
            return

        self.completed = True
        self.session = session
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Offline callers have no proxy event loop to wait for another request.
            self._finish()
            return
        self._timeout_handle = loop.call_later(_FOLLOWUP_TIMEOUT_SECONDS, self._finish)
        print("Rocket Now pairing: session saved; waiting briefly for app headers")

    def _capture_followup(self, request: object) -> None:
        session = self.session
        if session is None or urlsplit(request.url).hostname != "csg.rocketnow.co.jp":
            return
        headers = request.headers
        if _request_header(headers, "Authorization") != "DPoP " + session.access_token:
            return
        for attribute, header in (
            ("token_binding", "X-Coupang-Sec-Token-Binding"),
            ("device_id", "X-Eats-Device-Id"),
            ("pcid", "X-Eats-Pcid"),
            ("app_session_id", "X-Eats-Session-Id"),
            ("member_pcid", "X-Member-Pcid"),
        ):
            value = _request_header(headers, header)
            if value is not None:
                setattr(session, attribute, value)
        try:
            save_session(session, session_path())
        except OSError as exc:
            print(f"Rocket Now pairing: could not update session ({type(exc).__name__})")
        self._finish()

    def _finish(self) -> None:
        if self._timeout_handle is not None:
            self._timeout_handle.cancel()
            self._timeout_handle = None
        print("Rocket Now pairing: proxy stopping")
        self.session = None
        self._shutdown()

    @staticmethod
    def _shutdown() -> None:
        try:
            from mitmproxy import ctx
        except ImportError:
            return
        master = getattr(ctx, "master", None)
        shutdown = getattr(master, "shutdown", None)
        if callable(shutdown):
            shutdown()


addons = [PairProxy()]
