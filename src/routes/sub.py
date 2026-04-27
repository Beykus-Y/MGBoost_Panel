import base64
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


def _fake_sub(reason: str, contact: str | None) -> bytes:
    title = _BLOCK_TITLES.get(reason, "⛔ Доступ ограничен")
    lines = [f"{_FAKE_URI}#{quote(title)}"]
    if contact:
        lines.append(f"{_FAKE_URI}#{quote('📩 ' + contact)}")
    payload = "\n".join(lines)
    return base64.b64encode(payload.encode("utf-8"))


def handle_sub(handler, token):
    extra_headers = {k: v for k, v in handler.headers.items()}

    try:
        body, marzban_headers = _client.get_sub(token, extra_headers)
    except URLError as e:
        print(f"[Sub] Error fetching from Marzban: {e}")
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
    handler.send_header("Content-Length", str(len(new_body)))
    for key, val in out_headers.items():
        handler.send_header(key, val)
    handler.end_headers()
    handler.wfile.write(new_body)
