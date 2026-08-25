"""PH4-04 Telegram presentation: `/newsub` is private-chat-only, requires a
canonical Telegram OWNER link (never mere legacy-link possession), never
re-shows a previously issued raw token, and -- the PH4-04 corrective fix --
never silently rotates an already-ACTIVE credential. Rotation requires an
explicit two-step confirmation (offer -> confirm/cancel) with no plaintext
token held in between steps."""

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
        "newsub": _get_handler(dp.message, "msg_new_opaque_subscription"),
        "confirm": _get_handler(dp.callback_query, "cb_newsub_confirm"),
        "cancel": _get_handler(dp.callback_query, "cb_newsub_cancel"),
        "do": _get_handler(dp.callback_query, "cb_newsub_do"),
    }


# Backwards-compatible single-handler fixture for tests that only need /newsub.
@pytest.fixture
def handler(handlers):
    return handlers["newsub"]


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
        self.answer_kwargs = []
        self.edited_markup = []
        self.edited_text = []
        self._fail_send = fail_send

    async def answer(self, text, **kwargs):
        if self._fail_send:
            raise RuntimeError("simulated Telegram send failure")
        self.answers.append(text)
        self.answer_kwargs.append(kwargs)

    async def edit_reply_markup(self, reply_markup=None):
        self.edited_markup.append(reply_markup)

    async def edit_text(self, text):
        self.edited_text.append(text)


class _FakeCallback:
    def __init__(self, uid, data, *, chat_type="private", fail_send=False):
        self.from_user = _FakeUser(uid)
        self.data = data
        self.message = _FakeMessage(uid, chat_type=chat_type, fail_send=fail_send)
        self.answered = False

    async def answer(self):
        self.answered = True


def _active_row(db, account_id):
    return db._conn.execute(
        "SELECT id, generation, status FROM mgboost_subscription_credentials "
        "WHERE account_id=? AND status='ACTIVE'", (account_id,),
    ).fetchone()


def _extract_token(text):
    return text.split("https://sub.beykus.fun/")[1].split("\n")[0]


# --- CASE A: no ACTIVE credential -> initial issuance works directly --------

def test_unlinked_telegram_id_is_denied(handler, db):
    msg = _FakeMessage(uid=777000001)
    asyncio.run(handler(msg, _FakeState()))
    assert len(msg.answers) == 1
    assert "sub.beykus.fun" not in msg.answers[0]
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_subscription_credentials"
    ).fetchone()[0] == 0


def test_no_credential_initial_issuance_works(handler, db):
    account, _alias_id, _slot = _account(db, mapping="BOT_NEWSUB_A", tg=777000002)
    msg = _FakeMessage(uid=777000002)
    asyncio.run(handler(msg, _FakeState()))
    assert len(msg.answers) == 1
    assert "https://sub.beykus.fun/" in msg.answers[0]
    token = _extract_token(msg.answers[0])
    assert len(token) == 43
    resolved = db.subscription_credentials.resolve(token)
    assert resolved is not None and resolved["account_id"] == account["account_id"]
    row = _active_row(db, account["account_id"])
    assert row["generation"] == 1


# --- CASE B: ACTIVE credential exists -> /newsub offers, does not rotate ----

def test_repeat_newsub_with_active_credential_does_not_change_generation(handler, db):
    account, _alias_id, _slot = _account(db, mapping="BOT_NEWSUB_B", tg=777000003)
    first_msg = _FakeMessage(uid=777000003)
    asyncio.run(handler(first_msg, _FakeState()))
    before = _active_row(db, account["account_id"])

    second_msg = _FakeMessage(uid=777000003)
    asyncio.run(handler(second_msg, _FakeState()))
    after = _active_row(db, account["account_id"])

    assert before["id"] == after["id"]
    assert before["generation"] == after["generation"]
    # the offer message must not contain a link/token
    assert "sub.beykus.fun" not in second_msg.answers[0]
    assert "Перевыпустить" in second_msg.answer_kwargs[0]["reply_markup"].inline_keyboard[0][0].text


def test_repeat_newsub_old_token_remains_active_and_resolvable(handler, db):
    account, _alias_id, _slot = _account(db, mapping="BOT_NEWSUB_B2", tg=777000013)
    first_msg = _FakeMessage(uid=777000013)
    asyncio.run(handler(first_msg, _FakeState()))
    old_token = _extract_token(first_msg.answers[0])

    second_msg = _FakeMessage(uid=777000013)
    asyncio.run(handler(second_msg, _FakeState()))

    resolved = db.subscription_credentials.resolve(old_token)
    assert resolved is not None and resolved["account_id"] == account["account_id"]


# --- confirmation flow ------------------------------------------------------

def test_confirm_button_shows_destructive_warning_not_rotation(handlers, db):
    account, _alias_id, _slot = _account(db, mapping="BOT_NEWSUB_C", tg=777000004)
    msg = _FakeMessage(uid=777000004)
    asyncio.run(handlers["newsub"](msg, _FakeState()))
    before = _active_row(db, account["account_id"])

    call = _FakeCallback(uid=777000004, data=f"newsub_confirm:{account['account_id']}")
    asyncio.run(handlers["confirm"](call))
    after = _active_row(db, account["account_id"])

    assert before["generation"] == after["generation"]
    assert call.answered
    assert "перестанет работать" in call.message.answers[0]
    kb = call.message.answer_kwargs[0]["reply_markup"].inline_keyboard[0]
    labels = {btn.text for btn in kb}
    assert "✅ Перевыпустить" in labels and "❌ Отмена" in labels


def test_cancel_leaves_everything_unchanged(handlers, db):
    account, _alias_id, _slot = _account(db, mapping="BOT_NEWSUB_D", tg=777000005)
    msg = _FakeMessage(uid=777000005)
    asyncio.run(handlers["newsub"](msg, _FakeState()))
    before = _active_row(db, account["account_id"])

    call = _FakeCallback(uid=777000005, data=f"newsub_cancel:{account['account_id']}")
    asyncio.run(handlers["cancel"](call))
    after = _active_row(db, account["account_id"])

    assert before["id"] == after["id"] and before["generation"] == after["generation"]
    assert "Отменено" in call.message.edited_text[0]


def test_confirmed_reissue_increments_generation(handlers, db):
    account, _alias_id, _slot = _account(db, mapping="BOT_NEWSUB_E", tg=777000006)
    msg = _FakeMessage(uid=777000006)
    asyncio.run(handlers["newsub"](msg, _FakeState()))
    before = _active_row(db, account["account_id"])

    call = _FakeCallback(uid=777000006, data=f"newsub_do:{account['account_id']}")
    asyncio.run(handlers["do"](call))
    after = _active_row(db, account["account_id"])

    assert after["generation"] == before["generation"] + 1


def test_confirmed_reissue_old_token_invalid_new_token_works(handlers, db):
    account, _alias_id, _slot = _account(db, mapping="BOT_NEWSUB_F", tg=777000007)
    msg = _FakeMessage(uid=777000007)
    asyncio.run(handlers["newsub"](msg, _FakeState()))
    old_token = _extract_token(msg.answers[0])

    call = _FakeCallback(uid=777000007, data=f"newsub_do:{account['account_id']}")
    asyncio.run(handlers["do"](call))
    new_token = _extract_token(call.message.answers[-1])

    assert old_token != new_token
    assert db.subscription_credentials.resolve(old_token) is None
    resolved = db.subscription_credentials.resolve(new_token)
    assert resolved is not None and resolved["account_id"] == account["account_id"]


def test_confirmed_reissue_does_not_touch_child_or_device_tables(handlers, db):
    account, _alias_id, _slot = _account(db, mapping="BOT_NEWSUB_G", tg=777000008)
    msg = _FakeMessage(uid=777000008)
    asyncio.run(handlers["newsub"](msg, _FakeState()))

    def counts():
        return (
            db._conn.execute("SELECT COUNT(*) FROM mgboost_device_slot_generations").fetchone()[0],
            db._conn.execute("SELECT COUNT(*) FROM mgboost_child_user_intents").fetchone()[0],
        )

    before = counts()
    call = _FakeCallback(uid=777000008, data=f"newsub_do:{account['account_id']}")
    asyncio.run(handlers["do"](call))
    after = counts()
    assert before == after


def test_failed_delivery_on_confirmed_reissue_leaves_old_credential_active(handlers, db):
    account, _alias_id, _slot = _account(db, mapping="BOT_NEWSUB_H", tg=777000009)
    msg = _FakeMessage(uid=777000009)
    asyncio.run(handlers["newsub"](msg, _FakeState()))
    old_token = _extract_token(msg.answers[0])
    before = _active_row(db, account["account_id"])

    call = _FakeCallback(uid=777000009, data=f"newsub_do:{account['account_id']}", fail_send=True)
    asyncio.run(handlers["do"](call))
    after = _active_row(db, account["account_id"])

    assert before["id"] == after["id"] and before["generation"] == after["generation"]
    assert db.subscription_credentials.resolve(old_token) is not None


def test_double_tap_confirm_creates_only_one_rotation(handlers, db):
    account, _alias_id, _slot = _account(db, mapping="BOT_NEWSUB_I", tg=777000010)
    msg = _FakeMessage(uid=777000010)
    asyncio.run(handlers["newsub"](msg, _FakeState()))
    before = _active_row(db, account["account_id"])

    async def _double_tap():
        call1 = _FakeCallback(uid=777000010, data=f"newsub_do:{account['account_id']}")
        call2 = _FakeCallback(uid=777000010, data=f"newsub_do:{account['account_id']}")
        await asyncio.gather(handlers["do"](call1), handlers["do"](call2))

    asyncio.run(_double_tap())
    after = _active_row(db, account["account_id"])
    assert after["generation"] == before["generation"] + 1


def test_non_owner_callback_denied(handlers, db):
    account, _alias_id, _slot = _account(db, mapping="BOT_NEWSUB_J", tg=777000011)
    msg = _FakeMessage(uid=777000011)
    asyncio.run(handlers["newsub"](msg, _FakeState()))
    before = _active_row(db, account["account_id"])

    # a different, unlinked Telegram id tries to act on this account's callback data
    call = _FakeCallback(uid=999999999, data=f"newsub_do:{account['account_id']}")
    asyncio.run(handlers["do"](call))
    after = _active_row(db, account["account_id"])
    assert before["generation"] == after["generation"]
    assert call.message.answers == []


def test_group_chat_callback_denied(handlers, db):
    account, _alias_id, _slot = _account(db, mapping="BOT_NEWSUB_K", tg=777000012)
    msg = _FakeMessage(uid=777000012)
    asyncio.run(handlers["newsub"](msg, _FakeState()))
    before = _active_row(db, account["account_id"])

    call = _FakeCallback(uid=777000012, data=f"newsub_do:{account['account_id']}", chat_type="group")
    asyncio.run(handlers["do"](call))
    after = _active_row(db, account["account_id"])
    assert before["generation"] == after["generation"]


# --- group chat / filter proof for the /newsub command itself --------------

def test_group_chat_is_never_matched_by_the_newsub_filter(db):
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
