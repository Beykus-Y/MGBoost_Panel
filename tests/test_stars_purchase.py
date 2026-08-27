"""PH5-05 canonical Stars purchase/renewal invariants."""

import importlib
import asyncio
import os
import tempfile
import threading
import time

import pytest


@pytest.fixture
def db(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="ph505-test-")
    monkeypatch.setenv("DATA_DIR", tmp)
    monkeypatch.setenv("PRIMARY_MGBOOST_ADMIN_ACTOR_ID", "owner:mgboost-primary:v1")
    monkeypatch.setenv("PRIMARY_MGBOOST_ADMIN_LOGIN", "authenticated-primary-login")
    import src.config as config
    import src.database as database
    importlib.reload(config)
    importlib.reload(database)
    database.DB_PATH = os.path.join(tmp, "db.sqlite3")
    instance = database.Database()
    from src.plan_catalog import seed_plan_catalog
    seed_plan_catalog(instance.plan_catalog, now=1)
    yield instance
    instance._conn.close()


def _account(db, telegram_id=900001):
    account = db.accounts.create_account("DIRECT", now=1)
    db.accounts.link_telegram_owner(
        account["id"], telegram_id, provenance="MIGRATION", actor="test", now=1
    )
    account["telegram_id"] = telegram_id
    return account


def _paid(db, account, *, plan="BASIC", days=30, invoice_now=100, payment_now=101, suffix="a"):
    invoice = db.stars_purchases.create_invoice(
        telegram_id=account["telegram_id"], plan_code=plan, duration_days=days,
        ttl_seconds=3600, now=invoice_now,
    )
    assert db.stars_purchases.capture_paid(
        invoice["id"], charge_id=f"charge-{suffix}", provider_charge_id=None,
        payer_telegram_id=account["telegram_id"], currency="XTR",
        amount=invoice["stars_price"], now=payment_now,
    ) == "paid"
    return invoice


def _application(db, invoice_id):
    return dict(db._conn.execute(
        "SELECT * FROM mgboost_stars_purchase_applications WHERE invoice_id=?", (invoice_id,)
    ).fetchone())


def test_first_purchase_snapshots_product_evidence_and_entitlement(db):
    """PH5-11 note: this test previously rode the WL 60d SKU. The first
    rollout gate makes WL/EXTENDED/FAMILY unpurchasable, so the snapshot
    coverage now rides the sellable BASIC 60d SKU; the WL-period scheduling
    semantics of the untouched PH5-02 engine stay covered below and in
    test_subscription_renewal.py."""
    account = _account(db)
    invoice = _paid(db, account, plan="BASIC", days=60)
    result = db.stars_purchases.apply_paid_invoice(invoice["id"], now=200)

    assert result["new_expiry"] == 200 + 60 * 86400
    assert result["entitlement"]["plan"]["code"] == "BASIC"
    assert result["entitlement"]["subscription"]["effective_expiry"] == result["new_expiry"]
    row = db.get_invoice(invoice["id"])
    assert row["invoice_kind"] == "CANONICAL_PLAN"
    assert (row["plan_code_snapshot"], row["catalog_version_snapshot"], row["price_amount_snapshot"]) == (
        "BASIC", "STARS-2026-08-26-v1", 169,
    )
    assert _application(db, invoice["id"])["applied_operation"] == "CREATE"
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_stars_payment_evidence WHERE invoice_id=?", (invoice["id"],)
    ).fetchone()[0] == 1
    # A Non-WL plan (wl_mode='NONE') schedules zero WL periods.
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_wl_periods WHERE account_id=?", (account["id"],)
    ).fetchone()[0] == 0


@pytest.mark.parametrize("plan_code,amount", [("WL", 349), ("EXTENDED", 249), ("FAMILY", 299)])
def test_first_rollout_purchase_gate_rejects_non_standard_plans(db, plan_code, amount):
    account = _account(db)
    with pytest.raises(Exception):
        db.stars_purchases.create_invoice(
            telegram_id=account["telegram_id"], plan=plan_code, duration_days=30,
            ttl_seconds=3600, now=100,
        )
    assert db._conn.execute("SELECT COUNT(*) FROM stars_invoices").fetchone()[0] == 0


def test_wl_period_scheduling_engine_remains_intact_behind_the_gate():
    """The PH5-02 engine still schedules two contiguous 30-day WL periods
    for a 60-day LIMITED-plan grant -- reachable only through the renewal
    engine (future WL rollout), never through the current Stars gate."""
    from src.subscription_renewal import schedule_wl_period_windows
    windows = schedule_wl_period_windows(anchor=1_000, duration_days=60, wl_period_days=30)
    assert windows == [(1_000, 1_000 + 30 * 86400), (1_000 + 30 * 86400, 1_000 + 60 * 86400)]


@pytest.mark.parametrize("current_expiry,now,expected_anchor", [(10_000, 200, 10_000), (100, 200, 200)])
def test_active_and_expired_renewal_use_dl044_formula(db, current_expiry, now, expected_anchor):
    account = _account(db)
    first = _paid(db, account, invoice_now=10, payment_now=11, suffix="first")
    db.stars_purchases.apply_paid_invoice(first["id"], now=20)
    db._conn.execute("UPDATE mgboost_subscriptions SET current_expiry=? WHERE account_id=?", (current_expiry, account["id"]))
    db._conn.commit()
    invoice = _paid(db, account, invoice_now=30, payment_now=31, suffix="renew")
    result = db.stars_purchases.apply_paid_invoice(invoice["id"], now=now)
    assert result["new_expiry"] == expected_anchor + 30 * 86400
    assert _application(db, invoice["id"])["applied_operation"] == "RENEW"


def test_30_60_and_repeated_same_plan_payments_add_once_each(db):
    account = _account(db)
    first = _paid(db, account, days=30, invoice_now=10, payment_now=11, suffix="30")
    r1 = db.stars_purchases.apply_paid_invoice(first["id"], now=100)
    second = _paid(db, account, days=60, invoice_now=20, payment_now=21, suffix="60")
    r2 = db.stars_purchases.apply_paid_invoice(second["id"], now=101)
    assert r2["new_expiry"] == r1["new_expiry"] + 60 * 86400
    replay = db.stars_purchases.apply_paid_invoice(second["id"], now=999)
    assert replay["already_applied"] is True
    assert replay["new_expiry"] == r2["new_expiry"]
    assert db._conn.execute("SELECT COUNT(*) FROM mgboost_subscription_terms WHERE account_id=?", (account["id"],)).fetchone()[0] == 2


def test_duplicate_callback_and_concurrent_duplicate_payment_do_not_double_grant(db):
    account = _account(db)
    invoice = db.stars_purchases.create_invoice(
        telegram_id=account["telegram_id"], plan_code="BASIC", duration_days=30, ttl_seconds=3600, now=10
    )
    outcomes = []
    lock = threading.Lock()
    def capture():
        value = db.stars_purchases.capture_paid(
            invoice["id"], charge_id="same-charge", provider_charge_id=None,
            payer_telegram_id=account["telegram_id"], currency="XTR", amount=99, now=11,
        )
        with lock:
            outcomes.append(value)
    threads = [threading.Thread(target=capture) for _ in range(4)]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    assert outcomes.count("paid") == 1
    assert outcomes.count("duplicate") == 3
    first = db.stars_purchases.apply_paid_invoice(invoice["id"], now=100)
    second = db.stars_purchases.apply_paid_invoice(invoice["id"], now=101)
    assert second["already_applied"] is True
    assert first["new_expiry"] == second["new_expiry"]


def test_concurrent_distinct_successful_payments_each_apply_once(db):
    account = _account(db)
    first = _paid(db, account, invoice_now=10, payment_now=11, suffix="one")
    second = _paid(db, account, invoice_now=12, payment_now=13, suffix="two")
    results = []
    lock = threading.Lock()
    def apply(invoice_id):
        value = db.stars_purchases.apply_paid_invoice(invoice_id, now=100)
        with lock:
            results.append(value)
    threads = [threading.Thread(target=apply, args=(item["id"],)) for item in (first, second)]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    expiry = db.entitlements.calculate(account_id=account["id"], now=100)["subscription"]["effective_expiry"]
    assert expiry == 100 + 60 * 86400
    assert {item["already_applied"] for item in results} == {False}


def test_crash_after_local_apply_before_invoice_ack_replays_without_extra_days(db):
    account = _account(db)
    invoice = _paid(db, account)
    # Model the exact crash boundary by committing PH5-02 and deliberately
    # leaving the invoice in ``paid`` before its acknowledgement/app sync.
    direct = db.subscription_renewal.apply_same_plan_purchase(
        account_id=account["id"], plan_code="BASIC", duration_days=30,
        payment_channel="TELEGRAM_STARS", mutation_source="DIRECT_PURCHASE", actor_type="TELEGRAM",
        idempotency_key="ph5-05-stars-invoice-%020d" % invoice["id"], now=100,
    )
    recovered = db.stars_purchases.apply_paid_invoice(invoice["id"], now=200)
    assert recovered["already_applied"] is True
    assert recovered["new_expiry"] == direct["new_expiry"]
    assert db.get_invoice(invoice["id"])["status"] == "canonical_applied"


def test_crash_replay_after_later_payment_preserves_both_paid_grants(db):
    """A lost acknowledgement for A may be replayed after B has extended it."""
    account = _account(db)
    first = _paid(db, account, invoice_now=10, payment_now=11, suffix="crash-a")
    direct = db.subscription_renewal.apply_same_plan_purchase(
        account_id=account["id"], plan_code="BASIC", duration_days=30,
        payment_channel="TELEGRAM_STARS", mutation_source="DIRECT_PURCHASE", actor_type="TELEGRAM",
        idempotency_key="ph5-05-stars-invoice-%020d" % first["id"], now=100,
    )
    second = _paid(db, account, invoice_now=12, payment_now=13, suffix="later-b")
    second_result = db.stars_purchases.apply_paid_invoice(second["id"], now=101)
    replay = db.stars_purchases.apply_paid_invoice(first["id"], now=102)
    assert replay["already_applied"] is True
    assert replay["new_expiry"] == direct["new_expiry"]
    assert second_result["new_expiry"] == direct["new_expiry"] + 30 * 86400
    assert db.entitlements.calculate(account_id=account["id"], now=102)["subscription"]["effective_expiry"] == second_result["new_expiry"]


@pytest.mark.parametrize("currency,amount,payer,reason", [
    ("RUB", 99, 900001, "amount_or_currency_mismatch"),
    ("XTR", 98, 900001, "amount_or_currency_mismatch"),
    ("XTR", 99, 900099, "payer_account_mismatch"),
])
def test_callback_mismatches_are_durable_manual_review(db, currency, amount, payer, reason):
    account = _account(db)
    invoice = db.stars_purchases.create_invoice(
        telegram_id=account["telegram_id"], plan_code="BASIC", duration_days=30, ttl_seconds=3600, now=1
    )
    assert db.stars_purchases.capture_paid(
        invoice["id"], charge_id=f"mismatch-{currency}-{amount}-{payer}", provider_charge_id=None,
        payer_telegram_id=payer, currency=currency, amount=amount, now=2,
    ) == "manual_review"
    row = db.get_invoice(invoice["id"])
    assert row["status"] == "manual_review" and row["manual_review_reason"] == reason


def test_other_plan_is_not_stacked_and_admin_override_survives(db):
    account = _account(db)
    first = _paid(db, account, plan="BASIC", suffix="basic")
    db.stars_purchases.apply_paid_invoice(first["id"], now=100)
    subscription_id = db._conn.execute(
        "SELECT id FROM mgboost_subscriptions WHERE account_id=?", (account["id"],)
    ).fetchone()[0]
    mutation_id = db._conn.execute(
        "INSERT INTO mgboost_entitlement_mutations "
        "(account_id,subscription_id,operation,payment_channel,mutation_source,actor_type,"
        "actor_ref,reason,idempotency_key_hash,before_json,after_json,created_at) "
        "VALUES (?,?,?,'ADMIN_GRANT','ADMIN','TEST','test','test override',?,'{}','{}',110)",
        (account["id"], subscription_id, "TEST_OVERRIDE", "a" * 64),
    ).lastrowid
    override_id = db._conn.execute(
        "INSERT INTO mgboost_entitlement_overrides "
        "(account_id,subscription_id,entitlement_key,value_type,boolean_value,integer_value,"
        "starts_at,expires_at,reason,mutation_id,created_at) VALUES (?,?,?,'BOOLEAN',1,NULL,110,10000,?,?,110)",
        (account["id"], subscription_id, "WL_ACCESS", "test override", mutation_id),
    ).lastrowid
    db._conn.commit()
    with pytest.raises(Exception):
        db.stars_purchases.create_invoice(
            telegram_id=account["telegram_id"], plan_code="WL", duration_days=30, ttl_seconds=3600, now=120
        )
    renewal = _paid(db, account, plan="BASIC", suffix="renew")
    db.stars_purchases.apply_paid_invoice(renewal["id"], now=130)
    entitlement = db.entitlements.calculate(account_id=account["id"], now=131)
    assert override_id in entitlement["overrides"]["applied_ids"]
    assert entitlement["plan"]["code"] == "BASIC"


def test_legacy_invoice_stays_expire_only_compatible(db):
    legacy = db.create_stars_invoice(
        created_by_telegram_id=1, marzban_username="legacy-user", tariff_id=1,
        tariff_name="legacy", duration_days=30, stars_price=320,
    )
    row = db.get_invoice(legacy["id"])
    assert row["invoice_kind"] == "LEGACY_EXPIRE"
    assert db.get_pending_apply_invoices() == []
    assert db.stars_purchases.pending_invoices() == []
    with pytest.raises(Exception, match="cannot be reinterpreted"):
        db._conn.execute("UPDATE stars_invoices SET invoice_kind='CANONICAL_PLAN' WHERE id=?", (legacy["id"],))


def test_stale_or_corrupt_product_reference_is_manual_review_not_a_guess(db):
    account = _account(db)
    # This models a stale/corrupt callback record from a partially rolled
    # catalog deployment.  It is intentionally inserted only in the test:
    # normal creation cannot make such a row.
    db._conn.execute(
        "INSERT INTO stars_invoices (created_by_telegram_id,marzban_username,tariff_name,duration_days,"
        "stars_price,status,expires_at,created_at,invoice_kind,account_id,plan_version_id,duration_id,"
        "catalog_version_id,price_id,plan_code_snapshot,plan_version_snapshot,catalog_version_snapshot,"
        "price_amount_snapshot) VALUES (?,?,?,?,?,'created',?,?,?,?,?,?,?,?,?,?,?,?)",
        (account["telegram_id"], account["public_id"], "Basic", 30, 99, 3_600, 1,
         "CANONICAL_PLAN", account["id"], 999, 999, 999, 999, "BASIC", 1,
         "STALE-CATALOG", 99),
    )
    invoice_id = db._conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    db._conn.commit()
    assert db.stars_purchases.capture_paid(
        invoice_id, charge_id="stale-product-charge", provider_charge_id=None,
        payer_telegram_id=account["telegram_id"], currency="XTR", amount=99, now=2,
    ) == "manual_review"
    assert db.get_invoice(invoice_id)["manual_review_reason"] == "product_or_account_state_mismatch"


def test_paid_renewal_converges_all_active_children_via_durable_sync_job(db):
    """PH5-05 uses PH3-08 outbox rather than touching children inline."""
    from src.broker_operations import BrokerOperations
    from src.plan_catalog import seed_plan_catalog
    from src.stars import _sync_canonical_purchase_children
    from tests.test_child_lifecycle import _build_applied_child
    from tests.test_child_provisioning import HWID_KEY
    from src.child_contract import source_contract_hash

    seed_plan_catalog(db.plan_catalog, now=1)
    fx = _build_applied_child(db, mapping="PH505_CHILDREN", tg=910001)
    account_id = fx["account"]["account_id"]
    basic = db.plan_catalog.get_plan_version("BASIC")
    db._conn.execute(
        "UPDATE mgboost_subscriptions SET current_plan_version_id=?,status='ACTIVE',current_expiry=? WHERE account_id=?",
        (basic["id"], 1_000, account_id),
    )
    db._conn.execute("UPDATE mgboost_accounts SET account_source='DIRECT' WHERE id=?", (account_id,))
    db._conn.commit()

    # Build two more real active children for the same parent through the
    # existing PH3-03 ensure/ACK contract.
    children = [fx["child_username"]]
    for index in range(2):
        slot = db.device_slots.claim(account_id, f"ph505-child-{index}", HWID_KEY, now=300 + index)
        source = dict(fx["remote"].users["alice"])
        source["username"] = f"ph505-source-{index}"
        fx["remote"].users[source["username"]] = source
        prepared = db.child_provisioning.prepare_child_ensure(
            account_id=account_id, slot_generation_id=slot["generation_id"], source_alias_id=fx["alias_id"],
            source_contract_hash=source_contract_hash(source), expire=1_000,
            idempotency_key=f"ph505-extra-child-{index}", now=310 + index,
        )
        claimed = db.child_provisioning.claim(prepared["operation_id"], worker_id="ph505-fixture", now=320 + index)
        created = BrokerOperations(fx["remote"]).dispatch("child.user.ensure", claimed["payload"])
        child_uuid = created.pop("uuid")
        db.child_provisioning.acknowledge(
            prepared["operation_id"], worker_id="ph505-fixture", outcome=created["outcome"],
            child_uuid=child_uuid, remote_result=created, now=330 + index,
        )
        children.append(prepared["child_username"])

    applied_now = int(time.time())
    invoice = _paid(
        db, {"id": account_id, "telegram_id": 910001},
        invoice_now=applied_now - 2, payment_now=applied_now - 1,
    )
    result = db.stars_purchases.apply_paid_invoice(invoice["id"], now=applied_now)

    class FailingService:
        def sync_child_user_state(self, payload):
            raise ConnectionError("temporary broker outage")

    # The canonical local grant/application is committed before this remote
    # boundary.  A process/broker failure leaves its durable hand-off pending,
    # never rolls back or double-grants the purchased term.
    asyncio.run(_sync_canonical_purchase_children(db, FailingService()))
    assert db.stars_purchases.pending_sync_jobs()[0]["invoice_id"] == invoice["id"]
    db._conn.execute(
        "UPDATE mgboost_parent_sync_operations SET lease_expires_at=0 WHERE account_id=? AND state='IN_FLIGHT'",
        (account_id,),
    )
    db._conn.commit()

    class Service:
        def sync_child_user_state(self, payload):
            return BrokerOperations(fx["remote"]).dispatch("child.user.state.sync", payload)

    asyncio.run(_sync_canonical_purchase_children(db, Service()))
    for child in children:
        assert fx["remote"].users[child]["expire"] == result["new_expiry"]
    job = db.stars_purchases.pending_sync_jobs()
    assert job == []
    assert db._conn.execute(
        "SELECT state FROM mgboost_stars_purchase_sync_jobs WHERE invoice_id=?", (invoice["id"],)
    ).fetchone()[0] == "SYNCED"
