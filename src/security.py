import hashlib
import hmac
import secrets
import threading
import time
from dataclasses import dataclass
from http.cookies import SimpleCookie

from .config import (
    ADMIN_SESSION_COOKIE_SECURE,
    ADMIN_SESSION_TTL_SECONDS,
    INTERNAL_API_ALLOWED_SKEW_SECONDS,
    INTERNAL_API_KEY,
)
from .http_utils import error_response, read_body

_SEEN_NONCES: dict[str, float] = {}
_MAX_TRACKED_NONCES = 2048
ADMIN_SESSION_COOKIE = "mgboost_admin"
ADMIN_CSRF_HEADER = "X-CSRF-Token"
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_MAX_ADMIN_SESSIONS = 256


@dataclass(frozen=True)
class AdminSession:
    username: str
    marzban_token: str
    csrf_token: str
    created_at: float
    expires_at: float


class AdminSessionStore:
    """Process-local opaque admin sessions.

    The browser receives only a high-entropy session id in an HttpOnly
    cookie.  Marzban's JWT never crosses the server/browser boundary.  The
    process-local scope is intentional for the current single-worker server;
    PH8-02 tracks moving shared state before multi-worker rollout.
    """

    def __init__(self):
        self._sessions: dict[str, AdminSession] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _key(raw_session_id: str) -> str:
        return hashlib.sha256(raw_session_id.encode("utf-8")).hexdigest()

    def _prune_locked(self, now: float):
        expired = [key for key, session in self._sessions.items() if session.expires_at <= now]
        for key in expired:
            self._sessions.pop(key, None)
        while len(self._sessions) >= _MAX_ADMIN_SESSIONS:
            oldest = min(self._sessions.items(), key=lambda item: item[1].expires_at)[0]
            self._sessions.pop(oldest, None)

    def create(self, username: str, marzban_token: str, *, now: float | None = None):
        issued_at = time.time() if now is None else now
        raw_session_id = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        session = AdminSession(
            username=username,
            marzban_token=marzban_token,
            csrf_token=csrf_token,
            created_at=issued_at,
            expires_at=issued_at + ADMIN_SESSION_TTL_SECONDS,
        )
        with self._lock:
            self._prune_locked(issued_at)
            self._sessions[self._key(raw_session_id)] = session
        return raw_session_id, session

    def get(self, raw_session_id: str, *, now: float | None = None) -> AdminSession | None:
        if not raw_session_id:
            return None
        checked_at = time.time() if now is None else now
        key = self._key(raw_session_id)
        with self._lock:
            self._prune_locked(checked_at)
            return self._sessions.get(key)

    def revoke(self, raw_session_id: str):
        if not raw_session_id:
            return
        with self._lock:
            self._sessions.pop(self._key(raw_session_id), None)

    def rotate(self, raw_session_id: str, *, now: float | None = None):
        rotated_at = time.time() if now is None else now
        old_key = self._key(raw_session_id)
        with self._lock:
            self._prune_locked(rotated_at)
            old = self._sessions.pop(old_key, None)
            if old is None:
                return None
            new_raw = secrets.token_urlsafe(32)
            new_session = AdminSession(
                username=old.username,
                marzban_token=old.marzban_token,
                csrf_token=secrets.token_urlsafe(32),
                created_at=rotated_at,
                expires_at=rotated_at + ADMIN_SESSION_TTL_SECONDS,
            )
            self._sessions[self._key(new_raw)] = new_session
            return new_raw, new_session

    def clear(self):
        """Test/support hook; production logout uses per-session revoke."""
        with self._lock:
            self._sessions.clear()


_ADMIN_SESSIONS = AdminSessionStore()


def _prune_expired(cache: dict[str, float], now: float):
    expired = [key for key, expires_at in cache.items() if expires_at <= now]
    for key in expired:
        cache.pop(key, None)


def _cookie_value(handler, name: str) -> str:
    raw = handler.headers.get("Cookie", "") or ""
    try:
        cookie = SimpleCookie()
        cookie.load(raw)
        morsel = cookie.get(name)
        return morsel.value if morsel else ""
    except Exception:
        return ""


def get_admin_session_id(handler) -> str:
    return _cookie_value(handler, ADMIN_SESSION_COOKIE)


def get_admin_session(handler) -> AdminSession | None:
    raw_session_id = get_admin_session_id(handler)
    return _ADMIN_SESSIONS.get(raw_session_id)


def create_admin_session(username: str, marzban_token: str):
    return _ADMIN_SESSIONS.create(username, marzban_token)


def revoke_admin_session(raw_session_id: str):
    _ADMIN_SESSIONS.revoke(raw_session_id)


def rotate_admin_session(raw_session_id: str):
    return _ADMIN_SESSIONS.rotate(raw_session_id)


def admin_session_cookie(raw_session_id: str, *, clear: bool = False) -> str:
    value = "" if clear else raw_session_id
    max_age = 0 if clear else ADMIN_SESSION_TTL_SECONDS
    secure = "; Secure" if ADMIN_SESSION_COOKIE_SECURE else ""
    return (
        f"{ADMIN_SESSION_COOKIE}={value}; Path=/; Max-Age={max_age}; "
        f"HttpOnly{secure}; SameSite=Strict"
    )


def require_admin_auth(handler) -> bool:
    raw_session_id = get_admin_session_id(handler)
    session = _ADMIN_SESSIONS.get(raw_session_id)
    if session is None:
        error_response(handler, 401, "Unauthorized")
        return False

    method = getattr(handler, "command", "GET").upper()
    if method not in _SAFE_METHODS:
        supplied = (handler.headers.get(ADMIN_CSRF_HEADER) or "").strip()
        if not supplied or not secrets.compare_digest(supplied, session.csrf_token):
            error_response(handler, 403, "CSRF validation failed")
            return False

    handler._admin_session = session
    handler._admin_session_id = raw_session_id
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
