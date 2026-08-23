"""
Route-level tests for the device-management authorization flow added to
src/routes/lk.py: POST /lk/api/mgmt/exchange, and the new session/cooldown
gating on handle_lk_device_delete / handle_lk_device_rename.

Uses a lightweight fake handler (same pattern as tests/test_admin_routes.py)
to call route functions directly, with MarzbanClient.get_username_for_token
monkeypatched so no real Marzban calls happen. Read-only endpoints
(handle_lk_info/usage/devices) are verified to keep working with just a
token and no management session/cookie.
"""
import json
import os
import sys
import tempfile
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ---------------------------------------------------------------------------
# Fake handler
# ---------------------------------------------------------------------------

class _Wfile:
    def __init__(self):
        self._buf = b""

    def write(self, data):
        self._buf += data


class _Rfile:
    def __init__(self, data):
        self._data = data

    def read(self, n):
        return self._data[:n]


class FakeHandler:
    """Minimal stand-in for BaseHTTPRequestHandler used by the lk.py routes."""

    def __init__(self, db, path="/", body=None, headers=None, client_ip="127.0.0.1"):
        self._response_code = None
        self._sent_headers = {}
        self._request_body = body or b""
        self.path = path
        self.wfile = _Wfile()
        self.rfile = _Rfile(self._request_body)
        self.server = type("S", (), {"db": db})()
        self.client_address = (client_ip, 12345)
        self._headers = dict(headers or {})
        self._headers.setdefault("Content-Length", str(len(self._request_body)))

    def send_response(self, code):
        self._response_code = code

    def send_header(self, k, v):
        # http.server allows the same header multiple times (e.g. multiple
        # Set-Cookie); keep a list so tests can inspect all of them.
        self._sent_headers.setdefault(k, []).append(v)

    def end_headers(self):
        pass

    @property
    def headers(self):
        return self._headers

    def json_response(self):
        return json.loads(self.wfile._buf)

    def cookie_value(self, name):
        for v in self._sent_headers.get("Set-Cookie", []):
            if v.startswith(f"{name}="):
                return v.split(";", 1)[0].split("=", 1)[1]
        return None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_lk_module_state():
    """The rate limiter and mutation cooldown are module-level dicts —
    reset them between tests so one test's timing can't bleed into another."""
    from src.routes import lk as lk_mod
    lk_mod._rate_limit.clear()
    lk_mod._mutation_cooldown.clear()
    yield
    lk_mod._rate_limit.clear()
    lk_mod._mutation_cooldown.clear()


@pytest.fixture
def db():
    tmp = tempfile.mkdtemp()
    os.environ["DATA_DIR"] = tmp
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


@pytest.fixture(autouse=True)
def _mock_username_lookup(monkeypatch):
    """Every /lk/api/* handler resolves a token -> username via
    MarzbanClient.get_username_for_token, which round-trips to Marzban.
    Mock it so tests never make a real network call; token 'tok-<user>'
    resolves to '<user>'."""
    from src.routes import lk as lk_mod

    def fake_get_username_for_token(self, token):
        if token and token.startswith("tok-"):
            return token[len("tok-"):]
        return None

    monkeypatch.setattr(
        lk_mod._client.__class__, "get_username_for_token", fake_get_username_for_token
    )


def _add_device(db, username, device_id_hint="devA"):
    now = int(time.time())
    db._conn.execute(
        "INSERT INTO user_devices (username, token, request_key, device_name, is_active, first_seen, last_seen)"
        " VALUES (?,?,?,?,1,?,?)",
        (username, f"tok-{username}", f"hwid:{device_id_hint}", "Phone", now, now),
    )
    db._conn.commit()
    row = db._conn.execute(
        "SELECT id FROM user_devices WHERE username=? ORDER BY id DESC LIMIT 1", (username,)
    ).fetchone()
    return row["id"]


def _get_mgmt_session_cookie(db, telegram_id, username):
    """Issue + exchange a code the way the real flow does, returning the
    raw session id to attach as a Cookie header in tests."""
    from src.routes.lk import handle_lk_mgmt_exchange

    code = db.create_mgmt_code(telegram_id, username)
    h = FakeHandler(
        db,
        path="/lk/api/mgmt/exchange",
        body=json.dumps({"code": code}).encode(),
        headers={"Content-Type": "application/json"},
    )
    handle_lk_mgmt_exchange(h)
    assert h._response_code == 200, h.json_response()
    return h.cookie_value("mgmt_session")


# ---------------------------------------------------------------------------
# Exchange endpoint
# ---------------------------------------------------------------------------

def test_exchange_valid_code_sets_httponly_cookie(db):
    from src.routes.lk import handle_lk_mgmt_exchange

    code = db.create_mgmt_code(111, "alice")
    h = FakeHandler(db, body=json.dumps({"code": code}).encode())
    handle_lk_mgmt_exchange(h)

    assert h._response_code == 200
    set_cookie = h._sent_headers["Set-Cookie"][0]
    assert "mgmt_session=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "Secure" in set_cookie
    assert "SameSite=Lax" in set_cookie


def test_exchange_code_second_attempt_fails(db):
    from src.routes.lk import handle_lk_mgmt_exchange

    code = db.create_mgmt_code(111, "alice")
    h1 = FakeHandler(db, body=json.dumps({"code": code}).encode())
    handle_lk_mgmt_exchange(h1)
    assert h1._response_code == 200

    h2 = FakeHandler(db, body=json.dumps({"code": code}).encode())
    handle_lk_mgmt_exchange(h2)
    assert h2._response_code == 401
    assert h2.json_response()["reason"] == "invalid_or_expired_code"


def test_exchange_expired_code_fails(db):
    from src.routes.lk import handle_lk_mgmt_exchange

    code = db.create_mgmt_code(111, "alice", ttl_seconds=1)
    db._conn.execute("UPDATE mgmt_codes SET expires_at=?", (int(time.time()) - 5,))
    db._conn.commit()

    h = FakeHandler(db, body=json.dumps({"code": code}).encode())
    handle_lk_mgmt_exchange(h)
    assert h._response_code == 401
    assert h.json_response()["reason"] == "invalid_or_expired_code"


def test_exchange_garbage_code_rejected(db):
    from src.routes.lk import handle_lk_mgmt_exchange

    h = FakeHandler(db, body=json.dumps({"code": "!!!not-valid???"}).encode())
    handle_lk_mgmt_exchange(h)
    assert h._response_code == 400


# ---------------------------------------------------------------------------
# Read-only endpoints unaffected
# ---------------------------------------------------------------------------

def test_read_only_devices_endpoint_works_without_mgmt_session(db):
    from src.routes.lk import handle_lk_devices

    _add_device(db, "alice")
    h = FakeHandler(db, path="/lk/api/devices?token=tok-alice")
    handle_lk_devices(h)
    assert h._response_code == 200
    data = h.json_response()
    assert len(data["devices"]) == 1


def test_read_only_info_endpoint_requires_only_token(db, monkeypatch):
    from src.routes import lk as lk_mod

    # Avoid a real Marzban admin-token round trip.
    monkeypatch.setattr(lk_mod, "_get_admin_token", lambda: "service")
    monkeypatch.setattr(
        lk_mod._client,
        "get_user",
        lambda username, _token: {"username": username, "status": "active", "expire": 123},
    )

    h = FakeHandler(db, path="/lk/api/info?token=tok-alice", headers={"Host": "example.com"})
    lk_mod.handle_lk_info(h)
    assert h._response_code == 200
    assert h.json_response()["username"] == "alice"


# ---------------------------------------------------------------------------
# Mutating endpoints: authorization
# ---------------------------------------------------------------------------

def test_delete_device_without_mgmt_session_rejected(db):
    from src.routes.lk import handle_lk_device_delete

    device_id = _add_device(db, "alice")
    h = FakeHandler(db, path=f"/lk/api/devices/{device_id}?token=tok-alice")
    handle_lk_device_delete(h, str(device_id))
    assert h._response_code == 401
    assert h.json_response()["reason"] == "management_session_required"

    # Device must remain untouched.
    row = db._conn.execute("SELECT is_active FROM user_devices WHERE id=?", (device_id,)).fetchone()
    assert row["is_active"] == 1


def test_delete_device_with_valid_session_succeeds(db):
    from src.routes.lk import handle_lk_device_delete

    device_id = _add_device(db, "alice")
    session_id = _get_mgmt_session_cookie(db, 111, "alice")

    h = FakeHandler(
        db,
        path=f"/lk/api/devices/{device_id}?token=tok-alice",
        headers={"Cookie": f"mgmt_session={session_id}"},
    )
    handle_lk_device_delete(h, str(device_id))
    assert h._response_code == 200

    row = db._conn.execute("SELECT is_active FROM user_devices WHERE id=?", (device_id,)).fetchone()
    assert row["is_active"] == 0


def test_rename_device_without_mgmt_session_rejected(db):
    from src.routes.lk import handle_lk_device_rename

    device_id = _add_device(db, "alice")
    h = FakeHandler(
        db,
        path=f"/lk/api/devices/{device_id}?token=tok-alice",
        body=json.dumps({"name": "New name"}).encode(),
    )
    handle_lk_device_rename(h, str(device_id))
    assert h._response_code == 401


def test_rename_device_with_valid_session_succeeds(db):
    from src.routes.lk import handle_lk_device_rename

    device_id = _add_device(db, "alice")
    session_id = _get_mgmt_session_cookie(db, 111, "alice")

    h = FakeHandler(
        db,
        path=f"/lk/api/devices/{device_id}?token=tok-alice",
        headers={"Cookie": f"mgmt_session={session_id}"},
        body=json.dumps({"name": "New name"}).encode(),
    )
    handle_lk_device_rename(h, str(device_id))
    assert h._response_code == 200

    row = db._conn.execute("SELECT display_name FROM user_devices WHERE id=?", (device_id,)).fetchone()
    assert row["display_name"] == "New name"


def test_session_for_other_username_cannot_mutate(db):
    """A management session issued for user B must not authorize a
    mutation on a device belonging to user A, even though the request is
    otherwise well-formed (valid cookie, valid device id)."""
    from src.routes.lk import handle_lk_device_delete

    device_id = _add_device(db, "alice")
    # Session belongs to bob, not alice.
    session_id = _get_mgmt_session_cookie(db, 222, "bobby")

    h = FakeHandler(
        db,
        path=f"/lk/api/devices/{device_id}?token=tok-alice",
        headers={"Cookie": f"mgmt_session={session_id}"},
    )
    handle_lk_device_delete(h, str(device_id))
    assert h._response_code == 403
    assert h.json_response()["reason"] == "session_username_mismatch"

    row = db._conn.execute("SELECT is_active FROM user_devices WHERE id=?", (device_id,)).fetchone()
    assert row["is_active"] == 1


def test_session_cannot_mutate_device_belonging_to_different_username(db):
    """Even with a legitimate session for username 'bob', a device that
    belongs to 'alice' cannot be touched, because deactivate_device()
    filters WHERE id=? AND username=?."""
    from src.routes.lk import handle_lk_device_delete

    alice_device_id = _add_device(db, "alice")
    bob_session_id = _get_mgmt_session_cookie(db, 222, "bobby")

    # Request is scoped to bob's own token/username context (?token=tok-bobby)
    # but references alice's device id directly.
    h = FakeHandler(
        db,
        path=f"/lk/api/devices/{alice_device_id}?token=tok-bobby",
        headers={"Cookie": f"mgmt_session={bob_session_id}"},
    )
    handle_lk_device_delete(h, str(alice_device_id))
    assert h._response_code == 404

    row = db._conn.execute("SELECT is_active FROM user_devices WHERE id=?", (alice_device_id,)).fetchone()
    assert row["is_active"] == 1


def test_expired_session_rejected(db):
    from src.routes.lk import handle_lk_device_delete

    device_id = _add_device(db, "alice")
    session_id = _get_mgmt_session_cookie(db, 111, "alice")
    db._conn.execute("UPDATE mgmt_sessions SET expires_at=?", (int(time.time()) - 5,))
    db._conn.commit()

    h = FakeHandler(
        db,
        path=f"/lk/api/devices/{device_id}?token=tok-alice",
        headers={"Cookie": f"mgmt_session={session_id}"},
    )
    handle_lk_device_delete(h, str(device_id))
    assert h._response_code == 401
    assert h.json_response()["reason"] == "management_session_required"


# ---------------------------------------------------------------------------
# Cooldown
# ---------------------------------------------------------------------------

def test_mutation_cooldown_blocks_rapid_consecutive_actions(db):
    from src.routes.lk import handle_lk_device_delete, handle_lk_device_rename

    device_id = _add_device(db, "alice")
    session_id = _get_mgmt_session_cookie(db, 111, "alice")

    h1 = FakeHandler(
        db,
        path=f"/lk/api/devices/{device_id}?token=tok-alice",
        headers={"Cookie": f"mgmt_session={session_id}"},
        body=json.dumps({"name": "First rename"}).encode(),
    )
    handle_lk_device_rename(h1, str(device_id))
    assert h1._response_code == 200

    # Immediately try another mutating action for the same username.
    h2 = FakeHandler(
        db,
        path=f"/lk/api/devices/{device_id}?token=tok-alice",
        headers={"Cookie": f"mgmt_session={session_id}"},
    )
    handle_lk_device_delete(h2, str(device_id))
    assert h2._response_code == 429
    assert h2.json_response()["reason"] == "cooldown_active"


# ---------------------------------------------------------------------------
# Rate limiting still applies to mutating endpoints
# ---------------------------------------------------------------------------

def test_session_lacking_scope_rejected(db):
    """Case 4: a valid, unexpired session that lacks the devices:manage
    scope must not authorize a mutation."""
    from src.routes.lk import handle_lk_device_delete

    from src.routes.lk import handle_lk_mgmt_exchange

    device_id = _add_device(db, "alice")
    code = db.create_mgmt_code(111, "alice", scope="")  # no scopes granted
    h_exchange = FakeHandler(
        db, path="/lk/api/mgmt/exchange", body=json.dumps({"code": code}).encode()
    )
    handle_lk_mgmt_exchange(h_exchange)
    assert h_exchange._response_code == 200
    session_id = h_exchange.cookie_value("mgmt_session")

    h = FakeHandler(
        db,
        path=f"/lk/api/devices/{device_id}?token=tok-alice",
        headers={"Cookie": f"mgmt_session={session_id}"},
    )
    handle_lk_device_delete(h, str(device_id))
    assert h._response_code == 403
    assert h.json_response()["reason"] == "insufficient_scope"

    row = db._conn.execute("SELECT is_active FROM user_devices WHERE id=?", (device_id,)).fetchone()
    assert row["is_active"] == 1


def test_mgmt_code_issued_for_one_username_cannot_produce_session_for_another(db):
    """Case 6: a one-time code is issued for a specific (telegram_id,
    marzban_username) pair at create_mgmt_code() time. Exchanging it can
    only ever yield a session for that same username — there is no request
    parameter that lets the exchange step override/redirect it to a
    different username."""
    from src.routes.lk import handle_lk_mgmt_exchange

    code = db.create_mgmt_code(111, "alice")

    # Attempt to smuggle a different username via the exchange request body
    # — exchange_mgmt_code()/handle_lk_mgmt_exchange only ever accept
    # `code`, so this can't influence which username the resulting session
    # is bound to.
    h = FakeHandler(
        db,
        body=json.dumps({"code": code, "marzban_username": "bobby", "username": "bobby"}).encode(),
    )
    handle_lk_mgmt_exchange(h)
    assert h._response_code == 200
    data = h.json_response()
    assert data["username"] == "alice"

    session_id = h.cookie_value("mgmt_session")
    session = db.get_mgmt_session(session_id)
    assert session["marzban_username"] == "alice"


# ---------------------------------------------------------------------------
# Item 1 safer redesign: management flow works with no ?token= at all,
# purely off the mgmt-session cookie.
# ---------------------------------------------------------------------------

def test_devices_endpoint_works_via_session_cookie_with_no_token(db):
    from src.routes.lk import handle_lk_devices

    _add_device(db, "alice")
    session_id = _get_mgmt_session_cookie(db, 111, "alice")

    h = FakeHandler(db, path="/lk/api/devices", headers={"Cookie": f"mgmt_session={session_id}"})
    handle_lk_devices(h)
    assert h._response_code == 200
    assert len(h.json_response()["devices"]) == 1


def test_delete_device_works_via_session_cookie_with_no_token(db):
    from src.routes.lk import handle_lk_device_delete

    device_id = _add_device(db, "alice")
    session_id = _get_mgmt_session_cookie(db, 111, "alice")

    h = FakeHandler(db, path=f"/lk/api/devices/{device_id}", headers={"Cookie": f"mgmt_session={session_id}"})
    handle_lk_device_delete(h, str(device_id))
    assert h._response_code == 200

    row = db._conn.execute("SELECT is_active FROM user_devices WHERE id=?", (device_id,)).fetchone()
    assert row["is_active"] == 0


def test_no_token_and_no_session_rejected(db):
    from src.routes.lk import handle_lk_devices

    h = FakeHandler(db, path="/lk/api/devices")
    handle_lk_devices(h)
    assert h._response_code == 400
    assert h.json_response()["reason"] == "management_session_required"


def test_session_only_delete_still_enforces_username_binding(db):
    """Even in the no-token flow, a session for 'bobby' must not be able to
    delete a device belonging to 'alice' — the session cookie's own
    marzban_username is what's used, and deactivate_device() still filters
    by that resolved username."""
    from src.routes.lk import handle_lk_device_delete

    alice_device_id = _add_device(db, "alice")
    bob_session_id = _get_mgmt_session_cookie(db, 222, "bobby")

    h = FakeHandler(
        db,
        path=f"/lk/api/devices/{alice_device_id}",
        headers={"Cookie": f"mgmt_session={bob_session_id}"},
    )
    handle_lk_device_delete(h, str(alice_device_id))
    assert h._response_code == 404

    row = db._conn.execute("SELECT is_active FROM user_devices WHERE id=?", (alice_device_id,)).fetchone()
    assert row["is_active"] == 1


def test_rate_limit_applies_to_delete_endpoint(db):
    from src.routes import lk as lk_mod
    from src.routes.lk import handle_lk_device_delete

    device_id = _add_device(db, "alice")
    session_id = _get_mgmt_session_cookie(db, 111, "alice")

    # Exhaust the per-IP rate limit budget.
    for _ in range(lk_mod._RATE_LIMIT_MAX):
        lk_mod._check_rate_limit("9.9.9.9")

    h = FakeHandler(
        db,
        path=f"/lk/api/devices/{device_id}?token=tok-alice",
        headers={"Cookie": f"mgmt_session={session_id}"},
        client_ip="9.9.9.9",
    )
    handle_lk_device_delete(h, str(device_id))
    assert h._response_code == 429
    assert h.json_response()["error"] == "Too many requests"


# ---------------------------------------------------------------------------
# Item 1 (fragment-based delivery) follow-up: the one-time code must never
# round-trip through a query string / GET request. The bot now emits
# https://{PUBLIC_HOST}/lk/#mgmt=<code> — URL fragments are never sent to
# the server by the browser, so the exchange endpoint only ever needs to
# (and only ever does) accept the code from the POST body. These tests
# exercise the backend exactly as the fragment-based JS flow calls it.
# ---------------------------------------------------------------------------

def test_exchange_ignores_code_passed_via_query_string(db):
    """A code passed only via ?code=... (never in the body) must be
    rejected — proves the endpoint doesn't have a query-string fallback
    that would defeat the point of moving the code off the URL."""
    from src.routes.lk import handle_lk_mgmt_exchange

    code = db.create_mgmt_code(111, "alice")
    h = FakeHandler(db, path=f"/lk/api/mgmt/exchange?code={code}", body=b"{}")
    handle_lk_mgmt_exchange(h)
    assert h._response_code == 400
    assert h.json_response()["reason"] == "invalid_code"

    # The code must still be exchangeable afterward via the body — proves
    # the query-string attempt didn't consume it.
    h2 = FakeHandler(db, body=json.dumps({"code": code}).encode())
    handle_lk_mgmt_exchange(h2)
    assert h2._response_code == 200


def test_exchange_query_string_code_does_not_override_body(db):
    """When a (wrong/garbage) code is in the query string and a valid code
    is in the body, the body value is what's used — the query string is
    never consulted at all."""
    from src.routes.lk import handle_lk_mgmt_exchange

    code = db.create_mgmt_code(111, "alice")
    h = FakeHandler(
        db,
        path="/lk/api/mgmt/exchange?code=totally-different-garbage-code-value",
        body=json.dumps({"code": code}).encode(),
    )
    handle_lk_mgmt_exchange(h)
    assert h._response_code == 200
    assert h.json_response()["username"] == "alice"


def test_get_lk_page_never_returns_or_requires_mgmt_credential(db):
    """A plain GET /lk/ (no query params at all) just serves the static
    page shell — it must not require, read, or echo back any management
    credential. The one-time code only ever travels in the URL fragment,
    which browsers never send to the server in the first place, so the
    handler for this route must not reference query params for `mgmt` at
    all."""
    from src.routes.lk import handle_lk_page

    h = FakeHandler(db, path="/lk/")
    handle_lk_page(h)
    assert h._response_code == 200
    assert h._sent_headers.get("Content-Type") == ["text/html; charset=utf-8"]
    assert h._sent_headers.get("Cache-Control") == ["no-store"]
    assert h._sent_headers.get("Referrer-Policy") == ["no-referrer"]
    assert h._sent_headers.get("X-Frame-Options") == ["DENY"]
    assert "default-src 'self'" in h._sent_headers["Content-Security-Policy"][0]
    # No cookie is set by merely loading the page shell.
    assert "Set-Cookie" not in h._sent_headers


def test_read_only_lk_accepts_subscription_header_without_query_bearer(db):
    from src.routes.lk import handle_lk_devices

    _add_device(db, "alice")
    h = FakeHandler(
        db,
        path="/lk/api/devices",
        headers={"X-MGBoost-Subscription": "tok-alice"},
    )
    handle_lk_devices(h)
    assert h._response_code == 200
    assert h.json_response()["active_count"] == 1
    assert h._sent_headers.get("Cache-Control") == ["no-store"]
    assert h._sent_headers.get("Referrer-Policy") == ["no-referrer"]


def test_exchange_response_sets_cache_control_no_store(db):
    """The exchange response carries a session-establishing Set-Cookie —
    it must never be cached/retained by an intermediate proxy."""
    from src.routes.lk import handle_lk_mgmt_exchange

    code = db.create_mgmt_code(111, "alice")
    h = FakeHandler(db, body=json.dumps({"code": code}).encode())
    handle_lk_mgmt_exchange(h)
    assert h._response_code == 200
    assert h._sent_headers.get("Cache-Control") == ["no-store"]


def test_mutating_device_response_sets_cache_control_no_store(db):
    from src.routes.lk import handle_lk_device_delete

    device_id = _add_device(db, "alice")
    session_id = _get_mgmt_session_cookie(db, 111, "alice")

    h = FakeHandler(
        db,
        path=f"/lk/api/devices/{device_id}",
        headers={"Cookie": f"mgmt_session={session_id}"},
    )
    handle_lk_device_delete(h, str(device_id))
    assert h._response_code == 200
    assert h._sent_headers.get("Cache-Control") == ["no-store"]
