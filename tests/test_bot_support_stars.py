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
    class HealthyMarzban:
        def get_admin_token_from_env(self):
            return "service"

        def get_user(self, username, _token):
            return {"username": username, "expire": int(time.time()) + 86400, "status": "active"}

    setup_support_handlers(dp, db, marzban=HealthyMarzban(), stars_trigger=trigger)
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


@pytest.mark.parametrize("payload", ["1.5", "+1", "01", "not-numeric"])
def test_pre_checkout_rejects_noncanonical_numeric_payload(db, handlers, payload):
    q = FakePreCheckoutQuery(payload, "XTR", 320)
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


def test_pre_checkout_ttl_boundary_rejects_exact_expiry(db, handlers, monkeypatch):
    inv = _paid_ready_invoice(db)
    import src.bot_support as bs
    monkeypatch.setattr(bs.time, "time", lambda: inv["expires_at"])
    q = FakePreCheckoutQuery(str(inv["id"]), "XTR", 320)
    asyncio.run(handlers["pre_checkout"](q))
    assert q.answers[0][0] is False
    assert "истёк" in q.answers[0][1]


def _payment_handlers_for_marzban(db, marzban):
    from aiogram import Dispatcher
    from src.bot_support import setup_support_handlers

    dp = Dispatcher()
    trigger = asyncio.Event()
    setup_support_handlers(dp, db, marzban=marzban, stars_trigger=trigger)
    return {
        "pre_checkout": _get_handler(dp.pre_checkout_query, "on_pre_checkout"),
        "successful_payment": _get_handler(dp.message, "on_successful_payment"),
        "trigger": trigger,
    }


def test_pre_checkout_rejects_when_broker_or_marzban_is_unavailable(db):
    class UnavailableMarzban:
        def get_admin_token_from_env(self):
            return "broker"

        def get_user(self, _username, _token):
            raise ConnectionError("broker unavailable")

    handlers = _payment_handlers_for_marzban(db, UnavailableMarzban())
    inv = _paid_ready_invoice(db)
    q = FakePreCheckoutQuery(str(inv["id"]), "XTR", 320)

    asyncio.run(handlers["pre_checkout"](q))

    assert q.answers[0][0] is False
    assert "временно недоступен" in q.answers[0][1]
    assert db.get_invoice(inv["id"])["status"] == "created"


def test_pre_checkout_rejects_when_entitlement_became_ineligible(db):
    class DisabledMarzban:
        def get_admin_token_from_env(self):
            return "broker"

        def get_user(self, username, _token):
            return {"username": username, "expire": int(time.time()) + 1000, "status": "disabled"}

    handlers = _payment_handlers_for_marzban(db, DisabledMarzban())
    inv = _paid_ready_invoice(db)
    q = FakePreCheckoutQuery(str(inv["id"]), "XTR", 320)

    asyncio.run(handlers["pre_checkout"](q))

    assert q.answers[0][0] is False
    assert "недоступна для продления" in q.answers[0][1]


def test_successful_payment_remains_durable_if_broker_fails_after_checkout(db):
    class DownAfterCheckout:
        def get_admin_token_from_env(self):
            raise ConnectionError("broker failed after Telegram accepted checkout")

    handlers = _payment_handlers_for_marzban(db, DownAfterCheckout())
    inv = _paid_ready_invoice(db)
    payment = FakeSuccessfulPayment(str(inv["id"]), charge_id="durable-during-outage")
    message = FakeSuccessfulPaymentMessage(payment, payer_id=111, bot=FakeBot())

    asyncio.run(handlers["successful_payment"](message, object()))

    row = db.get_invoice(inv["id"])
    assert row["status"] == "paid"
    assert row["telegram_payment_charge_id"] == "durable-during-outage"
    assert handlers["trigger"].is_set()


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
    msg = FakeSuccessfulPaymentMessage(sp, payer_id=111, bot=FakeBot())

    class FakeState:
        pass

    asyncio.run(handlers["successful_payment"](msg, FakeState()))
    row = db.get_invoice(inv["id"])
    assert row["status"] == "paid"
    assert row["payer_telegram_id"] == 111
    assert row["created_by_telegram_id"] == 111  # unchanged, still the creator
    assert handlers["trigger"].is_set()


def test_successful_payment_unexpected_payer_routes_manual_review(db, handlers):
    """Forwarded/gift invoice payment is not an MVP flow. If Telegram ever
    reports a payer other than the private-chat invoice creator, preserve
    trusted payer data but do not auto-apply."""
    inv = _paid_ready_invoice(db, created_by_telegram_id=111)
    sp = FakeSuccessfulPayment(str(inv["id"]))
    msg = FakeSuccessfulPaymentMessage(sp, payer_id=555, bot=FakeBot())

    class FakeState:
        pass

    asyncio.run(handlers["successful_payment"](msg, FakeState()))
    row = db.get_invoice(inv["id"])
    assert row["status"] == "manual_review"
    assert row["manual_review_reason"] == "unexpected_payer_for_single_chat_invoice"
    assert row["created_by_telegram_id"] == 111
    assert row["payer_telegram_id"] == 555
    assert row["created_by_telegram_id"] != row["payer_telegram_id"]


def test_successful_payment_duplicate_delivery_is_safe_noop(db, handlers):
    inv = _paid_ready_invoice(db)
    sp = FakeSuccessfulPayment(str(inv["id"]), charge_id="dup-1")
    bot = FakeBot()

    class FakeState:
        pass

    msg1 = FakeSuccessfulPaymentMessage(sp, payer_id=111, bot=bot)
    asyncio.run(handlers["successful_payment"](msg1, FakeState()))
    msg2 = FakeSuccessfulPaymentMessage(sp, payer_id=111, bot=bot)
    asyncio.run(handlers["successful_payment"](msg2, FakeState()))  # must not raise / double-apply

    row = db.get_invoice(inv["id"])
    assert row["status"] == "paid"
    # Only the first delivery produced a user-facing "payment received" reply.
    assert msg1.answers == ["Оплата получена! Продлеваем подписку…"]
    assert msg2.answers == []


def test_same_invoice_second_distinct_charge_is_orphan_not_second_payment(db, handlers):
    inv = _paid_ready_invoice(db, created_by_telegram_id=111)

    class FakeState:
        pass

    first = FakeSuccessfulPayment(str(inv["id"]), charge_id="single-chat-charge-1")
    second = FakeSuccessfulPayment(str(inv["id"]), charge_id="single-chat-charge-2")
    asyncio.run(handlers["successful_payment"](
        FakeSuccessfulPaymentMessage(first, payer_id=111, bot=FakeBot()), FakeState()
    ))
    asyncio.run(handlers["successful_payment"](
        FakeSuccessfulPaymentMessage(second, payer_id=111, bot=FakeBot()), FakeState()
    ))

    row = db.get_invoice(inv["id"])
    assert row["telegram_payment_charge_id"] == "single-chat-charge-1"
    orphans = db.list_stars_orphan_payments()
    assert len(orphans) == 1
    assert orphans[0]["telegram_payment_charge_id"] == "single-chat-charge-2"
    assert orphans[0]["reason"] == "invoice_not_payable:paid"


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
    orphans = db.list_stars_orphan_payments()
    assert len(orphans) == 1
    assert orphans[0]["telegram_payment_charge_id"] == "charge-1"


def test_successful_payment_oversized_numeric_payload_is_orphan(db, handlers):
    payload = "9999999999999999999"
    sp = FakeSuccessfulPayment(payload, charge_id="oversized-charge")
    msg = FakeSuccessfulPaymentMessage(sp, payer_id=777, bot=FakeBot())

    asyncio.run(handlers["successful_payment"](msg, object()))

    rows = db.list_stars_orphan_payments()
    assert len(rows) == 1
    assert rows[0]["telegram_payment_charge_id"] == "oversized-charge"
    assert rows[0]["invoice_payload"] == payload
    assert rows[0]["reason"] == "invalid_invoice_payload"


def test_successful_payment_sqlite_max_id_is_safe_normal_lookup(db, handlers):
    payload = str((1 << 63) - 1)
    sp = FakeSuccessfulPayment(payload, charge_id="sqlite-max-charge")

    asyncio.run(handlers["successful_payment"](
        FakeSuccessfulPaymentMessage(sp, payer_id=777, bot=FakeBot()), object()
    ))

    row = db.list_stars_orphan_payments()[0]
    assert row["reason"] == "invoice_not_found"
    assert row["invoice_payload"] == payload


def test_successful_payment_sqlite_max_plus_one_is_orphan_without_lookup(
    db, handlers, monkeypatch
):
    payload = str(1 << 63)
    original_get_invoice = db.get_invoice

    def forbidden_lookup(invoice_id):
        raise AssertionError(f"unsafe SQLite lookup: {invoice_id}")

    monkeypatch.setattr(db, "get_invoice", forbidden_lookup)
    sp = FakeSuccessfulPayment(payload, charge_id="sqlite-overflow-charge")
    asyncio.run(handlers["successful_payment"](
        FakeSuccessfulPaymentMessage(sp, payer_id=777, bot=FakeBot()), object()
    ))
    monkeypatch.setattr(db, "get_invoice", original_get_invoice)

    row = db.list_stars_orphan_payments()[0]
    assert row["reason"] == "invalid_invoice_payload"


def test_successful_payment_db_failure_fails_loudly_and_retry_is_durable(
    db, handlers, monkeypatch
):
    from src.bot_support import PaymentDurabilityError

    inv = _paid_ready_invoice(db)
    original_get_invoice = db.get_invoice
    calls = 0

    def fail_once(invoice_id):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("temporary storage failure")
        return original_get_invoice(invoice_id)

    monkeypatch.setattr(db, "get_invoice", fail_once)
    sp = FakeSuccessfulPayment(str(inv["id"]), charge_id="retry-after-storage")
    message = FakeSuccessfulPaymentMessage(sp, payer_id=111, bot=FakeBot())

    with pytest.raises(PaymentDurabilityError):
        asyncio.run(handlers["successful_payment"](message, object()))
    assert db.get_invoice(inv["id"])["status"] == "created"
    assert db.list_stars_orphan_payments() == []

    asyncio.run(handlers["successful_payment"](message, object()))
    asyncio.run(handlers["successful_payment"](message, object()))
    row = db.get_invoice(inv["id"])
    assert row["status"] == "paid"
    assert row["telegram_payment_charge_id"] == "retry-after-storage"
    assert len(db.list_stars_invoices()) == 1


@pytest.mark.parametrize("payload", ["not-an-invoice", "1.5", "+1", "01"])
def test_successful_payment_invalid_payload_and_duplicate_are_one_orphan(db, handlers, payload):
    sp = FakeSuccessfulPayment(payload, charge_id=f"orphan-invalid-{payload}")
    bot = FakeBot()

    class FakeState:
        pass

    asyncio.run(handlers["successful_payment"](
        FakeSuccessfulPaymentMessage(sp, payer_id=777, bot=bot), FakeState()
    ))
    asyncio.run(handlers["successful_payment"](
        FakeSuccessfulPaymentMessage(sp, payer_id=777, bot=bot), FakeState()
    ))
    rows = db.list_stars_orphan_payments()
    assert len(rows) == 1
    assert rows[0]["reason"] == "invalid_invoice_payload"
    assert rows[0]["payer_telegram_id"] == 777
    assert rows[0]["invoice_payload"] == payload


class StubTelegramSession:
    """Small BaseSession-compatible factory used by dispatcher-level tests."""

    @staticmethod
    def make():
        from aiogram.client.session.base import BaseSession

        class Session(BaseSession):
            def __init__(self):
                super().__init__()
                self.methods = []

            async def close(self):
                pass

            async def make_request(self, bot, method, timeout=None):
                self.methods.append(method)
                return True

            async def stream_content(self, url, headers=None, timeout=30,
                                     chunk_size=65536, raise_for_status=True):
                if False:
                    yield b""

        return Session()


def _successful_payment_update(invoice_id, update_id=1, charge_id="dispatcher-charge"):
    from datetime import datetime, timezone
    from aiogram.types import Chat, Message, SuccessfulPayment, Update, User

    payment = SuccessfulPayment(
        currency="XTR",
        total_amount=320,
        invoice_payload=str(invoice_id),
        telegram_payment_charge_id=charge_id,
        provider_payment_charge_id="",
    )
    message = Message(
        message_id=10 + update_id,
        date=datetime.now(timezone.utc),
        chat=Chat(id=111, type="private"),
        from_user=User(id=111, is_bot=False, first_name="Payer"),
        successful_payment=payment,
    )
    return Update(update_id=update_id, message=message)


@pytest.mark.parametrize("fsm_state", [None, "SupportStates:in_dialog", "Other:any_state"])
def test_dispatcher_routes_successful_payment_before_fsm_fallback_after_restart(db, fsm_state):
    """The invoice predates this fresh Dispatcher/MemoryStorage instance."""
    from aiogram import Bot, Dispatcher
    from aiogram.fsm.storage.memory import MemoryStorage
    from src.bot_support import setup_support_handlers

    inv = _paid_ready_invoice(db, created_by_telegram_id=111)

    async def scenario():
        session = StubTelegramSession.make()
        bot = Bot("123456:abcdefghijklmnopqrstuvwxyzABCDE", session=session)
        dp = Dispatcher(storage=MemoryStorage())
        trigger = asyncio.Event()
        setup_support_handlers(dp, db, marzban=None, stars_trigger=trigger)
        if fsm_state is not None:
            context = dp.fsm.get_context(bot=bot, chat_id=111, user_id=111)
            await context.set_state(fsm_state)
        await dp.feed_update(bot, _successful_payment_update(inv["id"]))
        await dp.storage.close()
        await bot.session.close()
        return session.methods, trigger.is_set()

    methods, triggered = asyncio.run(scenario())
    row = db.get_invoice(inv["id"])
    assert row["status"] == "paid"
    assert row["telegram_payment_charge_id"] == "dispatcher-charge"
    assert row["payer_telegram_id"] == 111
    assert triggered is True
    sent_texts = [getattr(method, "text", "") for method in methods]
    assert sent_texts == ["Оплата получена! Продлеваем подписку…"]
    assert "Чем могу помочь?" not in sent_texts


def test_dispatcher_duplicate_successful_payment_is_not_rehandled_as_no_state(db):
    from aiogram import Bot, Dispatcher
    from aiogram.fsm.storage.memory import MemoryStorage
    from src.bot_support import setup_support_handlers

    inv = _paid_ready_invoice(db, created_by_telegram_id=111)

    async def scenario():
        session = StubTelegramSession.make()
        bot = Bot("123456:abcdefghijklmnopqrstuvwxyzABCDE", session=session)
        dp = Dispatcher(storage=MemoryStorage())
        setup_support_handlers(dp, db, marzban=None)
        await dp.feed_update(bot, _successful_payment_update(inv["id"], update_id=1))
        await dp.feed_update(bot, _successful_payment_update(inv["id"], update_id=2))
        await dp.storage.close()
        await bot.session.close()
        return session.methods

    methods = asyncio.run(scenario())
    assert db.get_invoice(inv["id"])["status"] == "paid"
    assert len([m for m in methods if getattr(m, "text", None)]) == 1


def test_send_invoice_uses_unique_single_chat_start_parameter(db):
    from aiogram import Dispatcher
    from src.bot_support import setup_support_handlers

    marzban = FakeMarzbanForMenu({
        "username": "alice", "expire": int(time.time()) + 1000, "status": "active"
    })
    dp = Dispatcher()
    setup_support_handlers(dp, db, marzban=marzban)
    db.save_tg_user(111, "alice")
    db.set_setting("stars:enabled", "1")
    tariff = db.save_stars_tariff({
        "name": "month", "duration_days": 30, "stars_price": 320
    })

    class InvoiceBot:
        def __init__(self):
            self.calls = []

        async def send_invoice(self, **kwargs):
            self.calls.append(kwargs)

    invoice_bot = InvoiceBot()

    class Msg:
        bot = invoice_bot

        async def answer(self, text, **kwargs):
            pass

    class Call:
        from_user = FakeFromUser(111)
        data = f"stars_buy:{tariff['id']}"
        message = Msg()

        async def answer(self):
            pass

    handler = _get_handler(dp.callback_query, "cb_stars_buy")
    asyncio.run(handler(Call(), object()))
    kwargs = invoice_bot.calls[0]
    invoice = db.list_stars_invoices()[0]
    assert kwargs["payload"] == str(invoice["id"])
    assert kwargs["start_parameter"] == f"stars_invoice_{invoice['id']}"
    assert kwargs["start_parameter"]
    assert len(kwargs["start_parameter"]) <= 64
    assert all(ch.isalnum() or ch in "_-" for ch in kwargs["start_parameter"])


def test_forwarded_invoice_deep_link_never_rebinds_or_reuses_invoice(db):
    from aiogram import Dispatcher
    from src.bot_support import setup_support_handlers

    db.save_tg_user(111, "alice")
    inv = _paid_ready_invoice(db, created_by_telegram_id=111)
    dp = Dispatcher()
    setup_support_handlers(dp, db, marzban=None)
    handler = _get_handler(dp.message, "cmd_start")

    class State:
        async def clear(self):
            pass

        async def set_state(self, value):
            pass

    class Msg:
        text = f"/start stars_invoice_{inv['id']}"
        from_user = FakeFromUser(111)

        def __init__(self):
            self.answers = []

        async def answer(self, text, **kwargs):
            self.answers.append(text)

    msg = Msg()
    asyncio.run(handler(msg, State()))
    assert "переслан" in msg.answers[0].lower()
    assert db.get_tg_user(111)["marzban_username"] == "alice"
    assert len(db.list_stars_invoices()) == 1


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
