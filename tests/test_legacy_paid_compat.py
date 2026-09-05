import importlib
import os
import tempfile
import time

import pytest


PRIMARY = "owner:primary-admin-stable-id"
PRIMARY_LOGIN = "authenticated-primary-login"


def _capability(db, username=PRIMARY_LOGIN):
    from src.security import AdminSessionStore
    _raw, session = AdminSessionStore().create(username, "test-server-jwt")
    return db.primary_admin_authority.authorize_session(session)


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


def _reviewed_account(db, *, username, tg, legacy_status="ACTIVE", legacy_expiry=10_000,
                       observed_device_count=2, decision_ref="dl-legacy-compat-test",
                       capability=None):
    capability = capability or _capability(db)
    account = db.direct_enrollment.enroll_direct_account(
        capability=capability, legacy_username=username, decision_ref=decision_ref,
        ownership_evidence="PROVEN", telegram_id=tg, alias_provenance="EVIDENCE_PROVEN",
        legacy_status=legacy_status, legacy_expiry=legacy_expiry,
        observed_device_count=observed_device_count, observed_hwid_count=observed_device_count,
        evidence={"source": "test"}, idempotency_key=f"enroll-{username}-op-legacy-compat",
        now=100,
    )
    db.direct_enrollment.record_owner_attested_legacy_payment(
        db, capability=capability, account_id=account["account_id"], decision_ref=decision_ref,
        attestation_note="Owner attests historical direct payment, details unknown",
        evidence={"source": "test"}, now=100,
    )
    return account, capability


# --- device limit derivation -----------------------------------------------

def test_active_unknown_expiry_is_rejected(db):
    from src import legacy_paid_compat as compat
    account, capability = _reviewed_account(
        db, username="ambiguous", tg=929000001, legacy_expiry=None,
    )
    with pytest.raises(compat.AmbiguousLegacyExpiry):
        compat.ensure_legacy_paid_compat_entitlement(
            db, capability=capability, account_id=account["account_id"],
            decision_ref="review-ambiguity", now=200,
        )
    assert db._conn.execute("SELECT count(*) FROM mgboost_subscriptions").fetchone()[0] == 0


def _malformed(db):
    from src import legacy_paid_compat as compat
    account, cap = _reviewed_account(db, username="old-ambiguous", tg=929000002, legacy_expiry=None)
    plan = compat._ensure_plan_version(db, device_limit=None, unlimited=True, now=100)
    db._conn.execute(
        "INSERT INTO mgboost_subscriptions (account_id,current_plan_version_id,status,started_at,"
        "current_expiry,created_at,updated_at) VALUES (?,?,'ACTIVE',100,NULL,100,100)",
        (account["account_id"], plan["id"]),
    )
    db._conn.commit()
    return dict(capability=cap, account_id=account["account_id"], decision_ref="owner-review-123",
                evidence={"review_ref": "review-123", "owner_confirmed": True})


@pytest.mark.parametrize("resolution,expiry,status,active", [
    ("FINITE_EXPIRY", 1000, "ACTIVE", True),
    ("FINITE_EXPIRY", 199, "EXPIRED", False),
    ("FINITE_EXPIRY", 200, "EXPIRED", False),
    ("NON_EXPIRING", None, "UNLIMITED", True),
])
def test_reviewed_expiry_resolution(db, resolution, expiry, status, active):
    from src import legacy_paid_compat as compat
    from src.entitlement_engine import EntitlementEngine, exact_wl_allowed_for_delivery
    args = _malformed(db)
    violations = compat.detect_legacy_expiry_ambiguities(db._conn)
    assert len(violations) == 1
    assert set(violations[0]) == {"account_id", "subscription_id", "plan_code", "violation_class"}
    result = compat.resolve_legacy_expiry_ambiguity(db, **args, resolution=resolution, expiry=expiry, now=200)
    assert result["status"] == status
    assert result["current_expiry"] == expiry
    entitlement = EntitlementEngine(db).calculate(account_id=args["account_id"], now=200)
    assert entitlement["subscription"]["effective_status"] == status
    assert entitlement["subscription"]["active"] == active
    assert exact_wl_allowed_for_delivery(db, account_id=args["account_id"], now=200) == active
    assert compat.detect_legacy_expiry_ambiguities(db._conn) == []
    assert compat.resolve_legacy_expiry_ambiguity(
        db, **args, resolution=resolution, expiry=expiry, now=201,
    )["already_applied"]
    ensured = compat.ensure_legacy_paid_compat_entitlement(
        db, **args, device_limit_exempt=True, now=201,
    )
    assert ensured["id"] == result["id"]
    assert not ensured["_is_new"]
    assert db._conn.execute("SELECT count(*) FROM mgboost_subscriptions").fetchone()[0] == 1
    alias = db._conn.execute("SELECT legacy_status,legacy_expiry FROM mgboost_legacy_account_aliases").fetchone()
    assert tuple(alias) == ("ACTIVE", None)


def test_expiry_resolution_audit_failure_is_atomic_and_retryable(db):
    import sqlite3
    from src import legacy_paid_compat as compat
    args = _malformed(db)
    db._conn.execute(
        "CREATE TEMP TRIGGER fail_expiry_audit BEFORE INSERT ON mgboost_entitlement_mutations "
        "WHEN NEW.operation='LEGACY_PAID_COMPAT_EXPIRY_RESOLVED' "
        "BEGIN SELECT RAISE(ABORT, 'injected audit failure'); END"
    )
    before = dict(db._conn.execute("SELECT * FROM mgboost_subscriptions").fetchone())
    with pytest.raises(sqlite3.IntegrityError, match="injected audit failure"):
        compat.resolve_legacy_expiry_ambiguity(db, **args, resolution="FINITE_EXPIRY", expiry=1000, now=200)
    assert dict(db._conn.execute("SELECT * FROM mgboost_subscriptions").fetchone()) == before
    assert compat._expiry_correction(db, args["account_id"]) is None
    db._conn.execute("DROP TRIGGER fail_expiry_audit")
    compat.resolve_legacy_expiry_ambiguity(db, **args, resolution="FINITE_EXPIRY", expiry=1000, now=200)
    assert compat._expiry_correction(db, args["account_id"]) is not None


@pytest.mark.parametrize("change", [
    {"decision_ref": ""}, {"evidence": {}}, {"evidence": None},
    {"evidence": {"review_ref": "review-123", "owner_confirmed": False}},
    {"evidence": {"token": "secret"}}, {"capability": None},
    {"resolution": "UNLIMITED"}, {"expiry": None}, {"expiry": True},
])
def test_expiry_resolution_requires_explicit_review(db, change):
    from src import legacy_paid_compat as compat
    args = {**_malformed(db), "resolution": "FINITE_EXPIRY", "expiry": 1000, **change}
    with pytest.raises(compat.LegacyPaidCompatError):
        compat.resolve_legacy_expiry_ambiguity(db, **args, now=200)
    assert len(compat.detect_legacy_expiry_ambiguities(db._conn)) == 1


@pytest.mark.parametrize("status,expiry", [("ACTIVE", 1000), ("DISABLED", None), ("EXPIRED", None), ("UNLIMITED", None)])
def test_expiry_resolution_refuses_other_subscription_states(db, status, expiry):
    from src import legacy_paid_compat as compat
    args = _malformed(db)
    db._conn.execute("UPDATE mgboost_subscriptions SET status=?,current_expiry=?", (status, expiry))
    db._conn.commit()
    with pytest.raises(compat.SubscriptionConflict):
        compat.resolve_legacy_expiry_ambiguity(db, **args, resolution="NON_EXPIRING", now=200)


def test_expiry_resolution_stale_and_changed_retry_refused(db):
    from src import legacy_paid_compat as compat
    args = _malformed(db)
    compat.resolve_legacy_expiry_ambiguity(db, **args, resolution="NON_EXPIRING", now=200)
    with pytest.raises(compat.SubscriptionConflict):
        compat.resolve_legacy_expiry_ambiguity(db, **args, resolution="FINITE_EXPIRY", expiry=1000, now=200)
    db._conn.execute("UPDATE mgboost_subscriptions SET row_version=row_version+1")
    db._conn.commit()
    with pytest.raises(compat.SubscriptionConflict):
        compat.resolve_legacy_expiry_ambiguity(db, **args, resolution="NON_EXPIRING", now=200)


@pytest.mark.parametrize("kind,billed,code", [
    ("COMMERCIAL", False, "ORDINARY_CATALOG"),
    ("INTERNAL", False, "LEGACY_PAID_COMPAT_V1_TEST_INTERNAL"),
    ("COMMERCIAL", True, "LEGACY_PAID_COMPAT_V1_TEST_BILLED"),
])
def test_expiry_resolution_refuses_other_plans(db, kind, billed, code):
    from src import legacy_paid_compat as compat
    args = _malformed(db)
    plan = db.accounts.create_plan_version({
        "plan_code": code, "version": 1, "display_name": "Test incompatible plan",
        "plan_kind": kind, "billing_required": billed, "device_limit_mode": "LIMITED",
        "device_limit": 3, "wl_mode": "NONE", "terms": {},
    }, now=100)
    db._conn.execute("UPDATE mgboost_subscriptions SET current_plan_version_id=?", (plan["id"],))
    db._conn.commit()
    with pytest.raises(compat.SubscriptionConflict):
        compat.resolve_legacy_expiry_ambiguity(db, **args, resolution="NON_EXPIRING", now=200)
    assert compat._expiry_correction(db, args["account_id"]) is None


@pytest.mark.parametrize("source", ["DIRECT", "INTERNAL"])
def test_expiry_resolution_refuses_unreviewed_account(db, source):
    from src import legacy_paid_compat as compat
    args = _malformed(db)
    args["account_id"] = db.accounts.create_account(source, now=100)["id"]
    with pytest.raises(compat.PrerequisiteMissing):
        compat.resolve_legacy_expiry_ambiguity(db, **args, resolution="NON_EXPIRING", now=200)


@pytest.mark.parametrize("legacy_status,expiry,status", [
    ("ACTIVE", 1000, "ACTIVE"), ("ACTIVE", 199, "EXPIRED"),
    ("UNLIMITED", None, "UNLIMITED"), ("DISABLED", None, "DISABLED"),
    ("EXPIRED", None, "EXPIRED"),
])
def test_valid_legacy_expiry_contracts(db, legacy_status, expiry, status):
    from src import legacy_paid_compat as compat
    account, cap = _reviewed_account(db, username="valid-expiry", tg=929000003,
                                     legacy_status=legacy_status, legacy_expiry=expiry)
    result = compat.ensure_legacy_paid_compat_entitlement(
        db, capability=cap, account_id=account["account_id"], decision_ref="valid-review", now=200,
    )
    assert result["status"] == status
    assert result["current_expiry"] == expiry

def test_default_legacy_paid_gets_d3(db):
    from src.legacy_paid_compat import ensure_legacy_paid_compat_entitlement
    account, capability = _reviewed_account(db, username="compat-user-a", tg=920000001)
    result = ensure_legacy_paid_compat_entitlement(
        db, capability=capability, account_id=account["account_id"],
        decision_ref="dl-legacy-compat-test", now=200,
    )
    plan = db._conn.execute(
        "SELECT * FROM mgboost_plan_versions WHERE id=?", (result["current_plan_version_id"],)
    ).fetchone()
    assert plan["plan_code"] == "LEGACY_PAID_COMPAT_V1_D3"
    assert plan["device_limit"] == 3
    assert plan["device_limit_mode"] == "LIMITED"


def test_approved_extra_one_gets_d4(db):
    from src.legacy_paid_compat import ensure_legacy_paid_compat_entitlement
    account, capability = _reviewed_account(db, username="compat-user-b", tg=920000002)
    result = ensure_legacy_paid_compat_entitlement(
        db, capability=capability, account_id=account["account_id"],
        approved_extra_device_slots=1, decision_ref="dl-legacy-compat-test",
        evidence={"note": "owner manually approved +1 slot"}, now=200,
    )
    plan = db._conn.execute(
        "SELECT * FROM mgboost_plan_versions WHERE id=?", (result["current_plan_version_id"],)
    ).fetchone()
    assert plan["plan_code"] == "LEGACY_PAID_COMPAT_V1_D4"
    assert plan["device_limit"] == 4


def test_approved_extra_three_gets_d6(db):
    from src.legacy_paid_compat import ensure_legacy_paid_compat_entitlement
    account, capability = _reviewed_account(db, username="compat-user-c", tg=920000003)
    result = ensure_legacy_paid_compat_entitlement(
        db, capability=capability, account_id=account["account_id"],
        approved_extra_device_slots=3, decision_ref="dl-legacy-compat-test",
        evidence={"note": "owner manually approved +3 slots"}, now=200,
    )
    plan = db._conn.execute(
        "SELECT * FROM mgboost_plan_versions WHERE id=?", (result["current_plan_version_id"],)
    ).fetchone()
    assert plan["plan_code"] == "LEGACY_PAID_COMPAT_V1_D6"
    assert plan["device_limit"] == 6


def test_same_dn_reuses_one_immutable_plan_variant(db):
    from src.legacy_paid_compat import ensure_legacy_paid_compat_entitlement
    account_a, capability = _reviewed_account(db, username="compat-user-d1", tg=920000004)
    account_b, _ = _reviewed_account(db, username="compat-user-d2", tg=920000005, capability=capability)
    result_a = ensure_legacy_paid_compat_entitlement(
        db, capability=capability, account_id=account_a["account_id"],
        decision_ref="dl-legacy-compat-test", now=200,
    )
    result_b = ensure_legacy_paid_compat_entitlement(
        db, capability=capability, account_id=account_b["account_id"],
        decision_ref="dl-legacy-compat-test", now=201,
    )
    assert result_a["current_plan_version_id"] == result_b["current_plan_version_id"]
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_plan_versions WHERE plan_code='LEGACY_PAID_COMPAT_V1_D3'"
    ).fetchone()[0] == 1


def test_device_rows_alone_do_not_raise_quota(db):
    """Registering many devices in user_devices/hwid_lock (legacy telemetry
    tables) must never, by itself, change the derived compat device limit --
    only an explicit approved_extra_device_slots does."""
    from src.legacy_paid_compat import ensure_legacy_paid_compat_entitlement
    account, capability = _reviewed_account(db, username="compat-user-e", tg=920000006,
                                             observed_device_count=2)
    now = int(time.time())
    for i in range(10):
        db._conn.execute(
            "INSERT INTO user_devices (username,token,request_key,device_name,first_seen,last_seen) "
            "VALUES (?,?,?,?,?,?)",
            ("compat-user-e", f"tok{i}", f"key{i}", f"device{i}", now, now),
        )
    db._conn.commit()
    result = ensure_legacy_paid_compat_entitlement(
        db, capability=capability, account_id=account["account_id"],
        decision_ref="dl-legacy-compat-test", now=200,
    )
    plan = db._conn.execute(
        "SELECT device_limit FROM mgboost_plan_versions WHERE id=?", (result["current_plan_version_id"],)
    ).fetchone()
    assert plan["device_limit"] == 3


def test_occupied_devices_exceeding_derived_limit_fails_closed(db):
    from src.legacy_paid_compat import DeviceOverageConflict, ensure_legacy_paid_compat_entitlement
    account, capability = _reviewed_account(db, username="compat-user-f", tg=920000007,
                                             observed_device_count=5)
    with pytest.raises(DeviceOverageConflict):
        ensure_legacy_paid_compat_entitlement(
            db, capability=capability, account_id=account["account_id"],
            decision_ref="dl-legacy-compat-test", now=200,
        )
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_subscriptions WHERE account_id=?", (account["account_id"],)
    ).fetchone()[0] == 0


# --- expiry / WL preservation -----------------------------------------------

def test_exact_legacy_expiry_preserved(db):
    from src.legacy_paid_compat import ensure_legacy_paid_compat_entitlement
    account, capability = _reviewed_account(db, username="compat-user-g", tg=920000008,
                                             legacy_expiry=1_800_000_000)
    result = ensure_legacy_paid_compat_entitlement(
        db, capability=capability, account_id=account["account_id"],
        decision_ref="dl-legacy-compat-test", now=200,
    )
    assert result["current_expiry"] == 1_800_000_000


def test_wl_is_unlimited_no_quota_bytes(db):
    from src.legacy_paid_compat import ensure_legacy_paid_compat_entitlement
    account, capability = _reviewed_account(db, username="compat-user-h", tg=920000009)
    result = ensure_legacy_paid_compat_entitlement(
        db, capability=capability, account_id=account["account_id"],
        decision_ref="dl-legacy-compat-test", now=200,
    )
    plan = db._conn.execute(
        "SELECT wl_mode, wl_quota_bytes FROM mgboost_plan_versions WHERE id=?",
        (result["current_plan_version_id"],),
    ).fetchone()
    assert plan["wl_mode"] == "UNLIMITED"
    assert plan["wl_quota_bytes"] is None


def test_no_price_reconstruction(db):
    from src.legacy_paid_compat import ensure_legacy_paid_compat_entitlement
    account, capability = _reviewed_account(db, username="compat-user-i", tg=920000010)
    result = ensure_legacy_paid_compat_entitlement(
        db, capability=capability, account_id=account["account_id"],
        decision_ref="dl-legacy-compat-test", now=200,
    )
    plan = db._conn.execute(
        "SELECT billing_required, terms_json FROM mgboost_plan_versions WHERE id=?",
        (result["current_plan_version_id"],),
    ).fetchone()
    assert plan["billing_required"] == 0
    assert "price" not in plan["terms_json"].lower()
    assert "amount" not in plan["terms_json"].lower()


# --- idempotency / conflicts -------------------------------------------------

def test_idempotent_retry_returns_same_subscription(db):
    from src.legacy_paid_compat import ensure_legacy_paid_compat_entitlement
    account, capability = _reviewed_account(db, username="compat-user-j", tg=920000011)
    first = ensure_legacy_paid_compat_entitlement(
        db, capability=capability, account_id=account["account_id"],
        decision_ref="dl-legacy-compat-test", now=200,
    )
    second = ensure_legacy_paid_compat_entitlement(
        db, capability=capability, account_id=account["account_id"],
        decision_ref="dl-legacy-compat-test", now=201,
    )
    assert first["id"] == second["id"]
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_subscriptions WHERE account_id=?", (account["account_id"],)
    ).fetchone()[0] == 1


def test_no_duplicate_subscription_created(db):
    from src.legacy_paid_compat import ensure_legacy_paid_compat_entitlement
    account, capability = _reviewed_account(db, username="compat-user-k", tg=920000012)
    for _ in range(3):
        ensure_legacy_paid_compat_entitlement(
            db, capability=capability, account_id=account["account_id"],
            decision_ref="dl-legacy-compat-test", now=200,
        )
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_subscriptions WHERE account_id=?", (account["account_id"],)
    ).fetchone()[0] == 1


def test_conflicting_subscription_is_rejected(db):
    from src.legacy_paid_compat import SubscriptionConflict, ensure_legacy_paid_compat_entitlement
    account, capability = _reviewed_account(db, username="compat-user-l", tg=920000013)
    ensure_legacy_paid_compat_entitlement(
        db, capability=capability, account_id=account["account_id"],
        decision_ref="dl-legacy-compat-test", now=200,
    )
    with pytest.raises(SubscriptionConflict):
        ensure_legacy_paid_compat_entitlement(
            db, capability=capability, account_id=account["account_id"],
            approved_extra_device_slots=1, decision_ref="dl-legacy-compat-test",
            evidence={"note": "conflicting later attempt"}, now=210,
        )
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_subscriptions WHERE account_id=?", (account["account_id"],)
    ).fetchone()[0] == 1


# --- prerequisite guards -----------------------------------------------------

def test_missing_owner_attested_payment_is_rejected(db):
    from src.legacy_paid_compat import PrerequisiteMissing, ensure_legacy_paid_compat_entitlement
    capability = _capability(db)
    account = db.direct_enrollment.enroll_direct_account(
        capability=capability, legacy_username="compat-user-m", decision_ref="dl-legacy-compat-test",
        ownership_evidence="PROVEN", telegram_id=920000014, alias_provenance="EVIDENCE_PROVEN",
        legacy_status="ACTIVE", legacy_expiry=10_000, observed_device_count=2,
        observed_hwid_count=2, evidence={"source": "test"},
        idempotency_key="enroll-compat-user-m-op", now=100,
    )
    with pytest.raises(PrerequisiteMissing):
        ensure_legacy_paid_compat_entitlement(
            db, capability=capability, account_id=account["account_id"],
            decision_ref="dl-legacy-compat-test", now=200,
        )


def test_missing_reviewed_ownership_is_rejected(db):
    from src.legacy_paid_compat import PrerequisiteMissing, ensure_legacy_paid_compat_entitlement
    capability = _capability(db)
    account = db.accounts.create_account("DIRECT", now=100)
    with pytest.raises(PrerequisiteMissing):
        ensure_legacy_paid_compat_entitlement(
            db, capability=capability, account_id=account["id"],
            decision_ref="dl-legacy-compat-test", now=200,
        )


# --- expired legacy user never gets a new paid period ------------------------

def test_expired_legacy_user_does_not_get_new_paid_period(db):
    from src.legacy_paid_compat import ensure_legacy_paid_compat_entitlement
    account, capability = _reviewed_account(
        db, username="compat-user-n", tg=920000015, legacy_status="ACTIVE", legacy_expiry=150,
    )
    result = ensure_legacy_paid_compat_entitlement(
        db, capability=capability, account_id=account["account_id"],
        decision_ref="dl-legacy-compat-test", now=200,
    )
    assert result["status"] == "EXPIRED"
    assert result["current_expiry"] == 150


def test_disabled_legacy_user_stays_disabled(db):
    from src.legacy_paid_compat import ensure_legacy_paid_compat_entitlement
    account, capability = _reviewed_account(
        db, username="compat-user-o", tg=920000016, legacy_status="DISABLED", legacy_expiry=10_000,
    )
    result = ensure_legacy_paid_compat_entitlement(
        db, capability=capability, account_id=account["account_id"],
        decision_ref="dl-legacy-compat-test", now=200,
    )
    assert result["status"] == "DISABLED"


# --- account identity / payment provenance unchanged -------------------------

def test_account_identity_and_payment_provenance_unchanged(db):
    from src.legacy_paid_compat import ensure_legacy_paid_compat_entitlement
    account, capability = _reviewed_account(db, username="compat-user-p", tg=920000017)
    before_public_id = db.accounts.get_account(account["account_id"])["public_id"]
    before_attestation = db._conn.execute(
        "SELECT * FROM mgboost_owner_attested_legacy_payments WHERE account_id=?",
        (account["account_id"],),
    ).fetchone()
    ensure_legacy_paid_compat_entitlement(
        db, capability=capability, account_id=account["account_id"],
        decision_ref="dl-legacy-compat-test", now=200,
    )
    after_public_id = db.accounts.get_account(account["account_id"])["public_id"]
    after_attestation = db._conn.execute(
        "SELECT * FROM mgboost_owner_attested_legacy_payments WHERE account_id=?",
        (account["account_id"],),
    ).fetchone()
    assert before_public_id == after_public_id
    assert dict(before_attestation) == dict(after_attestation)
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_payment_records WHERE account_id=?", (account["account_id"],)
    ).fetchone()[0] == 0


# --- schema idempotence for the plan/subscription tables is already covered
# by PH3-01's own suite; this module adds no new schema.


# --- end-to-end integration: reviewed enrollment -> attestation -> compat
# entitlement -> PH4-02 migration -> child ------------------------------------

from src.child_contract import source_contract_hash  # noqa: E402
from src.legacy_bridge_resolver import is_fall_through_outcome  # noqa: E402
from src.migration_lifecycle import process_migration_bridge_request  # noqa: E402
from src.opaque_resolver import OUTCOME_OK  # noqa: E402

from tests.test_child_provisioning import HWID_KEY  # noqa: E402
from tests.test_opaque_resolver import _known_hwid_meta, _remote_and_ensure_fn  # noqa: E402


def test_end_to_end_reviewed_enrollment_to_migrated_child(db):
    from src.legacy_paid_compat import ensure_legacy_paid_compat_entitlement

    username = "compat-e2e-user"
    account, capability = _reviewed_account(
        db, username=username, tg=920000099, legacy_expiry=10**9, observed_device_count=1,
    )
    account_id = account["account_id"]
    public_id_before = db.accounts.get_account(account_id)["public_id"]

    entitlement = ensure_legacy_paid_compat_entitlement(
        db, capability=capability, account_id=account_id,
        decision_ref="dl-legacy-compat-test", now=100,
    )
    assert entitlement["status"] == "ACTIVE"
    assert entitlement["current_expiry"] == 10**9

    alias = db._conn.execute(
        "SELECT id FROM mgboost_legacy_account_aliases WHERE account_id=? AND alias_role='PRIMARY'",
        (account_id,),
    ).fetchone()

    remote, ensure_fn, subscription_fn = _remote_and_ensure_fn()
    remote.users[username] = remote.users.pop("alice")
    remote.users[username]["username"] = username

    # `resolve_account_device` never invents the account's first child --
    # it requires a `source_contract_hash` already established by the
    # existing PH3-03 pipeline (mirrors the exact real production
    # methodology: seed one child on a synthetic device BEFORE enabling any
    # bridge binding, so a real customer's device is never exposed to a
    # PROVISIONING_UNAVAILABLE gap).
    seed_slot = db.device_slots.claim(account_id, "seed-device-e2e", HWID_KEY, now=100)
    seed_request_hash = source_contract_hash(remote.users[username])
    seed_prepared = db.child_provisioning.prepare_child_ensure(
        account_id=account_id, slot_generation_id=seed_slot["generation_id"],
        source_alias_id=alias["id"], source_contract_hash=seed_request_hash,
        expire=10**9, idempotency_key="seed-e2e-child-op", now=101,
    )
    seed_claimed = db.child_provisioning.claim(
        seed_prepared["operation_id"], worker_id="seed-worker", now=102, lease_seconds=5,
    )
    seed_created = ensure_fn(seed_claimed["payload"])
    seed_uuid = seed_created.pop("uuid")
    db.child_provisioning.acknowledge(
        seed_prepared["operation_id"], worker_id="seed-worker",
        outcome=seed_created["outcome"], child_uuid=seed_uuid, remote_result=seed_created, now=103,
    )

    db.legacy_bridge.create_binding(
        capability=capability, account_id=account_id, legacy_alias_id=alias["id"], enabled=True,
        decision_ref="dl-legacy-compat-test", now=104,
    )

    result = process_migration_bridge_request(
        db, username, _known_hwid_meta("e2e-canary-device-1"), hmac_key=HWID_KEY,
        ensure_fn=ensure_fn, subscription_fn=subscription_fn, worker_id="legacy-compat-e2e-worker",
        now=200,
    )
    assert not is_fall_through_outcome(result.outcome)
    assert result.outcome == OUTCOME_OK
    assert result.child_username is not None
    assert result.body_b64 is not None

    # same parent, no duplicate/invented account or payment
    acct_after = db.accounts.get_account(account_id)
    assert acct_after["public_id"] == public_id_before
    assert acct_after["account_source"] == "DIRECT"
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_accounts WHERE account_source='DIRECT'"
    ).fetchone()[0] == 1
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_payment_records"
    ).fetchone()[0] == 0

    # expiry preserved
    sub_after = db._conn.execute(
        "SELECT current_expiry FROM mgboost_subscriptions WHERE account_id=?", (account_id,)
    ).fetchone()
    assert sub_after["current_expiry"] == 10**9

    # device limit correct (D3), no WL quota
    plan_after = db._conn.execute(
        "SELECT device_limit, wl_mode, wl_quota_bytes FROM mgboost_plan_versions WHERE id=?",
        (entitlement["current_plan_version_id"],),
    ).fetchone()
    assert plan_after["device_limit"] == 3
    assert plan_after["wl_mode"] == "UNLIMITED"
    assert plan_after["wl_quota_bytes"] is None

    # durable migration lineage recorded, MIGRATED
    binding = db._conn.execute(
        "SELECT state FROM mgboost_migration_bindings WHERE account_id=?", (account_id,)
    ).fetchone()
    assert binding["state"] == "MIGRATED"

    # legacy Marzban user (simulated remote) is untouched/active
    assert remote.users[username]["status"] == "active"

    # no shared legacy fallback after durable migration: a downstream
    # failure on the same device must not fall through to the legacy body
    def _broken_ensure_fn(payload):
        raise RuntimeError("simulated remote outage")

    retry = process_migration_bridge_request(
        db, username, _known_hwid_meta("e2e-canary-device-1"), hmac_key=HWID_KEY,
        ensure_fn=_broken_ensure_fn, subscription_fn=subscription_fn,
        worker_id="legacy-compat-e2e-worker", now=201,
    )
    assert not is_fall_through_outcome(retry.outcome)
    assert retry.outcome == OUTCOME_OK  # already-ACTIVE child served from durable state, no remote call needed


# --- PH4-03 mass-migration device-policy: device_limit_exempt --------------

def test_device_limit_exempt_creates_unlimited_plan_and_bypasses_overage(db):
    from src.legacy_paid_compat import ensure_legacy_paid_compat_entitlement

    account, capability = _reviewed_account(
        db, username="exempt-user-a", tg=920000101, observed_device_count=37,
    )
    result = ensure_legacy_paid_compat_entitlement(
        db, capability=capability, account_id=account["account_id"],
        device_limit_exempt=True, decision_ref="dl-legacy-compat-test",
        evidence={"source": "owner decision -- family account, no meaningful device ceiling"},
        now=200,
    )
    plan = db._conn.execute(
        "SELECT * FROM mgboost_plan_versions WHERE id=?", (result["current_plan_version_id"],)
    ).fetchone()
    assert plan["plan_code"] == "LEGACY_PAID_COMPAT_V1_UNLIMITED"
    assert plan["device_limit"] is None
    assert plan["device_limit_mode"] == "UNLIMITED"
    assert plan["wl_mode"] == "UNLIMITED"  # unchanged legacy WL semantics


def test_device_limit_exempt_requires_evidence(db):
    from src.legacy_paid_compat import ensure_legacy_paid_compat_entitlement, LegacyPaidCompatError

    account, capability = _reviewed_account(db, username="exempt-user-b", tg=920000102)
    with pytest.raises(LegacyPaidCompatError):
        ensure_legacy_paid_compat_entitlement(
            db, capability=capability, account_id=account["account_id"],
            device_limit_exempt=True, decision_ref="dl-legacy-compat-test", now=200,
        )


def test_device_limit_exempt_and_extra_slots_are_mutually_exclusive(db):
    from src.legacy_paid_compat import ensure_legacy_paid_compat_entitlement, LegacyPaidCompatError

    account, capability = _reviewed_account(db, username="exempt-user-c", tg=920000103)
    with pytest.raises(LegacyPaidCompatError):
        ensure_legacy_paid_compat_entitlement(
            db, capability=capability, account_id=account["account_id"],
            device_limit_exempt=True, approved_extra_device_slots=3,
            decision_ref="dl-legacy-compat-test", evidence={"source": "test"}, now=200,
        )


def test_device_limit_exempt_is_idempotent(db):
    from src.legacy_paid_compat import ensure_legacy_paid_compat_entitlement

    account, capability = _reviewed_account(db, username="exempt-user-d", tg=920000104)
    first = ensure_legacy_paid_compat_entitlement(
        db, capability=capability, account_id=account["account_id"],
        device_limit_exempt=True, decision_ref="dl-legacy-compat-test",
        evidence={"source": "test"}, now=200,
    )
    second = ensure_legacy_paid_compat_entitlement(
        db, capability=capability, account_id=account["account_id"],
        device_limit_exempt=True, decision_ref="dl-legacy-compat-test",
        evidence={"source": "test"}, now=201,
    )
    assert first["id"] == second["id"]


def test_d4_and_d8_baselines_work_end_to_end(db):
    from src.legacy_paid_compat import ensure_legacy_paid_compat_entitlement

    for limit, extra in ((4, 1), (8, 5)):
        account, capability = _reviewed_account(
            db, username=f"d{limit}-user", tg=920000200 + limit, observed_device_count=limit,
        )
        result = ensure_legacy_paid_compat_entitlement(
            db, capability=capability, account_id=account["account_id"],
            approved_extra_device_slots=extra, decision_ref="dl-legacy-compat-test",
            evidence={"source": "owner-approved device count review"}, now=200,
        )
        plan = db._conn.execute(
            "SELECT device_limit FROM mgboost_plan_versions WHERE id=?",
            (result["current_plan_version_id"],),
        ).fetchone()
        assert plan["device_limit"] == limit
        # actually claim devices to prove device_slots.py accepts the new baseline
        for i in range(limit):
            claimed = db.device_slots.claim(
                account["account_id"], f"d{limit}-device-{i}", HWID_KEY, now=201,
            )
            assert claimed["slot_kind"] == "BASE"


def test_acknowledge_observed_overage_allows_explicit_lower_limit(db):
    """Owner decision 2026-08-26 (account 10/German pattern): raw observed
    count can include duplicate registrations of one physical device under
    two clients -- an explicit, evidenced owner acknowledgment allows the
    chosen limit to stay below the raw count without silently dropping the
    safety check for every other (unreviewed) case."""
    from src.legacy_paid_compat import ensure_legacy_paid_compat_entitlement, DeviceOverageConflict

    account, capability = _reviewed_account(db, username="overage-ack-user", tg=920000300, observed_device_count=7)

    with pytest.raises(DeviceOverageConflict):
        ensure_legacy_paid_compat_entitlement(
            db, capability=capability, account_id=account["account_id"],
            approved_extra_device_slots=3, decision_ref="dl-legacy-compat-test",
            evidence={"source": "test"}, now=200,
        )

    result = ensure_legacy_paid_compat_entitlement(
        db, capability=capability, account_id=account["account_id"],
        approved_extra_device_slots=3, acknowledge_observed_overage=True,
        decision_ref="dl-legacy-compat-test",
        evidence={"source": "owner reviewed: 2 of 7 raw rows are one duplicate device"}, now=200,
    )
    plan = db._conn.execute(
        "SELECT device_limit FROM mgboost_plan_versions WHERE id=?",
        (result["current_plan_version_id"],),
    ).fetchone()
    assert plan["device_limit"] == 6


def test_acknowledge_observed_overage_still_requires_evidence(db):
    from src.legacy_paid_compat import ensure_legacy_paid_compat_entitlement, LegacyPaidCompatError

    account, capability = _reviewed_account(db, username="overage-ack-user-2", tg=920000301, observed_device_count=7)
    with pytest.raises(LegacyPaidCompatError):
        ensure_legacy_paid_compat_entitlement(
            db, capability=capability, account_id=account["account_id"],
            approved_extra_device_slots=3, acknowledge_observed_overage=True,
            decision_ref="dl-legacy-compat-test", now=200,
        )
