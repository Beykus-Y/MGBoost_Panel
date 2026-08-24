import base64
import logging
import os
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote

from ..device_headers import extract_device_metadata
from ..config import SUB_BROWSER_CSP_ENFORCE
from ..marzban import MarzbanClient
from ..subscription import process_subscription

_client = MarzbanClient()
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


def handle_sub(handler, token):
    started_at = time.monotonic()
    if not token or len(token) > _MAX_LEGACY_TOKEN_LENGTH:
        _invalid_subscription_response(handler, started_at)
        return

    ua = handler.headers.get("User-Agent", "")
    if _BROWSER_UA_RE.search(ua):
        proto = handler.headers.get("X-Forwarded-Proto", "https")
        host = handler.headers.get("Host", "")
        sub_url = f"{proto}://{host}/sub/{token}"
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

    new_body, out_headers = process_subscription(body, marzban_headers, token, username, db)

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
