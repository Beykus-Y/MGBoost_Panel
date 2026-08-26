"""Wave A account-centric admin read models and authenticated routes."""

import importlib
import io
import json
import os
import tempfile

import pytest

from src import security
from src.admin_read_models import account_detail, account_summaries, dashboard_summary
from src.routes import admin_accounts
from src.security import AdminSessionStore
from tests.test_child_provisioning import PRIMARY, PRIMARY_LOGIN, _account


class FakeHandler:
    def __init__(self, *, headers=None):
        self.command = "GET"
        self.headers = headers or {}
        self.rfile = io.BytesIO()
        self.wfile = io.BytesIO()
        self.status = None
        self.response_headers = []

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.response_headers.append((name, value))

    def end_headers(self):
        pass

    def json(self):
        return json.loads(self.wfile.getvalue())


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


def _handler(db, *, authenticated=True):
    headers = {}
    if authenticated:
        raw_session_id, _session = security.create_admin_session(PRIMARY_LOGIN, "jwt")
        headers["Cookie"] = f"{security.ADMIN_SESSION_COOKIE}={raw_session_id}"
    handler = FakeHandler(headers=headers)
    handler.server = type("Server", (), {"db": db})()
    return handler


def _capability(db):
    _raw, session = AdminSessionStore().create(PRIMARY_LOGIN, "jwt")
    return db.primary_admin_authority.authorize_session(session)


def test_summary_keeps_parent_readiness_separate_from_real_device_migration(db):
    account, alias_id, slot = _account(db, mapping="ADMIN_READ_SUMMARY", alias="summary-user")
    db.legacy_bridge.create_binding(
        account_id=account["account_id"], legacy_alias_id=alias_id,
        capability=_capability(db), decision_ref="wave-a-test",
        enabled=True, now=200,
    )

    row = account_summaries(db, now=300)[0]
    assert row["primary_alias"] == "summary-user"
    assert row["parent_ready"] is True
    assert row["active_devices"] == 1
    assert row["migrated_devices"] == 0
    assert row["migration_action"] == "WAITING_FOR_REGISTRATION"

    verifier = db._conn.execute(
        "SELECT hwid_verifier FROM mgboost_device_slot_generations WHERE id=?",
        (slot["generation_id"],),
    ).fetchone()[0]
    db.migration_lifecycle.prepare_migration(
        account_id=account["account_id"], legacy_alias_id=alias_id,
        hwid_verifier=verifier, actor_ref="test",
        reason="real device lineage", idempotency_key="wave-a-migration-lineage", now=301,
    )
    after = account_summaries(db, now=302)[0]
    assert after["parent_ready"] is True
    assert after["migrated_devices"] == 0
    assert after["migration_action"] == "WAITING_FOR_REGISTRATION"


def test_detail_exposes_masked_operational_device_and_keeps_internal_ids_technical(db):
    account, _alias_id, _slot = _account(db, mapping="ADMIN_READ_DETAIL", alias="detail-user")
    detail = account_detail(db, account["account_id"], now=400)

    device = detail["devices"][0]
    assert device["hwid_masked"].startswith("hwid_")
    assert "hwid_verifier" not in device
    assert "slot_generation_id" not in device
    assert "child_username" not in device
    assert detail["technical"]["device_lineage"][0]["hwid_verifier"].startswith("hmac-sha256:")
    assert detail["telegram"]["status"] == "BOUND"
    assert detail["subscription"]["effective"]["device_limit"] == 10


def test_dashboard_grace_block_is_conditional_and_ticket_counter_is_compact(db):
    account = db.accounts.create_account("DIRECT")
    assert dashboard_summary(db, now=1_000)["grace_campaign"] is None

    db.legacy_grace.start(
        account_id=account["id"], cohort_ref="wave-a-cohort",
        capability=_capability(db), reason="wave a dashboard fixture",
        idempotency_key="wave-a-grace-fixture", now=2_000,
    )
    summary = dashboard_summary(db, now=2_100)
    assert summary["grace_campaign"]["accounts_total"] == 1
    assert summary["grace_campaign"]["real_devices_child_backed"] == 0
    assert summary["grace_campaign"]["real_device_lineages"] == 0
    assert summary["tickets"] == {"open": 0, "unanswered": 0}


def test_read_routes_require_auth_and_return_account_models(db):
    account, _alias_id, _slot = _account(db, mapping="ADMIN_READ_ROUTE", alias="route-user")

    denied = _handler(db, authenticated=False)
    admin_accounts.handle_admin_accounts_list(denied)
    assert denied.status == 401

    listed = _handler(db)
    admin_accounts.handle_admin_accounts_list(listed)
    assert listed.status == 200
    assert listed.json()["accounts"][0]["primary_alias"] == "route-user"

    detailed = _handler(db)
    admin_accounts.handle_admin_account_detail(detailed, str(account["account_id"]))
    assert detailed.status == 200
    assert detailed.json()["account"]["id"] == account["account_id"]

    missing = _handler(db)
    admin_accounts.handle_admin_account_detail(missing, "999999")
    assert missing.status == 404
