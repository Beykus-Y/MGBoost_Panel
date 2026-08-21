"""Admin HTTP route tests for Telegram Stars tariff CRUD, settings toggle,
and the payments ledger operator actions (§9/§4.4)."""
import json
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


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
    def __init__(self, db, body=None, bot_runner=None, path="/"):
        self._response_code = None
        self._headers = {}
        self._request_body = body or b""
        self.wfile = _Wfile()
        self.rfile = _Rfile(self._request_body)
        self.server = type("S", (), {"db": db, "bot_runner": bot_runner})()
        self.path = path

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


# --- tariff CRUD -------------------------------------------------------------

def test_tariffs_list_empty_by_default(db):
    from src.routes.admin import handle_stars_tariffs_list
    h = FakeHandler(db)
    handle_stars_tariffs_list(h)
    assert h._response_code == 200
    assert h.json_response() == []


def test_tariffs_save_create(db):
    from src.routes.admin import handle_stars_tariffs_save
    body = json.dumps({"name": "1 месяц", "duration_days": 30, "stars_price": 320}).encode()
    h = FakeHandler(db, body=body)
    handle_stars_tariffs_save(h)
    assert h._response_code == 200
    data = h.json_response()
    assert data["name"] == "1 месяц"
    assert data["duration_days"] == 30
    assert data["stars_price"] == 320
    assert data["active"] == 1


def test_tariffs_save_rejects_non_positive_duration(db):
    from src.routes.admin import handle_stars_tariffs_save
    body = json.dumps({"name": "x", "duration_days": 0, "stars_price": 5}).encode()
    h = FakeHandler(db, body=body)
    handle_stars_tariffs_save(h)
    assert h._response_code == 400


def test_tariffs_save_rejects_non_positive_price(db):
    from src.routes.admin import handle_stars_tariffs_save
    body = json.dumps({"name": "x", "duration_days": 5, "stars_price": 0}).encode()
    h = FakeHandler(db, body=body)
    handle_stars_tariffs_save(h)
    assert h._response_code == 400


def test_tariffs_save_rejects_empty_name(db):
    from src.routes.admin import handle_stars_tariffs_save
    body = json.dumps({"name": "  ", "duration_days": 5, "stars_price": 5}).encode()
    h = FakeHandler(db, body=body)
    handle_stars_tariffs_save(h)
    assert h._response_code == 400


def test_tariffs_delete(db):
    from src.routes.admin import handle_stars_tariffs_save, handle_stars_tariffs_delete
    body = json.dumps({"name": "x", "duration_days": 5, "stars_price": 5}).encode()
    h = FakeHandler(db, body=body)
    handle_stars_tariffs_save(h)
    tid = h.json_response()["id"]
    h2 = FakeHandler(db)
    handle_stars_tariffs_delete(h2, str(tid))
    assert h2._response_code == 200
    assert db.get_stars_tariffs() == []


def test_tariffs_toggle(db):
    from src.routes.admin import handle_stars_tariffs_save, handle_stars_tariffs_toggle
    body = json.dumps({"name": "x", "duration_days": 5, "stars_price": 5}).encode()
    h = FakeHandler(db, body=body)
    handle_stars_tariffs_save(h)
    tid = h.json_response()["id"]

    h2 = FakeHandler(db, body=json.dumps({"active": False}).encode())
    handle_stars_tariffs_toggle(h2, str(tid))
    assert h2._response_code == 200
    assert db.get_active_stars_tariffs() == []


# --- global toggle -------------------------------------------------------------

def test_stars_settings_defaults_disabled(db):
    from src.routes.admin import handle_stars_settings_get
    h = FakeHandler(db)
    handle_stars_settings_get(h)
    assert h.json_response() == {"enabled": False}


def test_stars_settings_save_and_get(db):
    from src.routes.admin import handle_stars_settings_save, handle_stars_settings_get
    h = FakeHandler(db, body=json.dumps({"enabled": True}).encode())
    handle_stars_settings_save(h)
    assert h._response_code == 200

    h2 = FakeHandler(db)
    handle_stars_settings_get(h2)
    assert h2.json_response() == {"enabled": True}


# --- payments ledger -----------------------------------------------------------

def test_payments_list_empty(db):
    from src.routes.admin import handle_stars_payments_list
    h = FakeHandler(db)
    handle_stars_payments_list(h)
    assert h.json_response() == []


def test_payments_list_filters_by_status(db):
    from src.routes.admin import handle_stars_payments_list
    inv1 = db.create_stars_invoice(1, "alice", None, "t", 30, 320)
    inv2 = db.create_stars_invoice(1, "bob", None, "t", 30, 320)
    db.mark_invoice_paid(inv2["id"], "c1", None, 1, 320)

    h = FakeHandler(db, path="/admin/stars-payments?status=paid")
    handle_stars_payments_list(h)
    rows = h.json_response()
    assert len(rows) == 1
    assert rows[0]["marzban_username"] == "bob"


def test_confirm_applied_route_requires_manual_review_or_retry_exhausted(db):
    from src.routes.admin import handle_stars_payment_confirm_applied
    inv = db.create_stars_invoice(1, "alice", None, "t", 30, 320)
    h = FakeHandler(db)
    handle_stars_payment_confirm_applied(h, str(inv["id"]))
    assert h._response_code == 409


def test_confirm_applied_route_success(db, monkeypatch):
    from src.routes import admin as admin_mod

    inv = db.create_stars_invoice(1, "alice", None, "t", 30, 320)
    db.mark_invoice_paid(inv["id"], "c1", None, 222, 320)
    db.commit_apply_plan(inv["id"], 0, 1000)
    db.mark_invoice_manual_review(inv["id"], reason="x")

    class FakeClient:
        def get_admin_token_from_env(self):
            return "tok"

        def get_user(self, username, admin_token):
            return {"username": username, "expire": 4242, "status": "active"}

    monkeypatch.setattr(admin_mod, "_get_stars_admin_token", lambda handler: ("tok", FakeClient()))

    h = FakeHandler(db)
    admin_mod.handle_stars_payment_confirm_applied(h, str(inv["id"]))
    assert h._response_code == 200
    row = db.get_invoice(inv["id"])
    assert row["status"] == "applied"
    assert row["applied_expire"] == 4242


def test_requeue_route(db):
    from src.routes.admin import handle_stars_payment_requeue
    inv = db.create_stars_invoice(1, "alice", None, "t", 30, 320)
    db.mark_invoice_paid(inv["id"], "c1", None, 222, 320)
    db.commit_apply_plan(inv["id"], 0, 1000)
    db.mark_invoice_manual_review(inv["id"], reason="x")

    h = FakeHandler(db)
    handle_stars_payment_requeue(h, str(inv["id"]))
    assert h._response_code == 200
    assert db.get_invoice(inv["id"])["status"] == "plan_committed"
    assert "30" in h.json_response()["message"]


def test_refund_route_requires_bot_running(db):
    from src.routes.admin import handle_stars_payment_refund
    inv = db.create_stars_invoice(1, "alice", None, "t", 30, 320)
    db.mark_invoice_paid(inv["id"], "c1", None, 222, 320)
    db.commit_apply_plan(inv["id"], 0, 1000)
    db.mark_invoice_applied(inv["id"], applied_expire=1000)

    h = FakeHandler(db, bot_runner=None)
    handle_stars_payment_refund(h, str(inv["id"]))
    assert h._response_code == 503


def test_refund_route_rejects_wrong_status(db):
    from src.routes.admin import handle_stars_payment_refund
    inv = db.create_stars_invoice(1, "alice", None, "t", 30, 320)
    h = FakeHandler(db)
    handle_stars_payment_refund(h, str(inv["id"]))
    assert h._response_code == 409
