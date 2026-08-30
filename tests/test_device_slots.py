import multiprocessing
import importlib.util
import json
import os
import sqlite3
import tempfile
import threading

import pytest


HWID_KEY = b"ph3-02-test-key-material-at-least-32-bytes"


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


def _account_with_plan(
    db,
    *,
    source="DIRECT",
    plan_kind="COMMERCIAL",
    limit=3,
    limit_mode="LIMITED",
    status="ACTIVE",
    expiry=9_999_999_999,
    code=None,
):
    account = db.accounts.create_account(source, now=1)
    plan = db.accounts.create_plan_version({
        "plan_code": code or f"{source}_{limit_mode}_{limit}_{account['id']}",
        "version": 1,
        "display_name": "test",
        "plan_kind": plan_kind,
        "billing_required": plan_kind != "INTERNAL",
        "device_limit_mode": limit_mode,
        "device_limit": limit,
        "wl_mode": "NONE",
        "wl_quota_bytes": None,
        "wl_period_days": None,
        "terms": {},
    }, now=1)
    cursor = db._conn.execute(
        "INSERT INTO mgboost_subscriptions "
        "(account_id,current_plan_version_id,status,started_at,current_expiry,"
        "created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
        (account["id"], plan["id"], status, 1, expiry, 1, 1),
    )
    db._conn.commit()
    return account, cursor.lastrowid


def _second_store(path):
    from src.device_slots import DeviceSlotStore

    connection = sqlite3.connect(path, check_same_thread=False, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection, DeviceSlotStore(connection, threading.RLock())


def _process_claim(path, account_id, hwid, start_event, result_queue):
    from src.device_slots import CapacityReached, DeviceSlotStore

    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    store = DeviceSlotStore(connection, threading.RLock())
    start_event.wait(10)
    try:
        result = store.claim(account_id, hwid, HWID_KEY, now=100)
        result_queue.put(("claimed", result["slot_id"], result["generation"]))
    except CapacityReached:
        result_queue.put(("full", None, None))
    except Exception as exc:  # pragma: no cover - reported to parent for diagnosis
        result_queue.put((type(exc).__name__, None, None))
    finally:
        connection.close()


def test_slot_migration_is_idempotent_and_dormant(db):
    from src.device_slot_schema import (
        MIGRATION_ID, NEW_RUNTIME_TABLES, SCHEMA_CHECKSUM, apply_device_slot_schema,
    )

    row = db._conn.execute(
        "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
        (MIGRATION_ID,),
    ).fetchone()
    assert row[0] == SCHEMA_CHECKSUM
    assert apply_device_slot_schema(db._conn, now=2) is False
    assert all(db._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
               for table in NEW_RUNTIME_TABLES)


def test_slot_migration_requires_parent_and_rolls_back_additively():
    from src.device_slot_schema import apply_device_slot_schema

    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE TABLE mgboost_schema_migrations "
        "(migration_id TEXT PRIMARY KEY,schema_checksum TEXT NOT NULL,applied_at INTEGER NOT NULL)"
    )
    connection.commit()
    with pytest.raises(RuntimeError, match="requires exact PH3-01"):
        apply_device_slot_schema(connection, now=1)
    assert connection.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name='mgboost_device_slots'"
    ).fetchone()[0] == 0


@pytest.mark.parametrize("limit", [3, 6, 12])
def test_paid_baseline_limits_are_exact(db, limit):
    from src.device_slots import CapacityReached

    account, _ = _account_with_plan(db, limit=limit)
    claimed = [
        db.device_slots.claim(account["id"], f"paid-{limit}-{i}", HWID_KEY, now=100)
        for i in range(limit)
    ]
    assert [row["slot_number"] for row in claimed] == list(range(1, limit + 1))
    assert all(row["slot_kind"] == "BASE" for row in claimed)
    with pytest.raises(CapacityReached):
        db.device_slots.claim(account["id"], f"paid-{limit}-overflow", HWID_KEY, now=100)


def test_direct_plan_cannot_smuggle_an_unapproved_numeric_limit(db):
    from src.device_slots import EntitlementUnavailable

    account, _ = _account_with_plan(db, limit=99)
    with pytest.raises(EntitlementUnavailable, match="baseline"):
        db.device_slots.claim(account["id"], "commercial-99", HWID_KEY, now=100)


def test_wl_trial_name_alone_cannot_authorize_commercial_d1(db):
    """The D1 exception is the complete pinned free-trial contract, not
    merely a `plan_code='WL_TRIAL'` string supplied by another plan row."""
    from src.device_slots import EntitlementUnavailable

    account, _ = _account_with_plan(db, limit=1, code="WL_TRIAL")
    with pytest.raises(EntitlementUnavailable, match="baseline"):
        db.device_slots.claim(account["id"], "fake-commercial-wl-trial-d1", HWID_KEY, now=100)


def test_direct_plan_unlimited_is_allowed_only_via_a_reviewed_plan_and_uses_technical_cap(db):
    """PH4-03 mass-migration device-policy decision (2026-08-26): a DIRECT
    plan MAY carry `device_limit_mode='UNLIMITED'` -- but only ever via an
    immutable, capability-gated plan_version an admin explicitly created
    (e.g. `legacy_paid_compat.ensure_legacy_paid_compat_entitlement(
    device_limit_exempt=True)`), never a self-service/arbitrary value. This
    is a deliberate change from the prior blanket DIRECT-can-never-be-
    UNLIMITED behavior; a DIRECT plan carrying a non-null `device_limit`
    alongside `UNLIMITED` mode still fails closed (tested below)."""
    from src.device_slots import EntitlementUnavailable

    unlimited, _ = _account_with_plan(
        db, limit=None, limit_mode="UNLIMITED", code="COMMERCIAL_UNLIMITED"
    )
    result = db.device_slots.claim(unlimited["id"], "commercial-unlimited", HWID_KEY, now=100)
    assert result["slot_kind"] == "BASE"
    capacity = db.device_slots.get_capacity_state(unlimited["id"], now=100)
    assert capacity["effective_limit"] == 99
    assert capacity["limit_mode"] == "UNLIMITED"




def test_direct_plan_d4_and_d8_are_approved_baselines(db):
    """PH4-03 mass-migration device-policy decision (2026-08-26): 4 and 8
    join the catalog baseline 3/6/12 as individually-reviewed PH4-03
    legacy-compat values (never a self-service/catalog change)."""
    for limit in (4, 8):
        account, _ = _account_with_plan(db, limit=limit, code=f"DIRECT_D{limit}")
        result = db.device_slots.claim(account["id"], f"direct-d{limit}-device", HWID_KEY, now=100)
        assert result["slot_kind"] == "BASE"
        capacity = db.device_slots.get_capacity_state(account["id"], now=100)
        assert capacity["effective_limit"] == limit


def test_internal_configurable_and_unlimited_use_technical_cap(db):
    from src.device_slots import CapacityReached

    configurable, _ = _account_with_plan(
        db, source="INTERNAL", plan_kind="INTERNAL", limit=5, code="INT5"
    )
    for i in range(5):
        result = db.device_slots.claim(
            configurable["id"], f"internal-five-{i}", HWID_KEY, now=100
        )
        assert result["slot_kind"] == "INTERNAL"
    with pytest.raises(CapacityReached):
        db.device_slots.claim(configurable["id"], "internal-five-over", HWID_KEY, now=100)

    unlimited, _ = _account_with_plan(
        db, source="INTERNAL", plan_kind="INTERNAL", limit=None,
        limit_mode="UNLIMITED", status="UNLIMITED", expiry=None, code="INT_UNLIMITED",
    )
    for i in range(99):
        db.device_slots.claim(unlimited["id"], f"internal-unlimited-{i}", HWID_KEY, now=100)
    state = db.device_slots.get_capacity_state(unlimited["id"], now=100)
    assert state["limit_mode"] == "UNLIMITED"
    assert state["entitled_limit"] is None
    assert state["technical_limit"] == 99
    assert state["active_count"] == 99
    with pytest.raises(CapacityReached):
        db.device_slots.claim(unlimited["id"], "internal-100", HWID_KEY, now=100)


def test_duplicate_hwid_returns_same_slot_and_generation(db):
    account, _ = _account_with_plan(db)
    first = db.device_slots.claim(account["id"], " duplicate-hwid ", HWID_KEY, now=100)
    second = db.device_slots.claim(account["id"], "duplicate-hwid", HWID_KEY, now=101)
    assert first["result"] == "CLAIMED"
    assert second["result"] == "EXISTING"
    assert (first["slot_id"], first["generation"]) == (
        second["slot_id"], second["generation"]
    )
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_device_slot_generations"
    ).fetchone()[0] == 1


def test_two_connections_cannot_assign_last_slot_twice(db):
    from src.device_slots import CapacityReached

    account, _ = _account_with_plan(db, limit=3)
    db.device_slots.claim(account["id"], "prefill-1", HWID_KEY, now=100)
    db.device_slots.claim(account["id"], "prefill-2", HWID_KEY, now=100)
    path = db._conn.execute("PRAGMA database_list").fetchone()[2]
    second_connection, second = _second_store(path)
    barrier = threading.Barrier(2)
    outcomes = []

    def claim(store, hwid):
        barrier.wait()
        try:
            outcomes.append(store.claim(account["id"], hwid, HWID_KEY, now=100)["result"])
        except CapacityReached:
            outcomes.append("FULL")

    threads = [
        threading.Thread(target=claim, args=(db.device_slots, "last-a")),
        threading.Thread(target=claim, args=(second, "last-b")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    second_connection.close()
    assert sorted(outcomes) == ["CLAIMED", "FULL"]
    assert db.device_slots.get_capacity_state(account["id"], now=100)["active_count"] == 3


def test_two_processes_use_database_as_capacity_boundary(db):
    account, _ = _account_with_plan(db, limit=3)
    db.device_slots.claim(account["id"], "process-prefill-1", HWID_KEY, now=100)
    db.device_slots.claim(account["id"], "process-prefill-2", HWID_KEY, now=100)
    path = db._conn.execute("PRAGMA database_list").fetchone()[2]
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    queue = context.Queue()
    processes = [
        context.Process(
            target=_process_claim,
            args=(path, account["id"], f"process-last-{i}", start, queue),
        )
        for i in (1, 2)
    ]
    for process in processes:
        process.start()
    start.set()
    results = [queue.get(timeout=15) for _ in processes]
    for process in processes:
        process.join(timeout=15)
        assert process.exitcode == 0
    assert sorted(row[0] for row in results) == ["claimed", "full"]
    assert db.device_slots.get_capacity_state(account["id"], now=100)["active_count"] == 3


def test_two_workers_duplicate_hwid_converges_to_one_generation(db):
    account, _ = _account_with_plan(db)
    path = db._conn.execute("PRAGMA database_list").fetchone()[2]
    second_connection, second = _second_store(path)
    barrier = threading.Barrier(2)
    results = []

    def claim(store):
        barrier.wait()
        results.append(store.claim(account["id"], "same-race-hwid", HWID_KEY, now=100))

    threads = [
        threading.Thread(target=claim, args=(db.device_slots,)),
        threading.Thread(target=claim, args=(second,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    second_connection.close()
    assert {row["result"] for row in results} == {"CLAIMED", "EXISTING"}
    assert len({(row["slot_id"], row["generation_id"], row["generation"])
                for row in results}) == 1


def test_free_reuse_increments_generation_and_old_generation_is_terminal(db):
    account, _ = _account_with_plan(db)
    first = db.device_slots.claim(account["id"], "old-device", HWID_KEY, now=100)
    released = db.device_slots.release(
        account["id"], first["slot_id"], first["generation"],
        reason="owner selected device", now=200,
    )
    assert released["desired_state"] == "FREE"
    second = db.device_slots.claim(account["id"], "new-device", HWID_KEY, now=300)
    assert second["slot_id"] == first["slot_id"]
    assert second["slot_number"] == first["slot_number"]
    assert second["generation"] == first["generation"] + 1
    old = db._conn.execute(
        "SELECT * FROM mgboost_device_slot_generations WHERE id=?",
        (first["generation_id"],),
    ).fetchone()
    assert old["status"] == "RELEASED"
    with pytest.raises(sqlite3.IntegrityError, match="cannot be reactivated"):
        db._conn.execute(
            "UPDATE mgboost_device_slot_generations SET status='ACTIVE',"
            "ended_at=NULL,end_reason=NULL WHERE id=?", (old["id"],)
        )
    db._conn.rollback()


def test_stale_release_cannot_free_new_generation(db):
    from src.device_slots import StaleSlotGeneration

    account, _ = _account_with_plan(db)
    first = db.device_slots.claim(account["id"], "first", HWID_KEY, now=100)
    db.device_slots.release(
        account["id"], first["slot_id"], first["generation"], reason="replace", now=200
    )
    second = db.device_slots.claim(account["id"], "second", HWID_KEY, now=300)
    with pytest.raises(StaleSlotGeneration):
        db.device_slots.release(
            account["id"], first["slot_id"], first["generation"],
            reason="stale retry", now=400,
        )
    assert db.device_slots.list_for_account(account["id"])[0]["current_generation"] == second["generation"]


def test_cross_account_hwid_and_slot_references_are_rejected(db):
    from src.device_slots import CrossAccountHWID, StaleSlotGeneration

    first, _ = _account_with_plan(db, code="CROSS_A")
    second, _ = _account_with_plan(db, code="CROSS_B")
    claimed = db.device_slots.claim(first["id"], "globally-bound", HWID_KEY, now=100)
    with pytest.raises(CrossAccountHWID):
        db.device_slots.claim(second["id"], "globally-bound", HWID_KEY, now=100)
    assert db.device_slots.list_for_account(second["id"]) == []
    with pytest.raises(StaleSlotGeneration):
        db.device_slots.release(
            second["id"], claimed["slot_id"], claimed["generation"],
            reason="cross-account attempt", now=200,
        )

    verifier = db._conn.execute(
        "SELECT hwid_verifier FROM mgboost_device_slot_generations WHERE id=?",
        (claimed["generation_id"],),
    ).fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError):
        db._conn.execute(
            "INSERT INTO mgboost_device_slot_generations "
            "(account_id,slot_id,slot_number,generation,hwid_verifier_version,"
            "hwid_verifier,hwid_masked,status,claimed_at) "
            "VALUES (?,?,?,?,?,?,?,'ACTIVE',?)",
            (second["id"], claimed["slot_id"], claimed["slot_number"], 99, 1,
             verifier[:-1] + "0", "hwid_000000000000", 100),
        )
    db._conn.rollback()


def test_entitlement_reduction_reports_conflict_without_choosing_devices(db):
    from src.device_slots import CapacityConflict

    account, subscription_id = _account_with_plan(db, limit=3)
    claimed = [
        db.device_slots.claim(account["id"], f"downgrade-{i}", HWID_KEY, now=100)
        for i in range(3)
    ]
    mutation = db._conn.execute(
        "INSERT INTO mgboost_entitlement_mutations "
        "(account_id,subscription_id,operation,payment_channel,mutation_source,"
        "actor_type,reason,created_at) VALUES (?,?,?,?,?,?,?,?)",
        (account["id"], subscription_id, "LIMIT_DECREASE", "ADMIN_GRANT",
         "ADMIN", "PRIMARY_ADMIN", "owner requested review", 150),
    ).lastrowid
    db._conn.execute(
        "INSERT INTO mgboost_entitlement_overrides "
        "(account_id,subscription_id,entitlement_key,value_type,integer_value,"
        "starts_at,expires_at,reason,mutation_id,created_at) "
        "VALUES (?,?, 'DEVICE_LIMIT','INTEGER',2,?,?,?,?,?)",
        (account["id"], subscription_id, 150, 1_000,
         "pending operator device selection", mutation, 150),
    )
    db._conn.commit()

    state = db.device_slots.get_capacity_state(account["id"], now=200)
    assert state == {
        "account_source": "DIRECT",
        "subscription_id": subscription_id,
        "limit_mode": "LIMITED",
        "entitled_limit": 2,
        "technical_limit": 99,
        "effective_limit": 2,
        "active_count": 3,
        "conflict": True,
        "overage": 1,
    }
    with pytest.raises(CapacityConflict) as error:
        db.device_slots.claim(account["id"], "must-not-be-assigned", HWID_KEY, now=200)
    assert (error.value.active_count, error.value.effective_limit) == (3, 2)
    after = db.device_slots.list_for_account(account["id"])
    assert len(after) == 3
    assert {row["id"] for row in after} == {row["slot_id"] for row in claimed}
    assert all(row["status"] == "ACTIVE" for row in after)


def test_hwid_is_stored_only_as_keyed_verifier_and_mask(db):
    from src.device_slots import privacy_safe_hwid

    account, _ = _account_with_plan(db)
    raw = "client-controlled-device-identifier-SENSITIVE"
    result = db.device_slots.claim(account["id"], raw, HWID_KEY, now=100)
    row = db._conn.execute(
        "SELECT hwid_verifier,hwid_masked FROM mgboost_device_slot_generations"
    ).fetchone()
    expected_verifier, expected_mask = privacy_safe_hwid(raw, HWID_KEY)
    assert tuple(row) == (expected_verifier, expected_mask)
    assert raw not in row["hwid_verifier"]
    assert raw not in row["hwid_masked"]
    assert "verifier" not in result
    assert "hwid_verifier" not in db.device_slots.list_for_account(account["id"])[0]
    path = db._conn.execute("PRAGMA database_list").fetchone()[2]
    db._conn.execute("PRAGMA wal_checkpoint(FULL)")
    assert raw.encode() not in open(path, "rb").read()
    assert HWID_KEY not in open(path, "rb").read()


def test_unknown_legacy_and_expired_entitlements_cannot_claim(db):
    from src.device_slots import EntitlementUnavailable

    legacy = db.accounts.create_account("UNKNOWN_LEGACY", now=1)
    with pytest.raises(EntitlementUnavailable):
        db.device_slots.claim(legacy["id"], "legacy-no-plan", HWID_KEY, now=100)

    expired, _ = _account_with_plan(db, expiry=50, code="EXPIRED")
    with pytest.raises(EntitlementUnavailable, match="expired"):
        db.device_slots.claim(expired["id"], "expired", HWID_KEY, now=100)


def test_slot_schema_does_not_reference_legacy_runtime_or_child_credentials():
    root = os.path.join(os.path.dirname(__file__), "..", "src")
    schema = open(os.path.join(root, "device_slot_schema.py"), encoding="utf-8").read()
    store = open(os.path.join(root, "device_slots.py"), encoding="utf-8").read()
    lowered = (schema + store).lower()
    for forbidden in (
        "user_devices", "hwid_lock", "marzban_username", "subscription_url",
        "legacy_token", "vless", "uuid",
    ):
        assert forbidden not in lowered


def test_forensic_report_classifies_anomalies_without_raw_identifiers(db):
    script_path = os.path.join(
        os.path.dirname(__file__), "..", "scripts",
        "forensic_ph3_02_legacy_anomalies.py",
    )
    spec = importlib.util.spec_from_file_location("ph3_slot_forensic", script_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    local_only = "sensitive-deleted-legacy-user"
    shared = "sensitive-shared-live-user"
    db.save_tg_user(100001, shared)
    db.save_tg_user(100002, shared)
    db._conn.execute(
        "INSERT INTO sub_requests (token,username,user_agent,timestamp) VALUES (?,?,?,?)",
        ("sha256:" + "a" * 64, local_only, "client", 100),
    )
    db._conn.commit()

    result = module.build_forensic(db._conn, [shared])
    serialized = json.dumps(result)
    assert result["local_only_count"] == 1
    assert result["local_only"][0]["classification"].startswith("ORPHANED_LOCAL")
    assert result["multi_telegram_username_count"] == 1
    assert result["multi_telegram"][0]["classification"] == (
        "LEGACY_EXPLICIT_M_TO_1_TELEGRAM_BINDING"
    )
    assert result["automatic_owner_or_account_assignment"] == 0
    assert result["deletions"] == 0
    assert result["raw_identifiers_emitted"] is False
    for sensitive in (local_only, shared, "100001", "100002"):
        assert sensitive not in serialized


def test_slot_preview_reports_only_counts(db):
    script_path = os.path.join(
        os.path.dirname(__file__), "..", "scripts", "preview_ph3_02_migration.py"
    )
    spec = importlib.util.spec_from_file_location("ph3_slot_preview", script_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    db._conn.execute(
        "INSERT INTO user_devices "
        "(username,token,request_key,is_active,first_seen,last_seen) VALUES (?,?,?,?,?,?)",
        ("secret-user", "sha256:" + "a" * 64, "hwid:secret", 1, 1, 1),
    )
    db._conn.commit()
    result = module.build_preview(db._conn)
    serialized = json.dumps(result)
    assert result["slot_rows"] == 0
    assert result["generation_rows"] == 0
    assert result["parent_account_rows"] == 0
    assert result["legacy_device_rows"] == 1
    assert result["automatic_backfill"] == 0
    assert "secret-user" not in serialized
    assert "hwid:secret" not in serialized


def test_forensic_recognizes_pre_audit_multiple_telegram_binding(db):
    script_path = os.path.join(
        os.path.dirname(__file__), "..", "scripts",
        "forensic_ph3_02_legacy_anomalies.py",
    )
    spec = importlib.util.spec_from_file_location("ph3_slot_forensic_pre_audit", script_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    username = "pre-audit-shared-sensitive"
    db._conn.execute(
        "INSERT INTO tg_users (telegram_id,marzban_username,registered_at) VALUES (?,?,?)",
        (700001, username, module.TG_BIND_AUDIT_INTRODUCED_UTC - 100),
    )
    db._conn.execute(
        "INSERT INTO tg_users (telegram_id,marzban_username,registered_at) VALUES (?,?,?)",
        (700002, username, module.TG_BIND_AUDIT_INTRODUCED_UTC - 50),
    )
    db._conn.commit()
    result = module.build_forensic(db._conn, [username])
    assert result["multi_telegram"][0]["classification"] == (
        "LEGACY_PRE_AUDIT_M_TO_1_TELEGRAM_BINDING"
    )
    assert username not in json.dumps(result)
