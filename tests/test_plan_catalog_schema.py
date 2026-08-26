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


def test_migration_is_idempotent_and_new_runtime_tables_start_empty(db):
    from src.plan_catalog_schema import (
        MIGRATION_ID, NEW_RUNTIME_TABLES, SCHEMA_CHECKSUM,
        apply_plan_catalog_schema,
    )

    migration = db._conn.execute(
        "SELECT * FROM mgboost_schema_migrations WHERE migration_id=?", (MIGRATION_ID,)
    ).fetchone()
    assert migration["schema_checksum"] == SCHEMA_CHECKSUM
    assert apply_plan_catalog_schema(db._conn, now=200) is False
    assert {table: db._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in NEW_RUNTIME_TABLES} == {table: 0 for table in NEW_RUNTIME_TABLES}


def test_migration_requires_exact_parent_ph3_01_schema():
    from src.plan_catalog_schema import apply_plan_catalog_schema

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE mgboost_schema_migrations "
        "(migration_id TEXT PRIMARY KEY, schema_checksum TEXT NOT NULL, applied_at INTEGER NOT NULL)"
    )
    conn.commit()
    with pytest.raises(RuntimeError, match="requires exact PH3-01 schema"):
        apply_plan_catalog_schema(conn, now=1)
    assert conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name='mgboost_plan_prices'"
    ).fetchone()[0] == 0


def test_catalog_versions_and_prices_are_immutable(db):
    plan = db.accounts.create_plan_version({
        "plan_code": "TESTPLAN", "version": 1, "display_name": "Test",
        "plan_kind": "COMMERCIAL", "billing_required": True,
        "device_limit_mode": "LIMITED", "device_limit": 3,
        "wl_mode": "NONE", "wl_quota_bytes": None, "wl_period_days": None,
        "terms": {},
    }, now=100)
    duration = db.accounts.add_plan_duration(plan["id"], 30, now=100)

    catalog = db.plan_catalog.get_or_create_catalog_version(
        "TELEGRAM_STARS", "STARS-TEST-v1", now=100
    )
    price = db.plan_catalog.get_or_create_price(
        catalog_version_id=catalog["id"], plan_version_id=plan["id"],
        duration_id=duration["id"], amount=99, now=100,
    )

    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db._conn.execute("UPDATE mgboost_plan_prices SET amount=1 WHERE id=?", (price["id"],))
    db._conn.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db._conn.execute("DELETE FROM mgboost_plan_prices WHERE id=?", (price["id"],))
    db._conn.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="never deleted"):
        db._conn.execute(
            "DELETE FROM mgboost_price_catalog_versions WHERE id=?", (catalog["id"],)
        )
    db._conn.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="identity fields are immutable"):
        db._conn.execute(
            "UPDATE mgboost_price_catalog_versions SET catalog_version='X' WHERE id=?",
            (catalog["id"],),
        )
    db._conn.rollback()


def test_only_one_active_catalog_version_per_channel(db):
    from src.plan_catalog import PlanCatalogError

    db.plan_catalog.get_or_create_catalog_version("RUB", "RUB-v1", now=100)
    with pytest.raises(PlanCatalogError, match="already has an active"):
        db.plan_catalog.get_or_create_catalog_version("RUB", "RUB-v2", now=100)
    # A different channel is independent.
    db.plan_catalog.get_or_create_catalog_version("TELEGRAM_STARS", "STARS-v1", now=100)


def test_price_requires_positive_integer_amount(db):
    from src.plan_catalog import PlanCatalogError

    plan = db.accounts.create_plan_version({
        "plan_code": "TESTPLAN2", "version": 1, "display_name": "Test",
        "plan_kind": "COMMERCIAL", "billing_required": True,
        "device_limit_mode": "LIMITED", "device_limit": 3,
        "wl_mode": "NONE", "wl_quota_bytes": None, "wl_period_days": None,
        "terms": {},
    }, now=100)
    duration = db.accounts.add_plan_duration(plan["id"], 30, now=100)
    catalog = db.plan_catalog.get_or_create_catalog_version("RUB", "RUB-v1", now=100)
    for bad in (0, -5, 1.5, True):
        with pytest.raises(PlanCatalogError):
            db.plan_catalog.get_or_create_price(
                catalog_version_id=catalog["id"], plan_version_id=plan["id"],
                duration_id=duration["id"], amount=bad, now=100,
            )
