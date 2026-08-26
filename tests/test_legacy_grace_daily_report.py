"""PH4-05 daily cohort report: telegram_status states, action
classification, and the report builder's aggregate/shared-boundary output."""

import importlib
import os
import tempfile

import pytest

from src.legacy_grace_observability import (
    ACTION_CONTACT_USER,
    ACTION_MANUAL_REVIEW,
    ACTION_OK_MIGRATED,
    ACTION_RECONCILE_REQUIRED,
    ACTION_WAITING_FOR_REGISTRATION,
    account_grace_snapshot,
    classify_action,
    telegram_status,
)
from src.legacy_grace_registration import bind_telegram_after_registration, bootstrap_grace_subject, start_grace_cohort
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
    _raw, session = AdminSessionStore().create(PRIMARY_LOGIN, "daily-report-test-jwt")
    return db.primary_admin_authority.authorize_session(session)


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


def _tg_link(db, telegram_id, username, *, now=1000):
    db._conn.execute(
        "INSERT OR REPLACE INTO tg_users (telegram_id, marzban_username, registered_at) VALUES (?,?,?)",
        (telegram_id, username, now),
    )
    db._conn.commit()


# --- telegram_status ---------------------------------------------------------

def test_telegram_status_unregistered_by_default(db):
    cap = _capability(db)
    result = _bootstrap(db, cap, "client060")
    assert telegram_status(db, result["account_id"]) == "UNREGISTERED"


def test_telegram_status_pending_link_after_bot_registration(db):
    cap = _capability(db)
    result = _bootstrap(db, cap, "client061")
    _tg_link(db, 1001, "client061")
    assert telegram_status(db, result["account_id"]) == "PENDING_LINK"


def test_telegram_status_ambiguous_two_distinct_ids(db):
    cap = _capability(db)
    result = _bootstrap(db, cap, "client062")
    _tg_link(db, 1002, "client062")
    _tg_link(db, 1003, "client062")
    assert telegram_status(db, result["account_id"]) == "AMBIGUOUS"


def test_telegram_status_bound_after_link(db):
    cap = _capability(db)
    result = _bootstrap(db, cap, "client063")
    _tg_link(db, 1004, "client063")
    bind_telegram_after_registration(db, legacy_username="client063", telegram_id=1004, actor="bot")
    assert telegram_status(db, result["account_id"]) == "BOUND"


# --- classify_action ---------------------------------------------------------

def _snapshot_at(db, account_id, now):
    return account_grace_snapshot(db, account_id, now=now)


def test_action_waiting_for_registration_default(db):
    cap = _capability(db)
    result = _bootstrap(db, cap, "client064")
    start_grace_cohort(
        db, capability=cap, account_ids=[result["account_id"]], cohort_ref="PH4-05-ACTION-TEST",
        reason="test", cohort_start_at=2000,
    )
    snapshot = _snapshot_at(db, result["account_id"], 2000 + 86400)
    assert classify_action(snapshot) == ACTION_WAITING_FOR_REGISTRATION


def test_action_manual_review_for_ambiguous(db):
    cap = _capability(db)
    result = _bootstrap(db, cap, "client065")
    _tg_link(db, 1005, "client065")
    _tg_link(db, 1006, "client065")
    start_grace_cohort(
        db, capability=cap, account_ids=[result["account_id"]], cohort_ref="PH4-05-ACTION-TEST",
        reason="test", cohort_start_at=3000,
    )
    snapshot = _snapshot_at(db, result["account_id"], 3000 + 86400)
    assert classify_action(snapshot) == ACTION_MANUAL_REVIEW


def test_action_contact_user_near_expiry_unregistered(db):
    cap = _capability(db)
    result = _bootstrap(db, cap, "client066")
    start_grace_cohort(
        db, capability=cap, account_ids=[result["account_id"]], cohort_ref="PH4-05-ACTION-TEST",
        reason="test", cohort_start_at=4000,
    )
    near_expiry = 4000 + GRACE_PERIOD_SECONDS - 2 * 86400  # 2 days left
    snapshot = _snapshot_at(db, result["account_id"], near_expiry)
    assert classify_action(snapshot) == ACTION_CONTACT_USER


def test_action_reconcile_required_wins_over_everything(db):
    cap = _capability(db)
    result = _bootstrap(db, cap, "client067")
    account_id = result["account_id"]
    start_grace_cohort(
        db, capability=cap, account_ids=[account_id], cohort_ref="PH4-05-ACTION-TEST",
        reason="test", cohort_start_at=5000,
    )
    from tests.test_child_provisioning import _account as internal_account

    seeded_account, alias_id, _slot = internal_account(db, mapping="RECONCILE_MAPPING", alias="client067b")
    binding = db.migration_lifecycle.prepare_migration(
        account_id=seeded_account["account_id"], legacy_alias_id=alias_id,
        hwid_verifier="hmac-sha256:" + "a" * 64, actor_ref="test",
        reason="force error_reconcile for action test", idempotency_key="reconcile-key-0000001", now=5000,
    )
    db.migration_lifecycle.mark_error_reconcile(
        binding["operation_id"], expected_revision=binding["revision"], error_class="INTERNAL_ERROR", now=5001,
    )
    # start grace for the seeded account too, to exercise this via the report path
    start_grace_cohort(
        db, capability=cap, account_ids=[seeded_account["account_id"]], cohort_ref="PH4-05-ACTION-TEST",
        reason="test", cohort_start_at=5000,
    )
    snapshot = _snapshot_at(db, seeded_account["account_id"], 5000 + 86400)
    assert classify_action(snapshot) == ACTION_RECONCILE_REQUIRED


def test_action_ok_migrated_when_fully_migrated(db):
    from src.migration_lifecycle import process_migration_bridge_request
    from tests.test_migration_lifecycle import HWID_KEY, _seed_bridged_account_with_first_child, _hv
    from tests.test_opaque_resolver import _known_hwid_meta

    cap = _capability(db)
    account, alias_id, slot, remote, ensure_fn, subscription_fn = _seed_bridged_account_with_first_child(
        db, mapping="OK_MIGRATED_MAPPING", tg=930001,
    )
    hwid = "ok-migrated-report-hwid"
    result = process_migration_bridge_request(
        db, "alice", _known_hwid_meta(hwid), hmac_key=HWID_KEY, ensure_fn=ensure_fn,
        subscription_fn=subscription_fn, worker_id="report-test-worker", now=1000,
    )
    from src.opaque_resolver import OUTCOME_OK
    assert result.outcome == OUTCOME_OK
    binding = db.migration_lifecycle.find_by_device(account["account_id"], _hv(hwid))
    assert binding["state"] == "MIGRATED"

    start_grace_cohort(
        db, capability=cap, account_ids=[account["account_id"]], cohort_ref="PH4-05-ACTION-TEST",
        reason="test", cohort_start_at=1000,
    )
    snapshot = _snapshot_at(db, account["account_id"], 1000 + 86400)
    assert snapshot["migrated_devices"] > 0
    assert classify_action(snapshot) == ACTION_OK_MIGRATED
