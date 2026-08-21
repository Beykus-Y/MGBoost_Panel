"""Admin HTTP route tests for Telegram Stars tariff CRUD, settings toggle,
and the payments ledger operator actions (§9/§4.4)."""
import json
import asyncio
import os
import sys
import tempfile
import threading
from types import SimpleNamespace

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


@pytest.mark.parametrize("field,value", [
    ("duration_days", 1.5), ("stars_price", 2.5),
    ("duration_days", True), ("stars_price", False),
    ("duration_days", 3651), ("stars_price", 1_000_001),
])
def test_tariffs_save_rejects_non_integer_and_out_of_bounds_values(db, field, value):
    from src.routes.admin import handle_stars_tariffs_save
    data = {"name": "x", "duration_days": 5, "stars_price": 5}
    data[field] = value
    h = FakeHandler(db, body=json.dumps(data).encode())
    handle_stars_tariffs_save(h)
    assert h._response_code == 400


def test_tariffs_update_unknown_id_is_404_not_200_null(db):
    from src.routes.admin import handle_stars_tariffs_save
    body = json.dumps({
        "id": 999999, "name": "x", "duration_days": 5, "stars_price": 5
    }).encode()
    h = FakeHandler(db, body=body)
    handle_stars_tariffs_save(h)
    assert h._response_code == 404
    assert h.json_response()["error"] == "Tariff not found"


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


@pytest.mark.parametrize("body", [b"[]", b"null", b"{}", b"{bad json"])
def test_tariffs_toggle_rejects_malformed_or_non_object_json(db, body):
    from src.routes.admin import handle_stars_tariffs_toggle

    tariff = db.save_stars_tariff({
        "name": "x", "duration_days": 5, "stars_price": 5,
    })
    before = db.get_stars_tariff(tariff["id"])
    h = FakeHandler(db, body=body)

    handle_stars_tariffs_toggle(h, str(tariff["id"]))

    assert h._response_code == 400
    assert db.get_stars_tariff(tariff["id"])["active"] == before["active"]


@pytest.mark.parametrize("active", [True, False])
def test_tariffs_toggle_keeps_valid_boolean_semantics(db, active):
    from src.routes.admin import handle_stars_tariffs_toggle

    tariff = db.save_stars_tariff({
        "name": "x", "duration_days": 5, "stars_price": 5,
    })
    h = FakeHandler(db, body=json.dumps({"active": active}).encode())
    handle_stars_tariffs_toggle(h, str(tariff["id"]))
    assert h._response_code == 200
    assert db.get_stars_tariff(tariff["id"])["active"] == int(active)


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


def test_stars_settings_string_false_does_not_enable(db):
    from src.routes.admin import handle_stars_settings_save, handle_stars_settings_get
    h = FakeHandler(db, body=json.dumps({"enabled": "false"}).encode())
    handle_stars_settings_save(h)
    assert h._response_code == 200
    h2 = FakeHandler(db)
    handle_stars_settings_get(h2)
    assert h2.json_response() == {"enabled": False}


@pytest.mark.parametrize("payload", [{"enabled": "yes"}, {"enabled": 2}, {}, []])
def test_stars_settings_rejects_malformed_boolean(db, payload):
    from src.routes.admin import handle_stars_settings_save
    h = FakeHandler(db, body=json.dumps(payload).encode())
    handle_stars_settings_save(h)
    assert h._response_code == 400


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
    assert len(db.get_audit_log(event_type="payment_requeued")) == 1


@pytest.mark.parametrize("reason", [
    "amount_or_currency_mismatch",
    "eligibility_changed_after_payment: status_disabled",
])
def test_requeue_rejects_pre_plan_manual_review(db, reason):
    from src.routes.admin import handle_stars_payment_requeue
    inv = db.create_stars_invoice(1, "alice", None, "t", 30, 320)
    db.mark_invoice_paid(inv["id"], "c-pre-plan", None, 1, 320)
    db.mark_invoice_manual_review(inv["id"], reason=reason)
    before = db.get_invoice(inv["id"])

    h = FakeHandler(db)
    handle_stars_payment_requeue(h, str(inv["id"]))
    assert h._response_code == 409
    after = db.get_invoice(inv["id"])
    assert after["status"] == "manual_review"
    assert after["base_expire_observed"] is None
    assert after["target_expire"] is None
    assert before["manual_review_reason"] == after["manual_review_reason"]


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


class LoopRunner:
    def __init__(self, bot):
        self._loop = asyncio.new_event_loop()
        self._bot = bot
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()

    @property
    def bot_instance(self):
        return self._bot

    def close(self):
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=2)
        self._loop.close()


def _applied_invoice(db, charge="refund-charge", payer=222):
    inv = db.create_stars_invoice(1, "alice", None, "t", 30, 320)
    db.mark_invoice_paid(inv["id"], charge, None, payer, 320)
    db.commit_apply_plan(inv["id"], 1000, 2000)
    db.mark_invoice_applied(inv["id"], 2000)
    return inv


def test_refund_success_uses_actual_payer_and_finishes_after_telegram(db):
    from src.routes.admin import handle_stars_payment_refund

    class Bot:
        def __init__(self):
            self.calls = []

        async def refund_star_payment(self, user_id, charge_id):
            self.calls.append((user_id, charge_id))
            return True

    inv = _applied_invoice(db, payer=999)
    bot = Bot()
    runner = LoopRunner(bot)
    try:
        h = FakeHandler(db, bot_runner=runner)
        handle_stars_payment_refund(h, str(inv["id"]))
        assert h._response_code == 200
        assert bot.calls == [(999, "refund-charge")]
        assert db.get_invoice(inv["id"])["status"] == "refunded"
    finally:
        runner.close()


def test_concurrent_refund_requests_make_one_telegram_call(db):
    from src.routes.admin import handle_stars_payment_refund

    started = threading.Event()
    release = threading.Event()

    class Bot:
        def __init__(self):
            self.calls = 0

        async def refund_star_payment(self, user_id, charge_id):
            self.calls += 1
            started.set()
            while not release.is_set():
                await asyncio.sleep(0.01)
            return True

    inv = _applied_invoice(db)
    bot = Bot()
    runner = LoopRunner(bot)
    first = FakeHandler(db, bot_runner=runner)
    first_thread = threading.Thread(
        target=handle_stars_payment_refund, args=(first, str(inv["id"]))
    )
    try:
        first_thread.start()
        assert started.wait(timeout=2)
        second = FakeHandler(db, bot_runner=runner)
        handle_stars_payment_refund(second, str(inv["id"]))
        assert second._response_code == 409
        release.set()
        first_thread.join(timeout=2)
        assert not first_thread.is_alive()
        assert first._response_code == 200
        assert bot.calls == 1
    finally:
        release.set()
        first_thread.join(timeout=2)
        runner.close()


def test_refund_timeout_becomes_unknown_and_blocks_second_refund(db, monkeypatch):
    from src.routes import admin as admin_mod

    class Bot:
        def __init__(self):
            self.calls = 0

        async def refund_star_payment(self, user_id, charge_id):
            self.calls += 1
            await asyncio.sleep(1)
            return True

    inv = _applied_invoice(db)
    bot = Bot()
    runner = LoopRunner(bot)
    monkeypatch.setattr(admin_mod, "REFUND_RESULT_TIMEOUT_SECONDS", 0.01)
    try:
        first = FakeHandler(db, bot_runner=runner)
        admin_mod.handle_stars_payment_refund(first, str(inv["id"]))
        assert first._response_code == 202
        assert db.get_invoice(inv["id"])["status"] == "refund_unknown"

        second = FakeHandler(db, bot_runner=runner)
        admin_mod.handle_stars_payment_refund(second, str(inv["id"]))
        assert second._response_code == 409
        assert bot.calls == 1
    finally:
        runner.close()


def test_refund_explicit_false_becomes_unknown_and_blocks_second_call(db):
    from src.routes.admin import handle_stars_payment_refund

    class Bot:
        def __init__(self):
            self.calls = 0

        async def refund_star_payment(self, user_id, charge_id):
            self.calls += 1
            return False

    inv = _applied_invoice(db)
    bot = Bot()
    runner = LoopRunner(bot)
    try:
        h = FakeHandler(db, bot_runner=runner)
        handle_stars_payment_refund(h, str(inv["id"]))
        assert h._response_code == 202
        row = db.get_invoice(inv["id"])
        assert row["status"] == "refund_unknown"

        second = FakeHandler(db, bot_runner=runner)
        handle_stars_payment_refund(second, str(inv["id"]))
        assert second._response_code == 409
        assert bot.calls == 1
    finally:
        runner.close()


def test_refund_exception_becomes_unknown(db):
    from src.routes.admin import handle_stars_payment_refund

    class Bot:
        async def refund_star_payment(self, user_id, charge_id):
            raise ConnectionError("ambiguous network failure")

    inv = _applied_invoice(db)
    runner = LoopRunner(Bot())
    try:
        h = FakeHandler(db, bot_runner=runner)
        handle_stars_payment_refund(h, str(inv["id"]))
        assert h._response_code == 202
        assert db.get_invoice(inv["id"])["status"] == "refund_unknown"
    finally:
        runner.close()


def test_refund_unknown_reconciliation_marks_confirmed_outgoing_transaction(db):
    from src.routes.admin import handle_stars_payment_reconcile_refund

    inv = _applied_invoice(db, charge="reconcile-charge")
    assert db.begin_invoice_refund(inv["id"])
    assert db.mark_invoice_refund_unknown(inv["id"], "timeout")

    class Bot:
        async def get_star_transactions(self, offset=0, limit=100):
            return SimpleNamespace(transactions=[SimpleNamespace(
                id="reconcile-charge",
                receiver=SimpleNamespace(user=SimpleNamespace(id=222)),
            )])

    runner = LoopRunner(Bot())
    try:
        h = FakeHandler(db, bot_runner=runner)
        handle_stars_payment_reconcile_refund(h, str(inv["id"]))
        assert h._response_code == 200
        row = db.get_invoice(inv["id"])
        assert row["status"] == "refunded"
        assert row["refund_reconciled_at"] is not None
    finally:
        runner.close()


def test_refund_reconciliation_not_found_leaves_state_unchanged(db):
    from src.routes.admin import handle_stars_payment_reconcile_refund

    inv = _applied_invoice(db, charge="missing-refund")
    assert db.begin_invoice_refund(inv["id"])
    assert db.mark_invoice_refund_unknown(inv["id"], "timeout")

    class Bot:
        async def get_star_transactions(self, offset=0, limit=100):
            return SimpleNamespace(transactions=[])

    runner = LoopRunner(Bot())
    try:
        h = FakeHandler(db, bot_runner=runner)
        handle_stars_payment_reconcile_refund(h, str(inv["id"]))
        assert h._response_code == 200
        assert h.json_response()["ok"] is False
        assert db.get_invoice(inv["id"])["status"] == "refund_unknown"
    finally:
        runner.close()


def test_refund_reconciliation_wrong_payer_is_not_confirmed(db):
    from src.routes.admin import handle_stars_payment_reconcile_refund

    inv = _applied_invoice(db, charge="wrong-payer-refund", payer=222)
    assert db.begin_invoice_refund(inv["id"])
    assert db.mark_invoice_refund_unknown(inv["id"], "timeout")

    class Bot:
        async def get_star_transactions(self, offset=0, limit=100):
            return SimpleNamespace(transactions=[SimpleNamespace(
                id="wrong-payer-refund",
                receiver=SimpleNamespace(user=SimpleNamespace(id=999)),
            )])

    runner = LoopRunner(Bot())
    try:
        h = FakeHandler(db, bot_runner=runner)
        handle_stars_payment_reconcile_refund(h, str(inv["id"]))
        assert h._response_code == 200
        assert h.json_response()["ok"] is False
        assert db.get_invoice(inv["id"])["status"] == "refund_unknown"
    finally:
        runner.close()


def test_orphan_payment_is_visible_to_admin_and_refundable(db):
    from src.routes.admin import handle_stars_orphan_payments_list

    orphan, created = db.record_stars_orphan_payment(
        "orphan-c1", None, 555, "XTR", 42, "broken", "invalid_invoice_payload"
    )
    assert created is True
    h = FakeHandler(db, path="/admin/stars-orphan-payments")
    handle_stars_orphan_payments_list(h)
    assert h._response_code == 200
    rows = h.json_response()
    assert len(rows) == 1
    assert rows[0]["id"] == orphan["id"]
    assert rows[0]["status"] == "manual_review"


def test_orphan_payment_refund_uses_captured_payer_and_charge(db):
    from src.routes.admin import handle_stars_orphan_payment_refund

    full_charge_id = "orphan-refund-" + "z" * 400
    orphan, _ = db.record_stars_orphan_payment(
        full_charge_id, None, 555, "XTR", 42, "broken", "invoice_not_found"
    )

    class Bot:
        def __init__(self):
            self.calls = []

        async def refund_star_payment(self, user_id, charge_id):
            self.calls.append((user_id, charge_id))
            return True

    bot = Bot()
    runner = LoopRunner(bot)
    try:
        h = FakeHandler(db, bot_runner=runner)
        handle_stars_orphan_payment_refund(h, str(orphan["id"]))
        assert h._response_code == 200
        assert bot.calls == [(555, full_charge_id)]
        assert db.get_stars_orphan_payment(orphan["id"])["status"] == "refunded"
    finally:
        runner.close()


def _orphan_payment(db, charge="orphan-financial", payer=555):
    row, created = db.record_stars_orphan_payment(
        charge, None, payer, "XTR", 42, "broken", "invoice_not_found"
    )
    assert created is True
    return row


def test_orphan_refund_false_becomes_unknown_and_duplicate_request_is_safe(db):
    from src.routes.admin import handle_stars_orphan_payment_refund

    class Bot:
        def __init__(self):
            self.calls = 0

        async def refund_star_payment(self, user_id, charge_id):
            self.calls += 1
            return False

    orphan = _orphan_payment(db, "orphan-false")
    bot = Bot()
    runner = LoopRunner(bot)
    try:
        first = FakeHandler(db, bot_runner=runner)
        handle_stars_orphan_payment_refund(first, str(orphan["id"]))
        assert first._response_code == 202
        assert db.get_stars_orphan_payment(orphan["id"])["status"] == "refund_unknown"

        second = FakeHandler(db, bot_runner=runner)
        handle_stars_orphan_payment_refund(second, str(orphan["id"]))
        assert second._response_code == 409
        assert bot.calls == 1
    finally:
        runner.close()


def test_orphan_refund_timeout_becomes_unknown(db, monkeypatch):
    from src.routes import admin as admin_mod

    class Bot:
        async def refund_star_payment(self, user_id, charge_id):
            await asyncio.sleep(1)
            return True

    orphan = _orphan_payment(db, "orphan-timeout")
    runner = LoopRunner(Bot())
    monkeypatch.setattr(admin_mod, "REFUND_RESULT_TIMEOUT_SECONDS", 0.01)
    try:
        h = FakeHandler(db, bot_runner=runner)
        admin_mod.handle_stars_orphan_payment_refund(h, str(orphan["id"]))
        assert h._response_code == 202
        assert db.get_stars_orphan_payment(orphan["id"])["status"] == "refund_unknown"
    finally:
        runner.close()


def test_concurrent_orphan_refunds_make_one_telegram_call(db):
    from src.routes.admin import handle_stars_orphan_payment_refund

    started = threading.Event()
    release = threading.Event()

    class Bot:
        def __init__(self):
            self.calls = 0

        async def refund_star_payment(self, user_id, charge_id):
            self.calls += 1
            started.set()
            while not release.is_set():
                await asyncio.sleep(0.01)
            return True

    orphan = _orphan_payment(db, "orphan-concurrent")
    bot = Bot()
    runner = LoopRunner(bot)
    first = FakeHandler(db, bot_runner=runner)
    first_thread = threading.Thread(
        target=handle_stars_orphan_payment_refund,
        args=(first, str(orphan["id"])),
    )
    try:
        first_thread.start()
        assert started.wait(timeout=2)
        second = FakeHandler(db, bot_runner=runner)
        handle_stars_orphan_payment_refund(second, str(orphan["id"]))
        assert second._response_code == 409
        release.set()
        first_thread.join(timeout=2)
        assert first._response_code == 200
        assert bot.calls == 1
    finally:
        release.set()
        first_thread.join(timeout=2)
        runner.close()


@pytest.mark.parametrize("found", [True, False])
def test_orphan_refund_reconciliation_found_or_not_found(db, found):
    from src.routes.admin import handle_stars_orphan_payment_reconcile_refund

    charge = f"orphan-reconcile-{found}"
    orphan = _orphan_payment(db, charge, payer=555)
    assert db.begin_orphan_refund(orphan["id"])
    assert db.mark_orphan_refund_unknown(orphan["id"], "timeout")

    class Bot:
        async def get_star_transactions(self, offset=0, limit=100):
            transactions = []
            if found:
                transactions.append(SimpleNamespace(
                    id=charge,
                    receiver=SimpleNamespace(user=SimpleNamespace(id=555)),
                ))
            return SimpleNamespace(transactions=transactions)

    runner = LoopRunner(Bot())
    try:
        h = FakeHandler(db, bot_runner=runner)
        handle_stars_orphan_payment_reconcile_refund(h, str(orphan["id"]))
        assert h._response_code == 200
        expected = "refunded" if found else "refund_unknown"
        assert db.get_stars_orphan_payment(orphan["id"])["status"] == expected
    finally:
        runner.close()
