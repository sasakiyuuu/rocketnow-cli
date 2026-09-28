"""JSON-first command-line interface for the user's Rocket Now account."""

from __future__ import annotations

import argparse
import getpass
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
from datetime import datetime, timezone

from .api import RocketNowAPI, RocketNowAPIError
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
from .transport import HTTPTransport, RocketNowHTTPError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rocketnow")
    sub = parser.add_subparsers(dest="command", required=True)
    auth = sub.add_parser("auth", help="Sign in or inspect the local session")
    auth_sub = auth.add_subparsers(dest="action", required=True)
    auth_sub.add_parser("login")
    auth_sub.add_parser("pair-proxy", help="Pair this CLI with a login from the phone through a dedicated proxy")
    auth_sub.add_parser("status")
    auth_sub.add_parser("refresh", help="Renew an expired bound session when the API offers rotation")

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

    categories = sub.add_parser("categories", help="List restaurant categories")
    categories.add_argument("--app-session", action="store_true", help="Read captured iPhone app traffic")
    category = sub.add_parser("category", help="List stores in a category")
    category.add_argument("category_id", type=int)
    category.add_argument("--app-session", action="store_true", help="Read captured iPhone app traffic")

    store = sub.add_parser("store", help="Show a store and its menu")
    store.add_argument("store_id")
    store.add_argument("--lat", type=float)
    store.add_argument("--lon", type=float)
    store.add_argument("--source-type", default="SEARCH")
    store.add_argument("--app-session", action="store_true", help="Read a menu captured from the iPhone app")

    dish = sub.add_parser("dish", help="Show a dish and its options")
    dish.add_argument("store_id")
    dish.add_argument("dish_id")
    dish.add_argument("--delivery-type", default="DELIVERY")

    dishes = sub.add_parser("dishes", help="List products captured from iPhone store menus")
    dishes.add_argument("--app-session", action="store_true", required=True)
    dishes.add_argument("--max-price", type=int)

    discover = sub.add_parser("discover-dishes", help="Scan nearby store menus for dishes by item price")
    discover.add_argument("--max-price", type=int, default=1000)
    discover.add_argument("--limit-stores", type=int, default=100)
    discover.add_argument("--categories", default="1,2,3,6,8,9,11,17,21,22", help="Comma-separated category IDs")
    discover.add_argument("--delay", type=float, default=0.3, help="Seconds between store requests (minimum 0.2)")

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
        ["mitmdump", "-q", "--listen-host", "0.0.0.0", "--listen-port", str(pair_port),
         "--ignore-hosts", r"^member\.rocketnow\.co\.jp(?::443)?$", "-s", str(addon)],
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
    if draft.get("payMethodCode", "GP_CARD") != "GP_CARD":
        return draft
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
    checkout_request, preview, _, address = _build_review(api, draft, draft.get("payMethodCode", "GP_CARD"))
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
    return _submit_reviewed_order(api, body, review, args.approve_hash, args.approve_amount)


def _submit_reviewed_order(api: RocketNowAPI, body: dict, review: dict, approve_hash: str, approve_amount: int) -> dict:
    """Submit one fresh, verified purchase intent without replaying it."""
    if review["requestedAmount"] != approve_amount:
        raise ValueError("Checkout changed since approval; run order check again")
    verify_review(approve_hash, body)
    pending_id = approve_hash
    create_pending_order(pending_id, review)
    state = load_pending_order(pending_id)
    state["prepayRequest"] = body
    update_pending_order(pending_id, state)
    consume_review(pending_id)
    try:
        prepay = api.prepay(body)
    except Exception as exc:
        state = load_pending_order(pending_id)
        state["status"] = "requires_reconciliation"
        failure = {"type": type(exc).__name__}
        detail = ""
        if isinstance(exc, RocketNowHTTPError):
            failure["httpStatus"] = exc.status
            detail = f" (HTTP {exc.status})"
        elif isinstance(exc, RocketNowAPIError) and isinstance(exc.error, dict):
            code = exc.error.get("code")
            if isinstance(code, int) or (isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,40}", code)):
                failure["apiCode"] = code
                detail = f" (API {code})"
        state["submissionFailure"] = failure
        update_pending_order(pending_id, state)
        raise RuntimeError(
            "Purchase submission outcome is unknown" + detail + "; check the app before trying again"
        ) from None
    if not isinstance(prepay, dict) or not prepay.get("success"):
        raise RuntimeError("Purchase was not accepted; check the app before trying again")
    state = load_pending_order(pending_id)
    if (
        prepay.get("amount") != approve_amount
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
    except Exception as exc:
        state["status"] = "requires_reconciliation"
        failure = {"type": type(exc).__name__}
        detail = ""
        if isinstance(exc, RocketNowHTTPError):
            failure["httpStatus"] = exc.status
            detail = f" (HTTP {exc.status})"
        elif isinstance(exc, RocketNowAPIError) and isinstance(exc.error, dict):
            code = exc.error.get("code")
            if isinstance(code, int) or (isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,40}", code)):
                failure["apiCode"] = code
                detail = f" (API {code})"
        state["confirmationFailure"] = failure
        update_pending_order(pending_id, state)
        raise RuntimeError(
            "Payment confirmation outcome is unknown" + detail + "; check the app before trying again"
        ) from None
    state["status"] = "approved" if result.get("status") == "PAYMENT_APPROVED" else "requires_reconciliation"
    state["paymentStatus"] = result.get("status")
    update_pending_order(pending_id, state)
    return {"pendingId": pending_id, "status": state["status"], "paymentStatus": state["paymentStatus"]}


def _safe_categories(items: object) -> list[dict]:
    if not isinstance(items, list):
        return []
    return [
        {key: item[key] for key in ("id", "name") if key in item}
        for item in items
        if isinstance(item, dict) and "id" in item and "name" in item
    ]


def _safe_stores(items: object) -> list[dict]:
    if not isinstance(items, list):
        return []
    visible = ("id", "name", "estimatedDeliveryTime", "openStatus", "reviewRating")
    return [
        {key: item[key] for key in visible if key in item}
        for item in items
        if isinstance(item, dict) and "id" in item and "name" in item
    ]


def _safe_menu(entry: dict) -> dict:
    dishes = entry.get("dishes")
    return {
        "id": entry.get("id"),
        "name": entry.get("name"),
        "dishes": [
            {key: dish[key] for key in ("id", "name", "price", "available") if key in dish}
            for dish in dishes
            if isinstance(dish, dict) and "id" in dish and "name" in dish
        ] if isinstance(dishes, list) else [],
    }


def _captured_dishes(catalog: dict, max_price: int | None) -> dict:
    if max_price is not None and max_price < 0:
        raise ValueError("--max-price must be nonnegative")
    menus = catalog.get("menuByStore")
    if not isinstance(menus, dict) or not menus:
        raise ValueError("No store menus have been captured; open stores in the iPhone app on proxy port 8080")
    rows = []
    for store_id, menu in menus.items():
        if not isinstance(menu, dict):
            continue
        store_name = menu.get("name")
        if not isinstance(store_name, str):
            continue
        for dish in menu.get("dishes", []):
            if not isinstance(dish, dict):
                continue
            price = dish.get("price")
            if (dish.get("available") is not True or
                    not isinstance(price, int) or isinstance(price, bool) or price < 0 or
                    (max_price is not None and price > max_price) or
                    not isinstance(dish.get("name"), str) or "id" not in dish):
                continue
            rows.append({
                "storeId": menu.get("id", store_id), "store": store_name,
                "id": dish["id"], "name": dish["name"],
                "price": price, "available": True,
            })
    rows.sort(key=lambda row: (row["price"], str(row["store"]), str(row["name"]), str(row["storeId"]), str(row["id"])))
    return {
        "source": catalog.get("source", "app_proxy_cache"),
        "capturedAt": catalog.get("capturedAt"),
        "count": len(rows),
        "dishes": rows,
    }


def run(args: argparse.Namespace) -> object:
    if args.command == "discover-dishes":
        if args.max_price <= 0:
            raise ValueError("--max-price must be positive")
        if not 1 <= args.limit_stores <= 100:
            raise ValueError("--limit-stores must be between 1 and 100")
        if not math.isfinite(args.delay) or args.delay < 0.2:
            raise ValueError("--delay must be at least 0.2 seconds")
        parts = args.categories.split(",")
        if not parts or any(not part.strip().isdigit() for part in parts):
            raise ValueError("--categories must be comma-separated positive integer IDs")
        category_ids = list(dict.fromkeys(int(part.strip()) for part in parts))
        if any(category_id <= 0 for category_id in category_ids):
            raise ValueError("--categories must be comma-separated positive integer IDs")
        session = load_session()
        api = RocketNowAPI(HTTPTransport(session))
        address = api.default_address()
        latitude, longitude = address.get("latitude"), address.get("longitude")
        if (isinstance(latitude, bool) or isinstance(longitude, bool) or
                not isinstance(latitude, (int, float)) or not isinstance(longitude, (int, float)) or
                not math.isfinite(latitude) or not math.isfinite(longitude) or
                not -90 <= latitude <= 90 or not -180 <= longitude <= 180):
            raise ValueError("Saved delivery address has no valid coordinates")
        from .discover import scan_catalog

        return scan_catalog(
            api, latitude, longitude, category_ids,
            max_price=args.max_price, limit_stores=args.limit_stores, delay_seconds=args.delay,
        )
    if args.command == "auth":
        if args.action == "login":
            return _auth_login()
        if args.action == "pair-proxy":
            return _auth_pair_proxy()
        session = load_session()
        if args.action == "refresh":
            refreshed = HTTPTransport(session).refresh()
            return {
                "authenticated": not session.expired,
                "refreshed": refreshed,
                "expiresAt": datetime.fromtimestamp(session.expires_at, timezone.utc).isoformat(),
            }
        renewal_error = None
        if session.expired:
            try:
                HTTPTransport(session).refresh()
            except RuntimeError as exc:
                renewal_error = str(exc)
        status = {
            "authenticated": not session.expired,
            "expiresAt": datetime.fromtimestamp(session.expires_at, timezone.utc).isoformat(),
        }
        if renewal_error:
            status["renewalError"] = renewal_error
        return status
    if args.command == "config":
        if args.action == "import-payment":
            password = os.environ.get("ROCKETNOW_MITMWEB_PASSWORD") or getpass.getpass("Local mitmweb password: ")
            import_payment_config(args.mitmweb_url, password)
        config = load_payment_config()
        return {
            "paymentConfigured": bool(config.get("merchantMallKey") and config.get("deviceUserAgent")),
            "preferredCardConfigured": bool(config.get("preferredPayMethodId")),
        }
    if args.command in ("categories", "category", "store", "dishes") and args.app_session:
        from .proxy_catalog import load_app_catalog

        catalog = load_app_catalog()
        if args.command == "dishes":
            return _captured_dishes(catalog, args.max_price)
        if args.command == "store":
            entry = catalog.get("menuByStore", {}).get(str(args.store_id))
            if not isinstance(entry, dict):
                raise ValueError("Store menu has not been captured; open this store in the iPhone app on proxy port 8080")
            return {
                "source": catalog.get("source", "app_proxy_cache"),
                "capturedAt": catalog.get("capturedAt"),
                **_safe_menu(entry),
            }
        if args.command == "categories":
            return {
                "source": catalog.get("source", "app_proxy_cache"),
                "capturedAt": catalog.get("capturedAt"),
                "categories": _safe_categories(catalog.get("categories", [])),
            }
        entry = catalog.get("categoryStores", {}).get(str(args.category_id))
        if entry is None:
            raise ValueError("Category has not been captured in the iPhone app yet")
        return {
            "source": catalog.get("source", "app_proxy_cache"),
            "categoryId": args.category_id,
            "name": entry.get("name"),
            "capturedAt": entry.get("capturedAt"),
            "stores": _safe_stores(entry.get("stores", [])),
        }
    if args.command == "store" and (args.lat is None or args.lon is None):
        raise ValueError("--lat and --lon are required for store unless --app-session is used")
    session = load_session()
    api = RocketNowAPI(HTTPTransport(session))
    if args.command == "categories":
        data = api.category_list()
        return {"source": "api", "title": data.get("title"), "categories": _safe_categories(data.get("list", []))}
    if args.command == "category":
        data = api.category_stores(args.category_id)
        stores = [item.get("entity", {}).get("data", {}) for item in data.get("entityList", []) if item.get("viewType") == "storeCardWithMenu"]
        return {"source": "api", "categoryId": args.category_id, "stores": _safe_stores(stores), "hasMore": bool(data.get("nextToken"))}
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
