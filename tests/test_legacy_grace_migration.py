"""PH4-03 mass-migration batch orchestration for PH4-05-bootstrapped
(ABSENT-ownership) accounts: genesis child on slot 1 -> bridge enable ->
real device transparently migrates via the unmodified PH4-01/02 resolver,
with zero second resolver and zero change to `routes/sub.py`."""

import importlib
import os
import tempfile

import pytest

from src.legacy_grace_migration import (
    GraceMigrationError,
    PrerequisiteMissing,
    migrate_bootstrapped_account,
)
from src.legacy_grace_registration import bootstrap_grace_subject
from src.security import AdminSessionStore

from tests.test_child_provisioning import HWID_KEY, PRIMARY, PRIMARY_LOGIN
from tests.test_opaque_resolver import _remote_and_ensure_fn


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
    _raw, session = AdminSessionStore().create(PRIMARY_LOGIN, "grace-migration-test-jwt")
    return db.primary_admin_authority.authorize_session(session)


def _bootstrap_with_entitlement(db, cap, username, *, now=1000):
    return bootstrap_grace_subject(
        db, capability=cap, legacy_username=username, legacy_status="ACTIVE",
        legacy_expiry=None, observed_device_count=1, observed_hwid_count=1,
        decision_ref="mass-grace-campaign-2026-08-26",
        payment_decision_ref="owner-attested-legacy-external-payment-2026",
        payment_attestation_note="Historical direct payment, no invented amount/date.",
        payment_evidence={"source": "owner-decision-2026-08-26"},
        idempotency_key=f"grace-bootstrap-v1:{username}", now=now,
    )


def _remote_for(username):
    remote, ensure_fn, subscription_fn = _remote_and_ensure_fn()
    remote.users[username] = remote.users.pop("alice")
    remote.users[username]["username"] = username
    return remote, ensure_fn, subscription_fn


def test_migrate_bootstrapped_account_happy_path(db):
    cap = _capability(db)
    _bootstrap_with_entitlement(db, cap, "client_mig_1")
    remote, ensure_fn, _sub_fn = _remote_for("client_mig_1")
    account_id = db._conn.execute(
        "SELECT account_id FROM mgboost_legacy_account_aliases WHERE legacy_username=?",
        ("client_mig_1",),
    ).fetchone()["account_id"]

    result = migrate_bootstrapped_account(
        db, capability=cap, account_id=account_id, hmac_key=HWID_KEY,
        marzban_user_snapshot=remote.users["client_mig_1"], ensure_fn=ensure_fn,
        decision_ref="mass-grace-campaign-2026-08-26", worker_id="test-migration-worker", now=2000,
    )
    assert result["bridge_enabled"] is True
    assert result["already_migrated"] is False
    assert result["genesis_child_username"] is not None

    binding = db._conn.execute(
        "SELECT enabled FROM mgboost_legacy_bridge_bindings WHERE account_id=?", (account_id,),
    ).fetchone()
    assert binding["enabled"] == 1
    intent = db._conn.execute(
        "SELECT observed_state FROM mgboost_child_user_intents WHERE account_id=?", (account_id,),
    ).fetchone()
    assert intent["observed_state"] == "ACTIVE"


def test_migrate_is_idempotent_no_duplicate_child_or_binding(db):
    cap = _capability(db)
    _bootstrap_with_entitlement(db, cap, "client_mig_2")
    remote, ensure_fn, _sub_fn = _remote_for("client_mig_2")
    account_id = db._conn.execute(
        "SELECT account_id FROM mgboost_legacy_account_aliases WHERE legacy_username=?",
        ("client_mig_2",),
    ).fetchone()["account_id"]

    first = migrate_bootstrapped_account(
        db, capability=cap, account_id=account_id, hmac_key=HWID_KEY,
        marzban_user_snapshot=remote.users["client_mig_2"], ensure_fn=ensure_fn,
        decision_ref="mass-grace-campaign-2026-08-26", worker_id="test-migration-worker", now=2000,
    )
    second = migrate_bootstrapped_account(
        db, capability=cap, account_id=account_id, hmac_key=HWID_KEY,
        marzban_user_snapshot=remote.users["client_mig_2"], ensure_fn=ensure_fn,
        decision_ref="mass-grace-campaign-2026-08-26", worker_id="test-migration-worker", now=2001,
    )
    assert second["already_migrated"] is True
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_child_user_intents WHERE account_id=?", (account_id,),
    ).fetchone()[0] == 1
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_legacy_bridge_bindings WHERE account_id=?", (account_id,),
    ).fetchone()[0] == 1
    assert first["genesis_child_username"] == db._conn.execute(
        "SELECT child_username FROM mgboost_child_user_intents WHERE account_id=?", (account_id,),
    ).fetchone()["child_username"]


def test_migrate_fails_closed_without_entitlement(db):
    """Accounts 8/10/11/13-style: bootstrapped but no subscription yet
    (DeviceOverageConflict pending owner decision) must never migrate."""
    cap = _capability(db)
    account = db.direct_enrollment.enroll_direct_account(
        capability=cap, legacy_username="client_mig_3", decision_ref="mass-grace-campaign-2026-08-26",
        ownership_evidence="ABSENT", telegram_id=None, alias_provenance="EVIDENCE_PROVEN",
        legacy_status="ACTIVE", legacy_expiry=None, observed_device_count=8, observed_hwid_count=8,
        evidence={"source": "test"}, idempotency_key="grace-bootstrap-v1:client_mig_3", now=1000,
    )
    remote, ensure_fn, _sub_fn = _remote_for("client_mig_3")
    with pytest.raises(PrerequisiteMissing):
        migrate_bootstrapped_account(
            db, capability=cap, account_id=account["account_id"], hmac_key=HWID_KEY,
            marzban_user_snapshot=remote.users["client_mig_3"], ensure_fn=ensure_fn,
            decision_ref="mass-grace-campaign-2026-08-26", worker_id="test-migration-worker", now=2000,
        )
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_legacy_bridge_bindings WHERE account_id=?",
        (account["account_id"],),
    ).fetchone()[0] == 0


def test_real_device_transparently_migrates_after_bridge_enable(db):
    """No second resolver: after migrate_bootstrapped_account() enables the
    bridge, the customer's REAL device (a different HWID than the genesis
    placeholder) migrates through the exact unmodified
    process_migration_bridge_request()/resolve_legacy_bridge() path."""
    from src.migration_lifecycle import process_migration_bridge_request
    from src.opaque_resolver import OUTCOME_OK

    cap = _capability(db)
    _bootstrap_with_entitlement(db, cap, "client_mig_4")
    remote, ensure_fn, subscription_fn = _remote_for("client_mig_4")
    account_id = db._conn.execute(
        "SELECT account_id FROM mgboost_legacy_account_aliases WHERE legacy_username=?",
        ("client_mig_4",),
    ).fetchone()["account_id"]

    migrate_bootstrapped_account(
        db, capability=cap, account_id=account_id, hmac_key=HWID_KEY,
        marzban_user_snapshot=remote.users["client_mig_4"], ensure_fn=ensure_fn,
        decision_ref="mass-grace-campaign-2026-08-26", worker_id="test-migration-worker", now=2000,
    )

    from tests.test_opaque_resolver import _known_hwid_meta

    real_hwid = "client-real-phone-hwid"
    result = process_migration_bridge_request(
        db, "client_mig_4", _known_hwid_meta(real_hwid), hmac_key=HWID_KEY,
        ensure_fn=ensure_fn, subscription_fn=subscription_fn, worker_id="real-device-worker", now=3000,
    )
    assert result.outcome == OUTCOME_OK
    assert result.child_username is not None

    from src.device_slots import privacy_safe_hwid
    real_hwid_verifier, _masked = privacy_safe_hwid(real_hwid, HWID_KEY)
    binding = db.migration_lifecycle.find_by_device(account_id, real_hwid_verifier)
    assert binding["state"] == "MIGRATED"

    # the genesis placeholder and the real device are two SEPARATE children
    child_usernames = {
        row["child_username"] for row in db._conn.execute(
            "SELECT child_username FROM mgboost_child_user_intents WHERE account_id=?", (account_id,),
        ).fetchall()
    }
    assert len(child_usernames) == 2
    assert result.child_username in child_usernames
