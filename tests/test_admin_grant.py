"""Admin-grant domain primitive: non-financial entitlement grant through the
existing PH5-02 apply engine, gated by PrimaryAdmin capability.

Not tied to canary usage -- a general-purpose primitive future admin UI /
promo-grant layers can reuse. This test file only exercises what the current
WL no-payment canary task needs: create-or-reuse a DIRECT account for a given
Telegram id, then grant an exact commercial plan/duration with zero financial
rows.
"""

import importlib
import os
import tempfile

import pytest

from src.admin_authority import PrimaryAdminAuthorizationError
from src.admin_grant import AdminGrantConflict, AdminGrantError, PlanMismatch, UnknownPlan
from src.security import AdminSessionStore

PRIMARY = "owner:mgboost-primary:v1"
PRIMARY_LOGIN = "authenticated-primary-login"


@pytest.fixture
def db(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="admin-grant-test-")
    monkeypatch.setenv("DATA_DIR", tmp)
    monkeypatch.setenv("PRIMARY_MGBOOST_ADMIN_ACTOR_ID", PRIMARY)
    monkeypatch.setenv("PRIMARY_MGBOOST_ADMIN_LOGIN", PRIMARY_LOGIN)
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


def _capability(db):
    _raw, session = AdminSessionStore().create(PRIMARY_LOGIN, "test-server-jwt")
    return db.primary_admin_authority.authorize_session(session)


def _bad_capability():
    from src.admin_authority import PrimaryAdminCapability
    return PrimaryAdminCapability("someone-else", "wrong-seal")


# --- authorization -----------------------------------------------------------


def test_requires_primary_admin_capability(db):
    with pytest.raises(PrimaryAdminAuthorizationError):
        db.admin_grants.grant_new_account(
            _bad_capability(), telegram_id=111000111, plan_code="WL",
            duration_days=30, reason="unit test canary grant",
            idempotency_key="admin-grant-test-unauth-000000000001",
        )


# --- new-account grant: exact commercial product, zero financial rows -------


def test_grant_new_account_creates_exact_wl_product_with_no_financial_rows(db):
    cap = _capability(db)
    result = db.admin_grants.grant_new_account(
        cap, telegram_id=222000222, plan_code="WL", duration_days=30,
        reason="controlled WL canary -- no Telegram Stars payment",
        idempotency_key="admin-grant-test-wl30-000000000001", now=1000,
    )
    assert result["already_applied"] is False
    assert result["new_expiry"] == 1000 + 30 * 86400
    assert len(result["wl_periods"]) == 1
    assert result["wl_periods"][0]["sequence_no"] == 1

    account_id = result["account_id"]
    account = db.accounts.get_account(account_id)
    assert account["account_source"] == "DIRECT"
    assert account["status"] == "ACTIVE"

    identity = db._conn.execute(
        "SELECT telegram_id,role,provenance,revoked_at FROM mgboost_telegram_identities "
        "WHERE account_id=?", (account_id,),
    ).fetchone()
    assert tuple(identity) == (222000222, "OWNER", "ADMIN_REBIND", None)

    mutation = db._conn.execute(
        "SELECT payment_channel,mutation_source,actor_type,actor_ref,reason,"
        "external_reference FROM mgboost_entitlement_mutations WHERE id=?",
        (result["mutation_id"],),
    ).fetchone()
    assert mutation["payment_channel"] == "ADMIN_GRANT"
    assert mutation["mutation_source"] == "ADMIN"
    assert mutation["actor_type"] == "PRIMARY_ADMIN"
    assert mutation["actor_ref"] == PRIMARY
    assert mutation["external_reference"] is None

    subscription = db._conn.execute(
        "SELECT status,current_expiry FROM mgboost_subscriptions WHERE account_id=?",
        (account_id,),
    ).fetchone()
    assert tuple(subscription) == ("ACTIVE", 1000 + 30 * 86400)

    entitlement = db.entitlements.calculate(account_id=account_id, now=1000)
    assert entitlement["plan"]["code"] == "WL"
    assert entitlement["subscription"]["effective_status"] == "ACTIVE"

    # Zero financial rows anywhere: this is the core no-revenue invariant.
    assert db._conn.execute("SELECT COUNT(*) FROM mgboost_payment_records").fetchone()[0] == 0
    assert db._conn.execute("SELECT COUNT(*) FROM stars_invoices").fetchone()[0] == 0
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_stars_payment_evidence"
    ).fetchone()[0] == 0
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_manual_payment_records"
    ).fetchone()[0] == 0


def test_grant_new_account_exact_wl_terms_match_catalog(db):
    cap = _capability(db)
    result = db.admin_grants.grant_new_account(
        cap, telegram_id=222000333, plan_code="WL", duration_days=30,
        reason="controlled WL canary -- exact WL 30d/D3/100GB terms",
        idempotency_key="admin-grant-test-wl30-terms-0000000001", now=1000,
    )
    term = db._conn.execute(
        "SELECT device_limit_mode_snapshot,device_limit_snapshot,wl_mode_snapshot,"
        "wl_quota_bytes_snapshot,wl_period_days_snapshot FROM mgboost_subscription_terms "
        "WHERE subscription_id=?", (result["subscription_id"],),
    ).fetchone()
    assert tuple(term) == ("LIMITED", 3, "LIMITED", 100_000_000_000, 30)


# --- idempotency / replay -----------------------------------------------------


def test_grant_new_account_is_idempotent_on_replay(db):
    cap = _capability(db)
    key = "admin-grant-test-replay-000000000001"
    first = db.admin_grants.grant_new_account(
        cap, telegram_id=222000444, plan_code="WL", duration_days=30,
        reason="controlled WL canary replay test", idempotency_key=key, now=1000,
    )
    second = db.admin_grants.grant_new_account(
        cap, telegram_id=222000444, plan_code="WL", duration_days=30,
        reason="controlled WL canary replay test", idempotency_key=key, now=5000,
    )
    assert second["already_applied"] is True
    assert second["account_id"] == first["account_id"]
    assert second["new_expiry"] == first["new_expiry"]
    assert db._conn.execute("SELECT COUNT(*) FROM mgboost_accounts").fetchone()[0] == 1


def test_grant_new_account_second_distinct_grant_reuses_same_telegram_account(db):
    """Two DIFFERENT idempotency keys against the SAME telegram_id must not
    create a second account -- the second call is a same-plan renewal of the
    one canary account, going through the same replay-safe engine."""
    cap = _capability(db)
    first = db.admin_grants.grant_new_account(
        cap, telegram_id=222000555, plan_code="WL", duration_days=30,
        reason="controlled WL canary first grant",
        idempotency_key="admin-grant-test-reuse-a-0000000001", now=1000,
    )
    second = db.admin_grants.grant_new_account(
        cap, telegram_id=222000555, plan_code="WL", duration_days=30,
        reason="controlled WL canary renewal grant",
        idempotency_key="admin-grant-test-reuse-b-0000000001", now=5000,
    )
    assert second["account_id"] == first["account_id"]
    assert second["already_applied"] is False
    assert second["new_expiry"] == first["new_expiry"] + 30 * 86400
    assert db._conn.execute("SELECT COUNT(*) FROM mgboost_accounts").fetchone()[0] == 1


# --- no implicit plan change on an existing account ---------------------------


def test_grant_existing_account_refuses_implicit_plan_change(db):
    cap = _capability(db)
    granted = db.admin_grants.grant_new_account(
        cap, telegram_id=222000666, plan_code="WL", duration_days=30,
        reason="controlled WL canary baseline",
        idempotency_key="admin-grant-test-planmismatch-0000001", now=1000,
    )
    with pytest.raises(PlanMismatch):
        db.admin_grants.grant_existing_account(
            cap, account_id=granted["account_id"], plan_code="EXTENDED",
            duration_days=30, reason="attempted implicit upgrade",
            idempotency_key="admin-grant-test-planmismatch-0000002", now=2000,
        )


def test_grant_existing_account_unknown_account_fails_closed(db):
    cap = _capability(db)
    with pytest.raises(AdminGrantError):
        db.admin_grants.grant_existing_account(
            cap, account_id=999999, plan_code="WL", duration_days=30,
            reason="nonexistent account", idempotency_key="admin-grant-test-noacct-01",
            now=1000,
        )


# --- input validation ----------------------------------------------------------


def test_reason_must_be_bounded_and_present(db):
    cap = _capability(db)
    with pytest.raises(AdminGrantError):
        db.admin_grants.grant_new_account(
            cap, telegram_id=222000777, plan_code="WL", duration_days=30,
            reason="short", idempotency_key="admin-grant-test-reason-0000000001",
        )


def test_unknown_plan_code_fails_closed(db):
    cap = _capability(db)
    with pytest.raises(UnknownPlan):
        db.admin_grants.grant_new_account(
            cap, telegram_id=222000888, plan_code="NOT_A_REAL_PLAN", duration_days=30,
            reason="controlled WL canary bad plan",
            idempotency_key="admin-grant-test-unknown-0000000001",
        )
