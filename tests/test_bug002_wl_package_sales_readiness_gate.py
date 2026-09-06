"""BUG-002 fix regression coverage: WL package sales readiness gate.

See `BUGS.md` BUG-002 and `src/wl_packages.py::assert_wl_package_sales_enabled`
for the full analysis. Static evidence established that WL packages are NOT
a launched, owner-supported customer-facing feature yet -- the Stars channel
already never lists them in its sellable catalog ("PH6-08 absent"), but the
manual (RUB) admin channel had no equivalent gate: `create_record` accepted
`package_sku` unconditionally, and the HTTP catalog/preview endpoints listed/
allowed it too. This is option A from the bug's own "Suggested fix" ("Gate
incomplete package sales across every channel"), not a reimplementation of
PH6-08 (effective-quota enforcement remains unbuilt and untouched here).

This module is narrowly scoped to BUG-002's readiness gate only -- it does
not touch BUG-001/003/004(already fixed)/005, promo chronology, unrelated
billing, account/device architecture, UI redesign or any other roadmap phase.
"""

from __future__ import annotations

import importlib
import os
import tempfile

import pytest

import src.config as config
from src.manual_payment import ManualPaymentError
from src.security import AdminSessionStore

from tests.test_manual_payment_ph509 import _paid_stars_subscription

PRIMARY = "owner:mgboost-primary:v1"
PRIMARY_LOGIN = "authenticated-primary-login"


@pytest.fixture
def db(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="bug002-test-")
    monkeypatch.setenv("DATA_DIR", tmp)
    monkeypatch.setenv("PRIMARY_MGBOOST_ADMIN_ACTOR_ID", PRIMARY)
    monkeypatch.setenv("PRIMARY_MGBOOST_ADMIN_LOGIN", PRIMARY_LOGIN)
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


def _capability(db):
    _raw, session = AdminSessionStore().create(PRIMARY_LOGIN, "test-server-jwt")
    return db.primary_admin_authority.authorize_session(session)


def _account(db):
    return db.accounts.create_account("DIRECT", now=1)


def _record_count(db) -> int:
    return db._conn.execute(
        "SELECT COUNT(*) c FROM mgboost_manual_payment_records"
    ).fetchone()["c"]


# --- 1/5/7: create/preview/apply fail closed by default; malformed or -----
#            repeated requests never mutate state -----------------------------

def test_package_create_is_blocked_by_default(db):
    """WL_PACKAGE_SALES_ENABLED defaults off: the confirmed default state."""
    assert config.WL_PACKAGE_SALES_ENABLED is False
    cap = _capability(db)
    account = _account(db)
    with pytest.raises(ManualPaymentError, match="not yet an enabled"):
        db.manual_payments.create_record(
            cap, account_id=account["id"], package_sku="WL_PACKAGE_50_GB",
            external_reference="pkg-blocked-1", recorded_amount_minor=139,
            payment_method="bank_transfer", idempotency_key="pkg-blocked-key-0001",
        )
    assert _record_count(db) == 0


def test_malformed_or_unknown_package_sku_changes_nothing_while_gated(db):
    """Even a bogus/unknown SKU never reaches catalog resolution or the DB
    while sales are gated -- the readiness check runs first."""
    cap = _capability(db)
    account = _account(db)
    for bogus_sku in ("", "NOT_A_REAL_SKU", "WL_PACKAGE_50_GB' OR '1'='1", "ARBITRARY_123_GB"):
        with pytest.raises(ManualPaymentError):
            db.manual_payments.create_record(
                cap, account_id=account["id"], package_sku=bogus_sku,
                external_reference=f"pkg-bogus-{bogus_sku!r}",
                recorded_amount_minor=139, payment_method="bank_transfer",
                idempotency_key=f"pkg-bogus-key-{abs(hash(bogus_sku))}",
            )
    assert _record_count(db) == 0


def test_repeated_blocked_request_is_idempotently_a_noop(db):
    """The exact same forbidden request retried (same idempotency key)
    applies nothing on either attempt -- there is no partial/duplicate state."""
    cap = _capability(db)
    account = _account(db)
    kwargs = dict(
        account_id=account["id"], package_sku="WL_PACKAGE_50_GB",
        external_reference="pkg-retry-1", recorded_amount_minor=139,
        payment_method="bank_transfer", idempotency_key="pkg-retry-key-000001",
    )
    for _ in range(2):
        with pytest.raises(ManualPaymentError):
            db.manual_payments.create_record(cap, **kwargs)
    assert _record_count(db) == 0


def test_ordinary_manual_plan_purchase_still_works_while_packages_are_gated(db):
    """Item 2: the gate is package-specific -- normal plan purchase/apply is
    completely unaffected."""
    from src.plan_catalog import RUB_PRICES
    cap = _capability(db)
    account = _account(db)
    record = db.manual_payments.create_record(
        cap, account_id=account["id"], plan_code="WL", duration_days=30,
        external_reference="plan-ok-1", recorded_amount_minor=RUB_PRICES[("WL", 30)],
        payment_method="bank_transfer", idempotency_key="plan-ok-key-0000001", now=100,
    )
    assert record["status"] == "PENDING"
    result = db.manual_payments.apply_record(cap, record["id"], now=200)
    assert result["already_applied"] is False
    applied = db.manual_payments.get_record(record["id"])
    assert applied["status"] == "APPLIED"


# --- 3: no direct-backend-request bypass -----------------------------------

def test_package_sku_cannot_be_reached_by_a_direct_store_call(db):
    """The gate lives in the store method itself (`ManualPaymentStore.
    create_record`), not only in the HTTP handler -- a direct backend/API
    call bypassing any UI/route cannot reach package creation either."""
    cap = _capability(db)
    account = _account(db)
    with pytest.raises(ManualPaymentError):
        db.manual_payments.create_record(
            cap, account_id=account["id"], package_sku="WL_PACKAGE_100_GB",
            external_reference="pkg-direct-1", recorded_amount_minor=249,
            payment_method="bank_transfer", idempotency_key="pkg-direct-key-000001",
        )
    assert _record_count(db) == 0


# --- HTTP layer: preview/catalog/create all agree with the store -----------

def test_http_catalog_preview_create_all_agree_the_feature_is_off(db):
    from src.routes import admin_payments as AP
    from tests._ops_helpers import make_handler

    catalog_handler = make_handler(db, command="GET")
    AP.handle_manual_payment_catalog(catalog_handler)
    assert catalog_handler.json()["packages"] == []

    account = _account(db)
    preview_handler = make_handler(db, payload={"package_sku": "WL_PACKAGE_50_GB"})
    AP.handle_manual_payment_preview(preview_handler, str(account["id"]))
    preview_body = preview_handler.json()
    assert preview_body["purchasable"] is False
    assert preview_body["not_purchasable_reason"] == "WL_PACKAGE_SALES_NOT_ENABLED"

    create_handler = make_handler(db, payload={
        "package_sku": "WL_PACKAGE_50_GB", "recorded_amount_minor": 139,
        "external_reference": "pkg-http-1", "payment_method": "cash",
        "idempotency_key": "pkg-http-key-0000000001",
    })
    AP.handle_manual_payment_create(create_handler, str(account["id"]))
    assert create_handler.status == 400
    assert _record_count(db) == 0


# --- 4: Stars channel remains fail-closed regardless of this flag ---------

def test_stars_channel_never_sells_packages_regardless_of_the_flag(db, monkeypatch):
    sellable = {item["plan_code"] for item in db.stars_purchases.sellable_catalog()}
    assert not any(code.startswith("WL_PACKAGE") for code in sellable)
    # Even if the manual-channel readiness flag were ever turned on, the
    # Stars channel has no package purchase code path at all (unrelated,
    # hardcoded SELLABLE_PLAN_CODES) -- confirm the two cannot silently
    # re-diverge in the other direction either.
    monkeypatch.setattr(config, "WL_PACKAGE_SALES_ENABLED", True)
    sellable_when_on = {item["plan_code"] for item in db.stars_purchases.sellable_catalog()}
    assert not any(code.startswith("WL_PACKAGE") for code in sellable_when_on)


# --- 6: existing/historical package rows are never touched by the gate ----

def test_existing_applied_package_row_is_untouched_after_the_gate_ships(db, monkeypatch):
    """Simulates a package that was already sold/applied before this
    readiness gate existed (or during an explicitly-approved rollout
    window): flipping the flag back off must not delete, cancel, refund or
    mutate it, and applying it again (idempotent retry) must still work."""
    monkeypatch.setattr(config, "WL_PACKAGE_SALES_ENABLED", True)
    cap = _capability(db)
    account = _account(db)
    _paid_stars_subscription(db, account["id"], plan="WL", days=60, telegram_id=901501)
    record = db.manual_payments.create_record(
        cap, account_id=account["id"], package_sku="WL_PACKAGE_250_GB",
        external_reference="pkg-historical-1", recorded_amount_minor=579,
        payment_method="bank_transfer", idempotency_key="pkg-historical-key-01",
        now=100,
    )
    result = db.manual_payments.apply_record(cap, record["id"], now=150)
    assert result["already_applied"] is False
    before = db.manual_payments.get_record(record["id"])
    assert before["status"] == "APPLIED"

    # Now the feature is (correctly) gated off again for everyone else.
    monkeypatch.setattr(config, "WL_PACKAGE_SALES_ENABLED", False)

    after = db.manual_payments.get_record(record["id"])
    assert after == before  # byte-for-byte unchanged, not cancelled/rewritten
    assert after["status"] == "APPLIED"

    # Idempotent re-apply (e.g. a retried bookkeeping call after a crash)
    # still converges safely -- the gate only blocks *new* package creation.
    retried = db.manual_payments.apply_record(cap, record["id"], now=151)
    assert retried["already_applied"] is True
    assert db.manual_payments.get_record(record["id"]) == before
