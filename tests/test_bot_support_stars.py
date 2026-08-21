"""Tests for the Stars bot-UX handlers registered by setup_support_handlers
(§8): kb_main's new button, pre_checkout_query's double-validation (§8.1)
including the invoice TTL boundary (§3.4), and successful_payment's own
independent re-validation + payer-identity split (§4.1/§6.1/§8.2)."""
import asyncio
import os
import sys
import tempfile
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


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


def _get_handler(dp_observer, name):
    for h in dp_observer.handlers:
        if h.callback.__name__ == name:
            return h.callback
    raise AssertionError(f"handler {name} not registered")


@pytest.fixture
def handlers(db):
    from aiogram import Dispatcher
    from src.bot_support import setup_support_handlers

    dp = Dispatcher()
    trigger = asyncio.Event()
    setup_support_handlers(dp, db, marzban=None, stars_trigger=trigger)
    return {
        "pre_checkout": _get_handler(dp.pre_checkout_query, "on_pre_checkout"),
        "successful_payment": _get_handler(dp.message, "on_successful_payment"),
        "trigger": trigger,
    }


# --- kb_main ---------------------------------------------------------------

def test_kb_main_has_stars_button(db):
    from aiogram import Dispatcher
    from src.bot_support import setup_support_handlers

    dp = Dispatcher()
    # kb_main is a closure — reachable indirectly via cmd_start's reply_markup.
    # Simplest robust check: call setup successfully and inspect a fresh
    # dispatcher's registered /start handler's use of kb_main by triggering
    # it end-to-end would require full aiogram plumbing; instead assert the
    # button text is present in the module by round-tripping through a
    # minimal fake message for a bound user.
    class FakeUser:
        id = 111

    class FakeMessage:
        from_user = FakeUser()
        sent = []

        async def answer(self, text, reply_markup=None, **kw):
            self.sent.append((text, reply_markup))

    class FakeState:
        async def clear(self):
            pass

        async def set_state(self, s):
            pass

    setup_support_handlers(dp, db, marzban=None)
    db.save_tg_user(111, "alice")
    cmd_start = _get_handler(dp.message, "cmd_start")
    msg = FakeMessage()
    asyncio.run(cmd_start(msg, FakeState()))
    assert msg.sent
    _, markup = msg.sent[0]
    texts = [btn.text for row in markup.keyboard for btn in row]
    assert "⭐️ Продлить подписку" in texts


# --- pre_checkout_query: double validation + TTL boundary (§3.4/§8.1) -----

class FakePreCheckoutQuery:
    def __init__(self, invoice_payload, currency, total_amount):
        self.invoice_payload = invoice_payload
        self.currency = currency
        self.total_amount = total_amount
        self.answers = []

    async def answer(self, ok, error_message=None):
        self.answers.append((ok, error_message))


def _paid_ready_invoice(db, **overrides):
    kwargs = dict(
        created_by_telegram_id=111, marzban_username="alice",
        tariff_id=1, tariff_name="1 month", duration_days=30, stars_price=320,
    )
    kwargs.update(overrides)
    return db.create_stars_invoice(**kwargs)


def test_pre_checkout_accepts_valid_invoice(db, handlers):
    inv = _paid_ready_invoice(db)
    q = FakePreCheckoutQuery(str(inv["id"]), "XTR", 320)
    asyncio.run(handlers["pre_checkout"](q))
    assert q.answers == [(True, None)]


def test_pre_checkout_rejects_unknown_invoice(db, handlers):
    q = FakePreCheckoutQuery("999999", "XTR", 320)
    asyncio.run(handlers["pre_checkout"](q))
    assert q.answers[0][0] is False


def test_pre_checkout_rejects_wrong_currency(db, handlers):
    inv = _paid_ready_invoice(db)
    q = FakePreCheckoutQuery(str(inv["id"]), "USD", 320)
    asyncio.run(handlers["pre_checkout"](q))
    assert q.answers[0][0] is False


def test_pre_checkout_rejects_wrong_amount(db, handlers):
    inv = _paid_ready_invoice(db)
    q = FakePreCheckoutQuery(str(inv["id"]), "XTR", 321)
    asyncio.run(handlers["pre_checkout"](q))
    assert q.answers[0][0] is False


def test_pre_checkout_rejects_already_paid_invoice(db, handlers):
    inv = _paid_ready_invoice(db)
    db.mark_invoice_paid(inv["id"], "c1", None, 222, 320)
    q = FakePreCheckoutQuery(str(inv["id"]), "XTR", 320)
    asyncio.run(handlers["pre_checkout"](q))
    assert q.answers[0][0] is False


def test_pre_checkout_ttl_boundary_accepts_one_second_before_expiry(db, handlers, monkeypatch):
    inv = _paid_ready_invoice(db)
    # expires_at = created_at + 3600; freeze "now" to expires_at - 1.
    import src.bot_support as bs
    monkeypatch.setattr(bs.time, "time", lambda: inv["expires_at"] - 1)
    q = FakePreCheckoutQuery(str(inv["id"]), "XTR", 320)
    asyncio.run(handlers["pre_checkout"](q))
    assert q.answers == [(True, None)]


def test_pre_checkout_ttl_boundary_rejects_one_second_after_expiry(db, handlers, monkeypatch):
    inv = _paid_ready_invoice(db)
    import src.bot_support as bs
    monkeypatch.setattr(bs.time, "time", lambda: inv["expires_at"] + 1)
    q = FakePreCheckoutQuery(str(inv["id"]), "XTR", 320)
    asyncio.run(handlers["pre_checkout"](q))
    assert q.answers[0][0] is False
    assert "истёк" in q.answers[0][1]


# --- successful_payment: independent re-validation + payer identity -------

class FakeSuccessfulPayment:
    def __init__(self, invoice_payload, currency="XTR", total_amount=320,
                 charge_id="charge-1", provider_charge_id=None):
        self.invoice_payload = invoice_payload
        self.currency = currency
        self.total_amount = total_amount
        self.telegram_payment_charge_id = charge_id
        self.provider_payment_charge_id = provider_charge_id


class FakeFromUser:
    def __init__(self, uid):
        self.id = uid


class FakeSuccessfulPaymentMessage:
    def __init__(self, sp, payer_id, bot=None):
        self.successful_payment = sp
        self.from_user = FakeFromUser(payer_id)
        self.bot = bot
        self.answers = []

    async def answer(self, text, **kw):
        self.answers.append(text)


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


def test_successful_payment_applies_and_records_payer_id(db, handlers):
    inv = _paid_ready_invoice(db)
    sp = FakeSuccessfulPayment(str(inv["id"]))
    msg = FakeSuccessfulPaymentMessage(sp, payer_id=999, bot=FakeBot())

    class FakeState:
        pass

    asyncio.run(handlers["successful_payment"](msg, FakeState()))
    row = db.get_invoice(inv["id"])
    assert row["status"] == "paid"
    assert row["payer_telegram_id"] == 999
    assert row["created_by_telegram_id"] == 111  # unchanged, still the creator
    assert handlers["trigger"].is_set()


def test_successful_payment_gift_payment_payer_differs_from_creator(db, handlers):
    """created_by_telegram_id (111, the account the invoice was issued for)
    must remain distinct from payer_telegram_id (a different account that
    actually completed payment) — never conflated."""
    inv = _paid_ready_invoice(db, created_by_telegram_id=111)
    sp = FakeSuccessfulPayment(str(inv["id"]))
    msg = FakeSuccessfulPaymentMessage(sp, payer_id=555, bot=FakeBot())

    class FakeState:
        pass

    asyncio.run(handlers["successful_payment"](msg, FakeState()))
    row = db.get_invoice(inv["id"])
    assert row["created_by_telegram_id"] == 111
    assert row["payer_telegram_id"] == 555
    assert row["created_by_telegram_id"] != row["payer_telegram_id"]


def test_successful_payment_duplicate_delivery_is_safe_noop(db, handlers):
    inv = _paid_ready_invoice(db)
    sp = FakeSuccessfulPayment(str(inv["id"]), charge_id="dup-1")
    bot = FakeBot()

    class FakeState:
        pass

    msg1 = FakeSuccessfulPaymentMessage(sp, payer_id=999, bot=bot)
    asyncio.run(handlers["successful_payment"](msg1, FakeState()))
    msg2 = FakeSuccessfulPaymentMessage(sp, payer_id=999, bot=bot)
    asyncio.run(handlers["successful_payment"](msg2, FakeState()))  # must not raise / double-apply

    row = db.get_invoice(inv["id"])
    assert row["status"] == "paid"
    # Only the first delivery produced a user-facing "payment received" reply.
    assert msg1.answers == ["Оплата получена! Продлеваем подписку…"]
    assert msg2.answers == []


def test_successful_payment_amount_mismatch_routes_manual_review_not_dropped(db, handlers):
    """Money already moved by this point — a mismatch must never be
    silently dropped, it must land in manual_review."""
    inv = _paid_ready_invoice(db)
    sp = FakeSuccessfulPayment(str(inv["id"]), total_amount=1)  # wrong amount
    msg = FakeSuccessfulPaymentMessage(sp, payer_id=999, bot=FakeBot())

    class FakeState:
        pass

    asyncio.run(handlers["successful_payment"](msg, FakeState()))
    row = db.get_invoice(inv["id"])
    assert row["status"] == "manual_review"
    assert row["payer_telegram_id"] == 999  # payment fields still recorded


def test_successful_payment_unknown_invoice_id_never_crashes(db, handlers):
    sp = FakeSuccessfulPayment("999999999")
    msg = FakeSuccessfulPaymentMessage(sp, payer_id=999, bot=FakeBot())

    class FakeState:
        pass

    asyncio.run(handlers["successful_payment"](msg, FakeState()))  # must not raise
    events = db.get_audit_log(event_type="payment_failed")
    assert len(events) == 1
    assert events[0]["metadata"]["reason"] == "invoice_not_found"


# --- no-tariffs / disabled-by-default UX (§8.3/§9) -------------------------

def test_stars_menu_shows_unavailable_when_disabled(db):
    from aiogram import Dispatcher
    from src.bot_support import setup_support_handlers

    dp = Dispatcher()
    setup_support_handlers(dp, db, marzban=None)
    db.save_tg_user(111, "alice")
    # stars:enabled defaults absent -> off

    class FakeUser:
        id = 111

    class FakeMessage:
        from_user = FakeUser()

        def __init__(self):
            self.sent = []

        async def answer(self, text, **kw):
            self.sent.append(text)

    class FakeState:
        async def set_state(self, s):
            pass

    handler = _get_handler(dp.message, "msg_stars_menu")
    msg = FakeMessage()
    asyncio.run(handler(msg, FakeState()))
    assert "недоступн" in msg.sent[0]


class FakeMarzbanForMenu:
    def __init__(self, user):
        self._user = user

    def get_admin_token_from_env(self):
        return "tok"

    def get_user(self, username, admin_token):
        return dict(self._user)


def test_stars_menu_offers_tariffs_when_eligible(db):
    from aiogram import Dispatcher
    from src.bot_support import setup_support_handlers

    dp = Dispatcher()
    marzban = FakeMarzbanForMenu({"username": "alice", "expire": int(time.time()) + 1000, "status": "active"})
    setup_support_handlers(dp, db, marzban=marzban)
    db.save_tg_user(111, "alice")
    db.set_setting("stars:enabled", "1")
    db.save_stars_tariff({"name": "1 месяц", "duration_days": 30, "stars_price": 320})

    class FakeUser:
        id = 111

    class FakeMessage:
        from_user = FakeUser()

        def __init__(self):
            self.sent = []

        async def answer(self, text, reply_markup=None, **kw):
            self.sent.append((text, reply_markup))

    class FakeState:
        async def set_state(self, s):
            pass

    handler = _get_handler(dp.message, "msg_stars_menu")
    msg = FakeMessage()
    asyncio.run(handler(msg, FakeState()))
    text, markup = msg.sent[0]
    assert markup is not None
    assert "320" in markup.inline_keyboard[0][0].text


def test_stars_menu_blocks_unlimited_account(db):
    from aiogram import Dispatcher
    from src.bot_support import setup_support_handlers

    dp = Dispatcher()
    marzban = FakeMarzbanForMenu({"username": "alice", "expire": 0, "status": "active"})
    setup_support_handlers(dp, db, marzban=marzban)
    db.save_tg_user(111, "alice")
    db.set_setting("stars:enabled", "1")
    db.save_stars_tariff({"name": "1 месяц", "duration_days": 30, "stars_price": 320})

    class FakeUser:
        id = 111

    class FakeMessage:
        from_user = FakeUser()

        def __init__(self):
            self.sent = []

        async def answer(self, text, **kw):
            self.sent.append(text)

    class FakeState:
        async def set_state(self, s):
            pass

    handler = _get_handler(dp.message, "msg_stars_menu")
    msg = FakeMessage()
    asyncio.run(handler(msg, FakeState()))
    assert "безлимит" in msg.sent[0]


def test_invoice_creation_callback_defense_in_depth_blocks_stale_unlimited(db):
    """Menu render happened while eligible; by the time of the tariff-tap
    callback the account has (hypothetically) become unlimited — the
    invoice-creation callback must independently re-check and refuse, per
    §2/§8 step 4 defense-in-depth. No stars_invoices row must be created."""
    from aiogram import Dispatcher
    from src.bot_support import setup_support_handlers

    dp = Dispatcher()
    marzban = FakeMarzbanForMenu({"username": "alice", "expire": 0, "status": "active"})
    setup_support_handlers(dp, db, marzban=marzban)
    db.save_tg_user(111, "alice")
    db.set_setting("stars:enabled", "1")
    tariff = db.save_stars_tariff({"name": "1 месяц", "duration_days": 30, "stars_price": 320})

    class FakeUser:
        id = 111

    class FakeMsg:
        def __init__(self):
            self.sent = []

        async def answer(self, text, **kw):
            self.sent.append(text)

        class bot:
            @staticmethod
            async def send_invoice(**kw):
                raise AssertionError("send_invoice must not be called for an ineligible account")

    class FakeCall:
        from_user = FakeUser()
        data = f"stars_buy:{tariff['id']}"
        message = FakeMsg()

        async def answer(self):
            pass

    class FakeState:
        pass

    handler = _get_handler(dp.callback_query, "cb_stars_buy")
    call = FakeCall()
    asyncio.run(handler(call, FakeState()))
    assert db.list_stars_invoices() == []
    assert "безлимит" in call.message.sent[-1]


def test_stars_menu_shows_unavailable_when_no_tariffs_even_if_enabled(db):
    from aiogram import Dispatcher
    from src.bot_support import setup_support_handlers

    dp = Dispatcher()
    setup_support_handlers(dp, db, marzban=None)
    db.save_tg_user(111, "alice")
    db.set_setting("stars:enabled", "1")
    # no tariffs created — table starts empty per §9

    class FakeUser:
        id = 111

    class FakeMessage:
        from_user = FakeUser()

        def __init__(self):
            self.sent = []

        async def answer(self, text, **kw):
            self.sent.append(text)

    class FakeState:
        async def set_state(self, s):
            pass

    handler = _get_handler(dp.message, "msg_stars_menu")
    msg = FakeMessage()
    asyncio.run(handler(msg, FakeState()))
    assert "недоступн" in msg.sent[0]
