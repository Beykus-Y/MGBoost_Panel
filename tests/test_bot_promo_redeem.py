"""PH5-13 Telegram presentation: self-service promo redeem. The FSM state is
UX only -- the race-safety mechanism is the deterministic per-event
idempotency key `(chat_id, message_id)`, so a redelivered message replays the
same redemption instead of applying the promo twice."""

import asyncio
import importlib
import os
import tempfile

import pytest

from src.security import AdminSessionStore

PRIMARY = "owner:mgboost-primary:v1"
PRIMARY_LOGIN = "authenticated-primary-login"


@pytest.fixture
def db(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="bot-promo-")
    monkeypatch.setenv("DATA_DIR", tmp)
    monkeypatch.setenv("PRIMARY_MGBOOST_ADMIN_ACTOR_ID", PRIMARY)
    monkeypatch.setenv("PRIMARY_MGBOOST_ADMIN_LOGIN", PRIMARY_LOGIN)
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

    class DummyMarzban:
        def get_admin_token_from_env(self):
            return "service"

        def get_user(self, username, _token):
            return {"username": username}

    setup_support_handlers(dp, db, marzban=DummyMarzban())
    return {
        "menu": _get_handler(dp.message, "msg_promo_menu"),
        "redeem": _get_handler(dp.message, "_promo_redeem_message"),
    }


class _FakeUser:
    def __init__(self, uid):
        self.id = uid


class _FakeChat:
    def __init__(self, chat_id, chat_type="private"):
        self.id = chat_id
        self.type = chat_type


class _FakeState:
    def __init__(self):
        self.value = None

    async def set_state(self, value):
        self.value = value


class _FakeMessage:
    def __init__(self, uid, *, chat_id, message_id, text=None, chat_type="private"):
        self.from_user = _FakeUser(uid)
        self.chat = _FakeChat(chat_id, chat_type)
        self.message_id = message_id
        self.text = text
        self.answers = []
        self.answer_kwargs = []

    async def answer(self, text, **kwargs):
        self.answers.append(text)
        self.answer_kwargs.append(kwargs)


def _wl_account(db, telegram_id):
    account = db.accounts.create_account("DIRECT", now=1)
    db.accounts.link_telegram_owner(account["id"], telegram_id, provenance="ADMIN_REBIND",
                                    actor="test", now=1)
    db.subscription_renewal.apply_same_plan_purchase(
        account_id=account["id"], plan_code="WL", duration_days=30,
        payment_channel="TELEGRAM_STARS", mutation_source="DIRECT_PURCHASE",
        actor_type="TELEGRAM_USER", idempotency_key=f"bot-promo-base-{telegram_id}",
        now=1_000,
    )
    return account


def _define_wl_promo(db, code="BOTWL7"):
    _raw, session = AdminSessionStore().create(PRIMARY_LOGIN, "test-server-jwt")
    cap = db.primary_admin_authority.authorize_session(session)
    db.promo.create_definition(
        cap, code=code, effect_kind="EXTEND_SUBSCRIPTION", trial_class=None,
        effect_params={"days": 7}, reason="bot promo test definition",
        idempotency_key=f"promo-def-{code}-000000000001", now=1_000,
    )


def test_unlinked_user_cannot_open_promo_menu(db, handlers):
    msg = _FakeMessage(555000001, chat_id=555000001, message_id=1)
    asyncio.run(handlers["menu"](msg, _FakeState()))
    assert "подтверждённому аккаунту" in msg.answers[0]
    assert db._conn.execute(
        "SELECT COUNT(*) c FROM mgboost_promo_redemptions"
    ).fetchone()["c"] == 0


def test_redeem_success_then_redelivery_of_same_message_replays(db, handlers):
    _wl_account(db, 555000002)
    _define_wl_promo(db, "BOTWL7")
    state = _FakeState()

    asyncio.run(handlers["menu"](
        _FakeMessage(555000002, chat_id=555000002, message_id=10), state))
    assert state.value is not None  # FSM moved to waiting_promo_code
    msg = _FakeMessage(555000002, chat_id=555000002, message_id=11, text="botwl7")
    asyncio.run(handlers["redeem"](msg, state))
    assert any("Промокод применён" in a for a in msg.answers)
    assert state.value == "SupportStates:in_dialog"  # back to the main dialog
    assert db._conn.execute(
        "SELECT COUNT(*) c FROM mgboost_wl_periods WHERE sequence_no=2"
    ).fetchone()["c"] == 1

    # Same message redelivered (retry/timeout): key (chat_id, message_id) is
    # identical -> replay, never a second period.
    state2 = _FakeState()
    asyncio.run(handlers["redeem"](msg, state2))
    assert any("уже был применён" in a for a in msg.answers)
    assert db._conn.execute(
        "SELECT COUNT(*) c FROM mgboost_wl_periods WHERE sequence_no=2"
    ).fetchone()["c"] == 1


def test_redeem_new_message_same_code_is_conflict_not_second_apply(db, handlers):
    _wl_account(db, 555000003)
    _define_wl_promo(db, "BOTWL7B")
    state = _FakeState()

    first = _FakeMessage(555000003, chat_id=555000003, message_id=21, text="BOTWL7B")
    asyncio.run(handlers["redeem"](first, state))
    assert any("Промокод применён" in a for a in first.answers)

    second = _FakeMessage(555000003, chat_id=555000003, message_id=22, text="BOTWL7B")
    asyncio.run(handlers["redeem"](second, state))
    assert any("уже был применён" in a for a in second.answers)
    assert db._conn.execute(
        "SELECT COUNT(*) c FROM mgboost_wl_periods WHERE sequence_no=2"
    ).fetchone()["c"] == 1


def test_redeem_unknown_code_keeps_waiting_state(db, handlers):
    _wl_account(db, 555000004)
    state = _FakeState()
    msg = _FakeMessage(555000004, chat_id=555000004, message_id=31, text="NOSUCH")
    asyncio.run(handlers["redeem"](msg, state))
    assert any("не найден" in a for a in msg.answers)
