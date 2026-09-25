"""One-shot mitmproxy addon for pairing an app login with this CLI's DPoP key.

The CLI launches this packaged addon in a dedicated proxy process.

Only the first exact token-exchange request is modified. No token or key is logged.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys


# mitmdump may use a different Python environment from the CLI install.
# Make the sibling rocketnow_cli package importable from this installed asset.
_package_root = Path(__file__).resolve().parents[2]
if str(_package_root) not in sys.path:
    sys.path.insert(0, str(_package_root))

from rocketnow_cli.auth import Session, jwt_exp, save_session, session_path  # noqa: E402
from rocketnow_cli.dpop import DPoPSigner  # noqa: E402


EXCHANGE_URL = "https://csg.rocketnow.co.jp/auth/exchange_token"
_FLOW_MARKER = "rocketnow_cli_pair_exchange"
_SESSION_FILE = session_path()  # Compatibility for callers inspecting the default path.


class PairProxy:
    """Bind one authorized app token exchange to a fresh local P-256 key."""

    def __init__(self) -> None:
        self.attempted = False
        self.completed = False
        self.signer: DPoPSigner | None = None

    def request(self, flow: object) -> None:
        request = flow.request
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
            session = Session(token, self.signer, jwt_exp(token))
            save_session(session, session_path())
        except (KeyError, TypeError, ValueError, UnicodeDecodeError, OSError) as exc:
            print(f"Rocket Now pairing: could not save session ({type(exc).__name__})")
            self._shutdown()
            return

        self.completed = True
        print("Rocket Now pairing: session saved; proxy stopping")
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
