from urllib.error import URLError

from ..device_headers import extract_device_metadata
from ..marzban import MarzbanClient
from ..subscription import process_subscription

_client = MarzbanClient()


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
