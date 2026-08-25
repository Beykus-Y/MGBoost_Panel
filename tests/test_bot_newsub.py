"""PH4-04 Telegram presentation: /newsub is private-chat-only, requires a
canonical Telegram OWNER link (never mere legacy-link possession), never
re-shows a previously issued raw token, and converges safely if delivery
(the Telegram send itself) fails."""

import asyncio
import importlib
import os
import tempfile

import pytest

from tests.test_child_provisioning import PRIMARY, PRIMARY_LOGIN, _account


@pytest.fixture
def db(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("DATA_DIR", tmp)
    monkeypatch.setenv("PRIMARY_MGBOOST_ADMIN_ACTOR_ID", PRIMARY)
    monkeypatch.setenv("PRIMARY_MGBOOST_ADMIN_LOGIN", PRIMARY_LOGIN)
    monkeypatch.setenv("OPAQUE_SUBSCRIPTION_ENABLED", "1")
    import src.config as config
    import src.database as database
    importlib.reload(config)
    importlib.reload(database)
    database.DB_PATH = os.path.join(tmp, "db.sqlite3")
    instance = database.Database()
    yield instance
    instance._conn.close()


def _get_handler(dp_observer, name):
    for h in dp_observer.handlers:
        if h.callback.__name__ == name:
            return h.callback
    raise AssertionError(f"handler {name} not registered")


@pytest.fixture
def handler(db):
    from aiogram import Dispatcher
    from src.bot_support import setup_support_handlers

    dp = Dispatcher()

    class DummyMarzban:
        def get_admin_token_from_env(self):
            return "service"

        def get_user(self, username, _token):
            return {"username": username}

    setup_support_handlers(dp, db, marzban=DummyMarzban())
    return _get_handler(dp.message, "msg_new_opaque_subscription")


class _FakeUser:
    def __init__(self, uid):
        self.id = uid


class _FakeChat:
    def __init__(self, chat_type):
        self.type = chat_type


class _FakeState:
    async def set_state(self, value):
        pass


class _FakeMessage:
    def __init__(self, uid, *, chat_type="private", fail_send=False):
        self.from_user = _FakeUser(uid)
        self.chat = _FakeChat(chat_type)
        self.answers = []
        self._fail_send = fail_send

    async def answer(self, text, **kwargs):
        if self._fail_send:
            raise RuntimeError("simulated Telegram send failure")
        self.answers.append(text)


def test_unlinked_telegram_id_is_denied(handler, db):
    msg = _FakeMessage(uid=777000001)
    asyncio.run(handler(msg, _FakeState()))
    assert len(msg.answers) == 1
    assert "sub.beykus.fun" not in msg.answers[0]
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_subscription_credentials"
    ).fetchone()[0] == 0


def test_canonical_owner_gets_a_working_link(handler, db):
    account, _alias_id, _slot = _account(db, mapping="BOT_NEWSUB_A", tg=777000002)
    msg = _FakeMessage(uid=777000002)
    asyncio.run(handler(msg, _FakeState()))
    assert len(msg.answers) == 1
    assert "https://sub.beykus.fun/" in msg.answers[0]
    token = msg.answers[0].split("https://sub.beykus.fun/")[1].split("\n")[0]
    assert len(token) == 43
    resolved = db.subscription_credentials.resolve(token)
    assert resolved is not None and resolved["account_id"] == account["account_id"]


def test_reissue_rotates_and_old_link_stops_working(handler, db):
    account, _alias_id, _slot = _account(db, mapping="BOT_NEWSUB_B", tg=777000003)
    first_msg = _FakeMessage(uid=777000003)
    asyncio.run(handler(first_msg, _FakeState()))
    old_token = first_msg.answers[0].split("https://sub.beykus.fun/")[1].split("\n")[0]

    second_msg = _FakeMessage(uid=777000003)
    asyncio.run(handler(second_msg, _FakeState()))
    new_token = second_msg.answers[0].split("https://sub.beykus.fun/")[1].split("\n")[0]

    assert old_token != new_token
    assert db.subscription_credentials.resolve(old_token) is None
    assert db.subscription_credentials.resolve(new_token) is not None


def test_failed_delivery_does_not_activate_and_old_credential_survives(handler, db):
    account, _alias_id, _slot = _account(db, mapping="BOT_NEWSUB_C", tg=777000004)
    first_msg = _FakeMessage(uid=777000004)
    asyncio.run(handler(first_msg, _FakeState()))
    old_token = first_msg.answers[0].split("https://sub.beykus.fun/")[1].split("\n")[0]

    failing_msg = _FakeMessage(uid=777000004, fail_send=True)
    asyncio.run(handler(failing_msg, _FakeState()))  # send raises internally, handler must not propagate

    # old credential must still resolve -- the failed rotation never activated
    assert db.subscription_credentials.resolve(old_token) is not None
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_subscription_credentials WHERE account_id=? AND status='ACTIVE'",
        (account["account_id"],),
    ).fetchone()[0] == 1


def test_group_chat_is_never_matched_by_the_filter(db):
    from aiogram import Dispatcher
    from src.bot_support import setup_support_handlers

    dp = Dispatcher()

    class DummyMarzban:
        def get_admin_token_from_env(self):
            return "service"

        def get_user(self, username, _token):
            return {"username": username}

    setup_support_handlers(dp, db, marzban=DummyMarzban())
    handlers_for_command = [
        h for h in dp.message.handlers if h.callback.__name__ == "msg_new_opaque_subscription"
    ]
    assert len(handlers_for_command) == 1
    magic_filters = [f for f in handlers_for_command[0].filters if f.magic is not None]
    assert len(magic_filters) == 1, "expected exactly one chat-type magic filter"

    group_msg = _FakeMessage(uid=1, chat_type="group")
    private_msg = _FakeMessage(uid=1, chat_type="private")
    assert not magic_filters[0].callback(value=group_msg)
    assert magic_filters[0].callback(value=private_msg)
