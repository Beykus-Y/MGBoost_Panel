"""Authentication and wire helpers for the localhost Marzban broker."""

import hashlib
import hmac
import ipaddress
import re
import threading
import time
from urllib.parse import urlsplit


BROKER_TIMESTAMP_HEADER = "X-MGBoost-Timestamp"
BROKER_NONCE_HEADER = "X-MGBoost-Nonce"
BROKER_SIGNATURE_HEADER = "X-MGBoost-Signature"
BROKER_CLIENT_HEADER = "X-MGBoost-Client"
BROKER_CONTENT_TYPE = "application/json"
BROKER_MAX_BODY_BYTES = 64 * 1024
BROKER_OPERATIONS = frozenset({
    "legacy.user.get",
    "legacy.user.usage",
    "legacy.users.list",
    "legacy.nodes.list",
    "legacy.nodes.usage",
    "legacy.inbounds.list",
    "legacy.user.create",
    "legacy.user.renew",
    "legacy.user.set_expire",
    "legacy.user.delete",
})
_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_SIGNATURE_RE = re.compile(r"^[0-9a-f]{64}$")


def validate_shared_key(value: str) -> bytes:
    if not isinstance(value, str) or len(value.encode("utf-8")) < 32:
        raise ValueError("broker authentication key must be at least 32 bytes")
    return value.encode("utf-8")


def validate_loopback_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "http" or not parsed.hostname:
        raise ValueError("broker URL must use http on a literal loopback address")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError as exc:
        raise ValueError("broker URL hostname must be a literal loopback address") from exc
    if not address.is_loopback or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("broker URL must be localhost-only and contain no credentials/query/fragment")
    if parsed.path not in ("", "/"):
        raise ValueError("broker URL must not contain a path")
    if parsed.port is None:
        raise ValueError("broker URL must include an explicit port")
    return value.rstrip("/")


def validate_loopback_host(value: str) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise ValueError("broker listen host must be a literal loopback address") from exc
    if not address.is_loopback:
        raise ValueError("broker listen host must be loopback-only")
    return value


def build_broker_signature(
    key: str | bytes,
    method: str,
    path: str,
    timestamp: str,
    nonce: str,
    client_id: str,
    body: bytes,
) -> str:
    key_bytes = key if isinstance(key, bytes) else validate_shared_key(key)
    body_hash = hashlib.sha256(body).hexdigest()
    signed = "\n".join([method.upper(), path, timestamp, nonce, client_id, body_hash])
    return hmac.new(key_bytes, signed.encode("utf-8"), hashlib.sha256).hexdigest()


class ReplayGuard:
    def __init__(self, *, max_entries: int = 4096):
        self._seen: dict[tuple[str, str], float] = {}
        self._max_entries = max_entries
        self._lock = threading.Lock()

    def consume(self, client_id: str, nonce: str, expires_at: float, *, now: float | None = None) -> bool:
        checked_at = time.time() if now is None else now
        key = (client_id, nonce)
        with self._lock:
            for old_key, expiry in list(self._seen.items()):
                if expiry <= checked_at:
                    self._seen.pop(old_key, None)
            if key in self._seen:
                return False
            while len(self._seen) >= self._max_entries:
                oldest = min(self._seen.items(), key=lambda item: item[1])[0]
                self._seen.pop(oldest, None)
            self._seen[key] = expires_at
            return True


def authenticate_broker_request(
    *, headers, method: str, path: str, body: bytes, expected_client_id: str,
    shared_key: bytes, allowed_skew_seconds: int, replay_guard: ReplayGuard,
    now: int | None = None,
) -> tuple[bool, int, str]:
    client_id = (headers.get(BROKER_CLIENT_HEADER) or "").strip()
    timestamp_raw = (headers.get(BROKER_TIMESTAMP_HEADER) or "").strip()
    nonce = (headers.get(BROKER_NONCE_HEADER) or "").strip()
    supplied = (headers.get(BROKER_SIGNATURE_HEADER) or "").strip().lower()
    if client_id != expected_client_id or not timestamp_raw or not nonce or not supplied:
        return False, 401, "Missing or invalid broker authentication"
    if not _NONCE_RE.fullmatch(nonce) or not _SIGNATURE_RE.fullmatch(supplied):
        return False, 401, "Invalid broker authentication format"
    try:
        timestamp = int(timestamp_raw)
    except ValueError:
        return False, 401, "Invalid broker timestamp"
    checked_at = int(time.time()) if now is None else now
    if abs(checked_at - timestamp) > allowed_skew_seconds:
        return False, 401, "Broker signature expired"
    expected = build_broker_signature(
        shared_key, method, path, timestamp_raw, nonce, client_id, body
    )
    if not hmac.compare_digest(supplied, expected):
        return False, 403, "Invalid broker signature"
    if not replay_guard.consume(
        client_id, nonce, checked_at + allowed_skew_seconds, now=float(checked_at)
    ):
        return False, 409, "Broker replay detected"
    return True, 200, ""
