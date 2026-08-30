"""Bot UX redesign (slices C0-C7): единая карточка подписки, unified buy
funnel (покупка = продление), второй guard против тихой второй подписки для
legacy-клиентов, настоящий Back в воронке, экран смены тарифа через
поддержку, сброс FSM при закрытии тикета."""

import asyncio
import importlib
import os
import sys
import tempfile
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture
def db(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="bot-ux-")
    monkeypatch.setenv("DATA_DIR", tmp)
    monkeypatch.setenv("PUBLIC_HOST", "sub.beykus.fun")
    import src.config as config
    import src.database as database
    importlib.reload(config)
    importlib.reload(database)
    database.DB_PATH = os.path.join(tmp, "db.sqlite3")
    instance = database.Database()
    from src.plan_catalog import seed_plan_catalog
    seed_plan_catalog(instance.plan_catalog, now=1)
    yield instance
    instance._conn.close()


def _seed_catalog(db):
    from src.plan_catalog import seed_plan_catalog
    seed_plan_catalog(db.plan_catalog, now=1)


def _canonical_owner(db, telegram_id):
    account = db.accounts.create_account("DIRECT", now=1)
    db.accounts.link_telegram_owner(
        account["id"], telegram_id, provenance="DIRECT_BIND", actor="test", now=1,
    )
    return account


def _grant(db, account, plan_code, key):
    db.subscription_renewal.apply_same_plan_purchase(
        account_id=account["id"], plan_code=plan_code, duration_days=30,
        payment_channel="TELEGRAM_STARS", mutation_source="DIRECT_PURCHASE",
        actor_type="TEST", idempotency_key=key, now=int(time.time()) - 3600,
    )


# --- read model --------------------------------------------------------------

def test_read_model_canonical_card_composes_plan_devices_wl(db):
    from src.entitlement_read_model import build_subscription_card
    account = _canonical_owner(db, 424001)
    _grant(db, account, "WL", "ux-read-model-wl-0001")

    card = build_subscription_card(db, telegram_id=424001)
    assert card["cohort"] == "canonical"
    assert card["status"] == "ACTIVE"
    assert card["plan_name"] == "WL"
    assert card["plan_code"] == "WL"
    assert card["devices"]["limit"] == 3
    assert card["devices"]["active"] == 0
    assert card["wl"] is not None and card["wl"]["mode"] == "LIMITED"
    assert card["wl"]["quota_bytes"] == 100 * 1_000_000_000
    assert card["has_active_credential"] is False


def test_read_model_legacy_fallback_and_unknown(db):
    from src.entitlement_read_model import build_subscription_card

    assert build_subscription_card(db, telegram_id=424002) is None

    db.save_tg_user(424002, "legacyuser")
    card = build_subscription_card(
        db, telegram_id=424002,
        legacy_user=db.get_tg_user(424002),
        legacy_marzban_user={"username": "legacyuser", "status": "active",
                             "expire": int(time.time()) + 86400,
                             "used_traffic": 1_000_000, "data_limit": 0},
    )
    assert card["cohort"] == "legacy"
    assert card["status"] == "active"
    assert card["traffic_limit"] == 0
    assert card["devices"]["active"] == 0


# --- card rendering ------------------------------------------------------------

def test_card_renders_status_expiry_devices_wl(db):
    from src.bot_support import render_subscription_card
    now = int(time.time())
    expiry = now + 3 * 86400
    card = {
        "cohort": "canonical", "status": "ACTIVE", "plan_name": "Базовый Плюс",
        "plan_code": "BASIC_PLUS", "unlimited": False, "expiry": expiry,
        "wl": {"mode": "LIMITED", "quota_bytes": 100 * 1_000_000_000,
               "consumed_bytes": 42 * 1_000_000_000,
               "period_ends_at": now + 12 * 86400},
        "devices": {"mode": "LIMITED", "limit": 6, "active": 3},
    }
    text = render_subscription_card(card, now=now)
    assert "🟢 Подписка активна" in text
    assert "Тариф: Базовый Плюс · до 6 устройств" in text
    assert "Действует до:" in text and "осталось 3 дн." in text
    assert "Устройства: 3 из 6 активных" in text
    assert "WL: 42 / 100 GB · текущий период до" in text
    # owner wording rule: the WL boundary is a period end, never a "сброс"
    assert "сброс" not in text


def test_card_renders_expired_and_empty_states(db):
    from src.bot_support import render_subscription_card
    now = int(time.time())
    expired = {"cohort": "canonical", "status": "EXPIRED", "plan_name": "Базовый",
               "unlimited": False, "expiry": now - 86400, "wl": None,
               "devices": {"mode": "LIMITED", "limit": 3, "active": 0}}
    assert "🔴 Подписка истекла" in render_subscription_card(expired, now=now)

    empty = {"cohort": "canonical", "status": "NONE", "plan_name": None,
             "unlimited": False, "expiry": None, "wl": None,
             "devices": {"mode": "NONE", "limit": 0, "active": 0}}
    text = render_subscription_card(empty, now=now)
    assert "Подписки пока нет" in text
    assert "🛒 Купить / Продлить" in text


# --- dispatcher-level flows ----------------------------------------------------

class StubTelegramSession:
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


def _text_update(telegram_id, text, update_id=1):
    from datetime import datetime, timezone
    from aiogram.types import Chat, Message, Update, User
    message = Message(
        message_id=300 + update_id,
        date=datetime.now(timezone.utc),
        chat=Chat(id=telegram_id, type="private"),
        from_user=User(id=telegram_id, is_bot=False, first_name="U"),
        text=text,
    )
    return Update(update_id=update_id, message=message)


def _feed(db, updates, telegram_id, initial_state=None):
    from aiogram import Bot, Dispatcher
    from aiogram.fsm.storage.memory import MemoryStorage
    from src.bot_support import setup_support_handlers

    async def scenario():
        session = StubTelegramSession.make()
        bot = Bot("123456:abcdefghijklmnopqrstuvwxyzABCDE", session=session)
        dp = Dispatcher(storage=MemoryStorage())
        setup_support_handlers(dp, db, marzban=None)
        context = dp.fsm.get_context(bot=bot, chat_id=telegram_id, user_id=telegram_id)
        if initial_state is not None:
            await context.set_state(initial_state)
        for index, upd in enumerate(updates, start=1):
            await dp.feed_update(bot, upd)
        state = await context.get_state()
        await dp.storage.close()
        await bot.session.close()
        return session.methods, state

    return asyncio.run(scenario())


def _texts(methods):
    return [getattr(m, "text", "") or "" for m in methods]


def test_my_subscription_card_has_link_renew_devices_buttons(db):
    account = _canonical_owner(db, 424003)
    _grant(db, account, "BASIC", "ux-card-basic-0000001")
    methods, _state = _feed(
        db, [_text_update(424003, "📱 Моя подписка")], 424003,
        initial_state="SupportStates:in_dialog",
    )
    texts = _texts(methods)
    assert any("Тариф: Базовый" in t for t in texts)
    assert any("активна" in t for t in texts)
    card_message = next(
        m for m in methods if getattr(m, "reply_markup", None) is not None
        and getattr(m.reply_markup, "inline_keyboard", None) is not None
    )
    data = [btn.callback_data for row in card_message.reply_markup.inline_keyboard
            for btn in row]
    assert "sub_link" in data
    assert "sub_renew" in data
    assert "sub_devices" in data
    assert "sub_refresh" in data


def test_buy_entry_for_canonical_shows_only_current_plan_and_change_plan(db):
    _canonical_owner(db, 424004)
    db.set_setting("stars:enabled", "1")
    account = db.accounts.get_active_account_by_telegram_id(424004)
    _grant(db, account, "BASIC", "ux-buy-basic-00000001")
    methods, _state = _feed(db, [_text_update(424004, "🛒 Купить / Продлить")], 424004)
    text = "".join(_texts(methods))
    assert "Ваш тариф: Базовый" in text
    markup = next(m for m in methods if getattr(m, "reply_markup", None) is not None)
    data = [btn.callback_data for row in markup.reply_markup.inline_keyboard
            for btn in row]
    assert sorted(d for d in data if d.startswith("buy_dur:")) == [
        "buy_dur:BASIC:30", "buy_dur:BASIC:60",
    ]
    assert "change_plan" in data
    # чужие тарифы не показываются как покупаемые (PH5-06)
    assert not any(d.startswith("buy_plan:") for d in data)


def test_change_plan_screen_routes_to_support(db):
    from aiogram import Dispatcher
    from src.bot_support import setup_support_handlers

    db.set_setting("stars:enabled", "1")
    dp = Dispatcher()
    setup_support_handlers(dp, db, marzban=None)

    def handler(name):
        return next(h.callback for h in dp.callback_query.handlers
                    if h.callback.__name__ == name)

    class Msg:
        async def answer(self, text, **kw):
            self.answered = (text, kw)

        async def edit_text(self, text, reply_markup=None, **kw):
            self.edited = (text, reply_markup)

    class Call:
        from_user = type("U", (), {"id": 424005})()
        data = "change_plan"
        message = Msg()

        async def answer(self):
            pass

    call = Call()
    asyncio.run(handler("cb_change_plan")(call))
    text, markup = call.message.edited
    assert "через поддержку" in text
    labels = [btn.callback_data for row in markup.inline_keyboard for btn in row]
    assert "call_human" in labels
    assert "buy_back_plan" in labels


def test_legacy_customer_buy_is_blocked_not_second_signup(db):
    """Owner rule: legacy-linked customer without a canonical account must
    never silently receive a SECOND paid subscription (no CANONICAL_SIGNUP
    invoice), neither from the entry nor from a stale pay callback."""
    db.save_tg_user(424006, "legacyuser")
    db.set_setting("stars:enabled", "1")
    methods, _state = _feed(db, [_text_update(424006, "🛒 Купить / Продлить")], 424006)
    assert any("уже есть подписка" in t for t in _texts(methods))

    from aiogram import Dispatcher
    from src.bot_support import setup_support_handlers
    dp = Dispatcher()
    setup_support_handlers(dp, db, marzban=None)
    pay = next(h.callback for h in dp.callback_query.handlers
               if h.callback.__name__ == "cb_buy_pay")

    class Msg:
        def __init__(self):
            self.answers = []

        async def answer(self, text, **kw):
            self.answers.append(text)

        async def edit_text(self, *a, **kw):
            pass

    class Bot:
        async def send_invoice(self, **kw):
            raise AssertionError("invoice must not be created for a legacy customer")

    class Call:
        from_user = type("U", (), {"id": 424006})()
        data = "buy_pay:BASIC:30"
        message = Msg()
        message.bot = Bot()

        async def answer(self):
            pass

    class State:
        async def get_data(self):
            return {}

    call = Call()
    asyncio.run(pay(call, State()))
    assert any("уже есть подписка" in t for t in call.message.answers)
    assert db._conn.execute("SELECT COUNT(*) c FROM stars_invoices").fetchone()["c"] == 0


def test_legacy_stars_callback_is_acknowledged_and_never_creates_invoice(db):
    """A button from the retired ``stars_buy`` graph remains in Telegram
    history. It must stop the callback spinner and preserve the legacy
    second-signup guard instead of becoming an unhandled callback."""
    from aiogram import Dispatcher
    from src.bot_support import setup_support_handlers

    db.save_tg_user(4240061, "legacyuser")
    db.set_setting("stars:enabled", "1")
    dp = Dispatcher()
    setup_support_handlers(dp, db, marzban=None)
    handler = next(h.callback for h in dp.callback_query.handlers
                   if h.callback.__name__ == "cb_legacy_stars_buy")

    class Msg:
        def __init__(self):
            self.answers = []

        async def answer(self, text, **kw):
            self.answers.append(text)

    class Call:
        from_user = type("U", (), {"id": 4240061})()
        data = "stars_buy:BASIC:30"
        message = Msg()

        def __init__(self):
            self.answered = False

        async def answer(self):
            self.answered = True

    call = Call()
    asyncio.run(handler(call, None))
    assert call.answered is True
    assert any("уже есть подписка" in text for text in call.message.answers)
    assert db._conn.execute("SELECT COUNT(*) FROM stars_invoices").fetchone()[0] == 0


def test_stars_invoice_primitive_blocks_legacy_second_signup(db):
    """The invariant also holds if a caller bypasses Telegram handlers."""
    from src.stars_purchase import StarsPurchaseError

    db.save_tg_user(4240062, "legacyuser")
    with pytest.raises(StarsPurchaseError, match="legacy-linked"):
        db.stars_purchases.create_invoice(
            telegram_id=4240062, plan_code="BASIC", duration_days=30,
            ttl_seconds=3600,
        )


def test_signup_pre_checkout_rejects_invalid_public_host_before_capture(db, monkeypatch):
    """A pre-existing signup invoice must still be refused before Telegram
    captures Stars when a deploy has an invalid PUBLIC_HOST."""
    from aiogram import Dispatcher
    import src.config as config
    from src.bot_support import setup_support_handlers

    db.set_setting("stars:enabled", "1")
    invoice = db.stars_purchases.create_invoice(
        telegram_id=4240063, plan_code="BASIC", duration_days=30, ttl_seconds=3600,
    )
    monkeypatch.setattr(config, "PUBLIC_HOST", "https://sub.beykus.fun")
    dp = Dispatcher()
    setup_support_handlers(dp, db, marzban=None)
    handler = next(h.callback for h in dp.pre_checkout_query.handlers
                   if h.callback.__name__ == "on_pre_checkout")

    class Query:
        invoice_payload = str(invoice["id"])
        currency = "XTR"
        total_amount = invoice["stars_price"]
        from_user = type("U", (), {"id": 4240063})()

        def __init__(self):
            self.answers = []

        async def answer(self, **kw):
            self.answers.append(kw)

    query = Query()
    asyncio.run(handler(query))
    assert query.answers == [{"ok": False, "error_message": "Покупка временно недоступна. Попробуйте позже."}]
    assert db.get_invoice(invoice["id"])["status"] == "created"


def test_subscription_refresh_falls_back_to_new_message_when_edit_fails(db):
    from aiogram import Dispatcher
    from src.bot_support import setup_support_handlers

    account = _canonical_owner(db, 4240064)
    _grant(db, account, "BASIC", "ux-refresh-fallback-0001")
    dp = Dispatcher()
    setup_support_handlers(dp, db, marzban=None)
    handler = next(h.callback for h in dp.callback_query.handlers
                   if h.callback.__name__ == "cb_sub_refresh")

    class Msg:
        def __init__(self):
            self.answers = []

        async def edit_text(self, *args, **kwargs):
            raise RuntimeError("message cannot be edited")

        async def answer(self, text, **kw):
            self.answers.append(text)

    class Call:
        from_user = type("U", (), {"id": 4240064})()
        message = Msg()

        def __init__(self):
            self.answered = False

        async def answer(self):
            self.answered = True

    call = Call()
    asyncio.run(handler(call))
    assert call.answered is True
    assert any("Тариф: Базовый" in text for text in call.message.answers)


def test_buy_back_plan_returns_to_entry_not_cancel(db):
    """Настоящий Back: шаг срока возвращается к выбору тарифа, а не
    отменяет покупку (до редизайна «⬅️ Назад» звал buy_cancel)."""
    from aiogram import Dispatcher
    from src.bot_support import setup_support_handlers

    db.set_setting("stars:enabled", "1")
    dp = Dispatcher()
    setup_support_handlers(dp, db, marzban=None)

    def handler(name):
        return next(h.callback for h in dp.callback_query.handlers
                    if h.callback.__name__ == name)

    class Msg:
        def __init__(self):
            self.edits = []

        async def answer(self, text, reply_markup=None, **kw):
            self.edits.append((text, reply_markup))

        async def edit_text(self, text, reply_markup=None, **kw):
            self.edits.append((text, reply_markup))

    class Call:
        def __init__(self, data):
            self.from_user = type("U", (), {"id": 424007})()
            self.data = data
            self.message = Msg()

        async def answer(self):
            pass

    plan_call = Call("buy_plan:BASIC")
    asyncio.run(handler("cb_buy_plan")(plan_call))
    back_call = Call("buy_back_plan")
    asyncio.run(handler("cb_buy_back_plan")(back_call))
    text, markup = back_call.message.edits[-1]
    assert "Выберите тариф" in text
    data = [btn.callback_data for row in markup.inline_keyboard for btn in row]
    assert "buy_plan:BASIC" in data
    # and it is genuinely not a cancel
    assert "Покупка отменена" not in text


def test_promo_cancel_button_returns_to_main_dialog(db):
    account = _canonical_owner(db, 424008)
    _grant(db, account, "WL", "ux-promo-wl-00000001")

    from aiogram import Dispatcher
    from src.bot_support import setup_support_handlers
    dp = Dispatcher()
    setup_support_handlers(dp, db, marzban=None)

    def handler(observer, name):
        return next(h.callback for h in observer.handlers
                    if h.callback.__name__ == name)

    class FakeUser:
        def __init__(self, uid):
            self.id = uid

    class FakeChat:
        def __init__(self, cid):
            self.id = cid
            self.type = "private"

    class Msg:
        def __init__(self, uid, text, mid):
            self.from_user = FakeUser(uid)
            self.chat = FakeChat(uid)
            self.message_id = mid
            self.text = text
            self.answers = []

        async def answer(self, text, reply_markup=None, **kw):
            self.answers.append((text, reply_markup))

    class State:
        def __init__(self):
            self.value = None
            self.data = {}

        async def set_state(self, v):
            self.value = v

        async def get_data(self):
            return dict(self.data)

        async def update_data(self, **kw):
            self.data.update(kw)

    menu = next(h.callback for h in dp.message.handlers
                if h.callback.__name__ == "msg_promo_menu")
    state = State()
    asyncio.run(menu(Msg(424008, "🎟 Промокод", 1), state))
    assert state.value is not None

    redeem_handler = handler(dp.message, "_promo_redeem_message")
    cancel_msg = Msg(424008, "❌ Отмена", 2)
    asyncio.run(redeem_handler(cancel_msg, state))
    assert state.value == "SupportStates:in_dialog"
    text, markup = cancel_msg.answers[0]
    texts = [btn.text for row in markup.keyboard for btn in row]
    assert "📱 Моя подписка" in texts


def test_ticket_closed_resets_fsm_and_restores_main_keyboard(db):
    """Fix for the silent-swallow dead end: after the operator closes the
    ticket the user must be back in in_dialog with kb_main, not stuck in
    waiting_human where every further message was dropped."""
    from aiogram import Bot, Dispatcher
    from aiogram.fsm.storage.memory import MemoryStorage
    from src.bot_support import notify_ticket_closed, setup_support_handlers

    async def scenario():
        session = StubTelegramSession.make()
        bot = Bot("123456:abcdefghijklmnopqrstuvwxyzABCDE", session=session)
        dp = Dispatcher(storage=MemoryStorage())
        setup_support_handlers(dp, db, marzban=None)
        context = dp.fsm.get_context(bot=bot, chat_id=424009, user_id=424009)
        await context.set_state("SupportStates:waiting_human")
        await notify_ticket_closed(bot, 424009)
        state = await context.get_state()
        await dp.storage.close()
        await bot.session.close()
        return session.methods, state

    methods, state = asyncio.run(scenario())
    assert state == "SupportStates:in_dialog"
    texts = _texts(methods)
    assert any("Вопрос закрыт" in t for t in texts)
    kb = next(m for m in methods if getattr(m, "reply_markup", None) is not None
              and getattr(m.reply_markup, "keyboard", None) is not None)
    buttons = [btn.text for row in kb.reply_markup.keyboard for btn in row]
    assert "📱 Моя подписка" in buttons
    assert "🆘 Поддержка" in buttons


def test_admin_ticket_close_path_resets_the_live_bot_fsm(db):
    """Exercise the actual admin route -> bot-runner loop notification path,
    not only ``notify_ticket_closed`` in isolation."""
    from aiogram import Bot, Dispatcher
    from aiogram.fsm.storage.memory import MemoryStorage
    from src.bot_support import setup_support_handlers
    from src.routes.admin import handle_ticket_close

    async def scenario():
        session = StubTelegramSession.make()
        bot = Bot("123456:abcdefghijklmnopqrstuvwxyzABCDE", session=session)
        dp = Dispatcher(storage=MemoryStorage())
        setup_support_handlers(dp, db, marzban=None)
        telegram_id = 424010
        context = dp.fsm.get_context(bot=bot, chat_id=telegram_id, user_id=telegram_id)
        await context.set_state("SupportStates:waiting_human")
        ticket_id = db.create_ticket(telegram_id, status="waiting_human")

        class Runner:
            bot_instance = bot
            _loop = asyncio.get_running_loop()

        class Handler:
            def send_response(self, _code):
                pass

            def send_header(self, *_args):
                pass

            def end_headers(self):
                pass

            class wfile:
                @staticmethod
                def write(_data):
                    pass

        handler = Handler()
        handler.server = type("Server", (), {"db": db, "bot_runner": Runner()})()
        handle_ticket_close(handler, ticket_id)
        for _ in range(10):
            if await context.get_state() == "SupportStates:in_dialog":
                break
            await asyncio.sleep(0)
        state = await context.get_state()
        await dp.storage.close()
        await bot.session.close()
        return state, session.methods

    state, methods = asyncio.run(scenario())
    assert state == "SupportStates:in_dialog"
    assert any("Вопрос закрыт" in text for text in _texts(methods))
