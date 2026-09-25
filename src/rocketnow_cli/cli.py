"""JSON-first command-line interface for the user's Rocket Now account."""

from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import subprocess
import sys
from datetime import datetime, timezone

from .api import RocketNowAPI
from .auth import LoginFlow, load_session, save_session, session_path
from .cart import build_cart_request, build_checkout_request
from .review import summarize_checkout
from .approval import consume_review, create_review, review_tracking, verify_review
from .order import (
    build_prepay_request,
    create_pending_order,
    load_payment_config,
    load_pending_order,
    resolve_search_tracking,
    update_pending_order,
)
from .mitmweb_import import import_payment_config
from .transport import HTTPTransport


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rocketnow")
    sub = parser.add_subparsers(dest="command", required=True)
    auth = sub.add_parser("auth", help="Sign in or inspect the local session")
    auth_sub = auth.add_subparsers(dest="action", required=True)
    auth_sub.add_parser("login")
    auth_sub.add_parser("pair-proxy", help="Pair this CLI with a login from the phone through a dedicated proxy")
    auth_sub.add_parser("status")

    config = sub.add_parser("config", help="Inspect or import local payment setup")
    config_sub = config.add_subparsers(dest="action", required=True)
    config_sub.add_parser("status")
    config_import = config_sub.add_parser("import-payment", help="Import setup from your own local mitmweb order capture")
    config_import.add_argument("--mitmweb-url", default="http://127.0.0.1:8081")

    search = sub.add_parser("search", help="Search stores or dishes")
    search.add_argument("keyword")
    search.add_argument("--lat", type=float, required=True)
    search.add_argument("--lon", type=float, required=True)

    autocomplete = sub.add_parser("autocomplete")
    autocomplete.add_argument("keyword")

    store = sub.add_parser("store", help="Show a store and its menu")
    store.add_argument("store_id")
    store.add_argument("--lat", type=float, required=True)
    store.add_argument("--lon", type=float, required=True)
    store.add_argument("--source-type", default="SEARCH")

    dish = sub.add_parser("dish", help="Show a dish and its options")
    dish.add_argument("store_id")
    dish.add_argument("dish_id")
    dish.add_argument("--delivery-type", default="DELIVERY")

    orders = sub.add_parser("orders", help="Show order history")
    orders.add_argument("--in-progress", action="store_true")
    sub.add_parser("address", help="Show the saved delivery address")
    sub.add_parser("payment-methods")

    quote = sub.add_parser("cart-quote", help="Calculate a cart from a JSON request file")
    quote.add_argument("request_file", type=Path)
    draft_quote = sub.add_parser("cart-quote-draft", help="Build and calculate a cart from dish IDs and option IDs")
    draft_quote.add_argument("draft_file", type=Path)
    preview = sub.add_parser("checkout-preview", help="Preview checkout from a JSON request file")
    preview.add_argument("request_file", type=Path)
    draft_preview = sub.add_parser("checkout-preview-draft", help="Build a cart and checkout preview from dish IDs")
    draft_preview.add_argument("draft_file", type=Path)
    draft_preview.add_argument("--pay-method-code", default="GP_CARD")
    draft_review = sub.add_parser("checkout-review-draft", help="Show a concise purchase review from dish IDs")
    draft_review.add_argument("draft_file", type=Path)
    draft_review.add_argument("--pay-method-code", default="GP_CARD")

    order = sub.add_parser("order", help="Purchase after explicit review and payment authentication")
    order_sub = order.add_subparsers(dest="action", required=True)
    check = order_sub.add_parser("check", help="Validate a draft and show the purchase review without ordering")
    check.add_argument("draft_file", type=Path)
    submit = order_sub.add_parser("submit", help="Start a reviewed saved-card purchase")
    submit.add_argument("draft_file", type=Path)
    submit.add_argument("--approve-hash", required=True)
    submit.add_argument("--approve-amount", type=int, required=True)
    payment_url = order_sub.add_parser("payment-url", help="Open the hosted payment page")
    payment_url.add_argument("pending_id")
    payment_url.add_argument("--reveal", action="store_true", help="Explicitly print the sensitive payment URL")
    confirm = order_sub.add_parser("confirm", help="Confirm the hosted payment result")
    confirm.add_argument("pending_id")
    confirm.add_argument("--payment-complete", action="store_true")
    status = order_sub.add_parser("status", help="Show locally stored purchase state")
    status.add_argument("pending_id")
    return parser


def _auth_login() -> dict:
    flow = LoginFlow()
    browser_script = Path(__file__).resolve().parent / "assets" / "login_browser.js"
    print("ブラウザでロケットナウにログインしてください。SMSやメールの認証コードはブラウザに入力します。", file=sys.stderr)
    result = subprocess.run(
        ["node", str(browser_script), flow.landing_url()],
        stdout=subprocess.PIPE,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError("Browser login failed; see the error above")
    auth_code = json.loads(result.stdout.strip())
    session = flow.exchange(auth_code["code"], auth_code["state"])
    save_session(session)
    return {"authenticated": True, "expiresAt": datetime.fromtimestamp(session.expires_at, timezone.utc).isoformat()}


def _auth_pair_proxy() -> dict:
    addon = Path(__file__).resolve().parent / "assets" / "pair_proxy.py"
    target = session_path()
    before = target.stat().st_mtime_ns if target.exists() else None
    pair_port = int(os.environ.get("ROCKETNOW_PAIR_PORT", "8082"))
    if not 1 <= pair_port <= 65535:
        raise ValueError("ROCKETNOW_PAIR_PORT must be a TCP port")
    print(f"スマホのプロキシを同じMacのポート{pair_port}に変更し、ロケットナウでログアウト→ログインしてください。", file=sys.stderr)
    print("CLIの認証後はスマホのプロキシを元の設定に戻し、アプリで再ログインしてください。", file=sys.stderr)
    result = subprocess.run(
        ["mitmdump", "-q", "--listen-host", "0.0.0.0", "--listen-port", str(pair_port), "-s", str(addon)],
        check=False,
    )
    after = target.stat().st_mtime_ns if target.exists() else None
    if result.returncode != 0 or after is None or after == before:
        raise RuntimeError("Phone pairing did not complete")
    session = load_session(target)
    return {"authenticated": not session.expired, "expiresAt": datetime.fromtimestamp(session.expires_at, timezone.utc).isoformat()}


def _json_file(path: Path) -> dict:
    with path.open(encoding="utf-8") as source:
        value = json.load(source)
    if not isinstance(value, dict):
        raise ValueError("Request file must contain a JSON object")
    return value


def _with_preferred_card(draft: dict) -> dict:
    if draft.get("payMethodId") is not None:
        return draft
    try:
        preferred_id = load_payment_config().get("preferredPayMethodId")
    except FileNotFoundError:
        return draft
    return dict(draft, payMethodId=preferred_id) if preferred_id is not None else draft


def _checked_address(api: RocketNowAPI, checkout_request: dict) -> dict:
    address = api.default_address()
    selected_id = checkout_request["customerAddressId"]
    if address.get("customerAddressId") != selected_id:
        raise ValueError("Default delivery address changed; review checkout again")
    location_id = getattr(api.transport, "location_address_id", None)
    if location_id is not None and location_id != selected_id:
        raise ValueError("Delivery location changed; review checkout again")
    return address


def _build_review(api: RocketNowAPI, draft: dict, pay_method_code: str) -> tuple[dict, dict, dict, dict]:
    cart_request = build_cart_request(api, draft)
    api.calculate_cart_price(cart_request)
    checkout_request = build_checkout_request(
        api, cart_request, pay_method_code=pay_method_code,
        pay_method_id=draft.get("payMethodId"),
    )
    address = _checked_address(api, checkout_request)
    preview = api.checkout_preview(checkout_request)
    review = summarize_checkout(preview, address, checkout_request)
    return checkout_request, preview, review, address


def _build_purchase_review(api: RocketNowAPI, session, draft: dict) -> tuple[dict, dict]:
    draft = _with_preferred_card(draft)
    checkout_request, preview, _, address = _build_review(api, draft, "GP_CARD")
    tracked_draft = resolve_search_tracking(api, draft)
    prepay_body = build_prepay_request(
        api, session, checkout_request, preview, tracked_draft, load_payment_config()
    )
    summary = summarize_checkout(preview, address, checkout_request, prepay_body)
    return prepay_body, summary


def _submit_order(api: RocketNowAPI, session, args: argparse.Namespace) -> dict:
    draft = _json_file(args.draft_file)
    if not draft.get("searchId") or not draft.get("searchJourneyId"):
        draft = dict(draft, **review_tracking(args.approve_hash))
    body, review = _build_purchase_review(api, session, draft)
    if review["requestedAmount"] != args.approve_amount:
        raise ValueError("Checkout changed since approval; run order check again")
    verify_review(args.approve_hash, body)
    pending_id = args.approve_hash
    create_pending_order(pending_id, review)
    consume_review(pending_id)
    try:
        prepay = api.prepay(body)
    except Exception:
        raise RuntimeError(
            "Purchase submission outcome is unknown; check the app before trying again"
        ) from None
    if not isinstance(prepay, dict) or not prepay.get("success"):
        raise RuntimeError("Purchase was not accepted; check the app before trying again")
    state = load_pending_order(pending_id)
    if (
        prepay.get("amount") != args.approve_amount
        or not prepay.get("orderId")
        or not prepay.get("paymentAuthToken")
        or not prepay.get("paymentUrl")
        or prepay.get("processType") != "PREPAY"
    ):
        state["status"] = "requires_reconciliation"
        state["prepay"] = prepay
        update_pending_order(pending_id, state)
        raise RuntimeError("Purchase response differs from approval; check the app and payment state")
    state["status"] = "awaiting_payment"
    state["prepay"] = prepay
    update_pending_order(pending_id, state)
    return {
        "pendingId": pending_id,
        "status": "awaiting_payment",
        "paymentPageAvailable": bool(prepay.get("paymentUrl")),
    }


def _confirm_order(api: RocketNowAPI, pending_id: str) -> dict:
    state = load_pending_order(pending_id)
    if state.get("status") != "awaiting_payment":
        raise ValueError("This order is not awaiting payment confirmation")
    prepay = state["prepay"]
    body = {
        "transactionToken": prepay.get("transactionToken"),
        "paymentAuthToken": prepay["paymentAuthToken"],
        "orderId": prepay["orderId"],
        "amount": prepay["amount"],
        "isLegacyPayment": True,
    }
    state["status"] = "confirming"
    update_pending_order(pending_id, state)
    try:
        result = api.confirm_payment_result(body)
    except Exception:
        raise RuntimeError(
            "Payment confirmation outcome is unknown; check the app before trying again"
        ) from None
    state["status"] = "approved" if result.get("status") == "PAYMENT_APPROVED" else "requires_reconciliation"
    state["paymentStatus"] = result.get("status")
    update_pending_order(pending_id, state)
    return {"pendingId": pending_id, "status": state["status"], "paymentStatus": state["paymentStatus"]}


def run(args: argparse.Namespace) -> object:
    if args.command == "auth":
        if args.action == "login":
            return _auth_login()
        if args.action == "pair-proxy":
            return _auth_pair_proxy()
        session = load_session()
        return {"authenticated": not session.expired, "expiresAt": datetime.fromtimestamp(session.expires_at, timezone.utc).isoformat()}
    if args.command == "config":
        if args.action == "import-payment":
            password = os.environ.get("ROCKETNOW_MITMWEB_PASSWORD") or getpass.getpass("Local mitmweb password: ")
            import_payment_config(args.mitmweb_url, password)
        config = load_payment_config()
        return {
            "paymentConfigured": bool(config.get("merchantMallKey") and config.get("deviceUserAgent")),
            "preferredCardConfigured": bool(config.get("preferredPayMethodId")),
        }
    session = load_session()
    api = RocketNowAPI(HTTPTransport(session))
    if args.command == "search":
        return api.search(args.keyword, args.lat, args.lon)
    if args.command == "autocomplete":
        return api.autocomplete(args.keyword)
    if args.command == "store":
        return api.store_with_menu(args.store_id, args.lat, args.lon, args.source_type)
    if args.command == "dish":
        return api.dish(args.store_id, args.dish_id, args.delivery_type)
    if args.command == "orders":
        return api.order_history(not args.in_progress)
    if args.command == "address":
        return api.default_address()
    if args.command == "payment-methods":
        return api.payment_methods()
    if args.command == "cart-quote":
        return api.calculate_cart_price(_json_file(args.request_file))
    if args.command == "cart-quote-draft":
        request_body = build_cart_request(api, _json_file(args.draft_file))
        return {"request": request_body, "quote": api.calculate_cart_price(request_body)}
    if args.command == "checkout-preview":
        return api.checkout_preview(_json_file(args.request_file))
    if args.command in ("checkout-preview-draft", "checkout-review-draft"):
        draft = _json_file(args.draft_file)
        if args.pay_method_code == "GP_CARD":
            draft = _with_preferred_card(draft)
        if args.command == "checkout-review-draft":
            _, _, review, _ = _build_review(api, draft, args.pay_method_code)
            return review
        cart_request = build_cart_request(api, draft)
        quote = api.calculate_cart_price(cart_request)
        checkout_request = build_checkout_request(
            api, cart_request, pay_method_code=args.pay_method_code,
            pay_method_id=draft.get("payMethodId"),
        )
        _checked_address(api, checkout_request)
        preview = api.checkout_preview(checkout_request)
        return {
            "cartRequest": cart_request,
            "quote": quote,
            "checkoutRequest": checkout_request,
            "preview": preview,
        }
    if args.command == "order":
        if args.action == "check":
            draft = _json_file(args.draft_file)
            body, review = _build_purchase_review(api, session, draft)
            review["reviewHash"] = create_review(body)
            return {"prepayReady": True, "review": review}
        if args.action == "submit":
            return _submit_order(api, session, args)
        state = load_pending_order(args.pending_id)
        if args.action == "payment-url":
            if state.get("status") != "awaiting_payment":
                raise ValueError("No pending payment URL is available")
            url = state["prepay"].get("paymentUrl")
            if not isinstance(url, str) or not url:
                raise ValueError("Payment URL is unavailable")
            if args.reveal:
                return {"paymentUrl": url}
            import webbrowser
            return {"opened": webbrowser.open(url), "pendingId": args.pending_id}
        if args.action == "confirm":
            if not args.payment_complete:
                raise ValueError("Use --payment-complete after finishing the payment page")
            return _confirm_order(api, args.pending_id)
        return {"pendingId": args.pending_id, "status": state.get("status"), "review": state.get("review")}
    raise ValueError("Unsupported command")


def main() -> None:
    args = _parser().parse_args()
    try:
        value = run(args)
    except (FileExistsError, FileNotFoundError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"rocketnow: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    print(json.dumps(value, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
