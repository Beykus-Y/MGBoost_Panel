"""PH5-09 manual external-payment record and entitlement application."""

import importlib
import os
import tempfile
import threading

import pytest

import src.manual_payment as manual_payment_module
from src.admin_authority import PrimaryAdminAuthorizationError
from src.manual_payment import (
    ApplyRequiresManualReview,
    ManualPaymentConflict,
    ManualPaymentError,
)
from src.plan_catalog import RUB_CATALOG_VERSION, RUB_PRICES
from src.security import AdminSessionStore

PRIMARY = "owner:mgboost-primary:v1"
PRIMARY_LOGIN = "authenticated-primary-login"


@pytest.fixture
def db(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="ph509-test-")
    monkeypatch.setenv("DATA_DIR", tmp)
    monkeypatch.setenv("PRIMARY_MGBOOST_ADMIN_ACTOR_ID", PRIMARY)
    monkeypatch.setenv("PRIMARY_MGBOOST_ADMIN_LOGIN", PRIMARY_LOGIN)
    import src.config as config
    import src.database as database
    importlib.reload(config)
    importlib.reload(database)
    # BUG-002 fix / PH6-08 readiness gate: this file specifically tests the
    # package create/apply *mechanics* (grant/consumption correctness), which
    # remain real and necessary groundwork even while the owner has not
    # launched package sales as a feature (see BUGS.md BUG-002,
    # src/wl_packages.py::assert_wl_package_sales_enabled). The gate itself is
    # covered by its own dedicated test module.
    config.WL_PACKAGE_SALES_ENABLED = True
    database.DB_PATH = os.path.join(tmp, "db.sqlite3")
    instance = database.Database()
    from src.plan_catalog import seed_plan_catalog
    from src.wl_package_catalog import seed_wl_package_catalog
    seed_plan_catalog(instance.plan_catalog, now=1)
    seed_wl_package_catalog(instance.wl_package_catalog, now=1)
    yield instance
    instance._conn.close()


def _capability(db):
    _raw, session = AdminSessionStore().create(PRIMARY_LOGIN, "test-server-jwt")
    return db.primary_admin_authority.authorize_session(session)


def _account(db):
    return db.accounts.create_account("DIRECT", now=1)


def _record(db, capability, account_id, *, plan="WL", days=30, ref="transfer-000001",
            method="bank_transfer", key=None, now=100, **kwargs):
    key = key or f"manual-payment-test-{ref}"
    return db.manual_payments.create_record(
        capability, account_id=account_id, plan_code=plan, duration_days=days,
        external_reference=ref, recorded_amount_minor=RUB_PRICES[(plan, days)],
        payment_method=method, idempotency_key=key, now=now, **kwargs,
    )


def _paid_stars_subscription(db, account_id, *, plan, days, telegram_id=900777):
    """Fixture grant through the PH5-02 engine. PH5-11: the Stars channel
    carries the first-rollout sellable-plan gate, so non-sellable plans
    (WL/EXTENDED/FAMILY) apply directly through the engine -- the gate is
    channel-level by design."""
    db.accounts.link_telegram_owner(
        account_id, telegram_id, provenance="MIGRATION", actor="test", now=1,
    )
    return db.subscription_renewal.apply_same_plan_purchase(
        account_id=account_id, plan_code=plan, duration_days=days,
        payment_channel="TELEGRAM_STARS", mutation_source="DIRECT_PURCHASE",
        actor_type="TELEGRAM", actor_ref=str(telegram_id),
        reason="fixture subscription",
        idempotency_key=f"fixture-mpay-{account_id}-{plan}-{days}",
        now=20,
    )


# --- approved catalog round-trips -----------------------------------------------


@pytest.mark.parametrize("plan,days", sorted(RUB_PRICES))
def test_every_commercial_plan_rub_product_applies_at_exact_fixed_price(db, plan, days):
    cap = _capability(db)
    account = _account(db)
    record = _record(db, cap, account["id"], plan=plan, days=days, ref=f"t-{plan}-{days}")
    assert record["status"] == "PENDING"
    assert record["currency"] == "RUB"
    assert record["expected_amount_minor"] == record["recorded_amount_minor"] == RUB_PRICES[(plan, days)]
    assert record["catalog_version_snapshot"] == RUB_CATALOG_VERSION
    result = db.manual_payments.apply_record(cap, record["id"], now=200)
    assert result["already_applied"] is False
    assert result["new_expiry"] == 200 + days * 86400
    applied = db.manual_payments.get_record(record["id"])
    assert applied["status"] == "APPLIED"
    assert applied["applied_operation"] == ("CREATE" if account["id"] else "RENEW")
    assert applied["external_reference"] == f"t-{plan}-{days}"
    application = db.manual_payments.get_application(record["id"])
    assert application["entitlement_mutation_id"] == applied["entitlement_mutation_id"]
    mutation = db._conn.execute(
        "SELECT payment_channel,mutation_source,actor_type FROM mgboost_entitlement_mutations "
        "WHERE id=?",
        (applied["entitlement_mutation_id"],),
    ).fetchone()
    assert (mutation["payment_channel"], mutation["mutation_source"], mutation["actor_type"]) == (
        "EXTERNAL_PAYMENT", "MANUAL_PAYMENT", "PRIMARY_ADMIN",
    )
    proof = result["entitlement"]
    assert proof["calculation_version"] == "ph5-04-entitlement-v1"
    assert proof["plan"]["code"] == plan
    assert proof["subscription"]["effective_expiry"] == result["new_expiry"]


def test_exact_rub_catalog_price_is_enforced(db):
    cap = _capability(db)
    account = _account(db)
    for wrong_amount in (RUB_PRICES[("WL", 30)] - 1, RUB_PRICES[("WL", 30)] + 1, 1, 0, -349):
        with pytest.raises(ManualPaymentError):
            db.manual_payments.create_record(
                cap, account_id=account["id"], plan_code="WL", duration_days=30,
                external_reference=f"bad-{wrong_amount}",
                recorded_amount_minor=wrong_amount,
                payment_method="bank_transfer",
                idempotency_key=f"bad-key-{wrong_amount}--------",
            )
    with pytest.raises(ManualPaymentError):
        db.manual_payments.create_record(
            cap, account_id=account["id"], plan_code="WL", duration_days=30,
            external_reference="bool-amount-------x", recorded_amount_minor=True,
            payment_method="bank_transfer", idempotency_key="bool-amount-key------xxx",
        )
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_manual_payment_records"
    ).fetchone()[0] == 0


def test_currency_is_server_pinned_rub_and_never_taken_from_input(db):
    cap = _capability(db)
    account = _account(db)
    # A Stars-price transfer number must never satisfy the fixed RUB table.
    with pytest.raises(ManualPaymentError):
        db.manual_payments.create_record(
            cap, account_id=account["id"], plan_code="BASIC", duration_days=30,
            external_reference="stars-priced-transfer-x", recorded_amount_minor=99,
            payment_method="bank_transfer", idempotency_key="stars-price-key-----xxxx",
        )
    record = _record(db, cap, account["id"], plan="BASIC", days=30, ref="rub-only-1")
    assert record["currency"] == "RUB"


def test_stale_retired_catalog_version_remains_the_contractual_price(db):
    cap = _capability(db)
    account = _account(db)
    record = _record(db, cap, account["id"], plan="WL", days=30, ref="retired-cat-1")
    catalog = db._conn.execute(
        "SELECT * FROM mgboost_price_catalog_versions WHERE channel='RUB' AND status='ACTIVE'"
    ).fetchone()
    db._conn.execute(
        "UPDATE mgboost_price_catalog_versions SET status='RETIRED',retired_at=? WHERE id=?",
        (150, catalog["id"]),
    )
    db._conn.commit()
    result = db.manual_payments.apply_record(cap, record["id"], now=200)
    assert result["new_expiry"] == 200 + 30 * 86400
    applied = db.manual_payments.get_record(record["id"])
    assert applied["expected_amount_minor"] == RUB_PRICES[("WL", 30)]
    assert applied["catalog_version_snapshot"] == RUB_CATALOG_VERSION
    application = db.manual_payments.get_application(record["id"])
    assert RUB_CATALOG_VERSION in application["entitlement_snapshot_json"] or (
        applied["catalog_version_snapshot"] == RUB_CATALOG_VERSION
    )


def test_cross_channel_or_corrupt_pin_fails_closed_instead_of_reinterpreting(db):
    cap = _capability(db)
    account = _account(db)
    record = _record(db, cap, account["id"], plan="WL", days=30, ref="corrupt-pin-1")
    stars_catalog = db._conn.execute(
        "SELECT id FROM mgboost_price_catalog_versions WHERE channel='TELEGRAM_STARS'"
    ).fetchone()
    db._conn.execute(
        "UPDATE mgboost_manual_payment_records SET catalog_version_id=?,"
        "catalog_version_snapshot='STARS-2026-08-26-v1' WHERE id=?",
        (stars_catalog["id"], record["id"]),
    )
    db._conn.commit()
    with pytest.raises(ManualPaymentError):
        db.manual_payments.apply_record(cap, record["id"], now=200)
    assert db.manual_payments.get_record(record["id"])["status"] == "PENDING"


# --- input authority / manipulation ----------------------------------------------


def test_manipulated_plan_duration_and_days_are_rejected(db):
    cap = _capability(db)
    account = _account(db)
    common = dict(account_id=account["id"], payment_method="bank_transfer")
    with pytest.raises(ManualPaymentError):
        db.manual_payments.create_record(
            capability=cap, plan_code="INTERNAL_ANYTHING", duration_days=30,
            external_reference="plan-tamper-1---------", recorded_amount_minor=169,
            idempotency_key="plan-tamper-key-1-------", **common,
        )
    with pytest.raises(ManualPaymentError):
        db.manual_payments.create_record(
            capability=cap, plan_code="BASIC", duration_days=45,
            external_reference="days-tamper-45------xx", recorded_amount_minor=169,
            idempotency_key="days-tamper-key-45-----x", **common,
        )
    for days in (0, -30):
        with pytest.raises(ManualPaymentError):
            db.manual_payments.create_record(
                capability=cap, plan_code="BASIC", duration_days=days,
                external_reference=f"neg-days-{days}------xx", recorded_amount_minor=169,
                idempotency_key=f"neg-days-key-{days}------x", **common,
            )


def test_arbitrary_package_sku_and_its_grant_go_through_fixed_catalog_only(db):
    cap = _capability(db)
    account = _account(db)
    _paid_stars_subscription(db, account["id"], plan="WL", days=60, telegram_id=901001)
    with pytest.raises(ManualPaymentError):
        db.manual_payments.create_record(
            cap, account_id=account["id"], package_sku="ARBITRARY_123_GB",
            external_reference="pkg-tamper------------", recorded_amount_minor=139,
            payment_method="bank_transfer", idempotency_key="pkg-tamper-key----------",
        )
    record = db.manual_payments.create_record(
        cap, account_id=account["id"], package_sku="WL_PACKAGE_50_GB",
        external_reference="pkg-ok------------------", recorded_amount_minor=139,
        payment_method="bank_transfer", idempotency_key="pkg-ok-key-------------xx",
        now=120,
    )
    assert record["package_bytes_snapshot"] == 50 * 10**9
    result = db.manual_payments.apply_record(cap, record["id"], now=130)
    assert result["granted_bytes"] == 50 * 10**9
    payment_rows = db._conn.execute(
        "SELECT payment_channel,record_status,amount_minor,currency FROM mgboost_payment_records"
    ).fetchall()
    assert len(payment_rows) == 1
    assert tuple(payment_rows[0]) == ("EXTERNAL_PAYMENT", "CONFIRMED", 139, "RUB")


def test_manual_package_refund_semantics_come_from_the_existing_engine(db):
    cap = _capability(db)
    account = _account(db)
    _paid_stars_subscription(db, account["id"], plan="WL", days=30, telegram_id=901002)
    pkg = db.manual_payments.create_record(
        cap, account_id=account["id"], package_sku="WL_PACKAGE_100_GB",
        external_reference="pkg-refund-ok----------", recorded_amount_minor=249,
        payment_method="cash", idempotency_key="pkg-refund-key----------x", now=120,
    )
    db.manual_payments.apply_record(cap, pkg["id"], now=130)
    grant = db._conn.execute(
        "SELECT * FROM mgboost_wl_package_grants WHERE account_id=?", (account["id"],),
    ).fetchone()
    # Zero consumption -> refund succeeds through the unchanged PH5-03 engine.
    refunded = db.wl_packages.refund_unused_package(
        account_id=account["id"], package_grant_id=grant["id"],
        refund_reference="unused-return-reference----x", evidence={"channel": "RUB"},
        idempotency_key="pkg-refund-unused---------x", actor_type="PRIMARY_ADMIN",
        actor_ref="owner:mgboost-primary:v1", now=140,
    )
    assert refunded["already_applied"] is False
    # A refunded manual-payment bucket can never be refunded or revived again.
    from src.wl_packages import PackageAlreadyRefunded
    with pytest.raises(PackageAlreadyRefunded):
        db.wl_packages.refund_unused_package(
            account_id=account["id"], package_grant_id=grant["id"],
            refund_reference="second-return-attempt-------", evidence={},
            idempotency_key="pkg-refund-consumed--------x", actor_type="PRIMARY_ADMIN",
            actor_ref="owner:mgboost-primary:v1", now=150,
        )
    replayed = db.wl_packages.refund_unused_package(
        account_id=account["id"], package_grant_id=grant["id"],
        refund_reference="unused-return-reference----x", evidence={"channel": "RUB"},
        idempotency_key="pkg-refund-unused---------x", actor_type="PRIMARY_ADMIN",
        actor_ref="owner:mgboost-primary:v1", now=160,
    )
    assert replayed["already_applied"] is True


# --- actor / target authority ---------------------------------------------------


def test_non_primary_actor_is_rejected(db):
    account = _account(db)
    real_cap = _capability(db)
    class ForgedCapability:
        pass
    for forged in (ForgedCapability(), None, "capability"):
        with pytest.raises(PrimaryAdminAuthorizationError):
            db.manual_payments.create_record(
                forged, account_id=account["id"], plan_code="BASIC", duration_days=30,
                external_reference="forged-cap--------------", recorded_amount_minor=169,
                payment_method="bank_transfer", idempotency_key="forged-cap-key----------",
            )
    with pytest.raises(PrimaryAdminAuthorizationError):
        db.manual_payments.apply_record(None, 1)
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_manual_payment_records"
    ).fetchone()[0] == 0
    # The record path stays reachable for the actual primary admin only.
    record = _record(db, real_cap, account["id"], plan="BASIC", days=30, ref="primary-only-1")
    assert record["actor_type"] == "PRIMARY_ADMIN"


def test_account_targets_are_verified_server_side(db, monkeypatch):
    cap = _capability(db)
    with pytest.raises(ManualPaymentError):
        _record(db, cap, 999999, plan="BASIC", days=30, ref="no-such-acct-1")
    account = _account(db)
    db._conn.execute("UPDATE mgboost_accounts SET status='CLOSED' WHERE id=?", (account["id"],))
    db._conn.commit()
    with pytest.raises(ManualPaymentError):
        _record(db, cap, account["id"], plan="BASIC", days=30, ref="closed-acct-1")


# --- duplicate/reference/idempotency semantics ----------------------------------


def test_duplicate_external_reference_is_rejected_across_kinds_and_states(db):
    cap = _capability(db)
    account = _account(db)
    first = _record(db, cap, account["id"], plan="BASIC", days=30, ref="same-ref-1")
    with pytest.raises(ManualPaymentConflict):
        _record(db, cap, account["id"], plan="BASIC", days=30, ref="same-ref-1",
                key="other-key-still-dup-ref---")
    with pytest.raises(ManualPaymentConflict):
        db.manual_payments.create_record(
            cap, account_id=account["id"], package_sku="WL_PACKAGE_50_GB",
            external_reference="same-ref-1", recorded_amount_minor=139,
            payment_method="cash", idempotency_key="cross-kind-dup-ref---------",
        )
    # A cancelled record keeps its reference reserved forever: money facts are
    # never resurrected under a reused transfer number.
    db.manual_payments.cancel_record(cap, first["id"], reason="recorded twice xxxxx", now=110)
    with pytest.raises(ManualPaymentConflict):
        _record(db, cap, account["id"], plan="BASIC", days=30, ref="same-ref-1",
                key="post-cancel-reuse-of-ref---")


def test_idempotency_key_replay_and_conflict_semantics(db):
    cap = _capability(db)
    account = _account(db)
    kwargs = dict(
        capability=cap, account_id=account["id"], plan_code="BASIC_PRO",
        duration_days=30, external_reference="idem-ref-1-----------",
        recorded_amount_minor=279, payment_method="bank_transfer",
        idempotency_key="shared-idem-key-------------",
    )
    first = db.manual_payments.create_record(now=100, **kwargs)
    replay = db.manual_payments.create_record(now=105, **kwargs)
    assert replay["public_id"] == first["public_id"]
    assert replay["created_at"] == first["created_at"]
    kwargs_changed = dict(kwargs)
    kwargs_changed.update(external_reference="idem-ref-2-----------", recorded_amount_minor=399)
    with pytest.raises(ManualPaymentConflict):
        db.manual_payments.create_record(now=106, **kwargs_changed)


def test_concurrent_apply_of_one_payment_yields_exactly_one_entitlement(db):
    cap = _capability(db)
    account = _account(db)
    record = _record(db, cap, account["id"], plan="FAMILY", days=60, ref="concurrent-1")
    outcomes, errors = [], []
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        try:
            outcomes.append(db.manual_payments.apply_record(cap, record["id"], now=300))
        except Exception as exc:  # pragma: no cover - diagnostic path
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    fresh_terms = db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_subscription_terms WHERE account_id=?", (account["id"],),
    ).fetchone()[0]
    fresh_mutations = db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_entitlement_mutations WHERE account_id=?", (account["id"],),
    ).fetchone()[0]
    fresh_applications = db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_manual_payment_applications WHERE payment_record_id=?",
        (record["id"],),
    ).fetchone()[0]
    assert (fresh_terms, fresh_mutations, fresh_applications) == (1, 1, 1)
    applied = db.manual_payments.get_record(record["id"])
    assert applied["status"] == "APPLIED"
    subscription = db._conn.execute(
        "SELECT current_expiry,row_version FROM mgboost_subscriptions WHERE account_id=?",
        (account["id"],),
    ).fetchone()
    assert subscription["current_expiry"] == applied["applied_expiry"] == 300 + 60 * 86400
    already = [o["already_applied"] for o in outcomes]
    assert sorted(already) == [False] + [True] * 7


def test_pending_edit_race_with_apply_never_corrupts_the_fact(db):
    cap = _capability(db)
    account = _account(db)
    record = _record(db, cap, account["id"], plan="BASIC", days=30, ref="edit-race-1")
    stop = threading.Event()

    def editor():
        i = 0
        while not stop.is_set():
            try:
                db.manual_payments.edit_pending_record(
                    cap, record["id"], reason=f"typo fix {i} xxxxxxxx",
                    changes={"comment": f"note-{i}"}, now=150 + i,
                )
            except (ManualPaymentError, ManualPaymentConflict):
                pass
            i += 1

    editor_thread = threading.Thread(target=editor)
    editor_thread.start()
    try:
        result = db.manual_payments.apply_record(cap, record["id"], now=400)
    finally:
        stop.set()
        editor_thread.join()
    assert result["already_applied"] is False
    applied = db.manual_payments.get_record(record["id"])
    assert applied["status"] == "APPLIED"
    assert applied["plan_code_snapshot"] == "BASIC"
    assert applied["duration_days_snapshot"] == 30
    history = db.manual_payments.edit_history(record["id"])
    assert all(entry["before_json"] != entry["after_json"] for entry in history)
    subscription = db._conn.execute(
        "SELECT current_expiry FROM mgboost_subscriptions WHERE account_id=?", (account["id"],),
    ).fetchone()[0]
    assert subscription == 400 + 30 * 86400
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_entitlement_mutations WHERE account_id=?", (account["id"],)
    ).fetchone()[0] == 1


# --- immutability of historical facts --------------------------------------------


def test_applied_record_edit_denial_api_and_sqlite_trigger(db):
    cap = _capability(db)
    account = _account(db)
    record = _record(db, cap, account["id"], plan="WL", days=30, ref="immutable-1")
    db.manual_payments.apply_record(cap, record["id"], now=200)
    for changes in (
        {"external_reference": "rewritten---------"},
        {"payment_method": "gift"},
        {"recorded_amount_minor": 1},
        {"plan_code": "BASIC", "duration_days": 30},
        {"package_sku": "WL_PACKAGE_50_GB"},
    ):
        with pytest.raises(ManualPaymentError):
            db.manual_payments.edit_pending_record(
                cap, record["id"], reason="rewrite after apply xxxxx", changes=changes,
            )
    with pytest.raises(manual_payment_module.sqlite3.IntegrityError):
        db._conn.execute(
            "UPDATE mgboost_manual_payment_records SET external_reference='hacked' WHERE id=?",
            (record["id"],),
        )
    db._conn.rollback()
    with pytest.raises(manual_payment_module.sqlite3.IntegrityError):
        db._conn.execute(
            "DELETE FROM mgboost_manual_payment_records WHERE id=?", (record["id"],)
        )
    db._conn.rollback()
    application = db.manual_payments.get_application(record["id"])
    for statement, params in (
        ("UPDATE mgboost_manual_payment_applications SET applied_expiry=1 WHERE id=?",
         (application["id"],)),
        ("DELETE FROM mgboost_manual_payment_applications WHERE id=?", (application["id"],)),
    ):
        with pytest.raises(manual_payment_module.sqlite3.IntegrityError):
            db._conn.execute(statement, params)
        db._conn.rollback()


def test_cancelled_record_is_terminal_and_uneditable(db):
    cap = _capability(db)
    account = _account(db)
    record = _record(db, cap, account["id"], plan="BASIC", days=30, ref="cancel-term-1")
    cancelled = db.manual_payments.cancel_record(
        cap, record["id"], reason="customer cancelled xxxx", now=150,
    )
    assert cancelled["status"] == "CANCELLED"
    with pytest.raises(ManualPaymentError, match="cancelled"):
        db.manual_payments.cancel_record(cap, record["id"], reason="double cancel xxxxx")
    with pytest.raises(ManualPaymentError):
        db.manual_payments.edit_pending_record(
            cap, record["id"], reason="revive cancelled xxxxx", changes={"comment": "x"},
        )
    with pytest.raises((ManualPaymentError, manual_payment_module.sqlite3.IntegrityError)):
        db.manual_payments.apply_record(cap, record["id"], now=200)
    db._conn.rollback()
    assert db.manual_payments.get_record(record["id"])["status"] == "CANCELLED"


def test_bounds_on_all_free_text_fields(db):
    cap = _capability(db)
    account = _account(db)
    long_reference = "r" * 513
    with pytest.raises(ManualPaymentError):
        db.manual_payments.create_record(
            cap, account_id=account["id"], plan_code="BASIC", duration_days=30,
            external_reference=long_reference, recorded_amount_minor=169,
            payment_method="bank_transfer", idempotency_key="bounds-ref-key-----------",
        )
    with pytest.raises(ManualPaymentError):
        db.manual_payments.create_record(
            cap, account_id=account["id"], plan_code="BASIC", duration_days=30,
            external_reference="ok-ref-bounds----------", recorded_amount_minor=169,
            payment_method="m" * 65, idempotency_key="bounds-method-key--------",
        )
    with pytest.raises(ManualPaymentError):
        db.manual_payments.create_record(
            cap, account_id=account["id"], plan_code="BASIC", duration_days=30,
            external_reference="ok-comment-bounds------", recorded_amount_minor=169,
            payment_method="bank_transfer", comment="c" * 1001,
            idempotency_key="bounds-comment-key-------",
        )
    record = _record(db, cap, account["id"], plan="BASIC", days=30, ref="bounds-trail-ref")
    with pytest.raises(ManualPaymentError):
        db.manual_payments.edit_pending_record(
            cap, record["id"], reason="short", changes={"comment": "x"},
        )


def test_public_projection_carries_no_device_or_credential_identity(db):
    cap = _capability(db)
    account = _account(db)
    record = _record(db, cap, account["id"], plan="BASIC", days=30, ref="view-safe-1")
    view = db.manual_payments.public_view(record)
    assert set(view) == {
        "public_id", "kind", "status", "account_id", "currency",
        "expected_amount_minor", "recorded_amount_minor", "payment_method",
        "external_reference", "created_at", "applied_expiry",
    }


# --- review / reconciliation durability ------------------------------------------


def test_review_state_is_durable_across_restart_and_resolvable(db, monkeypatch):
    cap = _capability(db)
    account = _account(db)
    _paid_stars_subscription(db, account["id"], plan="BASIC_PLUS", days=30, telegram_id=902001)
    record = _record(db, cap, account["id"], plan="BASIC", days=30, ref="review-flow-1")
    with pytest.raises(ApplyRequiresManualReview):
        db.manual_payments.apply_record(cap, record["id"], now=300)
    reviewed = db.manual_payments.get_record(record["id"])
    assert reviewed["status"] == "MANUAL_REVIEW"
    assert reviewed["review_reason"] == "apply_state_mismatch:PlanMismatch"

    data_dir = os.environ["DATA_DIR"]
    db._conn.close()
    import src.config as config2
    import src.database as database2
    importlib.reload(config2)
    importlib.reload(database2)
    database2.DB_PATH = os.path.join(data_dir, "db.sqlite3")
    reopened = database2.Database()
    try:
        survived = reopened.manual_payments.get_record(record["id"])
        assert survived["status"] == "MANUAL_REVIEW"
        assert survived["review_reason"] == "apply_state_mismatch:PlanMismatch"
        assert reopened.manual_payments.list_records(status="PENDING") == []
        _raw, session = AdminSessionStore().create(PRIMARY_LOGIN, "test-server-jwt")
        cap2 = reopened.primary_admin_authority.authorize_session(session)
        with pytest.raises(ApplyRequiresManualReview):
            reopened.manual_payments.apply_record(cap2, record["id"], now=310)
        resolved = reopened.manual_payments.resolve_manual_review(
            cap2, record["id"], resolution_note="operator reconciled with customer xxx", now=320,
        )
        assert resolved["status"] == "PENDING"
        corrected = reopened.manual_payments.edit_pending_record(
            cap2, record["id"], reason="switch to matching plan xxxxxxxx",
            changes={"plan_code": "BASIC_PLUS", "duration_days": 30,
                     "recorded_amount_minor": 239},
            now=330,
        )
        assert corrected["plan_code_snapshot"] == "BASIC_PLUS"
        result = reopened.manual_payments.apply_record(cap2, record["id"], now=340)
        # The Stars-created BASIC_PLUS subscription is still active (bought at
        # now=20), so DL-044 extends from its future expiry, not from `now`.
        prior_expiry = 20 + 30 * 86400
        assert result["new_expiry"] == prior_expiry + 30 * 86400
        kinds = [entry["edit_kind"] for entry in reopened.manual_payments.edit_history(record["id"])]
        assert kinds == ["RESOLVE_REVIEW", "FIELD_EDIT"]
    finally:
        reopened._conn.close()


def test_apply_crash_between_commit_and_proof_recovers_exactly_once(db, monkeypatch):
    cap = _capability(db)
    account = _account(db)
    record = _record(db, cap, account["id"], plan="WL", days=30, ref="crash-proof-1")
    original = manual_payment_module.calculate_effective_entitlement
    calls = {"n": 0}

    def crashing_engine(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated crash after local renewal commit")
        return original(*args, **kwargs)

    monkeypatch.setattr(
        manual_payment_module, "calculate_effective_entitlement", crashing_engine
    )
    with pytest.raises(RuntimeError, match="simulated crash"):
        db.manual_payments.apply_record(cap, record["id"], now=300)
    monkeypatch.undo()
    mid = db.manual_payments.get_record(record["id"])
    # BUG-001 fix: the crash happened *after* the canonical renewal already
    # committed (`calculate_effective_entitlement` is the PH5-04 proof step
    # that runs strictly after `apply_same_plan_purchase`'s own commit) --
    # the record must be the durable APPLYING freeze state, never PENDING,
    # and cancel/edit must both refuse it exactly because of that.
    assert mid["status"] == "APPLYING"
    with pytest.raises(ManualPaymentError, match="currently applying"):
        db.manual_payments.cancel_record(cap, record["id"], reason="should be refused", now=305)
    with pytest.raises(ManualPaymentError, match="can no longer be edited"):
        db.manual_payments.edit_pending_record(
            cap, record["id"], reason="should be refused too",
            changes={"comment": "attempted edit"}, now=305,
        )
    assert db.manual_payments.get_record(record["id"])["status"] == "APPLYING"
    recovered = db.manual_payments.apply_record(cap, record["id"], now=310)
    assert recovered["already_applied"] is True
    assert recovered["new_expiry"] == 310 - 10 + 30 * 86400
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_subscription_terms WHERE account_id=?", (account["id"],),
    ).fetchone()[0] == 1
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_manual_payment_applications WHERE payment_record_id=?",
        (record["id"],),
    ).fetchone()[0] == 1
    assert len(db.manual_payments.pending_sync_jobs()) == 1


def test_package_eligibility_conflict_lands_in_durable_review(db):
    cap = _capability(db)
    account = _account(db)
    _paid_stars_subscription(db, account["id"], plan="BASIC", days=30, telegram_id=902002)
    pkg = db.manual_payments.create_record(
        cap, account_id=account["id"], package_sku="WL_PACKAGE_250_GB",
        external_reference="base-pkg-payment-------", recorded_amount_minor=579,
        payment_method="bank_transfer", idempotency_key="base-pkg-key------------x",
        now=120,
    )
    with pytest.raises(ApplyRequiresManualReview):
        db.manual_payments.apply_record(cap, pkg["id"], now=130)
    reviewed = db.manual_payments.get_record(pkg["id"])
    assert reviewed["status"] == "MANUAL_REVIEW"
    assert reviewed["review_reason"] == "package_grant_conflict:PackageEligibilityError"
    payment = db._conn.execute(
        "SELECT payment_channel,record_status FROM mgboost_payment_records"
    ).fetchone()
    # Money evidence stays durable for operator reconciliation; nothing granted.
    assert tuple(payment) == ("EXTERNAL_PAYMENT", "CONFIRMED")
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_wl_package_grants WHERE account_id=?", (account["id"],),
    ).fetchone()[0] == 0


def test_legacy_stars_invoices_are_never_touched_by_this_module(db):
    cap = _capability(db)
    account = _account(db)
    _record(db, cap, account["id"], plan="BASIC", days=30, ref="legacy-safety-1")
    legacy = db._conn.execute("SELECT COUNT(*) FROM stars_invoices").fetchone()[0]
    canonical = db._conn.execute(
        "SELECT COUNT(*) FROM stars_invoices WHERE invoice_kind!='LEGACY_EXPIRE'"
    ).fetchone()[0]
    assert (legacy, canonical) == (0, 0)
