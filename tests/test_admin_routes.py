"""
Tests for admin HTTP routes related to bot settings and node settings extensions.
Uses a lightweight fake handler to call route functions directly.
"""
import json
import os
import sys
import tempfile
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ---------------------------------------------------------------------------
# Helpers
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
    """Minimal stand-in for BaseHTTPRequestHandler."""

    def __init__(self, db, body=None):
        self._response_code = None
        self._headers = {}
        self._request_body = body or b""
        self.wfile = _Wfile()
        self.rfile = _Rfile(self._request_body)
        self.server = type("S", (), {"db": db})()

    def send_response(self, code):
        self._response_code = code

    def send_header(self, k, v):
        self._headers[k] = v

    def end_headers(self):
        pass

    @property
    def headers(self):
        return {"Content-Length": str(len(self._request_body))}

    def json_response(self):
        return json.loads(self.wfile._buf)


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


# ---------------------------------------------------------------------------
# Bot settings routes
# ---------------------------------------------------------------------------

def test_get_bot_settings_returns_defaults(db):
    from src.routes.admin import handle_bot_settings_get
    h = FakeHandler(db)
    handle_bot_settings_get(h)
    assert h._response_code == 200
    data = h.json_response()
    assert data["enabled"] is False
    assert data["token_set"] is False
    assert "token" not in data
    assert data["channel_id"] == "@MGBoost_News"
    assert data["proxy_enabled"] is False


def test_post_bot_settings_saves_and_returns(db):
    from src.routes.admin import handle_bot_settings_save
    payload = json.dumps({
        "enabled": True,
        "token": "123:MYTOKEN",
        "channel_id": "@TestChannel",
        "proxy_enabled": True,
        "proxy_host": "150.241.74.147",
        "proxy_port": 1080,
        "proxy_user": "socks",
        "proxy_pass": "telegram",
    }).encode()
    h = FakeHandler(db, body=payload)
    handle_bot_settings_save(h)
    assert h._response_code == 200

    assert db.get_setting("bot:token") == "123:MYTOKEN"
    assert db.get_setting("bot:channel_id") == "@TestChannel"
    assert db.get_setting("bot:enabled") == "1"
    assert db.get_setting("bot:proxy_enabled") == "1"
    assert db.get_setting("bot:proxy_host") == "150.241.74.147"
    assert db.get_setting("bot:proxy_port") == "1080"


def test_get_bot_settings_reflects_saved_values(db):
    from src.routes.admin import handle_bot_settings_get, handle_bot_settings_save
    payload = json.dumps({
        "enabled": True, "token": "999:XYZ", "channel_id": "@Chan",
        "proxy_enabled": False, "proxy_host": "", "proxy_port": 1080,
        "proxy_user": "socks", "proxy_pass": "",
    }).encode()
    handle_bot_settings_save(FakeHandler(db, body=payload))

    h = FakeHandler(db)
    handle_bot_settings_get(h)
    data = h.json_response()
    assert data["enabled"] is True
    assert "token" not in data
    assert "token_masked" not in data
    assert data["token_set"] is True
    assert data["channel_id"] == "@Chan"
    assert data["proxy_enabled"] is False


def test_bot_settings_get_never_leaks_raw_secrets(db):
    """Item 5 / item 3: bot token, proxy password, and OpenRouter key must
    never come back from GET /admin/bot-settings in any form — not
    plaintext, and not even a masked trailing-chars fragment. Only boolean
    *_set presence flags are allowed."""
    from src.routes.admin import handle_bot_settings_get, handle_bot_settings_save
    secrets = {
        "token": "123456:SUPERSECRETTOKEN",
        "proxy_pass": "supersecretproxypass",
        "openrouter_api_key": "sk-or-v1-supersecretkey",
    }
    payload = json.dumps(secrets).encode()
    handle_bot_settings_save(FakeHandler(db, body=payload))

    h = FakeHandler(db)
    handle_bot_settings_get(h)
    data = h.json_response()
    raw_body = h.wfile._buf.decode()

    for field in ("token", "proxy_pass", "openrouter_api_key"):
        assert field not in data
        assert f"{field}_masked" not in data

    # No substring (even a short masked fragment) of any real secret value
    # may appear anywhere in the response body.
    # Note: length-4 fragments are skipped because trivially short fragments
    # (e.g. "pass") can coincidentally match field names like
    # "proxy_pass_set" — that's a false positive, not a real leak.
    for secret_value in secrets.values():
        for length in (6, 8):
            fragment = secret_value[-length:]
            assert fragment not in raw_body, f"leaked fragment {fragment!r} of secret"

    assert data["token_set"] is True
    assert data["proxy_pass_set"] is True
    assert data["openrouter_api_key_set"] is True
    assert "token_masked" not in data
    assert "proxy_pass_masked" not in data
    assert "openrouter_api_key_masked" not in data


def test_bot_settings_get_reports_unset_secrets(db):
    from src.routes.admin import handle_bot_settings_get
    h = FakeHandler(db)
    handle_bot_settings_get(h)
    data = h.json_response()
    assert data["token_set"] is False
    assert data["proxy_pass_set"] is False
    assert data["openrouter_api_key_set"] is False
    assert "token_masked" not in data


def test_bot_settings_save_omitting_secret_keeps_existing_value(db):
    """POST without the secret field means 'keep the existing secret as-is' —
    this is how the frontend behaves when the admin didn't type a new value."""
    from src.routes.admin import handle_bot_settings_save
    handle_bot_settings_save(FakeHandler(db, body=json.dumps({
        "token": "123:ORIGINAL", "openrouter_api_key": "sk-original",
    }).encode()))

    # Save again, only touching an unrelated field, omitting the secrets.
    handle_bot_settings_save(FakeHandler(db, body=json.dumps({
        "channel_id": "@NewChannel",
    }).encode()))

    assert db.get_setting("bot:token") == "123:ORIGINAL"
    assert db.get_setting("bot:openrouter_api_key") == "sk-original"
    assert db.get_setting("bot:channel_id") == "@NewChannel"


# ---------------------------------------------------------------------------
# Node settings route extensions
# ---------------------------------------------------------------------------

def _save_node(db, node_id=1, node_name="beget", address="1.2.3.4"):
    db.save_node_setting({
        "node_id": node_id, "node_name": node_name,
        "node_address": address, "updated_at": int(time.time()),
    })


def test_node_settings_get_includes_monitor_quiet_hours(db):
    from src.routes.admin import handle_node_settings_get
    _save_node(db)
    db.set_node_quiet_hours(1, [{"from": "13:45", "to": "14:30"}])
    h = FakeHandler(db)
    handle_node_settings_get(h)
    assert h._response_code == 200
    data = h.json_response()
    assert "1" in data
    assert data["1"]["monitor_quiet_hours"] == [{"from": "13:45", "to": "14:30"}]


def test_node_settings_save_accepts_custom_node_name(db):
    from src.routes.admin import handle_node_settings_save
    payload = json.dumps({
        "node_id": 1,
        "node_name": "🇷🇺 Новосибирск",
        "node_address": "178.250.186.127",
        "monitor_quiet_hours": [],
    }).encode()
    h = FakeHandler(db, body=payload)
    handle_node_settings_save(h)
    assert h._response_code == 200
    s = db.get_node_setting(1)
    assert s["node_name"] == "🇷🇺 Новосибирск"


def test_node_settings_save_persists_quiet_hours(db):
    from src.routes.admin import handle_node_settings_save
    qh = [{"from": "18:00", "to": "19:30"}]
    payload = json.dumps({
        "node_id": 2,
        "node_name": "Selectel",
        "node_address": "5.178.85.8",
        "monitor_quiet_hours": qh,
    }).encode()
    h = FakeHandler(db, body=payload)
    handle_node_settings_save(h)
    assert db.get_node_quiet_hours(2) == qh


def test_node_settings_get_empty_quiet_hours_by_default(db):
    from src.routes.admin import handle_node_settings_get
    _save_node(db, node_id=3)
    h = FakeHandler(db)
    handle_node_settings_get(h)
    data = h.json_response()
    assert data["3"]["monitor_quiet_hours"] == []
