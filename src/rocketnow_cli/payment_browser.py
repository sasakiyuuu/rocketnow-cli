"""Private browser setup for the observed Rocket Now PayPay web flow."""

from __future__ import annotations

import os
from pathlib import Path

from .auth import Session, save_session
from .api import RocketNowAPI


def refresh_web_session(api: RocketNowAPI, session: Session) -> None:
    data = api.web_session()
    header = data.get("ssoAuthHeader") if isinstance(data, dict) else None
    if not isinstance(header, str) or not header:
        raise ValueError("Payment web session refresh did not return authentication")
    session.sso_auth_header = header
    save_session(session)


def browser_config(session: Session, payment_url: str, amount: int, output_dir: Path, user_agent: str, expected_order_id: int | str | None = None) -> dict:
    if not session.access_token_hash or not session.sso_auth_header:
        raise ValueError("Payment web credentials are unavailable; pair the CLI again")
    if not user_agent.startswith("Mozilla/5.0 ") or "AppleWebKit/" not in user_agent:
        raise ValueError("Payment browser requires the captured WebView User-Agent, separate from deviceUserAgent")
    if amount != 1000:
        raise ValueError("This PayPay browser flow is authorized for 1000 JPY only")
    app_version = os.environ.get("ROCKETNOW_APP_VERSION", "1.15.1")
    ios_version = os.environ.get("ROCKETNOW_IOS_VERSION", "26.6.1")
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ja",
        "User-Agent": user_agent,
        "Rocketpay-App": f"EATS|0|{session.pcid}|iOS|{ios_version}|1.0.0|",
        "X-Coupang-Accept-Language": "ja-JP",
        "X-Coupang-Sec-Token-Binding": session.token_binding,
        "X-Eats-App-Version": app_version,
        "X-Eats-OS-Type": "iOS",
        "X-Eats-OS-Version": ios_version,
        "X-Eats-Pcid": session.pcid,
        "X-Sso-Auth": session.sso_auth_header,
    }
    if session.member_pcid:
        headers["X-Member-Pcid"] = session.member_pcid
    config = {
        "paymentUrl": payment_url,
        "accessToken": session.access_token,
        "privateKeyPem": session.signer.private_pem().decode(),
        "headers": headers,
        "cookies": [{
            "name": "CT_ATH", "value": session.access_token_hash,
            "domain": ".rocketnow.co.jp", "path": "/", "secure": True,
            "httpOnly": True, "sameSite": "None",
        }],
        "expectedAmount": amount,
        "outputDir": str(output_dir),
    }
    if expected_order_id is not None:
        identifier = str(expected_order_id)
        if not identifier.isdigit():
            raise ValueError("Payment browser needs a numeric order ID")
        config["expectedOrderId"] = identifier
    return config
