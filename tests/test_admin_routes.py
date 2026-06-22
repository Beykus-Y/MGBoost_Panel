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
    assert data["token"] == ""
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
    assert data["token"] == "999:XYZ"
    assert data["channel_id"] == "@Chan"
    assert data["proxy_enabled"] is False


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
