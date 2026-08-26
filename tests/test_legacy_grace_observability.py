"""PH4-05 read-only grace observability: assembled snapshot fields,
inactive-client detection, no mutation."""

import importlib
import os
import tempfile

import pytest

from src.legacy_grace_activity import SECONDS_PER_DAY, record_activity
from src.legacy_grace_observability import account_grace_snapshot
from src.legacy_grace_schema import GRACE_PERIOD_SECONDS
from src.security import AdminSessionStore

from tests.test_child_provisioning import PRIMARY, PRIMARY_LOGIN


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
    _raw, session = AdminSessionStore().create(PRIMARY_LOGIN, "grace-observability-test-jwt")
    return db.primary_admin_authority.authorize_session(session)


def test_snapshot_without_grace_row_reports_none(db):
    acct = db.accounts.create_account("DIRECT")["id"]
    snapshot = account_grace_snapshot(db, acct, now=1000)
    assert snapshot["grace"] is None
    assert snapshot["active_devices"] == 0
    assert snapshot["inactive_since_grace_start"] is None


def test_snapshot_reflects_grace_window_and_remaining_time(db):
    acct = db.accounts.create_account("DIRECT")["id"]
    cap = _capability(db)
    db.legacy_grace.start(
        account_id=acct, cohort_ref="cohort-1", capability=cap, reason="canary",
        idempotency_key="obs-key-000000001", now=1_000_000,
    )
    snapshot = account_grace_snapshot(db, acct, now=1_000_000 + 5 * 86400)
    assert snapshot["grace"]["active"] is True
    assert snapshot["grace"]["day_of_14"] == 6
    assert snapshot["grace"]["seconds_remaining"] == GRACE_PERIOD_SECONDS - 5 * 86400

    after_expiry = account_grace_snapshot(db, acct, now=1_000_000 + GRACE_PERIOD_SECONDS)
    assert after_expiry["grace"]["active"] is False
    assert after_expiry["grace"]["seconds_remaining"] == 0


def test_inactive_client_never_seen_after_grace_start(db):
    import src.database as database
    acct = db.accounts.create_account("DIRECT")["id"]
    cap = _capability(db)
    started_at = 2_000_000
    # last legacy activity strictly BEFORE grace started
    record_activity(database.DB_PATH, acct, "LEGACY", now=started_at - SECONDS_PER_DAY)
    db.legacy_grace.start(
        account_id=acct, cohort_ref="cohort-inactive", capability=cap, reason="canary",
        idempotency_key="inactive-key-00001", now=started_at,
    )
    snapshot = account_grace_snapshot(db, acct, now=started_at + 3 * SECONDS_PER_DAY)
    assert snapshot["inactive_since_grace_start"] is True


def test_active_client_seen_after_grace_start_is_not_inactive(db):
    import src.database as database
    acct = db.accounts.create_account("DIRECT")["id"]
    cap = _capability(db)
    started_at = 2_000_000
    db.legacy_grace.start(
        account_id=acct, cohort_ref="cohort-active", capability=cap, reason="canary",
        idempotency_key="active-key-000001", now=started_at,
    )
    record_activity(database.DB_PATH, acct, "OPAQUE", now=started_at + SECONDS_PER_DAY)
    snapshot = account_grace_snapshot(db, acct, now=started_at + 3 * SECONDS_PER_DAY)
    assert snapshot["inactive_since_grace_start"] is False
    assert snapshot["opaque_requests_72h"] == 1


def test_migration_state_counts_reflect_bindings(db):
    from tests.test_child_provisioning import _account

    account, alias_id, _slot = _account(db, mapping="OBS_MAPPING", alias="obsuser")
    db.migration_lifecycle.prepare_migration(
        account_id=account["account_id"], legacy_alias_id=alias_id,
        hwid_verifier="hmac-sha256:" + "a" * 64, actor_ref="test",
        reason="observability fixture device", idempotency_key="mig-obs-key-0001", now=1000,
    )
    snapshot = account_grace_snapshot(db, account["account_id"], now=2000)
    assert snapshot["migration_state"]["MIGRATING"] == 1
    assert snapshot["migration_state"]["MIGRATED"] == 0
