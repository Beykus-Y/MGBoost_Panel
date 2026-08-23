import io
import json
from http.cookies import SimpleCookie

import pytest

from src import security
from src.routes import admin_session


class FakeHandler:
    def __init__(self, *, method="GET", body=None, headers=None, client_ip="127.0.0.1"):
        self.command = method
        self.path = "/admin/session"
        self._body = body or b""
        self.rfile = io.BytesIO(self._body)
        self.wfile = io.BytesIO()
        self.headers = {"Content-Length": str(len(self._body)), **(headers or {})}
        self.client_address = (client_ip, 12345)
        self.status = None
        self.response_headers = []

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.response_headers.append((name, value))

    def end_headers(self):
        pass

    def header(self, name):
        values = [value for key, value in self.response_headers if key.lower() == name.lower()]
        return values[-1] if values else None

    def json(self):
        return json.loads(self.wfile.getvalue())


def _cookie_pair(set_cookie):
    parsed = SimpleCookie()
    parsed.load(set_cookie)
    morsel = parsed[security.ADMIN_SESSION_COOKIE]
    return f"{security.ADMIN_SESSION_COOKIE}={morsel.value}", morsel.value


def _login_headers(**extra):
    return {
        "Content-Type": "application/json",
        "X-MGBoost-Admin-Login": "1",
        **extra,
    }


@pytest.fixture(autouse=True)
def clear_sessions():
    security._ADMIN_SESSIONS.clear()
    security._ADMIN_LOGIN_LIMITER.clear()
    yield
    security._ADMIN_SESSIONS.clear()
    security._ADMIN_LOGIN_LIMITER.clear()


def test_login_keeps_marzban_jwt_server_side_and_sets_hardened_cookie(monkeypatch):
    monkeypatch.setattr(admin_session._client, "get_token", lambda username, password: "marzban-secret-jwt")
    body = json.dumps({"username": "admin", "password": "secret"}).encode()
    handler = FakeHandler(method="POST", body=body, headers=_login_headers())

    admin_session.handle_admin_session_login(handler)

    assert handler.status == 200
    data = handler.json()
    assert data["authenticated"] is True
    assert data["username"] == "admin"
    assert data["csrf_token"]
    assert "marzban-secret-jwt" not in handler.wfile.getvalue().decode()
    cookie = handler.header("Set-Cookie")
    assert "HttpOnly" in cookie
    assert "Secure" in cookie
    assert "SameSite=Strict" in cookie
    assert "Path=/" in cookie

    cookie_header, raw_session_id = _cookie_pair(cookie)
    session = security._ADMIN_SESSIONS.get(raw_session_id)
    assert session.marzban_token == "marzban-secret-jwt"


def test_bearer_header_is_not_accepted_as_admin_authentication():
    handler = FakeHandler(headers={"Authorization": "Bearer legacy-browser-jwt"})
    assert security.require_admin_auth(handler) is False
    assert handler.status == 401


def test_login_rejects_cross_site_form_compatible_request(monkeypatch):
    called = False

    def get_token(_username, _password):
        nonlocal called
        called = True

    monkeypatch.setattr(admin_session._client, "get_token", get_token)
    body = json.dumps({"username": "admin", "password": "secret"}).encode()

    missing_guard = FakeHandler(
        method="POST", body=body, headers={"Content-Type": "application/json"}
    )
    admin_session.handle_admin_session_login(missing_guard)
    assert missing_guard.status == 403

    form_content_type = FakeHandler(
        method="POST",
        body=body,
        headers={"Content-Type": "text/plain", "X-MGBoost-Admin-Login": "1"},
    )
    admin_session.handle_admin_session_login(form_content_type)
    assert form_content_type.status == 403
    assert called is False


def test_cookie_auth_requires_csrf_for_mutation():
    raw_session_id, session = security.create_admin_session("admin", "server-jwt")
    cookie = f"{security.ADMIN_SESSION_COOKIE}={raw_session_id}"

    get_handler = FakeHandler(headers={"Cookie": cookie})
    assert security.require_admin_auth(get_handler) is True
    assert get_handler._admin_session.username == "admin"

    missing = FakeHandler(method="POST", headers={"Cookie": cookie})
    assert security.require_admin_auth(missing) is False
    assert missing.status == 403

    valid = FakeHandler(
        method="POST",
        headers={"Cookie": cookie, security.ADMIN_CSRF_HEADER: session.csrf_token},
    )
    assert security.require_admin_auth(valid) is True


def test_logout_revokes_session_and_clears_cookie():
    raw_session_id, session = security.create_admin_session("admin", "server-jwt")
    cookie = f"{security.ADMIN_SESSION_COOKIE}={raw_session_id}"
    handler = FakeHandler(
        method="POST",
        headers={"Cookie": cookie, security.ADMIN_CSRF_HEADER: session.csrf_token},
    )
    assert security.require_admin_auth(handler) is True

    admin_session.handle_admin_session_logout(handler)

    assert handler.status == 200
    assert security._ADMIN_SESSIONS.get(raw_session_id) is None
    assert "Max-Age=0" in handler.header("Set-Cookie")


def test_rotation_invalidates_old_session_and_changes_csrf():
    raw_session_id, session = security.create_admin_session("admin", "server-jwt")
    cookie = f"{security.ADMIN_SESSION_COOKIE}={raw_session_id}"
    handler = FakeHandler(
        method="POST",
        headers={"Cookie": cookie, security.ADMIN_CSRF_HEADER: session.csrf_token},
    )
    assert security.require_admin_auth(handler) is True

    admin_session.handle_admin_session_rotate(handler)

    new_cookie, new_raw = _cookie_pair(handler.header("Set-Cookie"))
    assert new_cookie != cookie
    assert security._ADMIN_SESSIONS.get(raw_session_id) is None
    rotated = security._ADMIN_SESSIONS.get(new_raw)
    assert rotated is not None
    assert rotated.csrf_token != session.csrf_token
    assert rotated.marzban_token == "server-jwt"


def test_expired_session_is_rejected(monkeypatch):
    monkeypatch.setattr(security, "ADMIN_SESSION_TTL_SECONDS", 1)
    raw_session_id, _ = security._ADMIN_SESSIONS.create("admin", "server-jwt", now=100)
    assert security._ADMIN_SESSIONS.get(raw_session_id, now=102) is None


def test_successful_login_revokes_cookie_session_to_prevent_fixation(monkeypatch):
    old_raw, _ = security.create_admin_session("old", "old-jwt")
    monkeypatch.setattr(admin_session._client, "get_token", lambda username, password: "new-jwt")
    body = json.dumps({"username": "admin", "password": "secret"}).encode()
    handler = FakeHandler(
        method="POST",
        body=body,
        headers=_login_headers(Cookie=f"{security.ADMIN_SESSION_COOKIE}={old_raw}"),
    )

    admin_session.handle_admin_session_login(handler)

    assert handler.status == 200
    assert security._ADMIN_SESSIONS.get(old_raw) is None
    _, new_raw = _cookie_pair(handler.header("Set-Cookie"))
    assert new_raw != old_raw


def test_failed_admin_logins_are_rate_limited_before_another_upstream_call(monkeypatch):
    limiter = security.AdminLoginRateLimiter(
        window_seconds=300, identity_failures=2, ip_failures=10
    )
    monkeypatch.setattr(admin_session, "_ADMIN_LOGIN_LIMITER", limiter)
    calls = []

    def reject(username, password):
        calls.append((username, password))
        return None

    monkeypatch.setattr(admin_session._client, "get_token", reject)
    body = json.dumps({"username": "admin", "password": "not-the-password"}).encode()
    first = FakeHandler(method="POST", body=body, headers=_login_headers(), client_ip="127.0.0.1")
    second = FakeHandler(method="POST", body=body, headers=_login_headers(), client_ip="127.0.0.1")
    blocked = FakeHandler(method="POST", body=body, headers=_login_headers(), client_ip="127.0.0.1")

    admin_session.handle_admin_session_login(first)
    admin_session.handle_admin_session_login(second)
    admin_session.handle_admin_session_login(blocked)

    assert first.status == second.status == 401
    assert blocked.status == 429
    assert int(blocked.header("Retry-After")) > 0
    assert len(calls) == 2


def test_login_limiter_uses_validated_real_ip_only_from_loopback_proxy(monkeypatch):
    limiter = security.AdminLoginRateLimiter(
        window_seconds=300, identity_failures=1, ip_failures=10
    )
    monkeypatch.setattr(admin_session, "_ADMIN_LOGIN_LIMITER", limiter)
    monkeypatch.setattr(admin_session._client, "get_token", lambda *_: None)
    body = json.dumps({"username": "admin", "password": "wrong"}).encode()

    proxied = FakeHandler(
        method="POST", body=body,
        headers=_login_headers(**{"X-Real-IP": "198.51.100.8"}),
        client_ip="127.0.0.1",
    )
    same_proxy_ip = FakeHandler(
        method="POST", body=body,
        headers=_login_headers(**{"X-Real-IP": "198.51.100.8"}),
        client_ip="127.0.0.1",
    )
    spoofed_from_remote = FakeHandler(
        method="POST", body=body,
        headers=_login_headers(**{"X-Real-IP": "198.51.100.8"}),
        client_ip="203.0.113.9",
    )

    admin_session.handle_admin_session_login(proxied)
    admin_session.handle_admin_session_login(same_proxy_ip)
    admin_session.handle_admin_session_login(spoofed_from_remote)

    assert proxied.status == 401
    assert same_proxy_ip.status == 429
    assert spoofed_from_remote.status == 401


def test_login_rate_limit_window_expires_and_success_clears_identity_bucket():
    limiter = security.AdminLoginRateLimiter(
        window_seconds=10, identity_failures=2, ip_failures=10
    )
    limiter.record_failure("198.51.100.1", "admin", now=100)
    limiter.record_failure("198.51.100.1", "admin", now=101)
    assert limiter.retry_after("198.51.100.1", "admin", now=102) == 8
    assert limiter.retry_after("198.51.100.1", "admin", now=111) == 0

    limiter.record_failure("198.51.100.1", "admin", now=120)
    limiter.record_success("198.51.100.1", "admin")
    assert limiter.retry_after("198.51.100.1", "admin", now=120) == 0


def test_login_limiter_never_stores_plaintext_username_or_password():
    limiter = security.AdminLoginRateLimiter(
        window_seconds=300, identity_failures=2, ip_failures=10
    )
    limiter.record_failure("198.51.100.1", "sensitive-admin")
    representation = repr(limiter._identities)
    assert "sensitive-admin" not in representation
    assert "password" not in representation
