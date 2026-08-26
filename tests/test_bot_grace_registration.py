"""PH4-05: the bot's existing legacy-link handler (`msg_waiting_link`) must
attempt `bind_telegram_after_registration` for a bootstrapped grace-cohort
account, must remain a silent no-op for every ordinary (non-cohort) user,
and a bind failure must never break the existing user-facing response."""

import asyncio
import importlib
import os
import tempfile

import pytest

from src.legacy_grace_registration import bootstrap_grace_subject
from tests.test_child_provisioning import PRIMARY, PRIMARY_LOGIN
from src.security import AdminSessionStore


@pytest.fixture
def db(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("DATA_DIR", tmp)
    monkeypatch.setenv("PRIMARY_MGBOOST_ADMIN_ACTOR_ID", PRIMARY)
    monkeypatch.setenv("PRIMARY_MGBOOST_ADMIN_LOGIN", PRIMARY_LOGIN)
    import src.config as config
    import src.database as database
    importlib.reload(config)
    importlib.reload(database)
    database.DB_PATH = os.path.join(tmp, "db.sqlite3")
    instance = database.Database()
    yield instance
    instance._conn.close()


def _capability(db):
    _raw, session = AdminSessionStore().create(PRIMARY_LOGIN, "bot-grace-test-jwt")
    return db.primary_admin_authority.authorize_session(session)


def _get_handler(dp_observer, name):
    for h in dp_observer.handlers:
        if h.callback.__name__ == name:
            return h.callback
    raise AssertionError(f"handler {name} not registered")


class _FakeUser:
    def __init__(self, uid):
        self.id = uid


class _FakeChat:
    type = "private"


class _FakeState:
    def __init__(self):
        self.state = None

    async def set_state(self, value):
        self.state = value


class _FakeMessage:
    def __init__(self, uid, text):
        self.from_user = _FakeUser(uid)
        self.chat = _FakeChat()
        self.text = text
        self.answers = []

    async def answer(self, text, **kwargs):
        self.answers.append(text)


class _DummyMarzban:
    def __init__(self, username="client070"):
        self._username = username

    def get_username_for_token(self, token):
        return self._username


@pytest.fixture
def waiting_link_handler(db):
    from aiogram import Dispatcher
    from src.bot_support import setup_support_handlers

    dp = Dispatcher()
    setup_support_handlers(dp, db, marzban=_DummyMarzban())
    return _get_handler(dp.message, "msg_waiting_link")


def _bootstrap(db, cap, username, *, now=1000):
    return bootstrap_grace_subject(
        db, capability=cap, legacy_username=username, legacy_status="ACTIVE",
        legacy_expiry=None, observed_device_count=1, observed_hwid_count=1,
        decision_ref="mass-grace-campaign-2026-08-26",
        payment_decision_ref="owner-attested-legacy-external-payment-2026",
        payment_attestation_note="Historical direct payment, no invented amount/date.",
        payment_evidence={"source": "owner-decision-2026-08-26"},
        idempotency_key=f"grace-bootstrap-v1:{username}", now=now,
    )


def test_ordinary_user_link_unaffected_no_bootstrapped_account(db, waiting_link_handler):
    msg = _FakeMessage(555001, "https://sub.beykus.fun/sub/some-token")
    asyncio.run(waiting_link_handler(msg, _FakeState()))
    assert len(msg.answers) == 1
    assert "✅ Аккаунт привязан" in msg.answers[0]
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_telegram_identities WHERE role='OWNER'"
    ).fetchone()[0] == 0


def test_bootstrapped_account_gets_bound_on_registration(db):
    cap = _capability(db)
    result = _bootstrap(db, cap, "client070")
    account_id = result["account_id"]

    from aiogram import Dispatcher
    from src.bot_support import setup_support_handlers

    dp = Dispatcher()
    setup_support_handlers(dp, db, marzban=_DummyMarzban(username="client070"))
    handler = _get_handler(dp.message, "msg_waiting_link")

    msg = _FakeMessage(555002, "https://sub.beykus.fun/sub/some-token")
    asyncio.run(handler(msg, _FakeState()))

    assert len(msg.answers) == 1
    assert "✅ Аккаунт привязан" in msg.answers[0]
    owner = db._conn.execute(
        "SELECT telegram_id FROM mgboost_telegram_identities WHERE account_id=? AND role='OWNER'",
        (account_id,),
    ).fetchone()
    assert owner is not None
    assert owner["telegram_id"] == 555002


def test_bind_failure_is_fail_open_response_unaffected(db, monkeypatch):
    cap = _capability(db)
    _bootstrap(db, cap, "client071")

    from aiogram import Dispatcher
    from src.bot_support import setup_support_handlers

    dp = Dispatcher()
    setup_support_handlers(dp, db, marzban=_DummyMarzban(username="client071"))
    handler = _get_handler(dp.message, "msg_waiting_link")

    import src.legacy_grace_registration as reg_module
    monkeypatch.setattr(
        reg_module, "bind_telegram_after_registration",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    msg = _FakeMessage(555003, "https://sub.beykus.fun/sub/some-token")
    asyncio.run(handler(msg, _FakeState()))
    assert len(msg.answers) == 1
    assert "✅ Аккаунт привязан" in msg.answers[0]
