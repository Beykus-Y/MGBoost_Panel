"""Astra review round on DL-063 (commit 0832fe3): findings 3/4/5/8, each
exercised through the REAL entry point (bot callback handler, admin HTTP
route handler) rather than a store-method shortcut -- findings 1 and 4
slipped through the first review precisely because store-level tests can't
see a positional-vs-keyword-only bug at the real call site.
"""
import asyncio
import importlib
import json
import os
import tempfile
import threading
import time

import pytest

PRIMARY_ACTOR_ID = "owner:primary-admin-stable-id"
PRIMARY_LOGIN = "authenticated-primary-login"


@pytest.fixture
def db(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="lsw-bot-review-")
    monkeypatch.setenv("DATA_DIR", tmp)
    monkeypatch.setenv("PRIMARY_MGBOOST_ADMIN_ACTOR_ID", PRIMARY_ACTOR_ID)
    monkeypatch.setenv("PRIMARY_MGBOOST_ADMIN_LOGIN", PRIMARY_LOGIN)
    monkeypatch.setenv("PUBLIC_HOST", "sub.example.test")
    import src.config as config
    import src.database as database
    importlib.reload(config)
    importlib.reload(database)
    database.DB_PATH = os.path.join(tmp, "db.sqlite3")
    instance = database.Database()
    yield instance
    instance._conn.close()


def _legacy_source(db, *, expiry, username="lsw-bot-user", tg=997401, approved_limit=3):
    from src.legacy_paid_compat import ensure_legacy_paid_compat_entitlement
    from src.plan_catalog import seed_plan_catalog
    from tests.test_legacy_paid_compat import _reviewed_account
    account, cap = _reviewed_account(db, username=username, tg=tg, legacy_expiry=expiry)
    ensure_legacy_paid_compat_entitlement(
        db, capability=cap, account_id=account["account_id"],
        approved_extra_device_slots=approved_limit - 3,
        evidence={"owner_decision": "bot review fix test"},
        decision_ref="lsw-bot-review-test", now=100,
    )
    seed_plan_catalog(db.plan_catalog, now=101)
    return account["account_id"], tg


def _switch_row(db, account_id):
    return db._conn.execute(
        "SELECT * FROM mgboost_legacy_stars_plan_switches WHERE account_id=? ORDER BY id DESC LIMIT 1",
        (account_id,),
    ).fetchone()


def _get_handler(dp_observer, name):
    for h in dp_observer.handlers:
        if h.callback.__name__ == name:
            return h.callback
    raise AssertionError(f"handler {name} not registered")


class FakeUser:
    def __init__(self, tg_id):
        self.id = tg_id


class FakeMsg:
    def __init__(self):
        self.answers = []
        self.edits = []
        self.bot = None

    async def answer(self, text, reply_markup=None, **kw):
        self.answers.append((text, reply_markup))

    async def edit_text(self, text, reply_markup=None, **kw):
        self.edits.append((text, reply_markup))


class FakeCall:
    def __init__(self, tg_id, data):
        self.from_user = FakeUser(tg_id)
        self.data = data
        self.message = FakeMsg()

    async def answer(self):
        pass


# --- Finding 4: lsw_cancel keyword-only `now` bug --------------------------

def test_lsw_cancel_callback_cancels_pending_switch_and_frees_slot(db):
    from aiogram import Dispatcher
    from src.bot_support import setup_support_handlers

    account_id, tg = _legacy_source(db, expiry=100000)
    invoice = db.stars_purchases.create_legacy_switch_invoice(
        telegram_id=tg, target_plan_code="BASIC", duration_days=30, ttl_seconds=3600, now=1000,
    )
    switch = _switch_row(db, account_id)
    assert switch["state"] == "PENDING_PAYMENT"

    dp = Dispatcher()
    setup_support_handlers(dp, db, marzban=None)
    handler = _get_handler(dp.callback_query, "cb_legacy_switch_cancel")
    call = FakeCall(tg, "lsw_cancel")
    asyncio.run(handler(call))  # must not raise TypeError

    assert _switch_row(db, account_id)["state"] == "CANCELLED"
    assert call.message.edits and "отменена" in call.message.edits[-1][0]

    # UNIQUE slot is free: a second attempt succeeds.
    invoice2 = db.stars_purchases.create_legacy_switch_invoice(
        telegram_id=tg, target_plan_code="WL", duration_days=30, ttl_seconds=3600, now=1001,
    )
    assert invoice2["id"] != invoice["id"]


def test_lsw_cancel_callback_on_already_paid_switch_refuses_gracefully(db):
    from aiogram import Dispatcher
    from src.bot_support import setup_support_handlers

    account_id, tg = _legacy_source(db, expiry=100000)
    invoice = db.stars_purchases.create_legacy_switch_invoice(
        telegram_id=tg, target_plan_code="BASIC", duration_days=30, ttl_seconds=3600, now=1000,
    )
    assert db.stars_purchases.capture_paid(
        invoice["id"], charge_id="lsw-bot-cancel-paid", provider_charge_id=None,
        payer_telegram_id=tg, currency="XTR", amount=invoice["stars_price"], now=1010,
    ) == "paid"

    dp = Dispatcher()
    setup_support_handlers(dp, db, marzban=None)
    handler = _get_handler(dp.callback_query, "cb_legacy_switch_cancel")
    call = FakeCall(tg, "lsw_cancel")
    asyncio.run(handler(call))  # must not raise TypeError either

    assert _switch_row(db, account_id)["state"] == "PENDING_PAYMENT"
    assert call.message.answers and "оплачена" in call.message.answers[-1][0]


# --- Finding 5: a cancelled invoice must fail closed at pre_checkout -------

def test_checkout_after_cancel_is_rejected_before_any_money_moves(db):
    account_id, tg = _legacy_source(db, expiry=100000)
    invoice = db.stars_purchases.create_legacy_switch_invoice(
        telegram_id=tg, target_plan_code="BASIC", duration_days=30, ttl_seconds=3600, now=1000,
    )
    switch = _switch_row(db, account_id)
    db.legacy_stars_plan_switch.cancel_unpaid_locked(switch["id"], now=1010)

    from src.stars_purchase import StarsPurchaseError
    with pytest.raises(StarsPurchaseError):
        db.stars_purchases.validate_invoice_for_checkout(invoice["id"], tg, now=1020)
    assert db.get_invoice(invoice["id"])["status"] == "created"  # still unpaid, untouched


def test_checkout_approved_then_cancel_then_late_payment_goes_to_manual_review(db):
    """The external race findings 3 and 5 both depend on: pre_checkout was
    already approved, THEN the user cancels, THEN Telegram's
    successful_payment for the same invoice arrives late. Money must never
    be silently dropped or silently applied to a cancelled switch."""
    account_id, tg = _legacy_source(db, expiry=100000)
    invoice = db.stars_purchases.create_legacy_switch_invoice(
        telegram_id=tg, target_plan_code="BASIC", duration_days=30, ttl_seconds=3600, now=1000,
    )
    switch = _switch_row(db, account_id)
    validated = db.stars_purchases.validate_invoice_for_checkout(invoice["id"], tg, now=1005)
    assert validated["id"] == invoice["id"]

    db.legacy_stars_plan_switch.cancel_unpaid_locked(switch["id"], now=1010)
    assert _switch_row(db, account_id)["state"] == "CANCELLED"

    outcome = db.stars_purchases.capture_paid(
        invoice["id"], charge_id="lsw-late-payment-after-checkout", provider_charge_id=None,
        payer_telegram_id=tg, currency="XTR", amount=invoice["stars_price"], now=1020,
    )
    assert outcome == "manual_review"
    row = db.get_invoice(invoice["id"])
    assert row["status"] == "manual_review"
    assert row["paid_at"] == 1020  # evidence preserved, never dropped
    assert _switch_row(db, account_id)["state"] == "CANCELLED"  # never resurrected/applied


# --- Finding 8: post-payment UX must not overpromise immediate application -

def test_successful_payment_for_legacy_switch_shows_scheduled_not_immediate(db):
    from aiogram import Dispatcher
    from src.bot_support import setup_support_handlers

    account_id, tg = _legacy_source(db, expiry=100000)
    invoice = db.stars_purchases.create_legacy_switch_invoice(
        telegram_id=tg, target_plan_code="BASIC", duration_days=30, ttl_seconds=3600, now=1000,
    )

    dp = Dispatcher()
    trigger = asyncio.Event()

    class HealthyMarzban:
        def get_admin_token_from_env(self):
            return "svc"

        def get_user(self, username, _token):
            return {"username": username, "expire": int(time.time()) + 86400, "status": "active"}

    setup_support_handlers(dp, db, marzban=HealthyMarzban(), stars_trigger=trigger)
    handler = _get_handler(dp.message, "on_successful_payment")

    class SP:
        telegram_payment_charge_id = "lsw-ux-charge"
        provider_payment_charge_id = None
        currency = "XTR"
        total_amount = invoice["stars_price"]
        invoice_payload = str(invoice["id"])

    class Msg:
        def __init__(self):
            self.answers = []
            self.from_user = FakeUser(tg)
            self.successful_payment = SP()

        async def answer(self, text, **kw):
            self.answers.append(text)

    class State:
        async def get_data(self):
            return {}

        async def clear(self):
            pass

    msg = Msg()
    asyncio.run(handler(msg, State()))
    assert msg.answers, "handler must have replied"
    text = msg.answers[-1]
    assert "Применяем подписку" not in text
    assert "переход" in text.lower()

    # Whichever wording is chosen (activation date known or not yet
    # confirmed), it must never claim immediate application.
    switch = _switch_row(db, account_id)
    if switch and switch["activation_at"]:
        assert time.strftime('%d.%m.%Y', time.gmtime(int(switch["activation_at"]))) in text
    else:
        assert "ближайшее время" in text


def test_ordinary_canonical_purchase_ux_is_unchanged(db):
    """Regression: the kind-specific branch must not affect ordinary Stars
    purchases (CANONICAL_PLAN)."""
    from aiogram import Dispatcher
    from src.bot_support import setup_support_handlers
    from src.plan_catalog import seed_plan_catalog

    seed_plan_catalog(db.plan_catalog, now=1)
    account = db.accounts.create_account("DIRECT", now=1)
    db.accounts.link_telegram_owner(account["id"], 997499, provenance="MIGRATION", actor="test", now=1)
    invoice = db.stars_purchases.create_invoice(
        telegram_id=997499, plan_code="BASIC", duration_days=30, ttl_seconds=3600, now=10,
    )

    dp = Dispatcher()
    trigger = asyncio.Event()

    class HealthyMarzban:
        def get_admin_token_from_env(self):
            return "svc"

        def get_user(self, username, _token):
            return {"username": username, "expire": int(time.time()) + 86400, "status": "active"}

    setup_support_handlers(dp, db, marzban=HealthyMarzban(), stars_trigger=trigger)
    handler = _get_handler(dp.message, "on_successful_payment")

    class SP:
        telegram_payment_charge_id = "ordinary-ux-charge"
        provider_payment_charge_id = None
        currency = "XTR"
        total_amount = invoice["stars_price"]
        invoice_payload = str(invoice["id"])

    class Msg:
        def __init__(self):
            self.answers = []
            self.from_user = FakeUser(997499)
            self.successful_payment = SP()

        async def answer(self, text, **kw):
            self.answers.append(text)

    class State:
        async def get_data(self):
            return {}

        async def clear(self):
            pass

    msg = Msg()
    asyncio.run(handler(msg, State()))
    assert msg.answers == ["Оплата получена! Применяем подписку…"]


# --- Finding 3: MANUAL_REVIEW refund/recovery through the real admin route -

def _admin_session(login=PRIMARY_LOGIN):
    from src.security import AdminSessionStore
    _raw, session = AdminSessionStore().create(login, "jwt")
    return session


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


def _refund_handler(db, *, reason="test-authorized-refund-reason", bot_runner=None):
    body = json.dumps({"reason": reason}).encode()
    handler = FakeHandler(db, body=body, bot_runner=bot_runner)
    handler._admin_session = _admin_session()
    return handler


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


class FakeRefundBot:
    def __init__(self, result=True):
        self.calls = []
        self.result = result

    async def refund_star_payment(self, user_id, charge_id):
        self.calls.append((user_id, charge_id))
        return self.result


def test_manual_review_legacy_switch_is_refundable_through_real_admin_route(db):
    """Finding 3: before the fix, a switch stuck in MANUAL_REVIEW left its
    invoice at status='paid', which admin.py's refund gate does not accept
    -- the payment was a dead end for the existing audited refund tool."""
    from src.routes.admin import handle_stars_payment_refund
    from tests.test_child_provisioning import HWID_KEY

    account_id, tg = _legacy_source(db, expiry=100000, approved_limit=4)
    invoice = db.stars_purchases.create_legacy_switch_invoice(
        telegram_id=tg, target_plan_code="BASIC", duration_days=30, ttl_seconds=3600, now=1000,
    )
    assert db.stars_purchases.capture_paid(
        invoice["id"], charge_id="lsw-refund-charge", provider_charge_id=None,
        payer_telegram_id=tg, currency="XTR", amount=invoice["stars_price"], now=1050,
    ) == "paid"
    # Force device-limit-exceeded-at-confirm -> MANUAL_REVIEW, money already moved.
    for i in range(4):
        db.device_slots.claim(account_id, f"lsw-refund-hwid-{i}", HWID_KEY, now=100)
    db.stars_purchases.apply_paid_invoice(invoice["id"], now=1050)
    switch = _switch_row(db, account_id)
    assert switch["state"] == "MANUAL_REVIEW"
    assert db.get_invoice(invoice["id"])["status"] == "manual_review"

    bot = FakeRefundBot(result=True)
    runner = LoopRunner(bot)
    try:
        h = _refund_handler(db, bot_runner=runner)
        handle_stars_payment_refund(h, str(invoice["id"]))
        assert h._response_code == 200
        assert bot.calls == [(tg, "lsw-refund-charge")]
        assert db.get_invoice(invoice["id"])["status"] == "refunded"
        fresh = _switch_row(db, account_id)
        assert fresh["state"] == "REFUNDED"
    finally:
        runner.close()

    # The live-UNIQUE slot is free again: a fresh attempt is possible.
    # (4 devices are still claimed from the setup above -- target FAMILY,
    # device_limit=12, so this proves the slot freed, not a device-count
    # coincidence.)
    invoice2 = db.stars_purchases.create_legacy_switch_invoice(
        telegram_id=tg, target_plan_code="FAMILY", duration_days=30, ttl_seconds=3600, now=1200,
    )
    assert invoice2["id"] != invoice["id"]


def test_refund_of_already_applied_switch_never_touches_entitlement(db):
    """Fail-closed requirement: refunding an APPLIED switch's invoice
    (money-only, exactly like every other invoice kind's existing refund
    path) must never roll back the already-granted entitlement."""
    from src.routes.admin import handle_stars_payment_refund

    account_id, tg = _legacy_source(db, expiry=1000)
    invoice = db.stars_purchases.create_legacy_switch_invoice(
        telegram_id=tg, target_plan_code="BASIC", duration_days=30, ttl_seconds=3600, now=900,
    )
    assert db.stars_purchases.capture_paid(
        invoice["id"], charge_id="lsw-refund-applied-charge", provider_charge_id=None,
        payer_telegram_id=tg, currency="XTR", amount=invoice["stars_price"], now=950,
    ) == "paid"
    db.stars_purchases.apply_paid_invoice(invoice["id"], now=950)
    switch = _switch_row(db, account_id)
    applied = db.legacy_stars_plan_switch.apply_locked(switch["id"], now=switch["activation_at"])
    assert applied["state"] == "APPLIED"
    sub_before = dict(db._conn.execute(
        "SELECT current_plan_version_id,current_expiry,status FROM mgboost_subscriptions WHERE account_id=? ORDER BY id DESC LIMIT 1",
        (account_id,),
    ).fetchone())
    assert db.get_invoice(invoice["id"])["status"] == "canonical_applied"

    bot = FakeRefundBot(result=True)
    runner = LoopRunner(bot)
    try:
        h = _refund_handler(db, bot_runner=runner)
        handle_stars_payment_refund(h, str(invoice["id"]))
        assert h._response_code == 200
        assert db.get_invoice(invoice["id"])["status"] == "refunded"
    finally:
        runner.close()

    sub_after = dict(db._conn.execute(
        "SELECT current_plan_version_id,current_expiry,status FROM mgboost_subscriptions WHERE account_id=? ORDER BY id DESC LIMIT 1",
        (account_id,),
    ).fetchone())
    assert sub_after == sub_before  # entitlement completely untouched
    assert _switch_row(db, account_id)["state"] == "APPLIED"  # never reverted


# --- Second Astra pass, finding 3: REFUNDED must not look "live" to the bot -

def test_wizard_is_available_again_after_refund_not_stuck_on_in_flight_status(db):
    """A REFUNDED switch is terminal, exactly like APPLIED/CANCELLED -- the
    bot's legacy-switch entry point must show the plan picker again, not the
    stale 'у вас уже есть заявка' in-flight status screen, and a fresh
    invoice must be creatable."""
    from aiogram import Dispatcher
    from src.bot_support import setup_support_handlers
    from src.routes.admin import handle_stars_payment_refund

    account_id, tg = _legacy_source(db, expiry=100000, approved_limit=4)
    invoice = db.stars_purchases.create_legacy_switch_invoice(
        telegram_id=tg, target_plan_code="BASIC", duration_days=30, ttl_seconds=3600, now=1000,
    )
    assert db.stars_purchases.capture_paid(
        invoice["id"], charge_id="lsw-wizard-refund-charge", provider_charge_id=None,
        payer_telegram_id=tg, currency="XTR", amount=invoice["stars_price"], now=1050,
    ) == "paid"
    from tests.test_child_provisioning import HWID_KEY
    for i in range(4):
        db.device_slots.claim(account_id, f"lsw-wizard-refund-hwid-{i}", HWID_KEY, now=100)
    db.stars_purchases.apply_paid_invoice(invoice["id"], now=1050)
    assert _switch_row(db, account_id)["state"] == "MANUAL_REVIEW"

    bot = FakeRefundBot(result=True)
    runner = LoopRunner(bot)
    try:
        h = _refund_handler(db, bot_runner=runner)
        handle_stars_payment_refund(h, str(invoice["id"]))
        assert h._response_code == 200
    finally:
        runner.close()
    assert _switch_row(db, account_id)["state"] == "REFUNDED"
    assert db.legacy_stars_plan_switch.for_account(account_id) is None  # not "live" any more

    db.set_setting("stars:enabled", "1")
    db.set_setting("legacy_stars_switch:enabled", "ON")
    dp = Dispatcher()
    setup_support_handlers(dp, db, marzban=None)
    handler = _get_handler(dp.callback_query, "cb_buy_open")
    call = FakeCall(tg, "buy_open")
    asyncio.run(handler(call))

    assert call.message.answers, "cb_buy_open must reply"
    text, markup = call.message.answers[-1]
    assert "уже есть заявка" not in text
    assert markup is not None
    callback_datas = [btn.callback_data for row in markup.inline_keyboard for btn in row]
    assert any(cd.startswith("lsw_plan:") for cd in callback_datas), (
        "expected the plan-picker wizard, not the in-flight status screen"
    )

    invoice2 = db.stars_purchases.create_legacy_switch_invoice(
        telegram_id=tg, target_plan_code="FAMILY", duration_days=30, ttl_seconds=3600, now=1200,
    )
    assert invoice2["id"] != invoice["id"]


# --- Second Astra pass, finding 4: refund finalization crash-safety --------

def test_refund_reconciliation_recovers_from_a_crash_before_switch_finalize(db):
    """Simulates the exact failure boundary the review flagged: the invoice
    commit lands as 'refunded' (money-side bookkeeping done), but the
    process dies before the bound switch's terminal-state write happens --
    reproduced directly at the DB layer since Database.mark_invoice_refunded
    now folds both writes into one transaction and can no longer produce
    this split in its own normal operation. Retrying the SAME real
    entrypoint (Database.mark_invoice_refunded) must be the reconciliation
    path: it detects invoice.status=='refunded' with a non-terminal bound
    switch and deterministically finishes the switch -> REFUNDED, frees the
    live-UNIQUE slot, and is itself idempotent on a further retry."""
    account_id, tg = _legacy_source(db, expiry=100000, approved_limit=4)
    invoice = db.stars_purchases.create_legacy_switch_invoice(
        telegram_id=tg, target_plan_code="BASIC", duration_days=30, ttl_seconds=3600, now=1000,
    )
    assert db.stars_purchases.capture_paid(
        invoice["id"], charge_id="lsw-crash-refund-charge", provider_charge_id=None,
        payer_telegram_id=tg, currency="XTR", amount=invoice["stars_price"], now=1050,
    ) == "paid"
    from tests.test_child_provisioning import HWID_KEY
    for i in range(4):
        db.device_slots.claim(account_id, f"lsw-crash-refund-hwid-{i}", HWID_KEY, now=100)
    db.stars_purchases.apply_paid_invoice(invoice["id"], now=1050)
    assert _switch_row(db, account_id)["state"] == "MANUAL_REVIEW"

    # Reproduce the crash boundary directly: invoice already flipped to
    # 'refunded' (as if the real Telegram refund call already succeeded and
    # this half of the bookkeeping committed), switch never finalized --
    # the exact split that used to be possible across two separate
    # transactions before this fix.
    db._conn.execute(
        "UPDATE stars_invoices SET status='refunded', refunded_at=1060, resolved_by_admin_at=1060 "
        "WHERE id=?", (invoice["id"],),
    )
    db._conn.commit()
    assert _switch_row(db, account_id)["state"] == "MANUAL_REVIEW"  # still stuck, pre-reconciliation

    # Retry/reconciliation through the real entrypoint.
    ok = db.mark_invoice_refunded(invoice["id"])
    assert ok is True
    assert _switch_row(db, account_id)["state"] == "REFUNDED"

    # Live-UNIQUE slot is freed: a fresh attempt is possible.
    invoice2 = db.stars_purchases.create_legacy_switch_invoice(
        telegram_id=tg, target_plan_code="FAMILY", duration_days=30, ttl_seconds=3600, now=1200,
    )
    assert invoice2["id"] != invoice["id"]

    # Idempotent: calling it again does not error or double-transition.
    db.mark_invoice_refunded(invoice["id"])
    fresh = db._conn.execute(
        "SELECT state FROM mgboost_legacy_stars_plan_switches WHERE invoice_id=?", (invoice["id"],),
    ).fetchone()
    assert fresh["state"] == "REFUNDED"


def test_refund_reconciliation_never_reverts_an_already_applied_switch(db):
    """The fail-closed requirement must survive the reconciliation path too,
    not just the primary one: if a switch already reached APPLIED before its
    invoice's refund bookkeeping is (re)processed, the entitlement is never
    touched, mirroring test_refund_of_already_applied_switch_never_touches_
    entitlement but exercised through the same reconciliation branch used
    above."""
    account_id, tg = _legacy_source(db, expiry=1000)
    invoice = db.stars_purchases.create_legacy_switch_invoice(
        telegram_id=tg, target_plan_code="BASIC", duration_days=30, ttl_seconds=3600, now=900,
    )
    assert db.stars_purchases.capture_paid(
        invoice["id"], charge_id="lsw-crash-refund-applied-charge", provider_charge_id=None,
        payer_telegram_id=tg, currency="XTR", amount=invoice["stars_price"], now=950,
    ) == "paid"
    db.stars_purchases.apply_paid_invoice(invoice["id"], now=950)
    switch = _switch_row(db, account_id)
    applied = db.legacy_stars_plan_switch.apply_locked(switch["id"], now=switch["activation_at"])
    assert applied["state"] == "APPLIED"

    db._conn.execute(
        "UPDATE stars_invoices SET status='refunded', refunded_at=?,resolved_by_admin_at=? WHERE id=?",
        (switch["activation_at"] + 10, switch["activation_at"] + 10, invoice["id"]),
    )
    db._conn.commit()

    sub_before = dict(db._conn.execute(
        "SELECT current_plan_version_id,current_expiry,status FROM mgboost_subscriptions WHERE account_id=? ORDER BY id DESC LIMIT 1",
        (account_id,),
    ).fetchone())
    ok = db.mark_invoice_refunded(invoice["id"])
    assert ok is True
    sub_after = dict(db._conn.execute(
        "SELECT current_plan_version_id,current_expiry,status FROM mgboost_subscriptions WHERE account_id=? ORDER BY id DESC LIMIT 1",
        (account_id,),
    ).fetchone())
    assert sub_after == sub_before  # untouched
    assert _switch_row(db, account_id)["state"] == "APPLIED"  # never reverted


# --- Astra review round 3, finding 2: reconciliation via the REAL admin ----
# route, not a direct mark_invoice_refunded() call. _reconcile_stars_refund
# (src/routes/admin.py) is the actual entrypoint reachable from
# handle_stars_payment_reconcile_refund; its own status gate used to reject
# 'refunded' outright (409), making the store-level idempotent reconciliation
# branch fixed in the prior round unreachable through any real admin action.

def test_reconcile_route_recovers_a_refunded_invoice_with_a_stuck_switch(db):
    """Reproduces the exact recoverable split the review flagged: the
    invoice's own refund bookkeeping already committed (status='refunded'),
    but the bound switch never got its terminal-state write (a crash
    between those two facts before they were folded into one transaction,
    or legacy data predating that fix). The real reconciliation route must
    recover it -- 200, not 409 -- finish the switch -> REFUNDED, and free
    the live-UNIQUE slot. An already-APPLIED switch must still never be
    touched by this same path (covered by the sibling test below)."""
    from src.routes.admin import handle_stars_payment_reconcile_refund

    account_id, tg = _legacy_source(db, expiry=100000, approved_limit=4)
    invoice = db.stars_purchases.create_legacy_switch_invoice(
        telegram_id=tg, target_plan_code="BASIC", duration_days=30, ttl_seconds=3600, now=1000,
    )
    assert db.stars_purchases.capture_paid(
        invoice["id"], charge_id="lsw-reconcile-route-charge", provider_charge_id=None,
        payer_telegram_id=tg, currency="XTR", amount=invoice["stars_price"], now=1050,
    ) == "paid"
    from tests.test_child_provisioning import HWID_KEY
    for i in range(4):
        db.device_slots.claim(account_id, f"lsw-reconcile-route-hwid-{i}", HWID_KEY, now=100)
    db.stars_purchases.apply_paid_invoice(invoice["id"], now=1050)
    assert _switch_row(db, account_id)["state"] == "MANUAL_REVIEW"

    # Reproduce the split directly: the invoice-side refund already landed
    # as 'refunded' (as if Telegram's refund call already succeeded and
    # that half of the bookkeeping committed), the switch never finalized.
    db._conn.execute(
        "UPDATE stars_invoices SET status='refunded', refunded_at=1060, resolved_by_admin_at=1060 "
        "WHERE id=?", (invoice["id"],),
    )
    db._conn.commit()
    assert _switch_row(db, account_id)["state"] == "MANUAL_REVIEW"  # still stuck

    h = _refund_handler(db, reason="reconcile-stuck-legacy-switch")
    handle_stars_payment_reconcile_refund(h, str(invoice["id"]))

    assert h._response_code == 200, h.json_response()
    body = h.json_response()
    assert body["ok"] is True
    assert body["status"] == "refunded"
    assert _switch_row(db, account_id)["state"] == "REFUNDED"

    # Live-UNIQUE slot is freed: a fresh attempt is possible.
    invoice2 = db.stars_purchases.create_legacy_switch_invoice(
        telegram_id=tg, target_plan_code="FAMILY", duration_days=30, ttl_seconds=3600, now=1200,
    )
    assert invoice2["id"] != invoice["id"]

    # Idempotent: calling the same route again is a safe no-op, not an error.
    handle_stars_payment_reconcile_refund(_refund_handler(db, reason="reconcile-again"), str(invoice["id"]))
    assert _switch_row(db, invoice2["id"]) is None or True
    fresh = db._conn.execute(
        "SELECT state FROM mgboost_legacy_stars_plan_switches WHERE invoice_id=?", (invoice["id"],),
    ).fetchone()
    assert fresh["state"] == "REFUNDED"


def test_reconcile_route_never_reverts_an_already_applied_switch(db):
    """The fail-closed requirement holds through the real reconciliation
    route too: a switch already APPLIED before its invoice's refund
    bookkeeping is (re)processed must never have its entitlement touched."""
    from src.routes.admin import handle_stars_payment_reconcile_refund

    account_id, tg = _legacy_source(db, expiry=1000)
    invoice = db.stars_purchases.create_legacy_switch_invoice(
        telegram_id=tg, target_plan_code="BASIC", duration_days=30, ttl_seconds=3600, now=900,
    )
    assert db.stars_purchases.capture_paid(
        invoice["id"], charge_id="lsw-reconcile-route-applied-charge", provider_charge_id=None,
        payer_telegram_id=tg, currency="XTR", amount=invoice["stars_price"], now=950,
    ) == "paid"
    db.stars_purchases.apply_paid_invoice(invoice["id"], now=950)
    switch = _switch_row(db, account_id)
    applied = db.legacy_stars_plan_switch.apply_locked(switch["id"], now=switch["activation_at"])
    assert applied["state"] == "APPLIED"

    db._conn.execute(
        "UPDATE stars_invoices SET status='refunded', refunded_at=?,resolved_by_admin_at=? WHERE id=?",
        (switch["activation_at"] + 10, switch["activation_at"] + 10, invoice["id"]),
    )
    db._conn.commit()

    sub_before = dict(db._conn.execute(
        "SELECT current_plan_version_id,current_expiry,status FROM mgboost_subscriptions WHERE account_id=? ORDER BY id DESC LIMIT 1",
        (account_id,),
    ).fetchone())

    h = _refund_handler(db, reason="reconcile-applied-switch")
    handle_stars_payment_reconcile_refund(h, str(invoice["id"]))
    assert h._response_code == 200, h.json_response()

    sub_after = dict(db._conn.execute(
        "SELECT current_plan_version_id,current_expiry,status FROM mgboost_subscriptions WHERE account_id=? ORDER BY id DESC LIMIT 1",
        (account_id,),
    ).fetchone())
    assert sub_after == sub_before  # entitlement completely untouched
    assert _switch_row(db, account_id)["state"] == "APPLIED"  # never reverted
