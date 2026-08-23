import io
import json
from urllib.error import HTTPError

from src import security
from src.routes import admin_proxy


class FakeHandler:
    def __init__(self, *, method="GET", path="/admin/marzban/system", body=b"", headers=None):
        self.command = method
        self.path = path
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.headers = {"Content-Length": str(len(body)), **(headers or {})}
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


def _attach_session(handler, token="server-only-jwt"):
    handler._admin_session = security.AdminSession(
        username="admin",
        marzban_token=token,
        csrf_token="csrf",
        created_at=1,
        expires_at=9999999999,
    )


def test_proxy_forwards_allowlisted_read_with_server_side_token(monkeypatch):
    seen = {}

    def fake_get_users(token, *, limit, offset):
        seen.update(token=token, limit=limit, offset=offset)
        return {"users": [{"username": "alice"}]}

    monkeypatch.setattr(admin_proxy._client, "get_users", fake_get_users)
    handler = FakeHandler(path="/admin/marzban/users?limit=25&offset=50")
    _attach_session(handler)

    admin_proxy.handle_admin_marzban_proxy(handler, "users")

    assert handler.status == 200
    assert seen == {"token": "server-only-jwt", "limit": 25, "offset": 50}
    assert handler.json() == {"users": [{"username": "alice"}]}
    assert "server-only-jwt" not in handler.wfile.getvalue().decode()


def test_proxy_rejects_arbitrary_path_and_duplicate_or_excessive_query():
    arbitrary = FakeHandler(path="/admin/marzban/admins")
    _attach_session(arbitrary)
    admin_proxy.handle_admin_marzban_proxy(arbitrary, "admins")
    assert arbitrary.status == 404

    duplicate = FakeHandler(path="/admin/marzban/users?limit=10&limit=20")
    _attach_session(duplicate)
    admin_proxy.handle_admin_marzban_proxy(duplicate, "users")
    assert duplicate.status == 400

    excessive = FakeHandler(path="/admin/marzban/users?limit=501")
    _attach_session(excessive)
    admin_proxy.handle_admin_marzban_proxy(excessive, "users")
    assert excessive.status == 400


def test_proxy_decodes_username_once_and_rejects_path_separator(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        admin_proxy._client,
        "get_user",
        lambda username, token: seen.update(username=username, token=token) or {"username": username},
    )
    handler = FakeHandler(path="/admin/marzban/user/a%20b")
    _attach_session(handler)
    admin_proxy.handle_admin_marzban_proxy(handler, "user/a%20b")
    assert handler.status == 200
    assert seen["username"] == "a b"

    separator = FakeHandler(path="/admin/marzban/user/a%2Fb")
    _attach_session(separator)
    admin_proxy.handle_admin_marzban_proxy(separator, "user/a%2Fb")
    assert separator.status == 404


def test_upstream_auth_failure_revokes_local_session(monkeypatch):
    raw_session_id, session = security.create_admin_session("admin", "expired-jwt")
    handler = FakeHandler(
        headers={"Cookie": f"{security.ADMIN_SESSION_COOKIE}={raw_session_id}"}
    )
    handler._admin_session = session

    def unauthorized(_token):
        raise HTTPError("http://marzban/api/system", 401, "Unauthorized", None, None)

    monkeypatch.setattr(admin_proxy._client, "get_system", unauthorized)
    admin_proxy.handle_admin_marzban_proxy(handler, "system")

    assert handler.status == 401
    assert security._ADMIN_SESSIONS.get(raw_session_id) is None
    assert "Max-Age=0" in handler.header("Set-Cookie")
    assert "expired-jwt" not in handler.wfile.getvalue().decode()


def test_proxy_preserves_legacy_admin_mutation_payloads(monkeypatch):
    calls = []
    monkeypatch.setattr(
        admin_proxy._client,
        "create_user",
        lambda payload, token: calls.append(("create", payload, token)) or payload,
    )
    monkeypatch.setattr(
        admin_proxy._client,
        "modify_user",
        lambda username, payload, token: calls.append(("modify", username, payload, token)) or payload,
    )
    monkeypatch.setattr(
        admin_proxy._client,
        "delete_user",
        lambda username, token: calls.append(("delete", username, token)) or {},
    )
    create_payload = {
        "username": "legacy-user",
        "proxies": {"vless": {}},
        "inbounds": {"vless": ["LEGACY"]},
        "expire": 1_900_000_000,
        "data_limit": None,
        "data_limit_reset_strategy": "no_reset",
    }
    update_payload = {"note": "manual renewal", "expire": 1_910_000_000, "data_limit": None}

    create = FakeHandler(method="POST", path="/admin/marzban/user", body=json.dumps(create_payload).encode())
    _attach_session(create)
    admin_proxy.handle_admin_marzban_proxy(create, "user")

    update = FakeHandler(
        method="PUT",
        path="/admin/marzban/user/legacy-user",
        body=json.dumps(update_payload).encode(),
    )
    _attach_session(update)
    admin_proxy.handle_admin_marzban_proxy(update, "user/legacy-user")

    delete = FakeHandler(method="DELETE", path="/admin/marzban/user/legacy-user")
    _attach_session(delete)
    admin_proxy.handle_admin_marzban_proxy(delete, "user/legacy-user")

    assert create.status == update.status == delete.status == 200
    assert calls == [
        ("create", create_payload, "server-only-jwt"),
        ("modify", "legacy-user", update_payload, "server-only-jwt"),
        ("delete", "legacy-user", "server-only-jwt"),
    ]
