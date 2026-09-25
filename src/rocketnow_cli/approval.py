"""Short-lived, single-use approval records for a concrete purchase intent."""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import time
from typing import Any


_VOLATILE_FIELDS = frozenset({"searchIds", "searchJourneyIds", "deviceInfo"})
_HASH_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


def review_directory() -> Path:
    return Path.home() / "Library" / "Application Support" / "rocketnow-cli" / "reviews"


def _review_path(review_hash: str) -> Path:
    if not _HASH_PATTERN.fullmatch(review_hash):
        raise ValueError("Invalid review hash")
    return review_directory() / f"{review_hash}.json"


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def intent_digest(intent: dict[str, Any]) -> str:
    """Bind every prepay field except the three observed volatile top-level fields."""
    if not isinstance(intent, dict):
        raise ValueError("Purchase intent must be an object")
    stable_intent = {key: value for key, value in intent.items() if key not in _VOLATILE_FIELDS}
    return hashlib.sha256(_canonical_json(stable_intent)).hexdigest()


def _bound_hash(digest: str, nonce: str) -> str:
    return hashlib.sha256(_canonical_json({"digest": digest, "nonce": nonce})).hexdigest()


def _write_atomic(path: Path, record: dict[str, Any], *, create: bool) -> None:
    fd, temp_name = tempfile.mkstemp(prefix=".review-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(_canonical_json(record))
            output.flush()
            os.fsync(output.fileno())
        if create:
            os.link(temp_name, path)
        else:
            os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _read_record(review_hash: str) -> dict[str, Any]:
    try:
        with _review_path(review_hash).open("r", encoding="utf-8") as source:
            record = json.load(source)
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise ValueError("Purchase review was not found or is invalid") from exc
    if not isinstance(record, dict):
        raise ValueError("Purchase review is invalid")
    digest, nonce = record.get("digest"), record.get("nonce")
    if not isinstance(digest, str) or not isinstance(nonce, str):
        raise ValueError("Purchase review is invalid")
    if not hmac.compare_digest(_bound_hash(digest, nonce), review_hash):
        raise ValueError("Purchase review hash mismatch")
    return record


def create_review(intent: dict[str, Any]) -> str:
    """Store a fresh approval capability; presenting it alone does not submit an order."""
    directory = review_directory()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    digest = intent_digest(intent)
    while True:
        nonce = secrets.token_hex(32)
        review_hash = _bound_hash(digest, nonce)
        record = {
            "nonce": nonce,
            "digest": digest,
            "createdAt": time.time(),
            "status": "prepared",
            "tracking": {
                "searchId": intent.get("searchIds"),
                "searchJourneyId": intent.get("searchJourneyIds"),
            },
        }
        try:
            _write_atomic(_review_path(review_hash), record, create=True)
            return review_hash
        except FileExistsError:
            continue


def verify_review(review_hash: str, intent: dict[str, Any], ttl_seconds: int = 600) -> None:
    """Reject missing, expired, consumed, or changed purchase approvals."""
    if ttl_seconds <= 0:
        raise ValueError("Approval lifetime must be positive")
    record = _read_record(review_hash)
    if record.get("status") != "prepared":
        raise ValueError("Purchase review has already been submitted")
    try:
        age = time.time() - float(record["createdAt"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Purchase review timestamp is invalid") from exc
    if age < 0 or age > ttl_seconds:
        raise ValueError("Purchase review has expired")
    if not hmac.compare_digest(record["digest"], intent_digest(intent)):
        raise ValueError("Purchase intent changed since review")


def review_tracking(review_hash: str) -> dict[str, str]:
    """Return the stored search IDs so submit does not repeat a search."""
    tracking = _read_record(review_hash).get("tracking") or {}
    if not isinstance(tracking, dict):
        raise ValueError("Purchase review tracking is invalid")
    search_id, journey_id = tracking.get("searchId"), tracking.get("searchJourneyId")
    if not isinstance(search_id, str) or not isinstance(journey_id, str):
        raise ValueError("Purchase review tracking is missing")
    return {"searchId": search_id, "searchJourneyId": journey_id}


def consume_review(review_hash: str) -> None:
    """Mark a review submitted exactly once, before sending the purchase request."""
    path = _review_path(review_hash)
    lock_path = path.with_suffix(".lock")
    try:
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    except FileNotFoundError as exc:
        raise ValueError("Purchase review was not found") from exc
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        record = _read_record(review_hash)
        if record.get("status") != "prepared":
            raise ValueError("Purchase review has already been submitted")
        record["status"] = "submitted"
        _write_atomic(path, record, create=False)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
