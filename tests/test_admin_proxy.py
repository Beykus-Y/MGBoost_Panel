import io
import json
import os
import tempfile
from urllib.error import HTTPError

import pytest

from src import security
from src.routes import admin_proxy

# PH7-16 Wave H: DELETE/PUT/reset on the raw Marzban proxy now require
# primary-admin capability + a mandatory reason (see src/routes/admin_proxy.py).
PRIMARY_LOGIN = "authenticated-primary-login"
PRIMARY_ACTOR_ID = "owner:mgboost-primary:v1"


class FakeHandler:
    def __init__(self, *, method="GET", path="/admin/marzban/system", body=b"", headers=None, db=None):
        self.command = method
        self.path = path
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.headers = {"Content-Length": str(len(body)), **(headers or {})}
        self.status = None
        self.response_headers = []
        if db is not None:
            self.server = type("S", (), {"db": db})()

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


@pytest.fixture
def db():
    """Real Database instance with primary-admin capability configured, for
    the destructive-proxy-operation tests (PUT/DELETE/reset) only -- the
    read-only proxy tests never touch handler.server.db and don't need it."""
    tmp = tempfile.mkdtemp(prefix="admin-proxy-test-")
    os.environ["DATA_DIR"] = tmp
    os.environ["PRIMARY_MGBOOST_ADMIN_ACTOR_ID"] = PRIMARY_ACTOR_ID
    os.environ["PRIMARY_MGBOOST_ADMIN_LOGIN"] = PRIMARY_LOGIN
    from importlib import reload
    import src.config as cfg
    reload(cfg)
    cfg.DATA_DIR = tmp
    import src.database as db_mod
    reload(db_mod)
    db_mod.DB_PATH = os.path.join(tmp, "db.sqlite3")
    instance = db_mod.Database()
    yield instance
    instance._conn.close()


def _attach_session(handler, token="server-only-jwt", *, login="admin"):
    _raw, handler._admin_session = security.AdminSessionStore().create(
        login, token
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


def test_secondary_admin_cannot_apply_node_filters_half_of_composite_user_save(db):
    from src.security import require_admin_auth
    from src.routes.admin import handle_node_filters_save

    db.save_node_filters({"alice": {"all": True}})
    payload = json.dumps({"alice": {"all": False, "allowed_configs": ["restricted"]}}).encode()
    handler = FakeHandler(method="POST", path="/admin/node-filters", body=payload, db=db)
    raw, session = security.create_admin_session("secondary-admin-login", "server-only-jwt")
    handler.headers.update({
        "Cookie": f"{security.ADMIN_SESSION_COOKIE}={raw}",
        "X-CSRF-Token": session.csrf_token,
    })

    assert require_admin_auth(handler) is True
    handle_node_filters_save(handler)

    assert handler.status == 403
    assert db.get_node_filters() == {"alice": {"all": True, "allowed_configs": []}}


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


def test_proxy_preserves_legacy_admin_mutation_payloads(db, monkeypatch):
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

    create = FakeHandler(method="POST", path="/admin/marzban/user", body=json.dumps(create_payload).encode(), db=db)
    _attach_session(create)
    admin_proxy.handle_admin_marzban_proxy(create, "user")

    # PUT/DELETE are now gated (PH7-16 Wave H): primary-admin session + a
    # `reason` query param.
    update = FakeHandler(
        method="PUT",
        path="/admin/marzban/user/legacy-user?reason=manual+renewal+per+support+ticket",
        body=json.dumps(update_payload).encode(),
        db=db,
    )
    _attach_session(update, login=PRIMARY_LOGIN)
    admin_proxy.handle_admin_marzban_proxy(update, "user/legacy-user")

    delete = FakeHandler(
        method="DELETE",
        path="/admin/marzban/user/legacy-user?reason=duplicate+account+cleanup",
        db=db,
    )
    _attach_session(delete, login=PRIMARY_LOGIN)
    admin_proxy.handle_admin_marzban_proxy(delete, "user/legacy-user")

    assert create.status == update.status == delete.status == 200
    assert calls == [
        ("create", create_payload, "server-only-jwt"),
        ("modify", "legacy-user", update_payload, "server-only-jwt"),
        ("delete", "legacy-user", "server-only-jwt"),
    ]
    entries = db.get_audit_log(event_type="marzban_proxy_destructive_action")
    assert [e["metadata"]["operation"] for e in reversed(entries)] == ["modify", "delete"]
    assert all(e["metadata"]["actor_ref"] == PRIMARY_ACTOR_ID for e in entries)


def test_proxy_destructive_operations_require_primary_capability(db, monkeypatch):
    monkeypatch.setattr(admin_proxy._client, "modify_user", lambda *a, **k: {})
    monkeypatch.setattr(admin_proxy._client, "delete_user", lambda *a, **k: {})
    monkeypatch.setattr(admin_proxy._client, "reset_user_traffic", lambda *a, **k: {})

    for method, path, op in (
        ("PUT", "/admin/marzban/user/x?reason=some+reason+text", "user/x"),
        ("DELETE", "/admin/marzban/user/x?reason=some+reason+text", "user/x"),
        ("POST", "/admin/marzban/user/x/reset?reason=some+reason+text", "user/x/reset"),
    ):
        handler = FakeHandler(method=method, path=path, db=db)
        _attach_session(handler, login="secondary-admin")
        admin_proxy.handle_admin_marzban_proxy(handler, op)
        assert handler.status == 403, (method, path)


def test_proxy_destructive_operations_require_bounded_reason(db, monkeypatch):
    monkeypatch.setattr(admin_proxy._client, "modify_user", lambda *a, **k: {})
    monkeypatch.setattr(admin_proxy._client, "delete_user", lambda *a, **k: {})
    monkeypatch.setattr(admin_proxy._client, "reset_user_traffic", lambda *a, **k: {})

    for method, path, op in (
        ("PUT", "/admin/marzban/user/x", "user/x"),
        ("DELETE", "/admin/marzban/user/x?reason=ab", "user/x"),
        ("POST", "/admin/marzban/user/x/reset?reason=", "user/x/reset"),
    ):
        handler = FakeHandler(method=method, path=path, db=db)
        _attach_session(handler, login=PRIMARY_LOGIN)
        admin_proxy.handle_admin_marzban_proxy(handler, op)
        assert handler.status == 400, (method, path)


def test_proxy_reset_traffic_requires_primary_capability_and_reason(db, monkeypatch):
    calls = []
    monkeypatch.setattr(
        admin_proxy._client, "reset_user_traffic",
        lambda username, token: calls.append((username, token)) or {},
    )
    handler = FakeHandler(
        method="POST", path="/admin/marzban/user/legacy-user/reset?reason=customer+requested+reset", db=db,
    )
    _attach_session(handler, login=PRIMARY_LOGIN)
    admin_proxy.handle_admin_marzban_proxy(handler, "user/legacy-user/reset")
    assert handler.status == 200
    assert calls == [("legacy-user", "server-only-jwt")]
    entries = db.get_audit_log(event_type="marzban_proxy_destructive_action")
    assert entries[0]["metadata"]["operation"] == "reset_traffic"


def test_proxy_create_user_and_reconnect_stay_ungated(db, monkeypatch):
    """POST user (create) and POST node/{id}/reconnect are deliberately
    NOT gated -- create is non-destructive and reconnect is a benign
    nudge, proportionate to blast radius per the Wave H directive."""
    monkeypatch.setattr(admin_proxy._client, "create_user", lambda payload, token: payload)
    monkeypatch.setattr(admin_proxy._client, "reconnect_node", lambda node_id, token: {"ok": True})

    create = FakeHandler(method="POST", path="/admin/marzban/user", body=b"{}", db=db)
    _attach_session(create, login="secondary-admin")
    admin_proxy.handle_admin_marzban_proxy(create, "user")
    assert create.status == 200

    reconnect = FakeHandler(method="POST", path="/admin/marzban/node/7/reconnect", db=db)
    _attach_session(reconnect, login="secondary-admin")
    admin_proxy.handle_admin_marzban_proxy(reconnect, "node/7/reconnect")
    assert reconnect.status == 200
