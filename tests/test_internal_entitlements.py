import importlib
import os
import sqlite3
import tempfile
import threading
from types import SimpleNamespace

import pytest


PRIMARY = "owner:primary-admin-stable-id"
PRIMARY_LOGIN = "authenticated-primary-login"


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


def _plan(db, *, mode="LIMITED", limit=6, code="INTERNAL_CANARY"):
    capability = db.primary_admin_authority.authorize_session(
        SimpleNamespace(username=PRIMARY_LOGIN)
    )
    return db.internal_entitlements.create_internal_plan(
        capability=capability,
        plan_code=code,
        version=1,
        display_name="Reviewed internal canary",
        device_limit_mode=mode,
        device_limit=limit,
        wl_mode="UNLIMITED",
        terms={"schema": 1, "cohort": "reviewed-canary"},
        now=100,
    )


def _reviewed(db, plan, *, username="legacy-internal-a", tg=123456):
    capability = db.primary_admin_authority.authorize_session(
        SimpleNamespace(username=PRIMARY_LOGIN)
    )
    return db.internal_entitlements.create_reviewed_account(
        capability=capability,
        plan_version_id=plan["id"],
        legacy_username=username,
        mapping_key="mapping-" + username,
        decision_ref="test-owner-decision-v1",
        legacy_aliases=[{
            "legacy_username": username,
            "alias_role": "PRIMARY",
            "ownership_provenance": "OWNER_APPROVED",
            "legacy_status": "UNLIMITED",
            "legacy_expiry": None,
            "observed_device_count": 2,
            "observed_hwid_count": 2,
            "evidence": {"source": "test"},
        }],
        ownership_evidence="PROVEN" if tg else "ABSENT",
        telegram_id=tg,
        legacy_status="UNLIMITED",
        legacy_expiry=None,
        device_evidence_count=2,
        hwid_evidence_count=2,
        internal_reason="Owner-reviewed service canary account",
        migration_confidence="HIGH",
        evidence={"source": "manual-owner-review", "schema": 1},
        idempotency_key="reviewed-account-operation-" + username,
        now=100,
    )


def test_schema_is_idempotent_and_starts_empty(db):
    from src.internal_entitlement_schema import (
        MIGRATION_ID, NEW_RUNTIME_TABLES, SCHEMA_CHECKSUM,
        apply_internal_entitlement_schema,
    )
    assert apply_internal_entitlement_schema(db._conn, now=101) is False
    marker = db._conn.execute(
        "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
        (MIGRATION_ID,),
    ).fetchone()
    assert marker[0] == SCHEMA_CHECKSUM
    assert all(db._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
               for table in NEW_RUNTIME_TABLES)


def test_non_primary_cannot_create_internal_plan_or_unlimited(db):
    from src.admin_authority import PrimaryAdminAuthorizationError, PrimaryAdminCapability
    from src.internal_entitlements import PrimaryAdminRequired
    with pytest.raises(PrimaryAdminAuthorizationError):
        db.primary_admin_authority.authorize_session(
            SimpleNamespace(username="secondary-admin")
        )
    with pytest.raises(PrimaryAdminRequired):
        db.internal_entitlements.create_internal_plan(
            capability=PrimaryAdminCapability(PRIMARY, "caller-forged-seal"),
            plan_code="NOPE", version=1,
            display_name="Nope", device_limit_mode="UNLIMITED",
            device_limit=None, now=100,
        )
    assert db._conn.execute("SELECT COUNT(*) FROM mgboost_plan_versions").fetchone()[0] == 0


def test_reviewed_account_requires_unambiguous_evidence_and_is_idempotent(db):
    from src.internal_entitlements import ReviewedEvidenceRequired
    plan = _plan(db)
    capability = db.primary_admin_authority.authorize_session(
        SimpleNamespace(username=PRIMARY_LOGIN)
    )
    with pytest.raises(ReviewedEvidenceRequired, match="ambiguous"):
        db.internal_entitlements.create_reviewed_account(
            capability=capability, plan_version_id=plan["id"],
            legacy_username="ambiguous-user", ownership_evidence="AMBIGUOUS",
            mapping_key="mapping-ambiguous-user", decision_ref="test-decision",
            legacy_aliases=[{
                "legacy_username": "ambiguous-user", "alias_role": "PRIMARY",
                "ownership_provenance": "OWNER_APPROVED", "legacy_status": "ACTIVE",
                "legacy_expiry": 999, "observed_device_count": 1,
                "observed_hwid_count": 1, "evidence": {},
            }],
            telegram_id=None, legacy_status="ACTIVE", legacy_expiry=999,
            device_evidence_count=1, hwid_evidence_count=1,
            internal_reason="Ambiguous identity must remain for manual review",
            migration_confidence="LOW", evidence={},
            idempotency_key="ambiguous-reviewed-operation", now=100,
        )
    first = _reviewed(db, plan)
    second = _reviewed(db, plan)
    assert first == second
    assert db._conn.execute("SELECT COUNT(*) FROM mgboost_accounts").fetchone()[0] == 1
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_telegram_identities"
    ).fetchone()[0] == 1


def test_owner_approved_multi_aliases_map_to_one_parent_and_remain_immutable(db):
    capability = db.primary_admin_authority.authorize_session(
        SimpleNamespace(username=PRIMARY_LOGIN)
    )
    plan = _plan(db, limit=10)
    aliases = [
        {
            "legacy_username": name,
            "alias_role": "PRIMARY" if index == 0 else "SECONDARY",
            "ownership_provenance": "OWNER_APPROVED",
            "legacy_status": "UNLIMITED",
            "legacy_expiry": None,
            "observed_device_count": count,
            "observed_hwid_count": count,
            "evidence": {"ref": f"masked-{index}"},
        }
        for index, (name, count) in enumerate(
            (("alias-one", 3), ("alias-two", 4), ("alias-three", 2))
        )
    ]
    account = db.internal_entitlements.create_reviewed_account(
        capability=capability,
        plan_version_id=plan["id"],
        legacy_username="alias-one",
        mapping_key="INTERNAL_OWNER_PRIMARY_TEST",
        decision_ref="owner-approval-test-v1",
        legacy_aliases=aliases,
        ownership_evidence="PROVEN",
        telegram_id=905302972,
        legacy_status="UNLIMITED",
        legacy_expiry=None,
        device_evidence_count=9,
        hwid_evidence_count=9,
        internal_reason="Owner explicitly approved this three-alias mapping",
        migration_confidence="HIGH",
        evidence={"schema": 1},
        idempotency_key="approved-multi-alias-test-operation",
        now=100,
    )
    rows = db._conn.execute(
        "SELECT account_id,legacy_username FROM mgboost_legacy_account_aliases "
        "ORDER BY id"
    ).fetchall()
    assert [row["legacy_username"] for row in rows] == [
        "alias-one", "alias-two", "alias-three"
    ]
    assert {row["account_id"] for row in rows} == {account["account_id"]}
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db._conn.execute(
            "UPDATE mgboost_legacy_account_aliases SET legacy_username='changed' "
            "WHERE legacy_username='alias-two'"
        )
    db._conn.rollback()


def test_internal_configurable_and_unlimited_resolve_to_cap(db):
    limited_plan = _plan(db, limit=5, code="INTERNAL_FIVE")
    limited = _reviewed(db, limited_plan, username="internal-five", tg=101)
    state = db.internal_entitlements.effective_entitlements(limited["account_id"], now=101)
    assert state["billing_required"] is False
    assert state["wl_mode"] == "UNLIMITED"
    assert (state["device_limit_mode"], state["effective_device_cap"]) == ("LIMITED", 5)

    unlimited_plan = _plan(db, mode="UNLIMITED", limit=None, code="INTERNAL_UNLIMITED")
    unlimited = _reviewed(db, unlimited_plan, username="internal-unlimited", tg=202)
    state = db.internal_entitlements.effective_entitlements(unlimited["account_id"], now=101)
    assert (state["device_limit_mode"], state["effective_device_cap"]) == ("UNLIMITED", 99)


def test_ordinary_account_cannot_receive_or_resolve_internal_override(db):
    from src.internal_entitlements import InternalEntitlementError
    account = db.accounts.create_account("DIRECT", now=1)
    plan = db.accounts.create_plan_version({
        "plan_code": "BASE", "version": 1, "display_name": "Base",
        "plan_kind": "COMMERCIAL", "billing_required": True,
        "device_limit_mode": "LIMITED", "device_limit": 3,
        "wl_mode": "NONE", "terms": {},
    }, now=1)
    db._conn.execute(
        "INSERT INTO mgboost_subscriptions "
        "(account_id,current_plan_version_id,status,started_at,current_expiry,created_at,updated_at) "
        "VALUES (?,?,'ACTIVE',1,9999,1,1)", (account["id"], plan["id"]),
    )
    db._conn.commit()
    with pytest.raises(InternalEntitlementError, match="ordinary"):
        db.internal_entitlements.effective_entitlements(account["id"], now=2)
    with pytest.raises(InternalEntitlementError, match="ordinary"):
        db.internal_entitlements.add_override(
            account["id"], capability=db.primary_admin_authority.authorize_session(
                SimpleNamespace(username=PRIMARY_LOGIN)
            ), entitlement_key="DEVICE_LIMIT",
            value_type="UNLIMITED", value=None,
            reason="Not allowed for a commercial account", expires_at=1000,
            idempotency_key="ordinary-account-override", now=2,
        )


def test_override_reason_expiry_and_auto_fallback(db):
    from src.internal_entitlements import InternalEntitlementError
    plan = _plan(db, limit=5)
    account = _reviewed(db, plan)
    capability = db.primary_admin_authority.authorize_session(
        SimpleNamespace(username=PRIMARY_LOGIN)
    )
    with pytest.raises(InternalEntitlementError, match="reason"):
        db.internal_entitlements.add_override(
            account["account_id"], capability=capability,
            entitlement_key="DEVICE_LIMIT", value_type="UNLIMITED", value=None,
            reason="", expires_at=200, idempotency_key="missing-reason-override", now=100,
        )
    db.internal_entitlements.add_override(
        account["account_id"], capability=capability,
        entitlement_key="DEVICE_LIMIT", value_type="UNLIMITED", value=None,
        reason="Temporary owner-approved canary capacity", expires_at=200,
        idempotency_key="temporary-unlimited-override", now=100,
    )
    active = db.internal_entitlements.effective_entitlements(account["account_id"], now=150)
    assert (active["override_mode"], active["effective_device_cap"]) == ("EXPLICIT", 99)
    expired = db.internal_entitlements.effective_entitlements(account["account_id"], now=200)
    assert (expired["override_mode"], expired["effective_device_cap"]) == ("AUTO", 5)

    db.internal_entitlements.add_override(
        account["account_id"], capability=capability,
        entitlement_key="DEVICE_LIMIT", value_type="INTEGER", value=3,
        reason="Temporary reviewed lower device capacity", expires_at=190,
        idempotency_key="temporary-limited-override", now=160,
    )
    limited = db.internal_entitlements.effective_entitlements(account["account_id"], now=170)
    assert (limited["device_limit_mode"], limited["device_limit"]) == ("LIMITED", 3)


def test_concurrent_same_override_is_single_idempotent_mutation(db):
    from src.internal_entitlements import InternalEntitlementStore
    plan = _plan(db)
    account = _reviewed(db, plan)
    path = db._conn.execute("PRAGMA database_list").fetchone()[2]
    second_conn = sqlite3.connect(path, check_same_thread=False)
    second_conn.row_factory = sqlite3.Row
    second_conn.execute("PRAGMA foreign_keys=ON")
    second = InternalEntitlementStore(
        second_conn, threading.RLock(), db.primary_admin_authority
    )
    capability = db.primary_admin_authority.authorize_session(
        SimpleNamespace(username=PRIMARY_LOGIN)
    )
    barrier = threading.Barrier(2)
    results = []

    def mutate(store):
        barrier.wait()
        results.append(store.add_override(
            account["account_id"], capability=capability,
            entitlement_key="DEVICE_LIMIT", value_type="INTEGER", value=4,
            reason="Concurrent reviewed capacity reduction", expires_at=200,
            idempotency_key="same-concurrent-override", now=100,
        )["id"])

    threads = [threading.Thread(target=mutate, args=(store,))
               for store in (db.internal_entitlements, second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    second_conn.close()
    assert len(set(results)) == 1
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_entitlement_overrides WHERE account_id=?",
        (account["account_id"],),
    ).fetchone()[0] == 1


def test_account_isolation_and_no_username_special_cases(db):
    first_plan = _plan(db, code="INT_A")
    second_plan = _plan(db, code="INT_B")
    first = _reviewed(db, first_plan, username="internal-a", tg=111)
    second = _reviewed(db, second_plan, username="internal-b", tg=222)
    capability = db.primary_admin_authority.authorize_session(
        SimpleNamespace(username=PRIMARY_LOGIN)
    )
    db.internal_entitlements.add_override(
        first["account_id"], capability=capability, entitlement_key="DEVICE_LIMIT",
        value_type="INTEGER", value=2, reason="Scoped first-account override",
        expires_at=200, idempotency_key="first-account-only-override", now=100,
    )
    assert db.internal_entitlements.effective_entitlements(first["account_id"], now=150)["device_limit"] == 2
    assert db.internal_entitlements.effective_entitlements(second["account_id"], now=150)["device_limit"] == 6

    root = os.path.join(os.path.dirname(__file__), "..", "src")
    text = "\n".join(open(os.path.join(root, name), encoding="utf-8").read().lower()
                     for name in (
                         "account_schema.py", "account_store.py", "device_slots.py",
                         "internal_entitlement_schema.py", "internal_entitlements.py",
                         "child_provisioning_schema.py", "child_provisioning.py",
                     ))
    for forbidden in ("beykus", "megochel", "german", "pensioner", "client_buy_9"):
        assert forbidden not in text
