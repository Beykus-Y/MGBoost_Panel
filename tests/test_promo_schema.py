"""PH5-13 promo codes schema: migration applies, checksum-gated, verifies."""

import os
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

    applied_again = apply_promo_schema(db._conn)
    assert applied_again is False  # already applied at Database() construction


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
