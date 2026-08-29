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


# --- first-device provisioning wiring (PH7-14) ---------------------------------
# A fresh account needs the same PH5-11-shaped provisioning wiring real
# self-service signup creates, or `opaque_resolver.resolve_account_device`
# fails closed (OUTCOME_INTERNAL_ERROR) for lack of a PRIMARY alias -- caught
# by an actual production first-device bootstrap attempt, not by any prior
# test in this file (none of them touched device provisioning at all).


def test_grant_new_account_creates_provisioning_wiring_for_first_device_bootstrap(db):
    cap = _capability(db)
    result = db.admin_grants.grant_new_account(
        cap, telegram_id=222000999, plan_code="WL", duration_days=30,
        reason="controlled WL canary -- bootstrap wiring check",
        idempotency_key="admin-grant-test-bootstrap-0000000001", now=1000,
    )
    account_id = result["account_id"]
    public_id = result["account_public_id"]

    alias = db._conn.execute(
        "SELECT account_id,legacy_username,alias_role,ownership_provenance,legacy_status "
        "FROM mgboost_legacy_account_aliases WHERE account_id=?", (account_id,),
    ).fetchone()
    assert alias is not None
    assert alias["legacy_username"] == f"tpl-{public_id}"
    assert alias["alias_role"] == "PRIMARY"
    assert alias["ownership_provenance"] == "OWNER_APPROVED"
    assert alias["legacy_status"] == "ACTIVE"

    review = db._conn.execute(
        "SELECT ownership_evidence FROM mgboost_direct_account_reviews WHERE account_id=?",
        (account_id,),
    ).fetchone()
    assert review["ownership_evidence"] == "PROVEN"

    jobs = db.admin_grants.pending_template_jobs()
    assert [j["account_id"] for j in jobs] == [account_id]
    assert jobs[0]["state"] == "PENDING"

    # Zero mgboost_signup_template_jobs rows -- that queue is PH5-11's
    # payment-anchored one and must never be touched by an ADMIN_GRANT.
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_signup_template_jobs WHERE account_id=?", (account_id,),
    ).fetchone()[0] == 0


def test_grant_new_account_replay_does_not_duplicate_provisioning_wiring(db):
    cap = _capability(db)
    first = db.admin_grants.grant_new_account(
        cap, telegram_id=222001000, plan_code="WL", duration_days=30,
        reason="controlled WL canary -- bootstrap replay check",
        idempotency_key="admin-grant-test-bootstrap-replay-a-01", now=1000,
    )
    db.admin_grants.grant_new_account(
        cap, telegram_id=222001000, plan_code="WL", duration_days=30,
        reason="controlled WL canary -- bootstrap replay renewal",
        idempotency_key="admin-grant-test-bootstrap-replay-b-01", now=5000,
    )
    account_id = first["account_id"]
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_legacy_account_aliases WHERE account_id=?", (account_id,),
    ).fetchone()[0] == 1
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_admin_grant_template_jobs WHERE account_id=?", (account_id,),
    ).fetchone()[0] == 1


def test_repair_missing_provisioning_wiring_backfills_pre_fix_account(db):
    """Simulates an account granted BEFORE this wiring existed: create the
    account/subscription via the engine directly (bypassing
    grant_new_account's own wiring call), confirm it starts wire-less, then
    repair it."""
    cap = _capability(db)
    account = db.accounts.create_account("DIRECT", now=1000)
    db.accounts.link_telegram_owner(
        account["id"], 222001222, provenance="ADMIN_REBIND", actor="test-actor", now=1000,
    )
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_legacy_account_aliases WHERE account_id=?",
        (account["id"],),
    ).fetchone()[0] == 0

    repaired = db.admin_grants.repair_missing_provisioning_wiring(
        cap, account_id=account["id"],
        reason="controlled WL canary -- pre-fix account repair", now=2000,
    )
    assert repaired is True

    alias = db._conn.execute(
        "SELECT legacy_username,alias_role,ownership_provenance FROM "
        "mgboost_legacy_account_aliases WHERE account_id=?", (account["id"],),
    ).fetchone()
    assert alias["legacy_username"] == f"tpl-{account['public_id']}"
    assert alias["alias_role"] == "PRIMARY"
    assert alias["ownership_provenance"] == "OWNER_APPROVED"
    assert len(db.admin_grants.pending_template_jobs()) == 1

    # Second call is a true no-op -- never re-wires/duplicates.
    repaired_again = db.admin_grants.repair_missing_provisioning_wiring(
        cap, account_id=account["id"], reason="repeat repair attempt", now=3000,
    )
    assert repaired_again is False
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_legacy_account_aliases WHERE account_id=?",
        (account["id"],),
    ).fetchone()[0] == 1


def test_repair_missing_provisioning_wiring_requires_primary_admin(db):
    account = db.accounts.create_account("DIRECT", now=1000)
    with pytest.raises(PrimaryAdminAuthorizationError):
        db.admin_grants.repair_missing_provisioning_wiring(
            _bad_capability(), account_id=account["id"], reason="unauthorized repair attempt",
        )


def test_repair_missing_provisioning_wiring_refuses_non_direct_account(db):
    cap = _capability(db)
    account = db.accounts.create_account("INTERNAL", now=1000)
    with pytest.raises(AdminGrantError):
        db.admin_grants.repair_missing_provisioning_wiring(
            cap, account_id=account["id"], reason="wrong account source",
        )


def test_record_template_result_transitions_job_out_of_pending(db):
    cap = _capability(db)
    result = db.admin_grants.grant_new_account(
        cap, telegram_id=222001111, plan_code="WL", duration_days=30,
        reason="controlled WL canary -- template job transition check",
        idempotency_key="admin-grant-test-bootstrap-job-0000001", now=1000,
    )
    account_id = result["account_id"]
    assert len(db.admin_grants.pending_template_jobs()) == 1

    db.admin_grants.record_template_result(account_id, state="READY", now=2000)

    assert db.admin_grants.pending_template_jobs() == []
    job = db._conn.execute(
        "SELECT state,attempts,ready_at FROM mgboost_admin_grant_template_jobs WHERE account_id=?",
        (account_id,),
    ).fetchone()
    assert job["state"] == "READY"
    assert job["attempts"] == 1
    assert job["ready_at"] == 2000


# --- create_account_only (admin UI: create account, grant later or MANUAL_RUB) --


def test_create_account_only_requires_primary_admin_capability(db):
    with pytest.raises(PrimaryAdminAuthorizationError):
        db.admin_grants.create_account_only(
            _bad_capability(), telegram_id=222002000,
            reason="admin UI create-only test", idempotency_key="admin-grant-test-create-000001",
        )


def test_create_account_only_creates_wiring_with_zero_subscription_rows(db):
    cap = _capability(db)
    result = db.admin_grants.create_account_only(
        cap, telegram_id=222002111, reason="admin UI create-only test",
        idempotency_key="admin-grant-test-create-000002", now=1000,
    )
    assert result["reused"] is False
    account_id = result["account_id"]

    account = db.accounts.get_account(account_id)
    assert account["account_source"] == "DIRECT"
    assert account["status"] == "ACTIVE"

    identity = db._conn.execute(
        "SELECT telegram_id,role,provenance FROM mgboost_telegram_identities WHERE account_id=?",
        (account_id,),
    ).fetchone()
    assert tuple(identity) == (222002111, "OWNER", "ADMIN_REBIND")

    alias = db._conn.execute(
        "SELECT legacy_username,alias_role,ownership_provenance FROM "
        "mgboost_legacy_account_aliases WHERE account_id=?", (account_id,),
    ).fetchone()
    assert alias["legacy_username"] == f"tpl-{result['account_public_id']}"
    assert alias["alias_role"] == "PRIMARY"

    # Zero engine activity: no subscription, no terms, no mutation, no
    # financial rows -- this call only creates the account, nothing else.
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_subscriptions WHERE account_id=?", (account_id,),
    ).fetchone()[0] == 0
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_entitlement_mutations WHERE account_id=?", (account_id,),
    ).fetchone()[0] == 0
    assert db._conn.execute("SELECT COUNT(*) FROM mgboost_payment_records").fetchone()[0] == 0
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_manual_payment_records"
    ).fetchone()[0] == 0


def test_create_account_only_reuses_existing_telegram_account(db):
    cap = _capability(db)
    first = db.admin_grants.create_account_only(
        cap, telegram_id=222002222, reason="admin UI create-only first call",
        idempotency_key="admin-grant-test-create-000003", now=1000,
    )
    second = db.admin_grants.create_account_only(
        cap, telegram_id=222002222, reason="admin UI create-only second call",
        idempotency_key="admin-grant-test-create-000004", now=2000,
    )
    assert second["account_id"] == first["account_id"]
    assert second["reused"] is True
    assert db._conn.execute("SELECT COUNT(*) FROM mgboost_accounts").fetchone()[0] == 1


def test_create_account_only_then_grant_existing_account_succeeds(db):
    """The MANUAL_RUB flow's real dependency: create the account first, then
    grant/sell it a plan through a SEPARATE call, and the provisioning
    wiring created by create_account_only must not be duplicated or
    conflict with grant_existing_account's own (no-op, already wired)
    pass through the same helper."""
    cap = _capability(db)
    created = db.admin_grants.create_account_only(
        cap, telegram_id=222002333, reason="admin UI create-only before grant",
        idempotency_key="admin-grant-test-create-000005", now=1000,
    )
    granted = db.admin_grants.grant_existing_account(
        cap, account_id=created["account_id"], plan_code="WL", duration_days=30,
        reason="admin UI grant after create-only", idempotency_key="admin-grant-test-create-000006",
        now=2000,
    )
    assert granted["account_id"] == created["account_id"]
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_legacy_account_aliases WHERE account_id=?",
        (created["account_id"],),
    ).fetchone()[0] == 1
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_subscriptions WHERE account_id=?",
        (created["account_id"],),
    ).fetchone()[0] == 1


def test_create_account_only_rejects_short_idempotency_key(db):
    cap = _capability(db)
    with pytest.raises(AdminGrantError):
        db.admin_grants.create_account_only(
            cap, telegram_id=222002444, reason="admin UI create-only bad key",
            idempotency_key="short",
        )
