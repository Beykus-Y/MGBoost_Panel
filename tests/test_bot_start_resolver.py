"""canary hotfix regression: /start linked/unlinked read-model vs canonical OWNER.

Root cause under test: ``cmd_start``/``msg_no_state`` resolved "linked
user" solely from the legacy ``tg_users`` table, so a customer with a
canonical DIRECT account + non-revoked OWNER telegram identity (the
CANONICAL_SIGNUP outcome) was greeted with new-user onboarding.

Every bot-path scenario here routes Updates through a real aiogram
Dispatcher (``feed_update``), not direct handler calls -- the earlier P0
showed handler-level unit calls can miss real routing.
"""
import asyncio
import os
import sys
import tempfile

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


def _text_update(telegram_id, text, update_id=1):
    from datetime import datetime, timezone
    from aiogram.types import Chat, Message, Update, User

    message = Message(
        message_id=100 + update_id,
        date=datetime.now(timezone.utc),
        chat=Chat(id=telegram_id, type="private"),
        from_user=User(id=telegram_id, is_bot=False, first_name="U"),
        text=text,
    )
    return Update(update_id=update_id, message=message)


def _successful_payment_update(telegram_id, invoice, charge_id, update_id=1):
    from datetime import datetime, timezone
    from aiogram.types import Chat, Message, SuccessfulPayment, Update, User

    payment = SuccessfulPayment(
        currency="XTR",
        total_amount=invoice["stars_price"],
        invoice_payload=str(invoice["id"]),
        telegram_payment_charge_id=charge_id,
        provider_payment_charge_id="",
    )
    message = Message(
        message_id=100 + update_id,
        date=datetime.now(timezone.utc),
        chat=Chat(id=telegram_id, type="private"),
        from_user=User(id=telegram_id, is_bot=False, first_name="Payer"),
        successful_payment=payment,
    )
    return Update(update_id=update_id, message=message)


async def _feed_text(db, updates, telegram_id):
    """Run text updates through a real Dispatcher; return (methods, final_state)."""
    from aiogram import Bot, Dispatcher
    from aiogram.fsm.storage.memory import MemoryStorage
    from src.bot_support import setup_support_handlers

    session = StubTelegramSession.make()
    bot = Bot("123456:abcdefghijklmnopqrstuvwxyzABCDE", session=session)
    dp = Dispatcher(storage=MemoryStorage())
    setup_support_handlers(dp, db, marzban=None)
    for index, upd in enumerate(updates, start=1):
        await dp.feed_update(bot, upd)
    context = dp.fsm.get_context(bot=bot, chat_id=telegram_id, user_id=telegram_id)
    state = await context.get_state()
    await dp.storage.close()
    await bot.session.close()
    return session.methods, state


def _texts(methods):
    return [getattr(m, "text", "") or "" for m in methods]


def _reply_keyboard_texts(methods):
    for m in methods:
        markup = getattr(m, "reply_markup", None)
        keyboard = getattr(markup, "keyboard", None)
        if keyboard:
            return [btn.text for row in keyboard for btn in row]
    return []


def _seed_catalog(db):
    from src.plan_catalog import seed_plan_catalog
    seed_plan_catalog(db.plan_catalog, now=1)


def _direct_owner(db, telegram_id):
    """Mirror the CANONICAL_SIGNUP ownership outcome: DIRECT account +
    non-revoked OWNER telegram identity, no legacy tg_users row."""
    _seed_catalog(db)
    account = db.accounts.create_account("DIRECT", now=1)
    db.accounts.link_telegram_owner(
        account["id"], telegram_id, provenance="DIRECT_BIND", actor="test", now=1,
    )
    return account


def _grant_basic_subscription(db, account, key):
    db.subscription_renewal.apply_same_plan_purchase(
        account_id=account["id"], plan_code="BASIC", duration_days=30,
        payment_channel="TELEGRAM_STARS", mutation_source="DIRECT_PURCHASE",
        actor_type="TEST", idempotency_key=key,
    )


# --- /start linked/unlinked routing -----------------------------------------

def test_brand_new_user_gets_onboarding(db):
    methods, state = asyncio.run(_feed_text(
        db, [_text_update(2001, "/start")], 2001,
    ))
    texts = _texts(methods)
    assert any("прислать существующую ссылку" in t for t in texts)
    assert _reply_keyboard_texts(methods) == ["🛒 Купить VPN"]
    assert state == "SupportStates:waiting_link"


def test_legacy_linked_user_keeps_existing_ux(db):
    db.save_tg_user(2002, "alice")
    methods, state = asyncio.run(_feed_text(
        db, [_text_update(2002, "/start")], 2002,
    ))
    texts = _texts(methods)
    assert any("С возвращением" in t for t in texts)
    assert "прислать существующую ссылку" not in "".join(texts)
    buttons = _reply_keyboard_texts(methods)
    assert "📋 Моя подписка" in buttons
    assert state == "SupportStates:in_dialog"


def test_canonical_direct_owner_gets_existing_ux(db):
    """The production bug: DIRECT account + OWNER binding existed after
    CANONICAL_SIGNUP, yet /start showed unlinked-user onboarding."""
    _direct_owner(db, 2003)
    methods, state = asyncio.run(_feed_text(
        db, [_text_update(2003, "/start")], 2003,
    ))
    texts = _texts(methods)
    assert any("С возвращением" in t for t in texts)
    assert "прислать существующую ссылку" not in "".join(texts)
    buttons = _reply_keyboard_texts(methods)
    assert "📋 Моя подписка" in buttons
    assert "⭐️ Продлить подписку" in buttons
    assert state == "SupportStates:in_dialog"


def test_canonical_signup_account_survives_fresh_db_and_dispatcher(db):
    """Full commercial-signup equivalent: CANONICAL_SIGNUP invoice paid
    through dispatcher #1, then a brand-new Database() + fresh Dispatcher
    (bot restart) must still resolve the account for /start."""
    _seed_catalog(db)
    telegram_id = 2004
    invoice = db.stars_purchases.create_invoice(
        telegram_id=telegram_id, plan_code="BASIC", duration_days=30,
        ttl_seconds=3600, now=1,
    )
    assert invoice["invoice_kind"] == "CANONICAL_SIGNUP"

    async def pay():
        from aiogram import Bot, Dispatcher
        from aiogram.fsm.storage.memory import MemoryStorage
        from src.bot_support import setup_support_handlers

        session = StubTelegramSession.make()
        bot = Bot("123456:abcdefghijklmnopqrstuvwxyzABCDE", session=session)
        dp = Dispatcher(storage=MemoryStorage())
        setup_support_handlers(dp, db, marzban=None, stars_trigger=asyncio.Event())
        await dp.feed_update(
            bot, _successful_payment_update(telegram_id, invoice, "signup-charge-1")
        )
        await dp.storage.close()
        await bot.session.close()

    asyncio.run(pay())
    account = db.accounts.get_active_account_by_telegram_id(telegram_id)
    assert account is not None, "signup payment did not create the DIRECT account"
    assert db.get_tg_user(telegram_id) is None  # no legacy link was ever made

    from src.database import Database
    fresh_db = Database()
    try:
        methods, state = asyncio.run(_feed_text(
            fresh_db, [_text_update(telegram_id, "/start")], telegram_id,
        ))
        texts = _texts(methods)
        assert any("С возвращением" in t for t in texts)
        assert "прислать существующую ссылку" not in "".join(texts)
        assert state == "SupportStates:in_dialog"
    finally:
        fresh_db._conn.close()


def test_revoked_owner_identity_gets_onboarding(db):
    _direct_owner(db, 2005)
    db._conn.execute(
        "UPDATE mgboost_telegram_identities SET revoked_at=2, revoke_reason='test' "
        "WHERE telegram_id=?",
        (2005,),
    )
    db._conn.commit()
    methods, state = asyncio.run(_feed_text(
        db, [_text_update(2005, "/start")], 2005,
    ))
    texts = _texts(methods)
    assert any("прислать существующую ссылку" in t for t in texts)
    assert state == "SupportStates:waiting_link"


def test_closed_account_gets_onboarding(db):
    account = _direct_owner(db, 2006)
    db._conn.execute(
        "UPDATE mgboost_accounts SET status='CLOSED' WHERE id=?", (account["id"],),
    )
    db._conn.commit()
    methods, state = asyncio.run(_feed_text(
        db, [_text_update(2006, "/start")], 2006,
    ))
    assert any("прислать существующую ссылку" in t for t in _texts(methods))
    assert state == "SupportStates:waiting_link"


def test_unrelated_user_cannot_resolve_foreign_direct_account(db):
    _direct_owner(db, 2007)
    methods, state = asyncio.run(_feed_text(
        db, [_text_update(2999, "/start")], 2999,
    ))
    texts = "".join(_texts(methods))
    assert "прислать существующую ссылку" in texts
    assert "С возвращением" not in texts
    assert state == "SupportStates:waiting_link"


def test_repeat_start_is_idempotent(db):
    _direct_owner(db, 2008)
    methods, state = asyncio.run(_feed_text(
        db,
        [_text_update(2008, "/start", update_id=1),
         _text_update(2008, "/start", update_id=2)],
        2008,
    ))
    texts = _texts(methods)
    assert len([t for t in texts if "С возвращением" in t]) == 2
    assert "прислать существующую ссылку" not in "".join(texts)
    assert state == "SupportStates:in_dialog"
    assert db.get_tg_user(2008) is None  # /start never fabricated a legacy link


# --- same read-model on adjacent bot paths ----------------------------------

def test_no_state_stray_message_canonical_owner_gets_main_menu(db):
    _direct_owner(db, 2009)
    methods, state = asyncio.run(_feed_text(
        db, [_text_update(2009, "привет")], 2009,
    ))
    texts = "".join(_texts(methods))
    assert "Чем могу помочь?" in texts
    assert "прислать существующую ссылку" not in texts
    assert "🛒 Купить VPN" in _reply_keyboard_texts(methods)
    assert state == "SupportStates:in_dialog"


def test_manage_devices_canonical_owner_not_pushed_into_link_loop(db):
    account = _direct_owner(db, 2010)
    _grant_basic_subscription(db, account, "mgmt-dev-sub-key-0001")

    async def scenario():
        from aiogram import Bot, Dispatcher
        from aiogram.fsm.storage.memory import MemoryStorage
        from src.bot_support import setup_support_handlers

        session = StubTelegramSession.make()
        bot = Bot("123456:abcdefghijklmnopqrstuvwxyzABCDE", session=session)
        dp = Dispatcher(storage=MemoryStorage())
        setup_support_handlers(dp, db, marzban=None)
        context = dp.fsm.get_context(bot=bot, chat_id=2010, user_id=2010)
        await context.set_state("SupportStates:in_dialog")
        await dp.feed_update(bot, _text_update(2010, "🔧 Управление устройствами"))
        state = await context.get_state()
        await dp.storage.close()
        await bot.session.close()
        return session.methods, state

    methods, state = asyncio.run(scenario())
    texts = "".join(_texts(methods))
    assert "Нужно сначала привязать подписку" not in texts
    assert "поддержк" in texts
    assert state == "SupportStates:in_dialog"


def test_my_subscription_canonical_renders_entitlements(db):
    account = _direct_owner(db, 2011)
    _grant_basic_subscription(db, account, "mysub-canonical-key-001")

    async def scenario():
        from aiogram import Bot, Dispatcher
        from aiogram.fsm.storage.memory import MemoryStorage
        from src.bot_support import setup_support_handlers

        session = StubTelegramSession.make()
        bot = Bot("123456:abcdefghijklmnopqrstuvwxyzABCDE", session=session)
        dp = Dispatcher(storage=MemoryStorage())
        setup_support_handlers(dp, db, marzban=None)
        context = dp.fsm.get_context(bot=bot, chat_id=2011, user_id=2011)
        await context.set_state("SupportStates:in_dialog")
        await dp.feed_update(bot, _text_update(2011, "📋 Моя подписка"))
        state = await context.get_state()
        await dp.storage.close()
        await bot.session.close()
        return session.methods, state

    methods, _state = asyncio.run(scenario())
    text = "".join(_texts(methods))
    assert "Тариф" in text
    assert "Базовый" in text
    assert "ACTIVE" in text
    assert "привязать" not in text


def test_buy_vpn_canonical_owner_lists_tariffs(db):
    _direct_owner(db, 2012)
    db.set_setting("stars:enabled", "1")
    methods, _state = asyncio.run(_feed_text(
        db, [_text_update(2012, "🛒 Купить VPN")], 2012,
    ))
    assert any("Выберите тариф" in t for t in _texts(methods))


# --- AI support tool status path (execute_tool, not a routed handler) -------

def test_support_tool_subscription_info_canonical_fallback(db):
    account = _direct_owner(db, 2013)
    _grant_basic_subscription(db, account, "tool-canonical-sub")

    async def scenario():
        from src.bot_support import execute_tool
        return await execute_tool(
            "get_subscription_info", {}, db=db, marzban=None,
            telegram_id=2013, node_states={}, node_names={},
        )

    result = asyncio.run(scenario())
    assert result != "Подписка не привязана."
    assert "Базовый" in result


def test_support_tool_subscription_info_legacy_still_works(db):
    db.save_tg_user(2014, "legacyuser")

    class Marzban:
        def get_admin_token_from_env(self):
            return "tok"

        def get_user(self, username, _token):
            return {"username": username, "expire": None, "status": "active"}

    async def scenario():
        from src.bot_support import execute_tool
        return await execute_tool(
            "get_subscription_info", {}, db=db, marzban=Marzban(),
            telegram_id=2014, node_states={}, node_names={},
        )

    result = asyncio.run(scenario())
    assert "legacyuser" in result


def test_support_tool_subscription_info_truly_unlinked(db):
    async def scenario():
        from src.bot_support import execute_tool
        return await execute_tool(
            "get_subscription_info", {}, db=db, marzban=None,
            telegram_id=2015, node_states={}, node_names={},
        )

    assert asyncio.run(scenario()) == "Подписка не привязана."
