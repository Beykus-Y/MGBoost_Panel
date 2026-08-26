import base64
import logging
import os
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote

from ..device_headers import extract_device_metadata
from ..config import DEVICE_SLOT_HMAC_KEY, LEGACY_BRIDGE_ENABLED, SUB_BROWSER_CSP_ENFORCE
from ..http_utils import client_ip
from ..legacy_bridge_resolver import is_fall_through_outcome
from ..marzban import MarzbanClient
from ..migration_lifecycle import process_migration_bridge_request
from ..opaque_resolver import OUTCOME_OK
from ..service_marzban import ServiceMarzbanClient
from ..shadow_resolver import schedule_shadow_resolution
from ..subscription import process_subscription
from ..subscription_rate_limit import SUBSCRIPTION_FETCH_LIMITER

_client = MarzbanClient()
_bridge_client = ServiceMarzbanClient()
logger = logging.getLogger(__name__)

_BLOCK_TITLES = {
    "device_locked":       "⛔ Устройство занято другой подпиской",
    "device_limit_reached": "⛔ Лимит устройств достигнут",
}

_FAKE_URI = "vless://00000000-0000-0000-0000-000000000000@0.0.0.0:1?type=tcp"

_BROWSER_UA_RE = re.compile(r"Mozilla|Chrome|Safari|Firefox|Edge|Opera", re.IGNORECASE)
_MAX_LEGACY_TOKEN_LENGTH = 4096
_INVALID_RESPONSE_FLOOR_SECONDS = 0.05
_INVALID_SUB_BODY = b"Subscription not found\n"
_BROWSER_CSP_BASELINE = "object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
_BROWSER_CSP_STRICT = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'none'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
    "form-action 'none'"
)

_BROWSER_PAGE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "frontend", "browser_page.html"
)


def _browser_page(sub_url: str) -> bytes:
    with open(_BROWSER_PAGE_PATH, "r", encoding="utf-8") as f:
        template = f.read()
    safe_text = sub_url.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    html = template.replace("__SUB_URL__", safe_text)
    return html.encode("utf-8")


def _fake_sub(reason: str, contact: str | None) -> bytes:
    title = _BLOCK_TITLES.get(reason, "⛔ Доступ ограничен")
    lines = [f"{_FAKE_URI}#{quote(title)}"]
    if contact:
        lines.append(f"{_FAKE_URI}#{quote('📩 ' + contact)}")
    payload = "\n".join(lines)
    return base64.b64encode(payload.encode("utf-8"))


def _plain_response(handler, status: int, body: bytes):
    handler.send_response(status)
    handler.send_header("Content-Type", "text/plain; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Referrer-Policy", "no-referrer")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("X-Frame-Options", "DENY")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _invalid_subscription_response(handler, started_at: float):
    remaining = _INVALID_RESPONSE_FLOOR_SECONDS - (time.monotonic() - started_at)
    if remaining > 0:
        time.sleep(remaining)
    _plain_response(handler, 404, _INVALID_SUB_BODY)


def _rate_limited_response(handler, retry_after: int):
    """PH2-06: a distinct, expected-visible signal -- never a token-validity
    oracle (issued identically regardless of whether any token in the
    request is well-formed, known, active or revoked)."""
    handler.send_response(429)
    handler.send_header("Content-Type", "text/plain; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Referrer-Policy", "no-referrer")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("Retry-After", str(retry_after))
    body = b"Too many requests\n"
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def check_subscription_rate_limit(handler) -> bool:
    """Returns True (and has already written a 429 response) if this
    request must be rejected before any further work -- token parsing,
    credential resolution and upstream/broker calls must never run for a
    rate-limited request."""
    retry_after = SUBSCRIPTION_FETCH_LIMITER.check(client_ip(handler))
    if retry_after:
        _rate_limited_response(handler, retry_after)
        return True
    return False


def _observe_compatibility_fail_open(db, token, device_metadata):
    """PH3-07 is strictly observational and must never affect VPN delivery."""
    observer = getattr(db, "observe_hwid_compatibility", None)
    if observer is None:
        return
    try:
        observer(token, device_metadata)
    except Exception as exc:
        # Never include token, username, HWID, UA or exception text: driver
        # errors can contain SQL/data. Logging itself is non-critical too.
        try:
            logger.warning(
                "compatibility telemetry write skipped error_type=%s",
                type(exc).__name__,
            )
        except Exception:
            pass


def _observe_grace_activity_fail_open(db, account_id, channel):
    """PH4-05 grace-activity counters are strictly observational (like
    PH3-07's own `_observe_compatibility_fail_open`) and must never affect
    VPN delivery -- no legacy/opaque request is denied or altered because
    this write failed, and nothing here changes any grace clock."""
    observer = getattr(db, "observe_legacy_grace_activity", None)
    if observer is None or account_id is None:
        return
    try:
        observer(account_id, channel)
    except Exception as exc:
        try:
            logger.warning(
                "grace activity telemetry write skipped error_type=%s", type(exc).__name__,
            )
        except Exception:
            pass


def _bridge_ensure_fn(payload):
    return _bridge_client.ensure_child_user(payload)


def _bridge_subscription_fn(payload):
    return _bridge_client.get_child_subscription(payload)


def _try_legacy_bridge(handler, db, username, device_metadata) -> bool:
    """Returns True if this request was fully handled by the bridge (either
    a real child config, or a fail-closed response after a durable slot
    claim already happened) -- the caller must return immediately. Returns
    False only when nothing durable happened (no mapping/binding, or any
    deny decision -- every one of those happens strictly before
    DeviceSlotStore.claim() could commit a row), meaning the caller must
    proceed with the exact unmodified legacy response.

    PH4-03: uses `process_migration_bridge_request` (PH4-02) instead of the
    bare `resolve_legacy_bridge` (PH4-01) -- adds the durable per-device
    MIGRATING/MIGRATED lifecycle record on top of the exact same resolution
    engine, no second resolver. Behavior for any not-yet-bridged account is
    byte-identical to before (still gated by `LEGACY_BRIDGE_ENABLED` and an
    explicit per-account binding)."""
    result = process_migration_bridge_request(
        db, username, device_metadata, hmac_key=DEVICE_SLOT_HMAC_KEY,
        ensure_fn=_bridge_ensure_fn, subscription_fn=_bridge_subscription_fn,
        worker_id="legacy-bridge-inline-worker",
    )
    if is_fall_through_outcome(result.outcome):
        return False
    if result.outcome == OUTCOME_OK:
        child_body = base64.b64decode(result.body_b64)
        new_body, out_headers = process_subscription(
            child_body, result.headers, result.child_username, result.child_username, db,
        )
        handler.send_response(200)
        handler.send_header("Content-Type", "text/plain; charset=utf-8")
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Referrer-Policy", "no-referrer")
        handler.send_header("X-Content-Type-Options", "nosniff")
        handler.send_header("X-Frame-Options", "DENY")
        handler.send_header("Content-Length", str(len(new_body)))
        for key, val in out_headers.items():
            safe_value = str(val)
            if "\r" in safe_value or "\n" in safe_value or len(safe_value) > 8192:
                continue
            handler.send_header(key, safe_value)
        handler.end_headers()
        handler.wfile.write(new_body)
        return True
    # A durable slot claim already happened for this device (every deny
    # decision above is side-effect-free and already handled by the
    # fall-through branch) -- a downstream failure here must never silently
    # hand this device the shared legacy credential instead.
    _plain_response(handler, 502, b"Subscription service unavailable\n")
    return True


def is_browser_request(handler) -> bool:
    """The single existing browser-vs-subscription-client detection
    mechanism -- reused unchanged by the opaque route too. Never treated as
    device/HWID evidence; a browser hit is presentational only."""
    ua = handler.headers.get("User-Agent", "")
    return bool(_BROWSER_UA_RE.search(ua))


def send_browser_landing(handler, sub_url: str) -> None:
    """Renders the existing legacy browser landing page for the given
    subscription URL. Shared verbatim by `/sub/{token}` and the opaque
    route so there is exactly one browser UX, not two parallel ones."""
    page = _browser_page(sub_url)
    handler.send_response(200)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Referrer-Policy", "no-referrer")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("X-Frame-Options", "DENY")
    if SUB_BROWSER_CSP_ENFORCE:
        handler.send_header("Content-Security-Policy", _BROWSER_CSP_STRICT)
    else:
        handler.send_header("Content-Security-Policy", _BROWSER_CSP_BASELINE)
        handler.send_header("Content-Security-Policy-Report-Only", _BROWSER_CSP_STRICT)
    handler.send_header(
        "Permissions-Policy",
        "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
    )
    handler.send_header("Content-Length", str(len(page)))
    handler.end_headers()
    handler.wfile.write(page)


def handle_sub(handler, token):
    started_at = time.monotonic()
    if check_subscription_rate_limit(handler):
        return
    if not token or len(token) > _MAX_LEGACY_TOKEN_LENGTH:
        _invalid_subscription_response(handler, started_at)
        return

    if is_browser_request(handler):
        proto = handler.headers.get("X-Forwarded-Proto", "https")
        host = handler.headers.get("Host", "")
        sub_url = f"{proto}://{host}/sub/{token}"
        send_browser_landing(handler, sub_url)
        return

    extra_headers = {k: v for k, v in handler.headers.items()}

    try:
        body, marzban_headers = _client.get_sub(token, extra_headers)
    except HTTPError as exc:
        if exc.code in (401, 403, 404):
            _invalid_subscription_response(handler, started_at)
            return
        print(f"[Sub] Upstream HTTP failure: {exc.code}")
        _plain_response(handler, 502, b"Subscription service unavailable\n")
        return
    except URLError as e:
        # urllib exception strings can include the raw path bearer.
        print(f"[Sub] Error fetching from Marzban: {type(e).__name__}")
        _plain_response(handler, 502, b"Subscription service unavailable\n")
        return

    db = handler.server.db
    username = _client.get_username_for_token(token)
    device_metadata = extract_device_metadata(handler.headers)
    _observe_compatibility_fail_open(db, token, device_metadata)
    if username:
        try:
            account_id = db.legacy_bridge.resolve_account_for_legacy_username(username)
        except Exception:
            account_id = None
        if account_id is not None:
            _observe_grace_activity_fail_open(db, account_id, "LEGACY")

    request_key = device_metadata.get("request_key")
    if request_key and request_key.startswith("hwid:") and username:
        blocked, reason = db.check_device_access(username, token, device_metadata)
        if blocked:
            contact = db.get_setting("block_contact") or None
            fake = _fake_sub(reason, contact)
            print(f"[Sub] Blocked {username} reason={reason} key={request_key[:16]}...")
            _plain_response(handler, 200, fake)
            return

    db.log_request(
        token,
        username,
        device_metadata.get("user_agent"),
        handler.client_address[0],
        device_metadata,
    )

    if LEGACY_BRIDGE_ENABLED and username:
        if _try_legacy_bridge(handler, db, username, device_metadata):
            return

    new_body, out_headers = process_subscription(body, marzban_headers, token, username, db)

    # PH3-03 SHADOW mode only: this legacy response is already fully built
    # and is what gets sent below, unconditionally. The shadow resolver runs
    # in a background thread purely for comparison/metrics and can never
    # change, delay or replace this response.
    try:
        schedule_shadow_resolution(token, username, device_metadata, body)
    except Exception as exc:
        try:
            logger.warning("shadow resolver schedule skipped error_type=%s", type(exc).__name__)
        except Exception:
            pass

    handler.send_response(200)
    handler.send_header("Content-Type", "text/plain; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Referrer-Policy", "no-referrer")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("X-Frame-Options", "DENY")
    handler.send_header("Content-Length", str(len(new_body)))
    for key, val in out_headers.items():
        safe_value = str(val)
        if "\r" in safe_value or "\n" in safe_value or len(safe_value) > 8192:
            continue
        handler.send_header(key, safe_value)
    handler.end_headers()
    handler.wfile.write(new_body)
