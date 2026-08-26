"""PH5-03 package catalog/bucket acceptance tests; no sales or enforcement wiring."""

import importlib
import os
import tempfile
import threading

import pytest

from src.broker_operations import BrokerOperations
from src.child_contract import source_contract_hash
from tests.test_marzban_broker import FakeMarzban
from src.security import AdminSessionStore


HWID_KEY = "package-test-hwid-key-that-is-at-least-32-bytes-long"


@pytest.fixture
def db(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("DATA_DIR", tmp)
    monkeypatch.setenv("PRIMARY_MGBOOST_ADMIN_ACTOR_ID", "package-test-primary")
    monkeypatch.setenv("PRIMARY_MGBOOST_ADMIN_LOGIN", "package-test-login")
    import src.config as config
    import src.database as database
    importlib.reload(config); importlib.reload(database)
    database.DB_PATH = os.path.join(tmp, "db.sqlite3")
    instance = database.Database()
    from src.plan_catalog import seed_plan_catalog
    from src.wl_package_catalog import seed_wl_package_catalog
    seed_plan_catalog(instance.plan_catalog, now=1)
    seed_wl_package_catalog(instance.wl_package_catalog, now=1)
    yield instance
    instance._conn.close()


def _account_with_wl(db, *, now=1_000, plan="WL"):
    account = db.accounts.create_account("DIRECT", now=1)
    purchase = db.subscription_renewal.apply_same_plan_purchase(
        account_id=account["id"], plan_code=plan, duration_days=30,
        payment_channel="TELEGRAM_STARS", mutation_source="DIRECT_PURCHASE",
        actor_type="TEST", idempotency_key=f"package-subscription-{account['id']}-0001", now=now,
    )
    return account, purchase


def _payment(db, account_id, *, channel, amount, ref, key):
    return db.provenance.record_payment(
        account_id, payment_channel="TELEGRAM_STARS" if channel == "TELEGRAM_STARS" else "EXTERNAL_PAYMENT",
        record_status="CONFIRMED", amount_minor=amount, currency="XTR" if channel == "TELEGRAM_STARS" else "RUB",
        payment_method="test", external_reference=ref, actor_type="TEST", actor_ref=None,
        evidence={"test": True}, idempotency_key=key, now=1_001,
    )


def _grant(db, account_id, sku, *, channel="TELEGRAM_STARS", now=1_100, suffix="one"):
    price = db.wl_package_catalog.active_price(sku, channel)
    payment = _payment(db, account_id, channel=channel, amount=price["amount"], ref=f"pay-{account_id}-{sku}-{channel}-{suffix}", key=f"payment-package-{account_id}-{sku}-{channel}-{suffix}")
    return db.wl_packages.grant_paid_package(
        account_id=account_id, sku=sku, price_channel=channel, payment_id=payment["id"],
        idempotency_key=f"grant-package-{account_id}-{sku}-{channel}-{suffix}", now=now,
    )


def _child(db, account_id):
    username = f"package_parent_{account_id}"
    db._conn.execute("INSERT INTO mgboost_legacy_alias_groups (account_id,mapping_key,decision_ref,created_by_actor,created_at) VALUES (?,?,?,?,?)", (account_id, f"package-map-{account_id}", "test", "TEST", 1))
    alias = db._conn.execute("INSERT INTO mgboost_legacy_account_aliases (account_id,legacy_username,alias_role,ownership_provenance,legacy_status,legacy_expiry,observed_device_count,observed_hwid_count,evidence_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)", (account_id, username, "PRIMARY", "OWNER_APPROVED", "ACTIVE", None, 1, 1, "{}", 1)).lastrowid
    db._conn.commit()
    slot = db.device_slots.claim(account_id, f"package-hwid-{account_id}", HWID_KEY, now=1)
    remote = FakeMarzban(); remote.users[username] = remote.users.pop("alice"); remote.users[username]["username"] = username
    prepared = db.child_provisioning.prepare_child_ensure(account_id=account_id, slot_generation_id=slot["generation_id"], source_alias_id=alias, source_contract_hash=source_contract_hash(remote.users[username]), expire=0, idempotency_key=f"package-child-{account_id}-0001", now=2)
    claim = db.child_provisioning.claim(prepared["operation_id"], worker_id="test", now=3, lease_seconds=5)
    created = BrokerOperations(remote).dispatch("child.user.ensure", claim["payload"]); uuid = created.pop("uuid")
    db.child_provisioning.acknowledge(prepared["operation_id"], worker_id="test", outcome=created["outcome"], child_uuid=uuid, remote_result=created, now=4)
    return prepared["child_intent_id"]


def _excess_usage(db, account_id, period_id, total):
    child_id = _child(db, account_id)
    db.wl_usage_ledger.record_sample(account_id=account_id, child_intent_id=child_id, node_id=4,
                                     cursor_after=total, collector_id="test", collected_at=2_000, wl_period_id=period_id)


def _capability(db):
    _raw, session = AdminSessionStore().create("package-test-login", "package-test-jwt")
    return db.primary_admin_authority.authorize_session(session)


def test_exact_all_four_package_prices_and_versioned_catalog_snapshots(db):
    expected = {"WL_PACKAGE_50_GB": (50, 79, 139), "WL_PACKAGE_100_GB": (100, 149, 249), "WL_PACKAGE_250_GB": (250, 349, 579), "WL_PACKAGE_500_GB": (500, 599, 999)}
    for sku, (gb, stars, rub) in expected.items():
        assert db.wl_package_catalog.active_price(sku, "TELEGRAM_STARS")["amount"] == stars
        assert db.wl_package_catalog.active_price(sku, "RUB")["amount"] == rub
        assert db.wl_package_catalog.active_price(sku, "RUB")["bytes"] == gb * 1_000_000_000
    account, _ = _account_with_wl(db)
    grant = _grant(db, account["id"], "WL_PACKAGE_50_GB")
    assert grant["catalog_version_snapshot"] == "STARS-2026-08-26-v1"
    assert grant["granted_bytes"] == 50 * 1_000_000_000 and grant["price_amount_snapshot"] == 79
    with pytest.raises(Exception, match="immutable"):
        db._conn.execute("UPDATE mgboost_wl_package_prices SET amount=1")


def test_rub_package_grant_uses_exact_rub_snapshot(db):
    account, _ = _account_with_wl(db)
    grant = _grant(db, account["id"], "WL_PACKAGE_500_GB", channel="RUB", suffix="rub")
    assert grant["price_channel"] == "RUB"
    assert grant["catalog_version_snapshot"] == "RUB-2026-08-23-v1"
    assert grant["price_amount_snapshot"] == 999


def test_base_rejected_even_if_wl_access_override_exists(db):
    from src.wl_packages import PackageEligibilityError
    account, purchase = _account_with_wl(db, plan="BASIC")
    sub_id = purchase["subscription_id"]
    mutation = db._conn.execute("INSERT INTO mgboost_entitlement_mutations (account_id,subscription_id,operation,payment_channel,mutation_source,actor_type,created_at) VALUES (?,?,?,?,?,?,?)", (account["id"], sub_id, "TEST_OVERRIDE", "ADMIN_GRANT", "ADMIN", "TEST", 1)).lastrowid
    db._conn.execute("INSERT INTO mgboost_entitlement_overrides (account_id,subscription_id,entitlement_key,value_type,boolean_value,starts_at,expires_at,reason,mutation_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)", (account["id"], sub_id, "WL_ACCESS", "BOOLEAN", 1, 1, 9_999_999, "test", mutation, 1)); db._conn.commit()
    price = db.wl_package_catalog.active_price("WL_PACKAGE_50_GB", "TELEGRAM_STARS")
    payment = _payment(db, account["id"], channel="TELEGRAM_STARS", amount=price["amount"], ref="base-payment", key="base-payment-package-0001")
    with pytest.raises(PackageEligibilityError):
        db.wl_packages.grant_paid_package(account_id=account["id"], sku="WL_PACKAGE_50_GB", price_channel="TELEGRAM_STARS", payment_id=payment["id"], idempotency_key="base-grant-package-0001", now=1_100)


def test_base_first_then_fifo_multiple_buckets_and_unused_only_refund(db):
    from src.wl_packages import PackageConsumed
    account, purchase = _account_with_wl(db)
    first = _grant(db, account["id"], "WL_PACKAGE_50_GB", now=1_100, suffix="first")
    second = _grant(db, account["id"], "WL_PACKAGE_100_GB", now=1_101, suffix="second")
    period = db._conn.execute("SELECT id FROM mgboost_wl_periods WHERE account_id=?", (account["id"],)).fetchone()[0]
    _excess_usage(db, account["id"], period, 110 * 1_000_000_000)  # 100 GB base + 10 GB package
    state = db.wl_packages.package_state(account_id=account["id"], now=2_000)
    by_id = {b["id"]: b for b in state["buckets"]}
    assert by_id[first["id"]]["derived_consumed_bytes"] == 10 * 1_000_000_000
    assert by_id[second["id"]]["derived_consumed_bytes"] == 0
    with pytest.raises(PackageConsumed):
        db.wl_packages.refund_unused_package(account_id=account["id"], package_grant_id=first["id"], refund_reference="refund-used", evidence={}, idempotency_key="refund-used-package-0001", actor_type="TEST", now=2_001)
    refunded = db.wl_packages.refund_unused_package(account_id=account["id"], package_grant_id=second["id"], refund_reference="refund-unused", evidence={"receipt": "test"}, idempotency_key="refund-unused-package-001", actor_type="TEST", now=2_001)
    assert refunded["already_applied"] is False
    assert db._conn.execute("SELECT status FROM mgboost_wl_package_grants WHERE id=?", (second["id"],)).fetchone()[0] == "REVOKED"


def test_rollover_period_reset_freeze_resume_and_restart_idempotency(db):
    account, purchase = _account_with_wl(db)
    grant = _grant(db, account["id"], "WL_PACKAGE_50_GB")
    expiry = purchase["new_expiry"]
    assert db.wl_packages.package_state(account_id=account["id"], now=expiry)["frozen"] is True
    renewal = db.subscription_renewal.apply_same_plan_purchase(account_id=account["id"], plan_code="WL", duration_days=30, payment_channel="TELEGRAM_STARS", mutation_source="DIRECT_PURCHASE", actor_type="TEST", idempotency_key="package-renewal-resume-0001", now=expiry)
    resumed = db.wl_packages.package_state(account_id=account["id"], now=renewal["anchor"] + 1)
    assert resumed["eligible_now"] is True and resumed["buckets"][0]["id"] == grant["id"]
    replay = _grant(db, account["id"], "WL_PACKAGE_50_GB", suffix="replay")
    again = db.wl_packages.grant_paid_package(account_id=account["id"], sku="WL_PACKAGE_50_GB", price_channel="TELEGRAM_STARS", payment_id=replay["payment_id"], idempotency_key="grant-package-%s-WL_PACKAGE_50_GB-TELEGRAM_STARS-replay" % account["id"], now=99_999)
    assert again["already_applied"] is True and again["id"] == replay["id"]


def test_period_reset_and_non_wl_transition_freeze_without_losing_remainder(db):
    account, purchase = _account_with_wl(db)
    grant = _grant(db, account["id"], "WL_PACKAGE_250_GB")
    period_id = db._conn.execute("SELECT id FROM mgboost_wl_periods WHERE account_id=?", (account["id"],)).fetchone()[0]
    db._conn.execute("UPDATE mgboost_wl_periods SET status='ACTIVE' WHERE id=?", (period_id,)); db._conn.commit()
    db.wl_period_admin_reset.reset_period(capability=_capability(db), period_id=period_id, reason="test", now=50_000)
    assert db.wl_packages.package_state(account_id=account["id"], now=50_001)["buckets"][0]["derived_remaining_bytes"] == grant["granted_bytes"]
    basic = db.plan_catalog.get_plan_version("BASIC")
    db._conn.execute("UPDATE mgboost_subscriptions SET current_plan_version_id=? WHERE id=?", (basic["id"], purchase["subscription_id"])); db._conn.commit()
    frozen = db.wl_packages.package_state(account_id=account["id"], now=50_001)
    assert frozen["frozen"] is True and frozen["buckets"][0]["derived_remaining_bytes"] == grant["granted_bytes"]


def test_stale_callback_and_concurrent_duplicate_refund_are_safe(db):
    from src.wl_packages import PackageAlreadyRefunded, PackageEligibilityError
    account, purchase = _account_with_wl(db)
    price = db.wl_package_catalog.active_price("WL_PACKAGE_50_GB", "TELEGRAM_STARS")
    stale_payment = _payment(db, account["id"], channel="TELEGRAM_STARS", amount=price["amount"], ref="stale-callback", key="stale-callback-payment")
    with pytest.raises(PackageEligibilityError):
        db.wl_packages.grant_paid_package(account_id=account["id"], sku="WL_PACKAGE_50_GB", price_channel="TELEGRAM_STARS", payment_id=stale_payment["id"], idempotency_key="stale-callback-grant-001", now=purchase["new_expiry"])
    grant = _grant(db, account["id"], "WL_PACKAGE_100_GB", suffix="refund-race")
    outcomes = []
    def refund(key):
        try:
            outcomes.append(db.wl_packages.refund_unused_package(account_id=account["id"], package_grant_id=grant["id"], refund_reference="refund-race-" + key, evidence={}, idempotency_key="refund-race-idempotency-" + key, actor_type="TEST", now=2_000)["id"])
        except PackageAlreadyRefunded:
            outcomes.append("revoked")
    threads = [threading.Thread(target=refund, args=(str(i),)) for i in range(2)]
    [t.start() for t in threads]; [t.join() for t in threads]
    assert len([o for o in outcomes if o != "revoked"]) == 1
    assert db._conn.execute("SELECT COUNT(*) FROM mgboost_wl_package_refunds WHERE package_grant_id=?", (grant["id"],)).fetchone()[0] == 1
