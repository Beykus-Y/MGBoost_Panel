import json
import os
import re
import time
from collections import defaultdict

from ..config import OPAQUE_SUBSCRIPTION_ENABLED, subscription_base_url
from ..http_utils import read_body as _read_body
from ..service_marzban import ServiceMarzbanClient
from ..subscription_credential_issuance import issue_or_reissue_credential

_client = ServiceMarzbanClient()

# Simple in-memory rate limiter: {ip: [timestamps]}
_rate_limit: dict = defaultdict(list)
_RATE_LIMIT_WINDOW = 60
_RATE_LIMIT_MAX = 30
_last_cleanup = time.time()
_ADMIN_TOKEN_TTL = 3600
_admin_token_cache: list = [None, 0.0]

# Cooldown between consecutive mutating device actions for the same
# marzban_username — defense in depth against automated abuse even by a
# legitimately-authorized management session. In-memory is fine: this is a
# single-process http.server, same as the rate limiter above.
_MUTATION_COOLDOWN_SECONDS = 3
_mutation_cooldown: dict = {}

MGMT_COOKIE_NAME = "mgmt_session"
_MGMT_SCOPE_DEVICES = "devices:manage"
_MGMT_CODE_RE = re.compile(r"^[A-Za-z0-9_\-]{16,64}$")
_SUBSCRIPTION_HEADER = "X-MGBoost-Subscription"
_LEGACY_TOKEN_RE = re.compile(r"^[a-zA-Z0-9_\-]{8,64}$")


def _get_admin_token():
    now = time.time()
    if _admin_token_cache[0] and _admin_token_cache[1] > now:
        return _admin_token_cache[0]
    try:
        tok = _client.get_admin_token_from_env()
        _admin_token_cache[0] = tok
        _admin_token_cache[1] = now + _ADMIN_TOKEN_TTL
        return tok
    except Exception:
        print("[LK] admin token fetch failed")
        return None


def _get_real_ip(handler) -> str:
    """Get real IP considering proxy headers."""
    forwarded = handler.headers.get("X-Real-IP")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return handler.client_address[0]


def _check_rate_limit(ip: str) -> bool:
    global _last_cleanup
    now = time.time()

    # Periodic cleanup of old records
    if now - _last_cleanup > 300:  # 5 minutes
        _last_cleanup = now
        stale = [k for k, v in _rate_limit.items()
                 if not v or v[-1] < now - _RATE_LIMIT_WINDOW]
        for k in stale:
            del _rate_limit[k]

    timestamps = _rate_limit[ip]
    timestamps[:] = [t for t in timestamps if now - t < _RATE_LIMIT_WINDOW]
    if len(timestamps) >= _RATE_LIMIT_MAX:
        return False
    timestamps.append(now)
    return True


# Define allowed origins - in production, this should come from config/environment
ALLOWED_ORIGINS = {
    "https://yourdomain.com",
    "https://panel.yourdomain.com",
    # Add other trusted origins as needed
}

def _json_ok(handler, data):
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    # These responses can carry per-user account/device data (or, for the
    # exchange endpoint below, a session-establishing Set-Cookie) — make
    # sure no intermediate cache/proxy retains a copy.
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Referrer-Policy", "no-referrer")
    handler.send_header("X-Content-Type-Options", "nosniff")

    # Fix CORS: don't rely on Host header which can be spoofed
    origin = handler.headers.get("Origin", "")
    if origin in ALLOWED_ORIGINS:
        handler.send_header("Access-Control-Allow-Origin", origin)
    # Optionally allow credentials if needed
    # handler.send_header("Access-Control-Allow-Credentials", "true")

    handler.end_headers()
    handler.wfile.write(body)


def _error(handler, status, message, reason=None):
    payload = {"error": message}
    if reason:
        # Machine-readable reason so the frontend can distinguish
        # "need a management link" from a generic failure without
        # string-matching the human-readable message.
        payload["reason"] = reason
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Referrer-Policy", "no-referrer")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.end_headers()
    handler.wfile.write(body)


def _json_ok_with_cookie(handler, data, cookie_name, cookie_value, max_age):
    """Same as _json_ok but also sets an HttpOnly session cookie."""
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    # This response carries a session-establishing Set-Cookie — an
    # intermediate cache/proxy must never retain (and potentially replay)
    # a copy of it.
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Referrer-Policy", "no-referrer")
    handler.send_header("X-Content-Type-Options", "nosniff")

    origin = handler.headers.get("Origin", "")
    if origin in ALLOWED_ORIGINS:
        handler.send_header("Access-Control-Allow-Origin", origin)

    cookie_attrs = f"{cookie_name}={cookie_value}; Path=/lk; Max-Age={max_age}; HttpOnly; Secure; SameSite=Lax"
    handler.send_header("Set-Cookie", cookie_attrs)

    handler.end_headers()
    handler.wfile.write(body)


def _get_mgmt_cookie(handler) -> str | None:
    cookie_header = handler.headers.get("Cookie", "") or ""
    for part in cookie_header.split(";"):
        part = part.strip()
        if part.startswith(f"{MGMT_COOKIE_NAME}="):
            return part[len(MGMT_COOKIE_NAME) + 1:]
    return None


def _resolve_username_from_session(handler) -> str | None:
    """Resolve marzban_username purely from a valid management-session
    cookie, with no subscription token involved at all. Used as a fallback
    when a request carries no ?token= — this is what lets the bot's
    management deep-link operate off nothing but the one-time `mgmt` code
    exchange, so the backend never needs to reconstitute and transmit the
    user's subscription token just to build a convenience link. Returns
    None if there is no session, it's expired, or it lacks the required
    scope — callers must not assume this call alone is authorization for a
    mutation; mutating handlers still call _require_mgmt_session()
    afterward as the single source of truth for that check."""
    raw_session_id = _get_mgmt_cookie(handler)
    if not raw_session_id:
        return None
    db = handler.server.db
    session = db.get_mgmt_session(raw_session_id)
    if not session:
        return None
    scopes = (session.get("scope") or "").split(",")
    if _MGMT_SCOPE_DEVICES not in scopes:
        return None
    return session.get("marzban_username")


def _require_mgmt_session(handler, expected_username: str):
    """Validate the management-session cookie for a mutating request.

    Every field used for authorization (telegram_id, marzban_username,
    scope) is read back from the server-side session record looked up by
    the (hashed) cookie value — never trusted from anything the frontend
    passed directly in the request body/query.

    Returns the session dict on success, or None after already writing an
    error response (401/403 with a machine-readable `reason`) to `handler`.
    """
    raw_session_id = _get_mgmt_cookie(handler)
    if not raw_session_id:
        _error(handler, 401, "Management session required", reason="management_session_required")
        return None

    db = handler.server.db
    session = db.get_mgmt_session(raw_session_id)
    if not session:
        _error(handler, 401, "Management session expired or invalid", reason="management_session_required")
        return None

    scopes = (session.get("scope") or "").split(",")
    if _MGMT_SCOPE_DEVICES not in scopes:
        _error(handler, 403, "Session lacks required scope", reason="insufficient_scope")
        return None

    if session.get("marzban_username") != expected_username:
        # A management session for one marzban_username must never
        # authorize a mutation under a different username's context, even
        # if the request otherwise references that other username.
        _error(handler, 403, "Session does not match this subscription", reason="session_username_mismatch")
        return None

    return session


def _check_mutation_cooldown(handler, username: str) -> bool:
    now = time.time()
    last = _mutation_cooldown.get(username, 0.0)
    if now - last < _MUTATION_COOLDOWN_SECONDS:
        _error(handler, 429, "Please wait a moment before trying again", reason="cooldown_active")
        return False
    _mutation_cooldown[username] = now
    return True


def handle_lk_page(handler):
    html_path = os.path.join(os.path.dirname(__file__), "../../frontend/lk.html")
    html_path = os.path.normpath(html_path)
    try:
        with open(html_path, "rb") as f:
            body = f.read()
    except FileNotFoundError:
        _error(handler, 404, "lk.html not found")
        return
    handler.send_response(200)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Referrer-Policy", "no-referrer")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("X-Frame-Options", "DENY")
    handler.send_header(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; object-src 'none'; base-uri 'self'; "
        "frame-ancestors 'none'; form-action 'self'",
    )
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _get_token_from_query(handler):
    from urllib.parse import unquote
    import re
    query = handler.path.split("?", 1)[1] if "?" in handler.path else ""
    for part in query.split("&"):
        if part.startswith("token="):
            token = unquote(part[6:])
            # Validation: only allow safe characters
            if _LEGACY_TOKEN_RE.fullmatch(token):
                return token
            return None
    return None


def _get_subscription_token(handler):
    """Prefer the non-URL header while accepting old LK query bookmarks."""
    header_token = (handler.headers.get(_SUBSCRIPTION_HEADER) or "").strip()
    if header_token:
        return header_token if _LEGACY_TOKEN_RE.fullmatch(header_token) else None
    return _get_token_from_query(handler)


def handle_lk_info(handler):
    ip = _get_real_ip(handler)
    if not _check_rate_limit(ip):
        _error(handler, 429, "Too many requests")
        return

    token = _get_subscription_token(handler)
    if token:
        username = _client.get_username_for_token(token)
        if not username:
            _error(handler, 404, "User not found")
            return
    else:
        # No token: fall back to a valid management-session cookie so the
        # bot's management deep-link (mgmt code only, no ?token=) still
        # renders this read-only card.
        username = _resolve_username_from_session(handler)
        if not username:
            _error(handler, 400, "Missing token", reason="management_session_required")
            return

    try:
        admin_token = _get_admin_token()

        if not admin_token:
            _error(handler, 503, "Account service temporarily unavailable")
            return
        try:
            user_data = _client.get_user(username, admin_token)
        except Exception:
            _error(handler, 503, "Account service temporarily unavailable")
            return

        expire = user_data.get("expire")
        status = user_data.get("status", "unknown")
        used_traffic = user_data.get("used_traffic", 0)
        data_limit = user_data.get("data_limit")

        # subscription_url intentionally omitted (None) when the request
        # has no token of its own (pure management-session flow) — we
        # never reconstitute/distribute the subscription token server-side
        # just to populate this convenience field.
        subscription_url = (
            f"https://{handler.headers.get('Host', '')}/sub/{token}" if token else None
        )

        _json_ok(handler, {
            "username": username,
            "status": status,
            "expire": expire,
            "used_traffic": used_traffic,
            "data_limit": data_limit,
            "subscription_url": subscription_url,
        })
    except Exception as e:
        print(f"[LK] info error: {type(e).__name__}")
        _error(handler, 500, "Internal error")


def handle_lk_usage(handler):
    ip = handler.client_address[0]
    if not _check_rate_limit(ip):
        _error(handler, 429, "Too many requests")
        return

    token = _get_subscription_token(handler)
    if token:
        username = _client.get_username_for_token(token)
        if not username:
            _error(handler, 404, "User not found")
            return
    else:
        username = _resolve_username_from_session(handler)
        if not username:
            _error(handler, 400, "Missing token", reason="management_session_required")
            return

    try:
        admin_token = _get_admin_token()

        if not admin_token:
            _error(handler, 503, "Traffic service temporarily unavailable")
            return
        try:
            raw = _client.get_user_usage(username, admin_token)
            usages = raw.get("usages", [])
            total = sum(u.get("used_traffic", 0) for u in usages)
            nodes_usage = []
            for u in usages:
                used = u.get("used_traffic", 0)
                nodes_usage.append({
                    "node_name": u.get("node_name", "Unknown"),
                    "used_traffic": used,
                    "percent": round(used / total * 100) if total > 0 else 0,
                })
        except Exception as e:
            print(f"[LK] usage fetch error: {type(e).__name__}")
            _error(handler, 503, "Traffic service temporarily unavailable")
            return

        _json_ok(handler, {"usages": nodes_usage})
    except Exception as e:
        print(f"[LK] usage error: {type(e).__name__}")
        _error(handler, 500, "Internal error")


def handle_lk_devices(handler):
    ip = handler.client_address[0]
    if not _check_rate_limit(ip):
        _error(handler, 429, "Too many requests")
        return

    token = _get_subscription_token(handler)
    if token:
        username = _client.get_username_for_token(token)
        if not username:
            _error(handler, 404, "User not found")
            return
    else:
        username = _resolve_username_from_session(handler)
        if not username:
            _error(handler, 400, "Missing token", reason="management_session_required")
            return

    db = handler.server.db
    devices = db.get_user_devices(username)
    limit = db.get_device_limit(username)
    active_count = sum(1 for d in devices if d["is_active"])

    for d in devices:
        d.pop("request_key", None)

    _json_ok(handler, {"devices": devices, "limit": limit, "active_count": active_count})


def handle_lk_device_delete(handler, device_id):
    ip = handler.client_address[0]
    if not _check_rate_limit(ip):
        _error(handler, 429, "Too many requests")
        return

    token = _get_subscription_token(handler)
    if token:
        username = _client.get_username_for_token(token)
        if not username:
            _error(handler, 404, "User not found")
            return
    else:
        # No token in this request: fall back to resolving username purely
        # from the management-session cookie (see
        # _resolve_username_from_session), so the bot's management link
        # never needs to carry a subscription token at all. The mandatory
        # _require_mgmt_session() check right below still independently
        # re-validates the session (scope/expiry/username) regardless of
        # how username was derived — that call is the single source of
        # truth for mutation authorization, not this fallback.
        username = _resolve_username_from_session(handler)
        if not username:
            _error(handler, 400, "Missing token", reason="management_session_required")
            return

    # Mutating action: subscription token alone is no longer sufficient —
    # requires a valid management session bound to this same username.
    if not _require_mgmt_session(handler, username):
        return
    if not _check_mutation_cooldown(handler, username):
        return

    try:
        did = int(device_id)
    except (ValueError, TypeError):
        _error(handler, 400, "Invalid device id")
        return

    # deactivate_device itself filters WHERE id=? AND username=?, so a
    # device belonging to a different marzban_username can never be
    # touched even if a device id from another account were guessed.
    ok = handler.server.db.deactivate_device(did, username)
    if not ok:
        _error(handler, 404, "Device not found")
        return

    _json_ok(handler, {"ok": True})


def _account_id_for_legacy_username(handler, username: str) -> int | None:
    row = handler.server.db._conn.execute(
        "SELECT account_id FROM mgboost_legacy_account_aliases WHERE legacy_username=?",
        (username,),
    ).fetchone()
    return row["account_id"] if row else None


def handle_lk_opaque_subscription_status(handler):
    ip = handler.client_address[0]
    if not _check_rate_limit(ip):
        _error(handler, 429, "Too many requests")
        return
    username = _resolve_username_from_session(handler)
    if not username:
        _error(handler, 400, "Missing token", reason="management_session_required")
        return
    if not _require_mgmt_session(handler, username):
        return
    account_id = _account_id_for_legacy_username(handler, username)
    if account_id is None:
        _json_ok(handler, {"available": False})
        return
    row = handler.server.db._conn.execute(
        "SELECT generation,status FROM mgboost_subscription_credentials "
        "WHERE account_id=? ORDER BY generation DESC LIMIT 1", (account_id,),
    ).fetchone()
    _json_ok(handler, {
        "available": True,
        "credential": {"generation": row["generation"], "status": row["status"]} if row else None,
    })


def handle_lk_opaque_subscription_issue(handler):
    """PH4-04: reviewed-account-only opaque URL issuance/rotation, gated by
    the SAME `_require_mgmt_session` boundary every other destructive LK
    device action already requires -- never the bare legacy subscription
    token alone (possession of a legacy link is not ownership proof)."""
    ip = handler.client_address[0]
    if not _check_rate_limit(ip):
        _error(handler, 429, "Too many requests")
        return
    if not OPAQUE_SUBSCRIPTION_ENABLED:
        _error(handler, 404, "Not found")
        return
    username = _resolve_username_from_session(handler)
    if not username:
        _error(handler, 400, "Missing token", reason="management_session_required")
        return
    if not _require_mgmt_session(handler, username):
        return
    if not _check_mutation_cooldown(handler, username):
        return
    account_id = _account_id_for_legacy_username(handler, username)
    if account_id is None:
        _error(handler, 404, "Account not found")
        return

    db = handler.server.db

    # PUBLIC_HOST gates the link URL itself: never issue (and thereby
    # rotate) a credential whose canonical_url we cannot build correctly.
    _sub_base = subscription_base_url()
    if _sub_base is None:
        _error(handler, 503, "Subscription domain is not configured")
        return

    # PH4-04 corrective fix: issuing while a credential is already ACTIVE is
    # a destructive rotation (the old URL stops working immediately) and
    # must never happen from a single accidental click -- the same rule the
    # Telegram surface enforces via its two-step confirm/cancel flow. A
    # fresh account with no ACTIVE credential yet is normal initial issuance
    # and stays a single call.
    existing = db._conn.execute(
        "SELECT id FROM mgboost_subscription_credentials WHERE account_id=? AND status='ACTIVE'",
        (account_id,),
    ).fetchone()
    if existing is not None:
        try:
            body = _read_body(handler)
            data = json.loads(body) if body else {}
        except (ValueError, json.JSONDecodeError):
            data = {}
        if not isinstance(data, dict) or data.get("confirm") is not True:
            _error(
                handler, 409,
                "An ACTIVE credential already exists. Resubmit with confirm: true to rotate it.",
                reason="requires_confirmation",
            )
            return

    delivered = {}

    def _deliver(raw_token: str) -> None:
        delivered["raw_token"] = raw_token

    import secrets
    idempotency_key = f"lk-issue-v1:{account_id}:{secrets.token_urlsafe(24)}"
    try:
        credential = issue_or_reissue_credential(
            db, account_id=account_id, actor_ref=f"lk-session:{username}", reason="LK issuance",
            idempotency_key=idempotency_key, deliver_fn=_deliver, now=int(time.time()),
        )
    except Exception:
        _error(handler, 500, "Issuance failed")
        return

    _json_ok(handler, {
        "credential": {"generation": credential["generation"], "status": credential["status"]},
        "raw_token": delivered["raw_token"],
        "canonical_url": f"{_sub_base}/{delivered['raw_token']}",
    })


def handle_lk_device_rename(handler, device_id):
    ip = handler.client_address[0]
    if not _check_rate_limit(ip):
        _error(handler, 429, "Too many requests")
        return

    token = _get_subscription_token(handler)
    if token:
        username = _client.get_username_for_token(token)
        if not username:
            _error(handler, 404, "User not found")
            return
    else:
        # No token: fall back to resolving username purely from the
        # management-session cookie — see handle_lk_device_delete for the
        # rationale. _require_mgmt_session() below is still the single
        # source of truth for mutation authorization.
        username = _resolve_username_from_session(handler)
        if not username:
            _error(handler, 400, "Missing token", reason="management_session_required")
            return

    # Mutating action: subscription token alone is no longer sufficient —
    # requires a valid management session bound to this same username.
    if not _require_mgmt_session(handler, username):
        return
    if not _check_mutation_cooldown(handler, username):
        return

    try:
        did = int(device_id)
    except (ValueError, TypeError):
        _error(handler, 400, "Invalid device id")
        return

    try:
        data = json.loads(_read_body(handler))
        name = str(data.get("name", "")).strip()
        if not name or len(name) > 50:
            raise ValueError("name must be 1-50 chars")
    except (ValueError, json.JSONDecodeError) as e:
        _error(handler, 400, str(e))
        return

    ok = handler.server.db.rename_device(did, username, name)
    if not ok:
        _error(handler, 404, "Device not found")
        return

    _json_ok(handler, {"ok": True})


def handle_lk_mgmt_exchange(handler):
    """Exchange a one-time device-management code (issued by the bot) for
    a management session, delivered as an HttpOnly cookie. Single-use: the
    code is atomically marked used at exchange time (see
    Database.exchange_mgmt_code), so a second attempt with the same code
    always fails."""
    ip = _get_real_ip(handler)
    if not _check_rate_limit(ip):
        _error(handler, 429, "Too many requests")
        return

    try:
        data = json.loads(_read_body(handler))
    except (ValueError, json.JSONDecodeError):
        _error(handler, 400, "Invalid body")
        return

    code = str(data.get("code", "")).strip()
    if not code or not _MGMT_CODE_RE.match(code):
        _error(handler, 400, "Invalid or missing code", reason="invalid_code")
        return

    db = handler.server.db
    result = db.exchange_mgmt_code(code)
    if not result:
        _error(handler, 401, "Code invalid, expired, or already used", reason="invalid_or_expired_code")
        return

    ttl_seconds = max(1, result["expires_at"] - int(time.time()))
    _json_ok_with_cookie(
        handler,
        {"ok": True, "username": result["marzban_username"]},
        MGMT_COOKIE_NAME,
        result["session_id"],
        max_age=ttl_seconds,
    )


_PROMO_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{8,128}$")


def handle_lk_promo_redeem(handler):
    """PH5-13 self-service promo redemption from the LK. Gated by the SAME
    `_require_mgmt_session` boundary as every destructive LK action -- never
    the bare legacy subscription token alone (possession of a legacy link is
    not ownership proof). The acting principal is the account's canonical
    OWNER telegram identity. The client MUST supply a stable `request_id`:
    it is the LK-side half of the deterministic per-event idempotency key
    (the server never mints one), so a retried request replays the same
    redemption instead of applying the promo twice."""
    ip = _get_real_ip(handler)
    if not _check_rate_limit(ip):
        _error(handler, 429, "Too many requests")
        return
    username = _resolve_username_from_session(handler)
    if not username:
        _error(handler, 400, "Missing token", reason="management_session_required")
        return
    if not _require_mgmt_session(handler, username):
        return
    account_id = _account_id_for_legacy_username(handler, username)
    if account_id is None:
        _error(handler, 404, "Account not found")
        return
    db = handler.server.db
    owner = db._conn.execute(
        "SELECT telegram_id FROM mgboost_telegram_identities "
        "WHERE account_id=? AND role='OWNER' AND revoked_at IS NULL",
        (account_id,),
    ).fetchone()
    if owner is None:
        _error(handler, 404, "Account not found")
        return
    try:
        body = _read_body(handler)
        data = json.loads(body) if body else {}
    except (ValueError, json.JSONDecodeError):
        _error(handler, 400, "Invalid request body")
        return
    if not isinstance(data, dict):
        _error(handler, 400, "Invalid request body")
        return
    code = str(data.get("code") or "").strip()
    request_id = str(data.get("request_id") or "").strip()
    if not 3 <= len(code) <= 64:
        _error(handler, 400, "Invalid promo code")
        return
    if not _PROMO_REQUEST_ID_RE.fullmatch(request_id):
        _error(handler, 400, "request_id required", reason="request_id_required")
        return
    telegram_id = int(owner["telegram_id"])
    idempotency_key = f"promo-redeem-v1:lk:{telegram_id}:{request_id}"
    from ..promo import PromoConflict, PromoError, PromoIneligible, PromoNotFound
    try:
        result = db.promo.redeem_for_telegram_user(
            code=code, telegram_id=telegram_id, idempotency_key=idempotency_key,
        )
    except PromoNotFound:
        _error(handler, 404, "Promo code not found or inactive")
        return
    except PromoConflict:
        _error(handler, 409, "Promo code conflict", reason="already_redeemed")
        return
    except PromoIneligible:
        _error(handler, 409, "Promo code not applicable", reason="ineligible")
        return
    except PromoError:
        _error(handler, 400, "Promo redemption failed")
        return
    _json_ok(handler, {
        "status": result["status"],
        "already_applied": bool(result.get("already_applied")),
    })
