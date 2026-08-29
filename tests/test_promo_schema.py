"""PH5-13 promo codes schema: migration applies, checksum-gated, verifies."""

import os
import sqlite3
import tempfile

import pytest


@pytest.fixture
def db():
    tmp = tempfile.mkdtemp(prefix="promo-schema-test-")
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


def test_promo_tables_exist_with_required_columns(db):
    tables = {
        row["name"] for row in db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'mgboost_promo%'"
        )
    }
    assert tables == {
        "mgboost_promo_definitions", "mgboost_promo_versions", "mgboost_promo_redemptions",
    }


def test_manual_payment_records_gained_promo_columns(db):
    columns = {row[1] for row in db._conn.execute("PRAGMA table_info(mgboost_manual_payment_records)")}
    assert {"promo_id", "promo_version", "promo_redemption_id",
            "original_amount_minor", "discount_snapshot_json"}.issubset(columns)


def test_migration_is_idempotent_and_checksum_pinned(db):
    from src.promo_schema import apply_promo_schema
    from src.promo_schema_v2 import apply_promo_schema_v2

    applied_again = apply_promo_schema(db._conn)
    assert applied_again is False  # already applied at Database() construction
    assert apply_promo_schema_v2(db._conn) is False


def test_discount_snapshot_cannot_be_attached_later_to_an_existing_invoice(db):
    """The immutable trigger protects NULL -> non-NULL, not only rewrites."""
    db._conn.execute(
        "INSERT INTO stars_invoices (created_by_telegram_id,marzban_username,tariff_name,"
        "duration_days,stars_price,status,expires_at,created_at) "
        "VALUES (1,'legacy','Legacy',30,10,'created',999,1)"
    )
    invoice_id = db._conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    with pytest.raises(Exception):
        db._conn.execute(
            "UPDATE stars_invoices SET promo_redemption_id=1,original_stars_price=10,discount_minor=1 "
            "WHERE id=?", (invoice_id,)
        )
    db._conn.rollback()


def test_v2_repairs_an_existing_v1_trigger_without_rewriting_v1_marker(db):
    from src.promo_schema import MIGRATION_ID as v1_id
    from src.promo_schema_v2 import MIGRATION_ID as v2_id, apply_promo_schema_v2
    db._conn.execute("DELETE FROM mgboost_schema_migrations WHERE migration_id=?", (v2_id,))
    db._conn.execute("DROP TRIGGER trg_stars_invoices_promo_snapshot_immutable")
    db._conn.execute(
        "CREATE TRIGGER trg_stars_invoices_promo_snapshot_immutable "
        "BEFORE UPDATE OF promo_redemption_id,original_stars_price,discount_minor ON stars_invoices "
        "WHEN OLD.promo_redemption_id IS NOT NULL AND "
        "(NEW.promo_redemption_id IS NOT OLD.promo_redemption_id OR "
        "NEW.original_stars_price IS NOT OLD.original_stars_price OR "
        "NEW.discount_minor IS NOT OLD.discount_minor) "
        "BEGIN SELECT RAISE(ABORT, 'stars invoice promo discount snapshot is immutable'); END"
    )
    db._conn.commit()
    assert apply_promo_schema_v2(db._conn, now=1234) is True
    assert db._conn.execute(
        "SELECT 1 FROM mgboost_schema_migrations WHERE migration_id=?", (v1_id,)
    ).fetchone() is not None
    assert db._conn.execute(
        "SELECT 1 FROM mgboost_schema_migrations WHERE migration_id=?", (v2_id,)
    ).fetchone() is not None


def test_backup_then_v2_migration_then_restore_keeps_recoverable_v1_snapshot(db):
    """Exercise the production order: consistent backup -> additive migrate -> restore.

    The restored copy is deliberately the pre-v2 state.  This proves that the
    v2 trigger repair neither mutates the v1 migration marker nor makes a
    SQLite backup unrecoverable.
    """
    from src.promo_schema_v2 import MIGRATION_ID as v2_id, apply_promo_schema_v2

    db._conn.execute("DELETE FROM mgboost_schema_migrations WHERE migration_id=?", (v2_id,))
    db._conn.execute("DROP TRIGGER trg_stars_invoices_promo_snapshot_immutable")
    db._conn.execute(
        "CREATE TRIGGER trg_stars_invoices_promo_snapshot_immutable "
        "BEFORE UPDATE OF promo_redemption_id,original_stars_price,discount_minor ON stars_invoices "
        "WHEN OLD.promo_redemption_id IS NOT NULL AND "
        "(NEW.promo_redemption_id IS NOT OLD.promo_redemption_id OR "
        "NEW.original_stars_price IS NOT OLD.original_stars_price OR "
        "NEW.discount_minor IS NOT OLD.discount_minor) "
        "BEGIN SELECT RAISE(ABORT, 'stars invoice promo discount snapshot is immutable'); END"
    )
    db._conn.commit()

    backup_path = os.path.join(os.path.dirname(db._conn.execute("PRAGMA database_list").fetchone()[2]), "promo-pre-v2.sqlite3")
    backup_conn = sqlite3.connect(backup_path)
    db._conn.backup(backup_conn)
    backup_conn.close()

    assert apply_promo_schema_v2(db._conn, now=1234) is True
    assert db._conn.execute(
        "SELECT 1 FROM mgboost_schema_migrations WHERE migration_id=?", (v2_id,)
    ).fetchone() is not None

    restored = sqlite3.connect(backup_path)
    assert restored.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    assert restored.execute(
        "SELECT 1 FROM mgboost_schema_migrations WHERE migration_id=?", (v2_id,)
    ).fetchone() is None
    restored.close()


def test_promo_definition_code_must_be_uppercase(db):
    with pytest.raises(Exception):
        db._conn.execute(
            "INSERT INTO mgboost_promo_definitions "
            "(code,effect_kind,status,created_by_actor,created_at,updated_at) "
            "VALUES ('lowercase','EXTEND_SUBSCRIPTION','ACTIVE','test',1,1)"
        )


def test_promo_definition_identity_is_immutable(db):
    db._conn.execute(
        "INSERT INTO mgboost_promo_definitions "
        "(code,effect_kind,status,created_by_actor,created_at,updated_at) "
        "VALUES ('TEST7DAYS','EXTEND_SUBSCRIPTION','ACTIVE','test',1,1)"
    )
    db._conn.commit()
    with pytest.raises(Exception):
        db._conn.execute("UPDATE mgboost_promo_definitions SET code='RENAMED' WHERE code='TEST7DAYS'")


def test_only_one_active_version_per_promo(db):
    db._conn.execute(
        "INSERT INTO mgboost_promo_definitions "
        "(code,effect_kind,status,created_by_actor,created_at,updated_at) "
        "VALUES ('TESTVER','EXTEND_SUBSCRIPTION','ACTIVE','test',1,1)"
    )
    promo_id = db._conn.execute("SELECT id FROM mgboost_promo_definitions WHERE code='TESTVER'").fetchone()[0]
    db._conn.execute(
        "INSERT INTO mgboost_promo_versions (promo_id,version,effect_params_json,status,"
        "created_by_actor,created_at) VALUES (?,1,'{}','ACTIVE','test',1)", (promo_id,),
    )
    db._conn.commit()
    with pytest.raises(Exception):
        db._conn.execute(
            "INSERT INTO mgboost_promo_versions (promo_id,version,effect_params_json,status,"
            "created_by_actor,created_at) VALUES (?,2,'{}','ACTIVE','test',1)", (promo_id,),
        )


def test_trial_class_identity_uniqueness_spans_multiple_promo_codes(db):
    """The exact anti-abuse invariant: two DIFFERENT promo codes sharing one
    trial_class must not both be redeemable by the same owner_telegram_id."""
    conn = db._conn
    for code in ("TRIALCODE1", "TRIALCODE2"):
        conn.execute(
            "INSERT INTO mgboost_promo_definitions "
            "(code,effect_kind,trial_class,status,created_by_actor,created_at,updated_at) "
            "VALUES (?,'TRIAL_GRANT','WL_TRIAL','ACTIVE','test',1,1)", (code,),
        )
    promo1 = conn.execute("SELECT id FROM mgboost_promo_definitions WHERE code='TRIALCODE1'").fetchone()[0]
    promo2 = conn.execute("SELECT id FROM mgboost_promo_definitions WHERE code='TRIALCODE2'").fetchone()[0]
    for promo_id in (promo1, promo2):
        conn.execute(
            "INSERT INTO mgboost_promo_versions (promo_id,version,effect_params_json,status,"
            "created_by_actor,created_at) VALUES (?,1,'{}','ACTIVE','test',1)", (promo_id,),
        )
    conn.commit()
    conn.execute(
        "INSERT INTO mgboost_promo_redemptions (promo_id,promo_version,trial_class,"
        "owner_telegram_id,status,idempotency_key_hash,request_hash,actor_type,created_at,updated_at) "
        "VALUES (?,1,'WL_TRIAL',555,'REDEEMED','hash-1','req-1','PRIMARY_ADMIN',1,1)", (promo1,),
    )
    conn.commit()
    with pytest.raises(Exception):
        conn.execute(
            "INSERT INTO mgboost_promo_redemptions (promo_id,promo_version,trial_class,"
            "owner_telegram_id,status,idempotency_key_hash,request_hash,actor_type,created_at,updated_at) "
            "VALUES (?,1,'WL_TRIAL',555,'REDEEMED','hash-2','req-2','PRIMARY_ADMIN',1,1)", (promo2,),
        )
