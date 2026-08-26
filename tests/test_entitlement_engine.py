"""PH5-04 acceptance: one deterministic, read-only entitlement calculation."""

import importlib
import json
import os
import tempfile

import pytest

from src.broker_operations import BrokerOperations
from src.child_contract import source_contract_hash
from src.security import AdminSessionStore
from tests.test_marzban_broker import FakeMarzban


HWID_KEY = "entitlement-test-hwid-key-at-least-32-bytes"


@pytest.fixture
def db(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("DATA_DIR", tmp)
    monkeypatch.setenv("PRIMARY_MGBOOST_ADMIN_ACTOR_ID", "entitlement-primary")
    monkeypatch.setenv("PRIMARY_MGBOOST_ADMIN_LOGIN", "entitlement-primary-login")
    import src.config as config
    import src.database as database
    importlib.reload(config)
    importlib.reload(database)
    database.DB_PATH = os.path.join(tmp, "db.sqlite3")
    instance = database.Database()
    from src.plan_catalog import seed_plan_catalog
    from src.wl_package_catalog import seed_wl_package_catalog
    seed_plan_catalog(instance.plan_catalog, now=1)
    seed_wl_package_catalog(instance.wl_package_catalog, now=1)
    yield instance
    instance._conn.close()


def _purchase(db, plan, *, duration=30, now=1_000):
    account = db.accounts.create_account("DIRECT", now=1)
    purchase = db.subscription_renewal.apply_same_plan_purchase(
        account_id=account["id"], plan_code=plan, duration_days=duration,
        payment_channel="TELEGRAM_STARS", mutation_source="DIRECT_PURCHASE",
        actor_type="TEST", idempotency_key=f"entitlement-purchase-{account['id']}-{plan}-{duration}",
        now=now,
    )
    return account, purchase


def _override(db, account_id, subscription_id, *, key, value_type, value, starts=1, expires=9_999_999):
    mutation_id = db._conn.execute(
        "INSERT INTO mgboost_entitlement_mutations "
        "(account_id,subscription_id,operation,payment_channel,mutation_source,actor_type,created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (account_id, subscription_id, "TEST_OVERRIDE", "ADMIN_GRANT", "ADMIN", "TEST", starts),
    ).lastrowid
    boolean_value = int(value) if value_type == "BOOLEAN" else None
    integer_value = value if value_type == "INTEGER" else None
    db._conn.execute(
        "INSERT INTO mgboost_entitlement_overrides "
        "(account_id,subscription_id,entitlement_key,value_type,boolean_value,integer_value,"
        "starts_at,expires_at,reason,mutation_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (account_id, subscription_id, key, value_type, boolean_value, integer_value,
         starts, expires, "deterministic entitlement test", mutation_id, starts),
    )
    db._conn.commit()


def _add_usage_child(db, account_id):
    username = f"entitlement-parent-{account_id}"
    db._conn.execute(
        "INSERT INTO mgboost_legacy_alias_groups "
        "(account_id,mapping_key,decision_ref,created_by_actor,created_at) VALUES (?,?,?,?,?)",
        (account_id, f"entitlement-map-{account_id}", "test", "TEST", 1),
    )
    alias_id = db._conn.execute(
        "INSERT INTO mgboost_legacy_account_aliases "
        "(account_id,legacy_username,alias_role,ownership_provenance,legacy_status,legacy_expiry,"
        "observed_device_count,observed_hwid_count,evidence_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (account_id, username, "PRIMARY", "OWNER_APPROVED", "ACTIVE", None, 1, 1, "{}", 1),
    ).lastrowid
    db._conn.commit()
    slot = db.device_slots.claim(account_id, f"entitlement-hwid-{account_id}", HWID_KEY, now=1)
    remote = FakeMarzban()
    remote.users[username] = remote.users.pop("alice")
    remote.users[username]["username"] = username
    prepared = db.child_provisioning.prepare_child_ensure(
        account_id=account_id, slot_generation_id=slot["generation_id"], source_alias_id=alias_id,
        source_contract_hash=source_contract_hash(remote.users[username]), expire=0,
        idempotency_key=f"entitlement-child-{account_id}-0001", now=2,
    )
    claim = db.child_provisioning.claim(prepared["operation_id"], worker_id="test", now=3, lease_seconds=5)
    created = BrokerOperations(remote).dispatch("child.user.ensure", claim["payload"])
    child_uuid = created.pop("uuid")
    db.child_provisioning.acknowledge(
        prepared["operation_id"], worker_id="test", outcome=created["outcome"],
        child_uuid=child_uuid, remote_result=created, now=4,
    )
    return prepared["child_intent_id"]


def _grant(db, account_id, sku, *, now, suffix):
    price = db.wl_package_catalog.active_price(sku, "TELEGRAM_STARS")
    payment = db.provenance.record_payment(
        account_id, payment_channel="TELEGRAM_STARS", record_status="CONFIRMED",
        amount_minor=price["amount"], currency="XTR", payment_method="test",
        external_reference=f"entitlement-payment-{account_id}-{suffix}", actor_type="TEST",
        actor_ref=None, evidence={}, idempotency_key=f"entitlement-payment-key-{account_id}-{suffix}", now=now,
    )
    return db.wl_packages.grant_paid_package(
        account_id=account_id, sku=sku, price_channel="TELEGRAM_STARS", payment_id=payment["id"],
        idempotency_key=f"entitlement-grant-key-{account_id}-{suffix}", now=now,
    )


@pytest.mark.parametrize(
    ("plan", "limit", "wl_mode", "quota"),
    [
        ("BASIC", 3, "NONE", None), ("BASIC_PLUS", 6, "NONE", None),
        ("BASIC_PRO", 12, "NONE", None), ("WL", 3, "LIMITED", 100),
        ("EXTENDED", 6, "LIMITED", 150), ("FAMILY", 12, "LIMITED", 150),
    ],
)
def test_all_six_commercial_plan_versions_are_the_canonical_device_and_wl_terms(db, plan, limit, wl_mode, quota):
    account, purchase = _purchase(db, plan)
    result = db.entitlements.calculate(account_id=account["id"], now=1_001)
    assert result["plan"]["code"] == plan and result["plan"]["version"] == 1
    assert result["device"] == {
        "limit_mode": "LIMITED", "limit": limit, "technical_cap": None,
        "source": "PLAN", "slot_addon_state": "NONE", "additional_slots": 0,
    }
    assert result["subscription"]["effective_expiry"] == purchase["new_expiry"]
    assert result["wl"]["real_plan_mode"] == wl_mode
    assert result["wl"]["package_eligible"] is (wl_mode == "LIMITED")
    if quota is None:
        assert result["wl"]["current_period"] is None
    else:
        assert result["wl"]["base_quota_bytes"] == quota * 1_000_000_000
        assert result["wl"]["current_period"]["sequence_no"] == 1


def test_30_and_60_day_pinned_periods_select_the_correct_canonical_window(db):
    account, purchase = _purchase(db, "WL", duration=60)
    first = db.entitlements.calculate(account_id=account["id"], now=1_001)
    second = db.entitlements.calculate(account_id=account["id"], now=1_000 + 30 * 86400 + 1)
    assert first["subscription"]["effective_expiry"] == purchase["new_expiry"]
    assert first["wl"]["current_period"]["sequence_no"] == 1
    assert second["wl"]["current_period"]["sequence_no"] == 2
    assert first["wl"]["base_quota_bytes"] == second["wl"]["base_quota_bytes"] == 100_000_000_000


def test_base_first_fifo_packages_and_canonical_consumption_are_composed_once(db):
    account, purchase = _purchase(db, "WL")
    first = _grant(db, account["id"], "WL_PACKAGE_50_GB", now=1_100, suffix="one")
    second = _grant(db, account["id"], "WL_PACKAGE_100_GB", now=1_101, suffix="two")
    period_id = db._conn.execute(
        "SELECT id FROM mgboost_wl_periods WHERE account_id=?", (account["id"],)
    ).fetchone()[0]
    child = _add_usage_child(db, account["id"])
    db.wl_usage_ledger.record_sample(
        account_id=account["id"], child_intent_id=child, node_id=4,
        cursor_after=110_000_000_000, collector_id="test", collected_at=2_000,
        wl_period_id=period_id,
    )
    result = db.entitlements.calculate(account_id=account["id"], now=2_000)
    buckets = {bucket["id"]: bucket for bucket in result["wl"]["packages"]}
    assert result["wl"]["consumed_bytes"] == 110_000_000_000
    assert result["wl"]["base_remaining_bytes"] == 0
    assert buckets[first["id"]]["derived_consumed_bytes"] == 10_000_000_000
    assert buckets[second["id"]]["derived_consumed_bytes"] == 0
    assert result["wl"]["package_remaining_bytes"] == 140_000_000_000
    assert result["wl"]["effective_remaining_bytes"] == 140_000_000_000
    assert all(bucket["catalog_version"] == "STARS-2026-08-26-v1" for bucket in buckets.values())


@pytest.mark.parametrize("used", [99_000_000_000, 100_000_000_000, 101_000_000_000])
def test_base_remaining_boundaries_are_exact_decimal_bytes(db, used):
    account, _purchase_result = _purchase(db, "WL")
    period_id = db._conn.execute("SELECT id FROM mgboost_wl_periods WHERE account_id=?", (account["id"],)).fetchone()[0]
    child = _add_usage_child(db, account["id"])
    db.wl_usage_ledger.record_sample(account_id=account["id"], child_intent_id=child, node_id=4, cursor_after=used, collector_id="test", collected_at=2_000, wl_period_id=period_id)
    result = db.entitlements.calculate(account_id=account["id"], now=2_000)
    assert result["wl"]["base_remaining_bytes"] == max(0, 100_000_000_000 - used)


def test_expiry_freezes_packages_and_same_real_wl_plan_resume_preserves_fifo_history(db):
    account, purchase = _purchase(db, "WL")
    grant = _grant(db, account["id"], "WL_PACKAGE_50_GB", now=1_100, suffix="freeze")
    expired = db.entitlements.calculate(account_id=account["id"], now=purchase["new_expiry"])
    assert expired["subscription"]["effective_status"] == "EXPIRED"
    assert expired["wl"]["package_state"] == "FROZEN"
    assert expired["wl"]["packages"][0]["id"] == grant["id"]
    assert expired["wl"]["effective_remaining_bytes"] is None
    renewal = db.subscription_renewal.apply_same_plan_purchase(
        account_id=account["id"], plan_code="WL", duration_days=30,
        payment_channel="TELEGRAM_STARS", mutation_source="DIRECT_PURCHASE", actor_type="TEST",
        idempotency_key=f"entitlement-resume-{account['id']}-0001", now=purchase["new_expiry"],
    )
    resumed = db.entitlements.calculate(account_id=account["id"], now=renewal["anchor"] + 1)
    assert resumed["wl"]["package_state"] == "ACTIVE"
    assert resumed["wl"]["packages"][0]["id"] == grant["id"]
    assert resumed["wl"]["packages"][0]["derived_remaining_bytes"] == 50_000_000_000


def test_force_enabled_base_is_visible_but_never_changes_real_billing_or_package_eligibility(db):
    account, purchase = _purchase(db, "BASIC")
    _override(db, account["id"], purchase["subscription_id"], key="WL_ACCESS", value_type="BOOLEAN", value=True)
    result = db.entitlements.calculate(account_id=account["id"], now=1_001)
    assert result["wl"]["effective_mode"] == "UNLIMITED"
    assert result["wl"]["access_override_mode"] == "FORCE_ENABLED"
    assert result["wl"]["package_eligible"] is False
    assert result["plan"]["billing_required"] is True
    assert result["wl"]["current_period"] is None


def test_expired_override_returns_to_auto_and_never_changes_pinned_plan(db):
    account, purchase = _purchase(db, "WL")
    _override(db, account["id"], purchase["subscription_id"], key="DEVICE_LIMIT", value_type="INTEGER", value=9, expires=2_000)
    active = db.entitlements.calculate(account_id=account["id"], now=1_001)
    expired = db.entitlements.calculate(account_id=account["id"], now=2_000)
    assert active["device"]["limit"] == 9 and active["device"]["source"] == "OVERRIDE"
    assert expired["device"]["limit"] == 3 and expired["device"]["source"] == "PLAN"
    assert expired["overrides"]["mode"] == "AUTO"
    assert expired["plan"]["code"] == "WL" and expired["plan"]["version"] == 1


@pytest.mark.parametrize(("device_mode", "device_limit"), [("UNLIMITED", None), ("LIMITED", 10)])
def test_internal_device_terms_are_explicit_models_not_username_behavior(db, device_mode, device_limit):
    raw, session = AdminSessionStore().create("entitlement-primary-login", "test-jwt")
    capability = db.primary_admin_authority.authorize_session(session)
    plan = db.internal_entitlements.create_internal_plan(
        capability=capability, plan_code=f"INTERNAL_TEST_{device_mode}", version=1, display_name="Internal",
        device_limit_mode=device_mode, device_limit=device_limit, wl_mode="UNLIMITED", now=100,
    )
    account = db.internal_entitlements.create_reviewed_account(
        capability=capability, plan_version_id=plan["id"], legacy_username="ordinary-fixture-name",
        mapping_key=f"internal-entitlement-fixture-{device_mode}", decision_ref="test", legacy_aliases=[{
            "legacy_username": "ordinary-fixture-name", "alias_role": "PRIMARY",
            "ownership_provenance": "OWNER_APPROVED", "legacy_status": "UNLIMITED", "legacy_expiry": None,
            "observed_device_count": 0, "observed_hwid_count": 0, "evidence": {},
        }], ownership_evidence="ABSENT", telegram_id=None, legacy_status="UNLIMITED", legacy_expiry=None,
        device_evidence_count=0, hwid_evidence_count=0, internal_reason="Internal entitlement test record", migration_confidence="HIGH",
        evidence={}, idempotency_key=f"internal-entitlement-fixture-{device_mode}-0001", now=100,
    )
    result = db.entitlements.calculate(account_id=account["account_id"], now=101)
    assert result["subscription"]["effective_status"] == "UNLIMITED"
    assert result["device"]["limit_mode"] == device_mode and result["device"]["limit"] == device_limit
    assert result["wl"]["effective_mode"] == "UNLIMITED"
    assert result["wl"]["package_eligible"] is False


def test_calculation_is_structurally_deterministic_and_does_not_mutate_db_or_period_status(db):
    account, _purchase_result = _purchase(db, "WL")
    before_changes = db._conn.total_changes
    before_status = db._conn.execute("SELECT status FROM mgboost_wl_periods WHERE account_id=?", (account["id"],)).fetchone()[0]
    first = db.entitlements.calculate(account_id=account["id"], now=1_001)
    second = db.entitlements.calculate(account_id=account["id"], now=1_001)
    assert json.dumps(first, sort_keys=True, separators=(",", ":")) == json.dumps(second, sort_keys=True, separators=(",", ":"))
    assert db._conn.total_changes == before_changes
    assert db._conn.execute("SELECT status FROM mgboost_wl_periods WHERE account_id=?", (account["id"],)).fetchone()[0] == before_status == "PLANNED"
    source = open("src/entitlement_engine.py", encoding="utf-8").read().lower()
    assert all(name not in source for name in ("beykus", "megochel", "german", "pensioner", "client_buy_9"))
