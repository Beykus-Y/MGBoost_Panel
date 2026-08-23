import json

from ..http_utils import read_body
from ..marzban import MarzbanClient
from ..security import (
    admin_session_cookie,
    create_admin_session,
    get_admin_session,
    get_admin_session_id,
    revoke_admin_session,
    rotate_admin_session,
)


_client = MarzbanClient()


def _session_response(handler, status: int, data: dict, *, cookie: str | None = None):
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    if cookie is not None:
        handler.send_header("Set-Cookie", cookie)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _public_session(session):
    return {
        "authenticated": True,
        "username": session.username,
        "csrf_token": session.csrf_token,
        "expires_at": int(session.expires_at),
    }


def handle_admin_session_login(handler):
    content_type = (handler.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
    login_guard = (handler.headers.get("X-MGBoost-Admin-Login") or "").strip()
    if content_type != "application/json" or login_guard != "1":
        _session_response(handler, 403, {"error": "Login request rejected"})
        return

    try:
        payload = json.loads(read_body(handler) or b"{}")
    except json.JSONDecodeError:
        _session_response(handler, 400, {"error": "Invalid JSON"})
        return

    if not isinstance(payload, dict):
        _session_response(handler, 400, {"error": "Invalid request"})
        return

    username = payload.get("username")
    password = payload.get("password")
    if not isinstance(username, str) or not isinstance(password, str):
        _session_response(handler, 400, {"error": "Username and password are required"})
        return
    username = username.strip()
    if not username or len(username) > 128 or not password or len(password) > 4096:
        _session_response(handler, 400, {"error": "Invalid credentials"})
        return

    try:
        marzban_token = _client.get_token(username, password)
    except Exception:
        marzban_token = None
    finally:
        password = None

    if not marzban_token:
        _session_response(handler, 401, {"error": "Invalid credentials"})
        return

    # Never reuse a caller-supplied id: a successful login always creates a
    # fresh server-side session and revokes any session already in the cookie.
    revoke_admin_session(get_admin_session_id(handler))
    raw_session_id, session = create_admin_session(username, marzban_token)
    _session_response(
        handler,
        200,
        _public_session(session),
        cookie=admin_session_cookie(raw_session_id),
    )


def handle_admin_session_status(handler):
    session = get_admin_session(handler)
    if session is None:
        _session_response(
            handler,
            401,
            {"authenticated": False},
            cookie=admin_session_cookie("", clear=True),
        )
        return
    _session_response(handler, 200, _public_session(session))


def handle_admin_session_logout(handler):
    raw_session_id = get_admin_session_id(handler)
    revoke_admin_session(raw_session_id)
    _session_response(
        handler,
        200,
        {"ok": True},
        cookie=admin_session_cookie("", clear=True),
    )


def handle_admin_session_rotate(handler):
    rotated = rotate_admin_session(get_admin_session_id(handler))
    if rotated is None:
        _session_response(
            handler,
            401,
            {"authenticated": False},
            cookie=admin_session_cookie("", clear=True),
        )
        return
    raw_session_id, session = rotated
    _session_response(
        handler,
        200,
        _public_session(session),
        cookie=admin_session_cookie(raw_session_id),
    )
