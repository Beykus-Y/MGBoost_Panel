import hashlib
import hmac
import secrets
import time

from .config import INTERNAL_API_ALLOWED_SKEW_SECONDS, INTERNAL_API_KEY
from .http_utils import error_response, read_body
from .marzban import MarzbanClient

_ADMIN_TOKEN_CACHE: dict[str, float] = {}
_ADMIN_TOKEN_TTL_SECONDS = 60
_SEEN_NONCES: dict[str, float] = {}
_MAX_TRACKED_NONCES = 2048


def _prune_expired(cache: dict[str, float], now: float):
    expired = [key for key, expires_at in cache.items() if expires_at <= now]
    for key in expired:
        cache.pop(key, None)


def validate_admin_token(token: str) -> bool:
    now = time.time()
    cached_until = _ADMIN_TOKEN_CACHE.get(token)
    if cached_until and cached_until > now:
        return True

    try:
        MarzbanClient().get_nodes(token)
    except Exception:
        return False

    _prune_expired(_ADMIN_TOKEN_CACHE, now)
    _ADMIN_TOKEN_CACHE[token] = now + _ADMIN_TOKEN_TTL_SECONDS
    return True


def require_admin_auth(handler) -> bool:
    auth = handler.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        error_response(handler, 401, "Unauthorized")
        return False

    token = auth[7:].strip()
    if not token or not validate_admin_token(token):
        error_response(handler, 403, "Forbidden")
        return False

    return True


def build_internal_signature(method: str, path: str, timestamp: str, nonce: str, body: bytes) -> str:
    body_hash = hashlib.sha256(body).hexdigest()
    payload = "\n".join([method.upper(), path, timestamp, nonce, body_hash])
    return hmac.new(INTERNAL_API_KEY.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def require_internal_auth(handler) -> bool:
    if not INTERNAL_API_KEY:
        error_response(handler, 503, "Internal API key is not configured")
        return False

    timestamp_raw = (handler.headers.get("X-Filin-Timestamp") or "").strip()
    nonce = (handler.headers.get("X-Filin-Nonce") or "").strip()
    signature = (handler.headers.get("X-Filin-Signature") or "").strip()

    if not timestamp_raw or not nonce or not signature:
        error_response(handler, 401, "Missing internal authentication headers")
        return False

    try:
        timestamp = int(timestamp_raw)
    except ValueError:
        error_response(handler, 401, "Invalid timestamp")
        return False

    now = int(time.time())
    if abs(now - timestamp) > INTERNAL_API_ALLOWED_SKEW_SECONDS:
        error_response(handler, 401, "Signature expired")
        return False

    _prune_expired(_SEEN_NONCES, float(now))
    if nonce in _SEEN_NONCES:
        error_response(handler, 409, "Replay detected")
        return False

    body = read_body(handler)
    expected = build_internal_signature(handler.command, handler.path, timestamp_raw, nonce, body)
    if not secrets.compare_digest(signature, expected):
        error_response(handler, 403, "Invalid internal signature")
        return False

    _SEEN_NONCES[nonce] = float(now + INTERNAL_API_ALLOWED_SKEW_SECONDS)
    if len(_SEEN_NONCES) > _MAX_TRACKED_NONCES:
        oldest = min(_SEEN_NONCES.items(), key=lambda item: item[1])[0]
        _SEEN_NONCES.pop(oldest, None)

    return True
