import base64
import json
import os
import re
from urllib.error import URLError
from urllib.parse import quote

from ..device_headers import extract_device_metadata
from ..marzban import MarzbanClient
from ..subscription import process_subscription

_client = MarzbanClient()

_BLOCK_TITLES = {
    "device_locked":       "⛔ Устройство занято другой подпиской",
    "device_limit_reached": "⛔ Лимит устройств достигнут",
}

_FAKE_URI = "vless://00000000-0000-0000-0000-000000000000@0.0.0.0:1?type=tcp"

_BROWSER_UA_RE = re.compile(r"Mozilla|Chrome|Safari|Firefox|Edge|Opera", re.IGNORECASE)

_BROWSER_PAGE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "frontend", "browser_page.html"
)


def _browser_page(sub_url: str) -> bytes:
    with open(_BROWSER_PAGE_PATH, "r", encoding="utf-8") as f:
        template = f.read()
    safe_text = sub_url.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    html = template.replace("__SUB_URL__", safe_text)
    html = html.replace("__SUB_URL_JSON__", json.dumps(sub_url))
    return html.encode("utf-8")


def _fake_sub(reason: str, contact: str | None) -> bytes:
    title = _BLOCK_TITLES.get(reason, "⛔ Доступ ограничен")
    lines = [f"{_FAKE_URI}#{quote(title)}"]
    if contact:
        lines.append(f"{_FAKE_URI}#{quote('📩 ' + contact)}")
    payload = "\n".join(lines)
    return base64.b64encode(payload.encode("utf-8"))


def handle_sub(handler, token):
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
        handler.send_header("Content-Length", str(len(page)))
        handler.end_headers()
        handler.wfile.write(page)
        return

    extra_headers = {k: v for k, v in handler.headers.items()}

    try:
        body, marzban_headers = _client.get_sub(token, extra_headers)
    except URLError as e:
        # urllib exception strings can include the raw path bearer.
        print(f"[Sub] Error fetching from Marzban: {type(e).__name__}")
        handler.send_response(502)
        handler.end_headers()
        return

    db = handler.server.db
    username = _client.get_username_for_token(token)
    device_metadata = extract_device_metadata(handler.headers)

    request_key = device_metadata.get("request_key")
    if request_key and request_key.startswith("hwid:") and username:
        blocked, reason = db.check_device_access(username, token, device_metadata)
        if blocked:
            contact = db.get_setting("block_contact") or None
            fake = _fake_sub(reason, contact)
            print(f"[Sub] Blocked {username} reason={reason} key={request_key[:16]}...")
            handler.send_response(200)
            handler.send_header("Content-Type", "text/plain")
            handler.send_header("Cache-Control", "no-store")
            handler.send_header("Referrer-Policy", "no-referrer")
            handler.send_header("X-Content-Type-Options", "nosniff")
            handler.send_header("Content-Length", str(len(fake)))
            handler.end_headers()
            handler.wfile.write(fake)
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
    handler.send_header("Content-Type", "text/plain")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Referrer-Policy", "no-referrer")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("Content-Length", str(len(new_body)))
    for key, val in out_headers.items():
        handler.send_header(key, val)
    handler.end_headers()
    handler.wfile.write(new_body)
