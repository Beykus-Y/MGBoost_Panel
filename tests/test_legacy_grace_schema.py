"""PH4-05 schema: fixed 14-day policy, idempotent apply, immutability."""

import importlib
import os
import tempfile

import pytest

from src.legacy_grace_schema import GRACE_PERIOD_SECONDS

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


def test_fixed_14_day_policy_opd09_dl023():
    assert GRACE_PERIOD_SECONDS == 14 * 86400 == 1209600


def test_schema_applied_and_reapply_is_idempotent(db):
    from src.legacy_grace_schema import apply_legacy_grace_schema

    assert apply_legacy_grace_schema(db._conn) is False  # already applied in _create_tables


def test_original_end_at_check_enforces_exact_14_days(db):
    acct = db.accounts.create_account("DIRECT")
    started = 1000
    with pytest.raises(Exception):
        db._conn.execute(
            "INSERT INTO mgboost_legacy_grace_periods "
            "(account_id,cohort_ref,started_at,original_end_at,current_end_at,revision,"
            "idempotency_key_hash,request_hash,actor_ref,reason,created_at,updated_at) "
            "VALUES (?,?,?,?,?,1,?,?,?,?,?,?)",
            (
                acct["id"], "cohort-x", started, started + GRACE_PERIOD_SECONDS - 1,
                started + GRACE_PERIOD_SECONDS - 1, "a" * 64, "b" * 64, "actor", "reason", started,
                started,
            ),
        )


def test_current_end_at_cannot_decrease(db):
    acct = db.accounts.create_account("DIRECT")
    started = 1000
    end_at = started + GRACE_PERIOD_SECONDS
    db._conn.execute(
        "INSERT INTO mgboost_legacy_grace_periods "
        "(account_id,cohort_ref,started_at,original_end_at,current_end_at,revision,"
        "idempotency_key_hash,request_hash,actor_ref,reason,created_at,updated_at) "
        "VALUES (?,?,?,?,?,1,?,?,?,?,?,?)",
        (acct["id"], "cohort-x", started, end_at, end_at, "a" * 64, "b" * 64,
         "actor", "reason", started, started),
    )
    db._conn.commit()
    with pytest.raises(Exception):
        db._conn.execute(
            "UPDATE mgboost_legacy_grace_periods SET current_end_at=? WHERE account_id=?",
            (end_at - 1, acct["id"]),
        )


def test_identity_columns_immutable(db):
    acct = db.accounts.create_account("DIRECT")
    started = 1000
    end_at = started + GRACE_PERIOD_SECONDS
    db._conn.execute(
        "INSERT INTO mgboost_legacy_grace_periods "
        "(account_id,cohort_ref,started_at,original_end_at,current_end_at,revision,"
        "idempotency_key_hash,request_hash,actor_ref,reason,created_at,updated_at) "
        "VALUES (?,?,?,?,?,1,?,?,?,?,?,?)",
        (acct["id"], "cohort-x", started, end_at, end_at, "a" * 64, "b" * 64,
         "actor", "reason", started, started),
    )
    db._conn.commit()
    with pytest.raises(Exception):
        db._conn.execute(
            "UPDATE mgboost_legacy_grace_periods SET started_at=? WHERE account_id=?",
            (started + 1, acct["id"]),
        )


def test_grace_periods_cannot_be_deleted(db):
    acct = db.accounts.create_account("DIRECT")
    started = 1000
    end_at = started + GRACE_PERIOD_SECONDS
    db._conn.execute(
        "INSERT INTO mgboost_legacy_grace_periods "
        "(account_id,cohort_ref,started_at,original_end_at,current_end_at,revision,"
        "idempotency_key_hash,request_hash,actor_ref,reason,created_at,updated_at) "
        "VALUES (?,?,?,?,?,1,?,?,?,?,?,?)",
        (acct["id"], "cohort-x", started, end_at, end_at, "a" * 64, "b" * 64,
         "actor", "reason", started, started),
    )
    db._conn.commit()
    with pytest.raises(Exception):
        db._conn.execute(
            "DELETE FROM mgboost_legacy_grace_periods WHERE account_id=?", (acct["id"],),
        )


def test_events_are_immutable_no_update_no_delete(db):
    acct = db.accounts.create_account("DIRECT")
    started = 1000
    end_at = started + GRACE_PERIOD_SECONDS
    cur = db._conn.execute(
        "INSERT INTO mgboost_legacy_grace_periods "
        "(account_id,cohort_ref,started_at,original_end_at,current_end_at,revision,"
        "idempotency_key_hash,request_hash,actor_ref,reason,created_at,updated_at) "
        "VALUES (?,?,?,?,?,1,?,?,?,?,?,?)",
        (acct["id"], "cohort-x", started, end_at, end_at, "a" * 64, "b" * 64,
         "actor", "reason", started, started),
    )
    db._conn.execute(
        "INSERT INTO mgboost_legacy_grace_events "
        "(grace_period_id,account_id,event_type,from_end_at,to_end_at,actor_ref,reason,"
        "evidence_ref,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (cur.lastrowid, acct["id"], "STARTED", None, end_at, "actor", "reason",
         None, started),
    )
    db._conn.commit()
    with pytest.raises(Exception):
        db._conn.execute(
            "UPDATE mgboost_legacy_grace_events SET reason=? WHERE grace_period_id=?",
            ("changed", cur.lastrowid),
        )
    with pytest.raises(Exception):
        db._conn.execute(
            "DELETE FROM mgboost_legacy_grace_events WHERE grace_period_id=?", (cur.lastrowid,),
        )
