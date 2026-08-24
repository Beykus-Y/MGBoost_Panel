import hashlib
import importlib.util
import json
import os
import sqlite3
import tempfile
import threading

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


def _plan(store, *, code="BASE", devices=3, wl_mode="NONE", quota=None,
          period=None, kind="COMMERCIAL", billing=True, version=1):
    return store.create_plan_version({
        "plan_code": code,
        "version": version,
        "display_name": code,
        "plan_kind": kind,
        "billing_required": billing,
        "device_limit_mode": "UNLIMITED" if devices is None else "LIMITED",
        "device_limit": devices,
        "wl_mode": wl_mode,
        "wl_quota_bytes": quota,
        "wl_period_days": period,
        "terms": {"schema": 1, "non_wl": "unlimited"},
    }, now=100)


def _subscription(conn, account_id, plan_id, *, status="ACTIVE", expiry=9999):
    cur = conn.execute(
        "INSERT INTO mgboost_subscriptions "
        "(account_id,current_plan_version_id,status,started_at,current_expiry,"
        "created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
        (account_id, plan_id, status, 100, expiry, 100, 100),
    )
    conn.commit()
    return cur.lastrowid


def _mutation(conn, account_id, subscription_id, *, ref="payment-1"):
    cur = conn.execute(
        "INSERT INTO mgboost_entitlement_mutations "
        "(account_id,subscription_id,operation,payment_channel,mutation_source,"
        "actor_type,external_reference,idempotency_key_hash,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (account_id, subscription_id, "RENEW", "TELEGRAM_STARS",
         "DIRECT_PURCHASE", "TELEGRAM_USER", ref,
         hashlib.sha256(ref.encode()).hexdigest(), 100),
    )
    conn.commit()
    return cur.lastrowid


def test_migration_is_idempotent_and_new_runtime_tables_start_empty(db):
    from src.account_schema import (
        MIGRATION_ID, NEW_RUNTIME_TABLES, SCHEMA_CHECKSUM,
        apply_parent_account_schema,
    )

    migration = db._conn.execute(
        "SELECT * FROM mgboost_schema_migrations WHERE migration_id=?", (MIGRATION_ID,)
    ).fetchone()
    assert migration["schema_checksum"] == SCHEMA_CHECKSUM
    assert apply_parent_account_schema(db._conn, now=200) is False
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_schema_migrations WHERE migration_id=?",
        (MIGRATION_ID,),
    ).fetchone()[0] == 1
    assert {table: db._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in NEW_RUNTIME_TABLES} == {table: 0 for table in NEW_RUNTIME_TABLES}


def test_migration_refuses_incompatible_preexisting_table_without_marker():
    from src.account_schema import apply_parent_account_schema

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE mgboost_accounts (id INTEGER PRIMARY KEY)")
    conn.commit()
    with pytest.raises(RuntimeError, match="incompatible table"):
        apply_parent_account_schema(conn, now=1)
    assert conn.execute(
        "SELECT COUNT(*) FROM sqlite_master "
        "WHERE type='table' AND name='mgboost_schema_migrations'"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name='mgboost_subscriptions'"
    ).fetchone()[0] == 0


def test_additive_migration_preserves_representative_legacy_schema_and_rows(tmp_path):
    from src.account_schema import NEW_RUNTIME_TABLES, apply_parent_account_schema

    path = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE tg_users (
            telegram_id INTEGER PRIMARY KEY,
            marzban_username TEXT NOT NULL,
            registered_at INTEGER NOT NULL
        );
        CREATE TABLE user_devices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            token TEXT NOT NULL,
            request_key TEXT NOT NULL,
            is_active INTEGER DEFAULT 1,
            first_seen INTEGER NOT NULL,
            last_seen INTEGER NOT NULL,
            UNIQUE(username, request_key)
        );
        CREATE TABLE hwid_lock (
            request_key TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            locked_at INTEGER NOT NULL
        );
        INSERT INTO tg_users VALUES (1001, 'legacy-a', 10);
        INSERT INTO user_devices
            (username,token,request_key,is_active,first_seen,last_seen)
            VALUES ('legacy-a','sha256:masked','hwid:masked',1,10,20);
        INSERT INTO hwid_lock VALUES ('hwid:masked','legacy-a',10);
    """)
    before_schema = {
        row["name"]: row["sql"] for row in conn.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='table' "
            "AND name IN ('tg_users','user_devices','hwid_lock')"
        )
    }
    before_rows = {
        "tg_users": [tuple(row) for row in conn.execute("SELECT * FROM tg_users")],
        "user_devices": [tuple(row) for row in conn.execute("SELECT * FROM user_devices")],
        "hwid_lock": [tuple(row) for row in conn.execute("SELECT * FROM hwid_lock")],
    }

    assert apply_parent_account_schema(conn, now=123) is True
    assert apply_parent_account_schema(conn, now=124) is False

    after_schema = {
        row["name"]: row["sql"] for row in conn.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='table' "
            "AND name IN ('tg_users','user_devices','hwid_lock')"
        )
    }
    assert after_schema == before_schema
    assert [tuple(row) for row in conn.execute("SELECT * FROM tg_users")] == before_rows["tg_users"]
    assert [tuple(row) for row in conn.execute("SELECT * FROM user_devices")] == before_rows["user_devices"]
    assert [tuple(row) for row in conn.execute("SELECT * FROM hwid_lock")] == before_rows["hwid_lock"]
    assert all(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
               for table in NEW_RUNTIME_TABLES)
    # Rollback means an old binary may continue using legacy tables while it
    # ignores additive PH3 tables; it does not require destructive down-DDL.
    conn.execute("UPDATE tg_users SET registered_at=11 WHERE telegram_id=1001")
    conn.commit()
    assert conn.execute("SELECT registered_at FROM tg_users").fetchone()[0] == 11


def test_account_has_no_marzban_username_and_telegram_identity_is_unique(db):
    from src.account_store import IdentityConflict

    columns = {row["name"] for row in db._conn.execute("PRAGMA table_info(mgboost_accounts)")}
    assert "marzban_username" not in columns
    assert "telegram_id" not in columns

    first = db.accounts.create_account("DIRECT", now=1)
    second = db.accounts.create_account("DIRECT", now=1)
    link = db.accounts.link_telegram_owner(
        first["id"], 123456789, provenance="DIRECT_BIND", now=2
    )
    assert link["account_id"] == first["id"]
    assert db.accounts.get_account_for_telegram(123456789)["id"] == first["id"]
    # Idempotent same binding is accepted; numeric Telegram ID is only a
    # lookup link and this repository method is not an authentication path.
    assert db.accounts.link_telegram_owner(
        first["id"], 123456789, provenance="DIRECT_BIND", now=3
    )["id"] == link["id"]
    with pytest.raises(IdentityConflict):
        db.accounts.link_telegram_owner(
            second["id"], 123456789, provenance="DIRECT_BIND", now=3
        )
    with pytest.raises(IdentityConflict):
        db.accounts.link_telegram_owner(
            first["id"], 987654321, provenance="DIRECT_BIND", now=3
        )


def test_revoked_telegram_history_allows_atomic_future_rebind_target(db):
    first = db.accounts.create_account("UNKNOWN_LEGACY", now=1)
    second = db.accounts.create_account("DIRECT", now=1)
    link = db.accounts.link_telegram_owner(
        first["id"], 777, provenance="UNKNOWN_LEGACY", now=2
    )
    db._conn.execute(
        "UPDATE mgboost_telegram_identities SET revoked_at=?,revoke_reason=?,"
        "revoked_by_actor=? WHERE id=?",
        (3, "owner-approved recovery", "primary-admin", link["id"]),
    )
    db._conn.commit()
    rebound = db.accounts.link_telegram_owner(
        second["id"], 777, provenance="ADMIN_REBIND",
        actor="primary-admin", now=3,
    )
    assert rebound["account_id"] == second["id"]
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_telegram_identities WHERE telegram_id=777"
    ).fetchone()[0] == 2


def test_concurrent_identity_claim_has_exactly_one_winner(db):
    from src.account_store import AccountStore, IdentityConflict

    first = db.accounts.create_account("DIRECT", now=1)
    second = db.accounts.create_account("DIRECT", now=1)
    path = db._conn.execute("PRAGMA database_list").fetchone()[2]
    other_conn = sqlite3.connect(path, check_same_thread=False)
    other_conn.row_factory = sqlite3.Row
    other_conn.execute("PRAGMA foreign_keys=ON")
    other = AccountStore(other_conn, threading.RLock())
    barrier = threading.Barrier(2)
    results = []

    def claim(store, account_id):
        barrier.wait()
        try:
            store.link_telegram_owner(
                account_id, 424242, provenance="DIRECT_BIND", now=2
            )
            results.append("won")
        except IdentityConflict:
            results.append("conflict")

    threads = [
        threading.Thread(target=claim, args=(db.accounts, first["id"])),
        threading.Thread(target=claim, args=(other, second["id"])),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    other_conn.close()
    assert sorted(results) == ["conflict", "won"]
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_telegram_identities "
        "WHERE telegram_id=424242 AND revoked_at IS NULL"
    ).fetchone()[0] == 1


def test_account_scoped_subscription_lookup_and_composite_foreign_keys_block_idor(db):
    first = db.accounts.create_account("DIRECT", now=1)
    second = db.accounts.create_account("DIRECT", now=1)
    plan = _plan(db.accounts)
    subscription_id = _subscription(db._conn, first["id"], plan["id"])

    assert db.accounts.get_subscription_for_account(first["id"], subscription_id)
    assert db.accounts.get_subscription_for_account(second["id"], subscription_id) is None
    with pytest.raises(sqlite3.IntegrityError):
        db._conn.execute(
            "INSERT INTO mgboost_entitlement_state "
            "(account_id,subscription_id,desired_status,updated_at) VALUES (?,?,?,?)",
            (second["id"], subscription_id, "ACTIVE", 100),
        )
    db._conn.rollback()


def test_versioned_plan_snapshot_is_immutable_and_supports_180_days(db):
    account = db.accounts.create_account("DIRECT", now=1)
    plan = _plan(
        db.accounts, code="WL", devices=3, wl_mode="LIMITED",
        quota=100_000_000_000, period=30,
    )
    duration = db.accounts.add_plan_duration(plan["id"], 180, now=2)
    subscription_id = _subscription(db._conn, account["id"], plan["id"])
    mutation_id = _mutation(db._conn, account["id"], subscription_id)
    snapshot = {
        "plan_code": "WL", "plan_version": 1, "duration_days": 180,
        "device_limit": 3, "wl_quota_bytes": 100_000_000_000,
        "wl_period_days": 30,
    }
    term = db._conn.execute(
        "INSERT INTO mgboost_subscription_terms ("
        "account_id,subscription_id,sequence_no,plan_version_id,duration_id,"
        "duration_days,starts_at,ends_at,billing_required_snapshot,"
        "device_limit_mode_snapshot,device_limit_snapshot,wl_mode_snapshot,"
        "wl_quota_bytes_snapshot,wl_period_days_snapshot,plan_snapshot_json,"
        "mutation_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (account["id"], subscription_id, 1, plan["id"], duration["id"], 180,
         1_000, 1_000 + 180 * 86400, 1, "LIMITED", 3, "LIMITED",
         100_000_000_000, 30, json.dumps(snapshot, sort_keys=True), mutation_id, 100),
    )
    db._conn.commit()
    stored = db._conn.execute(
        "SELECT * FROM mgboost_subscription_terms WHERE id=?", (term.lastrowid,)
    ).fetchone()
    assert json.loads(stored["plan_snapshot_json"]) == snapshot
    assert stored["duration_days"] == 180
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db._conn.execute(
            "UPDATE mgboost_plan_versions SET display_name='changed' WHERE id=?",
            (plan["id"],),
        )
    db._conn.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db._conn.execute(
            "UPDATE mgboost_subscription_terms SET duration_days=30 WHERE id=?",
            (term.lastrowid,),
        )
    db._conn.rollback()


@pytest.mark.parametrize("devices", [3, 6, 12])
def test_commercial_device_limits_are_first_class_plan_data(db, devices):
    plan = _plan(db.accounts, code=f"BASE_{devices}", devices=devices)
    assert plan["device_limit"] == devices
    assert plan["device_limit_mode"] == "LIMITED"
    assert plan["wl_mode"] == "NONE"


def test_internal_unlimited_and_expired_subscription_states(db):
    internal = db.accounts.create_account("INTERNAL", now=1)
    plan = _plan(
        db.accounts, code="INTERNAL", devices=None, wl_mode="UNLIMITED",
        kind="INTERNAL", billing=False,
    )
    unlimited = _subscription(
        db._conn, internal["id"], plan["id"], status="UNLIMITED", expiry=None
    )
    assert db.accounts.get_subscription_for_account(internal["id"], unlimited)["current_expiry"] is None

    expired_account = db.accounts.create_account("DIRECT", now=1)
    expired = _subscription(
        db._conn, expired_account["id"], plan["id"], status="EXPIRED", expiry=50
    )
    assert db.accounts.get_subscription_for_account(expired_account["id"], expired)["status"] == "EXPIRED"


@pytest.mark.parametrize(
    ("payment_channel", "mutation_source"),
    [
        ("TELEGRAM_STARS", "DIRECT_PURCHASE"),
        ("EXTERNAL_PAYMENT", "MANUAL_PAYMENT"),
        ("ADMIN_GRANT", "ADMIN"),
        ("UNKNOWN_LEGACY", "UNKNOWN_LEGACY"),
    ],
)
def test_payment_channel_and_mutation_provenance_are_distinct(
    db, payment_channel, mutation_source
):
    account = db.accounts.create_account("UNKNOWN_LEGACY", now=1)
    db._conn.execute(
        "INSERT INTO mgboost_entitlement_mutations "
        "(account_id,operation,payment_channel,mutation_source,actor_type,"
        "external_reference,created_at) VALUES (?,?,?,?,?,?,?)",
        (account["id"], "CREATE", payment_channel, mutation_source,
         "MIGRATION", payment_channel + "-ref", 1),
    )
    db._conn.commit()
    row = db._conn.execute(
        "SELECT payment_channel,mutation_source FROM mgboost_entitlement_mutations"
    ).fetchone()
    assert tuple(row) == (payment_channel, mutation_source)


def test_wl_periods_can_represent_stacked_60_and_future_180_day_terms(db):
    account = db.accounts.create_account("DIRECT", now=1)
    plan = _plan(
        db.accounts, code="WL_STACK", devices=3, wl_mode="LIMITED",
        quota=100_000_000_000, period=30,
    )
    duration = db.accounts.add_plan_duration(plan["id"], 60, now=2)
    subscription_id = _subscription(db._conn, account["id"], plan["id"])
    mutation_id = _mutation(db._conn, account["id"], subscription_id)
    term_id = db._conn.execute(
        "INSERT INTO mgboost_subscription_terms ("
        "account_id,subscription_id,sequence_no,plan_version_id,duration_id,"
        "duration_days,starts_at,ends_at,billing_required_snapshot,"
        "device_limit_mode_snapshot,device_limit_snapshot,wl_mode_snapshot,"
        "wl_quota_bytes_snapshot,wl_period_days_snapshot,plan_snapshot_json,"
        "mutation_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (account["id"], subscription_id, 1, plan["id"], duration["id"], 60,
         1000, 1000 + 60 * 86400, 1, "LIMITED", 3, "LIMITED",
         100_000_000_000, 30, "{}", mutation_id, 100),
    ).lastrowid
    for sequence in (1, 2):
        starts = 1000 + (sequence - 1) * 30 * 86400
        db._conn.execute(
            "INSERT INTO mgboost_wl_periods "
            "(account_id,subscription_id,subscription_term_id,sequence_no,"
            "starts_at,ends_at,quota_mode,base_quota_bytes,status,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (account["id"], subscription_id, term_id, sequence, starts,
             starts + 30 * 86400, "LIMITED", 100_000_000_000,
             "PLANNED", 100),
        )
    db._conn.commit()
    periods = db._conn.execute(
        "SELECT * FROM mgboost_wl_periods ORDER BY sequence_no"
    ).fetchall()
    assert len(periods) == 2
    assert all(row["ends_at"] - row["starts_at"] == 30 * 86400 for row in periods)


def test_no_username_special_cases_in_new_account_modules():
    root = os.path.join(os.path.dirname(__file__), "..", "src")
    text = "\n".join(
        open(os.path.join(root, name), encoding="utf-8").read()
        for name in ("account_schema.py", "account_store.py")
    ).lower()
    for forbidden in ("beykus", "megochel", "german", "pensioner", "client_buy_9"):
        assert forbidden not in text


def test_preview_is_aggregate_only_and_proposes_no_automatic_backfill(db):
    script_path = os.path.join(
        os.path.dirname(__file__), "..", "scripts", "preview_ph3_01_migration.py"
    )
    spec = importlib.util.spec_from_file_location("ph3_preview", script_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    db.save_tg_user(9988776655, "sensitive-legacy-username")
    db._conn.execute(
        "INSERT INTO user_devices "
        "(username,token,request_key,is_active,first_seen,last_seen) "
        "VALUES (?,?,?,?,?,?)",
        ("sensitive-legacy-username", "sha256:" + "a" * 64,
         "hwid:sensitive-value", 1, 1, 1),
    )
    db._conn.commit()

    preview = module.build_preview(db._conn, authoritative_users=25)
    serialized = json.dumps(preview)
    assert preview["automatic_backfill"]["accounts"] == 0
    assert preview["telegram_binding_evidence"]["single_link_candidates"] == 1
    assert preview["authoritative_marzban_user_count"] == 25
    assert preview["sensitive_values_emitted"] is False
    for secret in ("9988776655", "sensitive-legacy-username", "hwid:sensitive-value"):
        assert secret not in serialized
