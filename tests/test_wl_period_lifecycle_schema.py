import os
import sqlite3
import tempfile

import pytest


@pytest.fixture
def db():
    tmp = tempfile.mkdtemp()
    os.environ["DATA_DIR"] = tmp
    from importlib import reload
    import src.config as cfg
    reload(cfg)
    cfg.DATA_DIR = tmp
    import src.database as db_mod
    reload(db_mod)
    db_mod.DB_PATH = os.path.join(tmp, "db.sqlite3")
    instance = db_mod.Database()
    yield instance
    instance._conn.close()


def _plan(db, *, code="WLPLAN", wl_quota=100_000_000_000, wl_period_days=30):
    plan = db.accounts.create_plan_version({
        "plan_code": code, "version": 1, "display_name": code,
        "plan_kind": "COMMERCIAL", "billing_required": True,
        "device_limit_mode": "LIMITED", "device_limit": 3,
        "wl_mode": "LIMITED", "wl_quota_bytes": wl_quota,
        "wl_period_days": wl_period_days, "terms": {},
    }, now=100)
    duration = db.accounts.add_plan_duration(plan["id"], 30, now=100)
    return plan, duration


def test_migration_is_idempotent(db):
    from src.wl_period_lifecycle_schema import (
        MIGRATION_ID, SCHEMA_CHECKSUM, apply_wl_period_lifecycle_schema,
    )

    row = db._conn.execute(
        "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
        (MIGRATION_ID,),
    ).fetchone()
    assert row["schema_checksum"] == SCHEMA_CHECKSUM
    assert apply_wl_period_lifecycle_schema(db._conn, now=200) is False


def test_migration_requires_exact_parent_ph3_01_schema():
    from src.wl_period_lifecycle_schema import apply_wl_period_lifecycle_schema

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE mgboost_schema_migrations "
        "(migration_id TEXT PRIMARY KEY, schema_checksum TEXT NOT NULL, applied_at INTEGER NOT NULL)"
    )
    conn.commit()
    with pytest.raises(RuntimeError, match="requires exact PH3-01 schema"):
        apply_wl_period_lifecycle_schema(conn, now=1)


def test_wl_period_identity_fields_and_rows_are_immutable(db):
    account = db.accounts.create_account("DIRECT", now=1)
    plan, duration = _plan(db)
    sub = db._conn.execute(
        "INSERT INTO mgboost_subscriptions "
        "(account_id,current_plan_version_id,status,started_at,current_expiry,"
        "created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
        (account["id"], plan["id"], "ACTIVE", 100, 100 + 30 * 86400, 100, 100),
    ).lastrowid
    mutation_id = db._conn.execute(
        "INSERT INTO mgboost_entitlement_mutations "
        "(account_id,subscription_id,operation,payment_channel,mutation_source,"
        "actor_type,idempotency_key_hash,created_at) VALUES (?,?,?,?,?,?,?,?)",
        (account["id"], sub, "RENEW", "TELEGRAM_STARS", "DIRECT_PURCHASE",
         "TELEGRAM_USER", "hash-1", 100),
    ).lastrowid
    term_id = db._conn.execute(
        "INSERT INTO mgboost_subscription_terms ("
        "account_id,subscription_id,sequence_no,plan_version_id,duration_id,"
        "duration_days,starts_at,ends_at,billing_required_snapshot,"
        "device_limit_mode_snapshot,device_limit_snapshot,wl_mode_snapshot,"
        "wl_quota_bytes_snapshot,wl_period_days_snapshot,plan_snapshot_json,"
        "mutation_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (account["id"], sub, 1, plan["id"], duration["id"], 30, 100, 100 + 30 * 86400,
         1, "LIMITED", 3, "LIMITED", 100_000_000_000, 30, "{}", mutation_id, 100),
    ).lastrowid
    period_id = db._conn.execute(
        "INSERT INTO mgboost_wl_periods "
        "(account_id,subscription_id,subscription_term_id,sequence_no,"
        "starts_at,ends_at,quota_mode,base_quota_bytes,status,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (account["id"], sub, term_id, 1, 100, 100 + 30 * 86400, "LIMITED",
         100_000_000_000, "PLANNED", 100),
    ).lastrowid
    db._conn.commit()

    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db._conn.execute(
            "UPDATE mgboost_wl_periods SET base_quota_bytes=1 WHERE id=?", (period_id,)
        )
    db._conn.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db._conn.execute(
            "UPDATE mgboost_wl_periods SET starts_at=0 WHERE id=?", (period_id,)
        )
    db._conn.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="never deleted"):
        db._conn.execute("DELETE FROM mgboost_wl_periods WHERE id=?", (period_id,))
    db._conn.rollback()

    # Status alone is not guarded by this trigger set (Phase 6's own future concern).
    db._conn.execute("UPDATE mgboost_wl_periods SET status='ACTIVE' WHERE id=?", (period_id,))
    db._conn.commit()
    assert db._conn.execute(
        "SELECT status FROM mgboost_wl_periods WHERE id=?", (period_id,)
    ).fetchone()[0] == "ACTIVE"
