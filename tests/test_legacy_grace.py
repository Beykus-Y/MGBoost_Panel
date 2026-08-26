"""PH4-05 LegacyGraceStore: fixed-window start, exact boundary semantics,
duplicate start, explicit audited extension, restart/persistence."""

import importlib
import os
import tempfile

import pytest

from src.legacy_grace import (
    GraceAlreadyStarted,
    GraceConflict,
    GraceStaleRevision,
    GraceTransitionError,
    PrimaryAdminRequired,
    day_index,
    grace_active,
    seconds_remaining,
)
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


@pytest.fixture
def data_dir(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("DATA_DIR", tmp)
    monkeypatch.setenv("PRIMARY_MGBOOST_ADMIN_ACTOR_ID", PRIMARY)
    monkeypatch.setenv("PRIMARY_MGBOOST_ADMIN_LOGIN", PRIMARY_LOGIN)
    import src.config as config
    import src.database as database
    importlib.reload(config)
    importlib.reload(database)
    database.DB_PATH = os.path.join(tmp, "db.sqlite3")
    yield database


def _capability(db):
    _raw, session = AdminSessionStore().create(PRIMARY_LOGIN, "legacy-grace-test-jwt")
    return db.primary_admin_authority.authorize_session(session)


def _account(db):
    return db.accounts.create_account("DIRECT")["id"]


# --- pure boundary helpers ---------------------------------------------------

def test_boundary_exact_less_than_end_is_active():
    assert grace_active(2000, now=1999) is True


def test_boundary_exact_equal_to_end_is_not_active():
    assert grace_active(2000, now=2000) is False


def test_boundary_past_end_is_not_active():
    assert grace_active(2000, now=2001) is False


def test_seconds_remaining_clamped_at_zero_after_end():
    assert seconds_remaining(2000, now=2500) == 0
    assert seconds_remaining(2000, now=1500) == 500


def test_day_index_starts_at_1():
    assert day_index(1000, now=1000) == 1
    assert day_index(1000, now=1000 + 86400) == 2
    assert day_index(1000, now=1000 + 13 * 86400 + 1) == 14


# --- start ---------------------------------------------------------------

def test_start_creates_exact_14_day_window(db):
    acct = _account(db)
    cap = _capability(db)
    row = db.legacy_grace.start(
        account_id=acct, cohort_ref="PH4-05-DRY-RUN", capability=cap,
        reason="canary cohort start", idempotency_key="grace-start-key-0001", now=5_000_000,
    )
    assert row["started_at"] == 5_000_000
    assert row["original_end_at"] == 5_000_000 + GRACE_PERIOD_SECONDS
    assert row["current_end_at"] == row["original_end_at"]
    assert row["revision"] == 1


def test_start_requires_primary_admin_capability(db):
    acct = _account(db)
    with pytest.raises(PrimaryAdminRequired):
        db.legacy_grace.start(
            account_id=acct, cohort_ref="c", capability=None, reason="x" * 5,
            idempotency_key="k" * 20, now=1000,
        )


def test_duplicate_start_same_idempotency_key_is_idempotent(db):
    acct = _account(db)
    cap = _capability(db)
    first = db.legacy_grace.start(
        account_id=acct, cohort_ref="cohort-a", capability=cap, reason="start",
        idempotency_key="same-key-000000001", now=1000,
    )
    second = db.legacy_grace.start(
        account_id=acct, cohort_ref="cohort-a", capability=cap, reason="start",
        idempotency_key="same-key-000000001", now=1000,
    )
    assert first == second


def test_duplicate_start_different_key_after_started_fails_closed(db):
    acct = _account(db)
    cap = _capability(db)
    db.legacy_grace.start(
        account_id=acct, cohort_ref="cohort-a", capability=cap, reason="start",
        idempotency_key="first-key-00000001", now=1000,
    )
    with pytest.raises(GraceAlreadyStarted):
        db.legacy_grace.start(
            account_id=acct, cohort_ref="cohort-a", capability=cap, reason="restart attempt",
            idempotency_key="second-key-0000001", now=2000,
        )
    # never mutated by the failed second attempt
    row = db.legacy_grace.find_by_account(acct)
    assert row["started_at"] == 1000
    assert row["revision"] == 1


def test_start_same_key_different_payload_conflicts(db):
    acct = _account(db)
    cap = _capability(db)
    db.legacy_grace.start(
        account_id=acct, cohort_ref="cohort-a", capability=cap, reason="start",
        idempotency_key="reused-key-0000001", now=1000,
    )
    other_acct = _account(db)
    with pytest.raises(GraceConflict):
        db.legacy_grace.start(
            account_id=other_acct, cohort_ref="cohort-a", capability=cap, reason="start",
            idempotency_key="reused-key-0000001", now=1000,
        )


# --- extend ----------------------------------------------------------------

def test_extend_requires_primary_admin_capability(db):
    acct = _account(db)
    cap = _capability(db)
    row = db.legacy_grace.start(
        account_id=acct, cohort_ref="c", capability=cap, reason="start",
        idempotency_key="k" * 20, now=1000,
    )
    with pytest.raises(PrimaryAdminRequired):
        db.legacy_grace.extend(
            account_id=acct, expected_revision=row["revision"], new_end_at=row["current_end_at"] + 1,
            capability=None, reason="explicit audited extension", now=2000,
        )


def test_extend_moves_end_forward_and_records_audited_event(db):
    acct = _account(db)
    cap = _capability(db)
    row = db.legacy_grace.start(
        account_id=acct, cohort_ref="c", capability=cap, reason="start",
        idempotency_key="k" * 20, now=1000,
    )
    new_end = row["current_end_at"] + 7 * 86400
    extended = db.legacy_grace.extend(
        account_id=acct, expected_revision=row["revision"], new_end_at=new_end,
        capability=cap, reason="owner approved 7-day support extension",
        evidence_ref="ticket-42", now=2000,
    )
    assert extended["current_end_at"] == new_end
    assert extended["original_end_at"] == row["original_end_at"]  # never rewritten
    assert extended["revision"] == row["revision"] + 1

    events = db.legacy_grace.list_events(acct)
    assert [e["event_type"] for e in events] == ["STARTED", "EXTENDED"]
    assert events[-1]["to_end_at"] == new_end
    assert events[-1]["evidence_ref"] == "ticket-42"


def test_extend_cannot_silently_shrink_or_noop(db):
    acct = _account(db)
    cap = _capability(db)
    row = db.legacy_grace.start(
        account_id=acct, cohort_ref="c", capability=cap, reason="start",
        idempotency_key="k" * 20, now=1000,
    )
    with pytest.raises(GraceTransitionError):
        db.legacy_grace.extend(
            account_id=acct, expected_revision=row["revision"], new_end_at=row["current_end_at"],
            capability=cap, reason="no-op attempt", now=2000,
        )
    with pytest.raises(GraceTransitionError):
        db.legacy_grace.extend(
            account_id=acct, expected_revision=row["revision"],
            new_end_at=row["current_end_at"] - 1, capability=cap, reason="shrink attempt", now=2000,
        )


def test_extend_stale_revision_rejected(db):
    acct = _account(db)
    cap = _capability(db)
    row = db.legacy_grace.start(
        account_id=acct, cohort_ref="c", capability=cap, reason="start",
        idempotency_key="k" * 20, now=1000,
    )
    db.legacy_grace.extend(
        account_id=acct, expected_revision=row["revision"], new_end_at=row["current_end_at"] + 100,
        capability=cap, reason="first extension", now=2000,
    )
    with pytest.raises(GraceStaleRevision):
        db.legacy_grace.extend(
            account_id=acct, expected_revision=row["revision"],  # stale, already bumped
            new_end_at=row["current_end_at"] + 200, capability=cap,
            reason="second extension using stale revision", now=3000,
        )


# --- restart / persistence --------------------------------------------------

def test_grace_period_survives_process_restart(data_dir):
    instance = data_dir.Database()
    acct = instance.accounts.create_account("DIRECT")["id"]
    cap = _capability(instance)
    started = instance.legacy_grace.start(
        account_id=acct, cohort_ref="restart-cohort", capability=cap, reason="start",
        idempotency_key="restart-key-0000001", now=10_000,
    )
    instance._conn.close()

    reopened = data_dir.Database()
    row = reopened.legacy_grace.find_by_account(acct)
    assert row["started_at"] == started["started_at"]
    assert row["current_end_at"] == started["current_end_at"]
    assert row["revision"] == started["revision"]
    reopened._conn.close()
