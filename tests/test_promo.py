"""PH5-13 promo codes: EXTEND_SUBSCRIPTION and TRIAL_GRANT.

PURCHASE_DISCOUNT is not covered here -- separate slice, not yet built.
"""

import importlib
import os
import tempfile

import pytest

from src.admin_authority import PrimaryAdminAuthorizationError
from src.promo import (
    PromoConflict, PromoError, PromoIneligible, PromoNotFound,
    compute_promo_quota_bytes,
)
from src.security import AdminSessionStore

PRIMARY = "owner:mgboost-primary:v1"
PRIMARY_LOGIN = "authenticated-primary-login"


@pytest.fixture
def db(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="promo-test-")
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
    from src.promo import ensure_wl_trial_plan_version
    seed_plan_catalog(instance.plan_catalog, now=1)
    ensure_wl_trial_plan_version(instance.accounts, now=1)
    yield instance
    instance._conn.close()


def _capability(db):
    _raw, session = AdminSessionStore().create(PRIMARY_LOGIN, "test-server-jwt")
    return db.primary_admin_authority.authorize_session(session)


def _bad_capability():
    from src.admin_authority import PrimaryAdminCapability
    return PrimaryAdminCapability("someone-else", "wrong-seal")


def _define(db, cap, *, code, effect_kind, trial_class=None, effect_params, now=1000):
    return db.promo.create_definition(
        cap, code=code, effect_kind=effect_kind, trial_class=trial_class,
        effect_params=effect_params, reason="test promo definition",
        idempotency_key=f"promo-def-{code}-000000000001", now=now,
    )


# --- prorating: exact DL-060 table -------------------------------------------


@pytest.mark.parametrize("days,expected_gb", [
    (1, 10), (3, 10), (7, 30), (10, 40), (15, 50), (20, 70), (30, 100),
])
def test_prorating_wl_100gb_30d_exact_table(days, expected_gb):
    result = compute_promo_quota_bytes(100_000_000_000, days)
    assert result == expected_gb * 1_000_000_000


@pytest.mark.parametrize("days,expected_gb", [
    (1, 10), (3, 20), (7, 40), (10, 50), (15, 80), (20, 100), (30, 150),
])
def test_prorating_extended_family_150gb_30d_exact_table(days, expected_gb):
    result = compute_promo_quota_bytes(150_000_000_000, days)
    assert result == expected_gb * 1_000_000_000


def test_prorating_rounds_up_never_down(db):
    assert compute_promo_quota_bytes(100_000_000_000, 1) > 100_000_000_000 * 1 // 30


# --- definitions --------------------------------------------------------------


def test_create_definition_requires_primary_admin(db):
    with pytest.raises(PrimaryAdminAuthorizationError):
        db.promo.create_definition(
            _bad_capability(), code="X7DAYS", effect_kind="EXTEND_SUBSCRIPTION",
            trial_class=None, effect_params={"days": 7}, reason="unauthorized",
            idempotency_key="promo-def-unauth-000000000001",
        )


def test_create_definition_uppercases_code_and_rejects_duplicate(db):
    cap = _capability(db)
    result = _define(db, cap, code="summer7", effect_kind="EXTEND_SUBSCRIPTION",
                     effect_params={"days": 7})
    assert result["code"] == "SUMMER7"
    with pytest.raises(PromoConflict):
        _define(db, cap, code="SUMMER7", effect_kind="EXTEND_SUBSCRIPTION",
               effect_params={"days": 14})


def test_create_definition_trial_grant_requires_trial_class(db):
    cap = _capability(db)
    with pytest.raises(PromoError):
        _define(db, cap, code="TRIALNOCLASS", effect_kind="TRIAL_GRANT",
               trial_class=None, effect_params={"days": 1})


def test_disable_definition_does_not_affect_existing_redemptions(db):
    cap = _capability(db)
    _define(db, cap, code="DISABLEME", effect_kind="EXTEND_SUBSCRIPTION",
           effect_params={"days": 7})
    account = db.accounts.create_account("DIRECT", now=1)
    db.accounts.link_telegram_owner(account["id"], 700000001, provenance="ADMIN_REBIND",
                                    actor="test", now=1)
    db.subscription_renewal.apply_same_plan_purchase(
        account_id=account["id"], plan_code="WL", duration_days=30,
        payment_channel="TELEGRAM_STARS", mutation_source="DIRECT_PURCHASE",
        actor_type="TELEGRAM_USER", idempotency_key="promo-disable-base-purchase-01", now=1_000,
    )
    result = db.promo.redeem_extend_or_trial(
        cap, code="DISABLEME", telegram_id=700000001, reason="pre-disable redemption",
        idempotency_key="promo-disable-redeem-key-000001", now=2_000,
    )
    db.promo.disable_definition(cap, code="DISABLEME", reason="promo campaign ended", now=3_000)
    assert result["status"] == "REDEEMED"  # unaffected by later disable


def test_redeem_rejects_disabled_or_unknown_code(db):
    cap = _capability(db)
    _define(db, cap, code="ONETIME", effect_kind="EXTEND_SUBSCRIPTION", effect_params={"days": 7})
    db.promo.disable_definition(cap, code="ONETIME", reason="promo campaign ended", now=1_500)

    with pytest.raises(PromoNotFound):
        db.promo.redeem_extend_or_trial(
            cap, code="ONETIME", telegram_id=1, reason="disabled code attempt",
            idempotency_key="promo-disabled-attempt-key-0001", now=2_000,
        )
    with pytest.raises(PromoNotFound):
        db.promo.redeem_extend_or_trial(
            cap, code="DOESNOTEXIST", telegram_id=1, reason="unknown code attempt",
            idempotency_key="promo-unknown-attempt-key-0001", now=2_000,
        )


# --- EXTEND_SUBSCRIPTION on LIMITED (WL) --------------------------------------


def _wl_account(db, *, telegram_id, now=1_000):
    account = db.accounts.create_account("DIRECT", now=now)
    db.accounts.link_telegram_owner(account["id"], telegram_id, provenance="ADMIN_REBIND",
                                    actor="test", now=now)
    purchase = db.subscription_renewal.apply_same_plan_purchase(
        account_id=account["id"], plan_code="WL", duration_days=30,
        payment_channel="TELEGRAM_STARS", mutation_source="DIRECT_PURCHASE",
        actor_type="TELEGRAM_USER", idempotency_key=f"promo-wl-base-{telegram_id}",
        now=now,
    )
    return account, purchase


def test_extend_subscription_on_wl_creates_prorated_period_after_current_chronology(db):
    cap = _capability(db)
    account, base = _wl_account(db, telegram_id=800000001, now=1_000)
    _define(db, cap, code="WL7DAYS", effect_kind="EXTEND_SUBSCRIPTION", effect_params={"days": 7})

    result = db.promo.redeem_extend_or_trial(
        cap, code="WL7DAYS", telegram_id=800000001, reason="loyalty bonus",
        idempotency_key="promo-extend-wl-key-000000001", now=2_000,
    )
    assert result["status"] == "REDEEMED"
    period = result["effect_result"]["wl_periods"][0]
    assert period["starts_at"] == base["wl_periods"][0]["ends_at"]  # no gap

    quota = db._conn.execute(
        "SELECT base_quota_bytes FROM mgboost_wl_periods WHERE sequence_no=2 AND account_id=?",
        (account["id"],),
    ).fetchone()["base_quota_bytes"]
    assert quota == 30_000_000_000  # DL-060 table: 7 days of WL 100GB/30d -> 30GB

    # Original period completely untouched.
    original_quota = db._conn.execute(
        "SELECT base_quota_bytes FROM mgboost_wl_periods WHERE sequence_no=1 AND account_id=?",
        (account["id"],),
    ).fetchone()["base_quota_bytes"]
    assert original_quota == 100_000_000_000

    # Zero financial rows -- this is not a Stars/manual purchase.
    assert db._conn.execute("SELECT COUNT(*) c FROM mgboost_payment_records").fetchone()["c"] == 0
    assert db._conn.execute(
        "SELECT COUNT(*) c FROM mgboost_manual_payment_records"
    ).fetchone()["c"] == 0


def test_extend_subscription_on_standard_plan_uses_expiry_only_no_wl_period(db):
    cap = _capability(db)
    account = db.accounts.create_account("DIRECT", now=1)
    db.accounts.link_telegram_owner(account["id"], 800000002, provenance="ADMIN_REBIND",
                                    actor="test", now=1)
    db.subscription_renewal.apply_same_plan_purchase(
        account_id=account["id"], plan_code="BASIC", duration_days=30,
        payment_channel="TELEGRAM_STARS", mutation_source="DIRECT_PURCHASE",
        actor_type="TELEGRAM_USER", idempotency_key="promo-basic-base-purchase-01", now=1_000,
    )
    _define(db, cap, code="BASIC7DAYS", effect_kind="EXTEND_SUBSCRIPTION", effect_params={"days": 7})

    result = db.promo.redeem_extend_or_trial(
        cap, code="BASIC7DAYS", telegram_id=800000002, reason="loyalty bonus",
        idempotency_key="promo-extend-basic-key-00000001", now=2_000,
    )
    assert result["status"] == "REDEEMED"
    assert db._conn.execute(
        "SELECT COUNT(*) c FROM mgboost_wl_periods WHERE account_id=?", (account["id"],),
    ).fetchone()["c"] == 0


def test_extend_subscription_requires_existing_account(db):
    cap = _capability(db)
    _define(db, cap, code="NOACCOUNT", effect_kind="EXTEND_SUBSCRIPTION", effect_params={"days": 7})
    with pytest.raises(PromoNotFound):
        db.promo.redeem_extend_or_trial(
            cap, code="NOACCOUNT", telegram_id=999999999, reason="no such account",
            idempotency_key="promo-noaccount-key-000000001", now=2_000,
        )


# --- TRIAL_GRANT ---------------------------------------------------------------


def test_trial_grant_creates_account_with_legitimate_wl_trial_entitlement(db):
    cap = _capability(db)
    _define(db, cap, code="TRYWL", effect_kind="TRIAL_GRANT", trial_class="WL_TRIAL",
           effect_params={"days": 1})

    result = db.promo.redeem_extend_or_trial(
        cap, code="TRYWL", telegram_id=900000001, reason="new user trial",
        idempotency_key="promo-trial-key-0000000000001", now=1_000,
    )
    assert result["status"] == "REDEEMED"
    account_id = result["account_id"]

    entitlement = db.entitlements.calculate(account_id=account_id, now=1_000)
    assert entitlement["plan"]["code"] == "WL_TRIAL"
    assert entitlement["device"]["limit"] == 1
    assert entitlement["wl"]["base_quota_bytes"] == 10_000_000_000

    assert db._conn.execute("SELECT COUNT(*) c FROM mgboost_payment_records").fetchone()["c"] == 0
    assert db._conn.execute(
        "SELECT COUNT(*) c FROM mgboost_manual_payment_records"
    ).fetchone()["c"] == 0


def test_trial_grant_one_identity_one_redemption_per_trial_class_across_different_codes(db):
    cap = _capability(db)
    _define(db, cap, code="TRYWL1", effect_kind="TRIAL_GRANT", trial_class="WL_TRIAL",
           effect_params={"days": 1})
    _define(db, cap, code="TRYWL2", effect_kind="TRIAL_GRANT", trial_class="WL_TRIAL",
           effect_params={"days": 1})

    db.promo.redeem_extend_or_trial(
        cap, code="TRYWL1", telegram_id=900000002, reason="first trial code",
        idempotency_key="promo-trial-dup-key-00000001", now=1_000,
    )
    with pytest.raises(PromoIneligible):
        db.promo.redeem_extend_or_trial(
            cap, code="TRYWL2", telegram_id=900000002, reason="second, different code",
            idempotency_key="promo-trial-dup-key-00000002", now=2_000,
        )


def test_trial_grant_new_account_for_same_identity_does_not_bypass_uniqueness(db):
    """A fresh account for the SAME Telegram identity must not grant a
    second trial -- uniqueness is keyed on owner_telegram_id, not account_id."""
    cap = _capability(db)
    _define(db, cap, code="TRYWL3", effect_kind="TRIAL_GRANT", trial_class="WL_TRIAL",
           effect_params={"days": 1})
    first = db.promo.redeem_extend_or_trial(
        cap, code="TRYWL3", telegram_id=900000003, reason="first trial",
        idempotency_key="promo-trial-newacct-key-0000001", now=1_000,
    )
    # Simulate the account being closed/abandoned and telegram_id freed up
    # for a brand new account (never happens organically without a real
    # rebind/close flow, but proves the DB-level guard, not app-level flow).
    db._conn.execute(
        "UPDATE mgboost_telegram_identities SET revoked_at=? WHERE account_id=?",
        (1_100, first["account_id"]),
    )
    db._conn.commit()
    other_account = db.accounts.create_account("DIRECT", now=1_200)
    db.accounts.link_telegram_owner(other_account["id"], 900000003, provenance="ADMIN_REBIND",
                                    actor="test", now=1_200)
    with pytest.raises(PromoIneligible):
        db.promo.redeem_extend_or_trial(
            cap, code="TRYWL3", telegram_id=900000003, reason="second attempt, new account",
            idempotency_key="promo-trial-newacct-key-0000002", now=1_300,
        )


def test_trial_grant_blocked_when_account_has_active_subscription(db):
    cap = _capability(db)
    account, _base = _wl_account(db, telegram_id=900000004, now=1_000)
    _define(db, cap, code="TRYWL4", effect_kind="TRIAL_GRANT", trial_class="WL_TRIAL_OTHER",
           effect_params={"days": 1})
    with pytest.raises(PromoIneligible):
        db.promo.redeem_extend_or_trial(
            cap, code="TRYWL4", telegram_id=900000004, reason="already subscribed",
            idempotency_key="promo-trial-active-key-00000001", now=2_000,
        )


def test_after_trial_expires_a_real_purchase_succeeds_cleanly(db):
    cap = _capability(db)
    _define(db, cap, code="TRYWL5", effect_kind="TRIAL_GRANT", trial_class="WL_TRIAL_5",
           effect_params={"days": 1})
    trial = db.promo.redeem_extend_or_trial(
        cap, code="TRYWL5", telegram_id=900000005, reason="trial grant before real purchase test",
        idempotency_key="promo-trial-postbuy-key-0000001", now=1_000,
    )
    account_id = trial["account_id"]

    # Trial expires after 1 day; buy real WL 30d well after expiry.
    purchase = db.subscription_renewal.apply_same_plan_purchase(
        account_id=account_id, plan_code="WL", duration_days=30,
        payment_channel="TELEGRAM_STARS", mutation_source="DIRECT_PURCHASE",
        actor_type="TELEGRAM_USER", idempotency_key="promo-trial-postbuy-purchase-01",
        now=1_000 + 2 * 86400,
    )
    assert purchase["plan_code"] == "WL"
    entitlement = db.entitlements.calculate(account_id=account_id, now=1_000 + 2 * 86400)
    assert entitlement["plan"]["code"] == "WL"
    assert entitlement["device"]["limit"] == 3  # real WL plan's own device limit, not the trial's


# --- crash-consistency ---------------------------------------------------------


def test_redemption_survives_a_crash_between_effect_apply_and_mark_redeemed(db, monkeypatch):
    cap = _capability(db)
    _define(db, cap, code="CRASHTEST", effect_kind="EXTEND_SUBSCRIPTION", effect_params={"days": 7})
    account, base = _wl_account(db, telegram_id=910000001, now=1_000)

    real_append = db.subscription_renewal.append_promo_wl_period
    call_count = {"n": 0}

    def _crash_after_apply(*args, **kwargs):
        call_count["n"] += 1
        result = real_append(*args, **kwargs)
        if call_count["n"] == 1:
            raise RuntimeError("simulated crash after effect applied, before REDEEMED marker")
        return result

    monkeypatch.setattr(db.subscription_renewal, "append_promo_wl_period", _crash_after_apply)

    with pytest.raises(RuntimeError):
        db.promo.redeem_extend_or_trial(
            cap, code="CRASHTEST", telegram_id=910000001, reason="crash test",
            idempotency_key="promo-crash-key-00000000001", now=2_000,
        )

    # Redemption row is durably PENDING_APPLY, not lost.
    pending = db._conn.execute(
        "SELECT status FROM mgboost_promo_redemptions WHERE idempotency_key_hash IS NOT NULL "
        "AND account_id=?", (account["id"],),
    ).fetchone()
    assert pending["status"] == "PENDING_APPLY"

    # Retry with the SAME idempotency_key converges: exactly ONE promo period,
    # never two, regardless of the crash.
    monkeypatch.setattr(db.subscription_renewal, "append_promo_wl_period", real_append)
    result = db.promo.redeem_extend_or_trial(
        cap, code="CRASHTEST", telegram_id=910000001, reason="crash test",
        idempotency_key="promo-crash-key-00000000001", now=3_000,
    )
    assert result["status"] == "REDEEMED"
    promo_periods = db._conn.execute(
        "SELECT COUNT(*) c FROM mgboost_wl_periods WHERE account_id=? AND sequence_no=2",
        (account["id"],),
    ).fetchone()["c"]
    assert promo_periods == 1  # not duplicated by the retry


def test_idempotency_key_reused_with_different_request_conflicts(db):
    cap = _capability(db)
    _define(db, cap, code="CONFLICTTEST", effect_kind="EXTEND_SUBSCRIPTION",
           effect_params={"days": 7})
    account, _base = _wl_account(db, telegram_id=910000002, now=1_000)
    db.promo.redeem_extend_or_trial(
        cap, code="CONFLICTTEST", telegram_id=910000002, reason="first reason",
        idempotency_key="promo-reuse-key-000000000001", now=2_000,
    )
    with pytest.raises(PromoConflict):
        db.promo.redeem_extend_or_trial(
            cap, code="CONFLICTTEST", telegram_id=910000002, reason="a completely different reason",
            idempotency_key="promo-reuse-key-000000000001", now=3_000,
        )


# --- self-service user redemption (bot / LK ingress, no admin capability) -----


def test_user_redeem_extends_wl_subscription_without_any_admin_capability(db):
    _define(db, _capability(db), code="USERWL7", effect_kind="EXTEND_SUBSCRIPTION",
           effect_params={"days": 7})
    account, base = _wl_account(db, telegram_id=920000001, now=1_000)

    result = db.promo.redeem_for_telegram_user(
        code="userwl7", telegram_id=920000001,
        idempotency_key="promo-redeem-v1:920000001:11111", now=2_000,
    )
    assert result["status"] == "REDEEMED"
    period = result["effect_result"]["wl_periods"][0]
    assert period["starts_at"] == base["wl_periods"][0]["ends_at"]
    row = db._conn.execute(
        "SELECT actor_type,actor_ref FROM mgboost_promo_redemptions WHERE id=?",
        (result["redemption_id"],),
    ).fetchone()
    assert row["actor_type"] == "TELEGRAM_USER"
    assert row["actor_ref"] == "telegram:920000001"


def test_user_redeem_replays_same_idempotency_key_and_never_applies_twice(db):
    """The Telegram/LK transports may redeliver the SAME event; the
    deterministic (chat_id, message_id)-derived key must replay the original
    redemption, never grant a second period."""
    _define(db, _capability(db), code="USERREPLAY", effect_kind="EXTEND_SUBSCRIPTION",
           effect_params={"days": 7})
    account, _base = _wl_account(db, telegram_id=920000002, now=1_000)
    key = "promo-redeem-v1:920000002:22222"

    first = db.promo.redeem_for_telegram_user(
        code="USERREPLAY", telegram_id=920000002, idempotency_key=key, now=2_000,
    )
    second = db.promo.redeem_for_telegram_user(
        code="USERREPLAY", telegram_id=920000002, idempotency_key=key, now=3_000,
    )
    assert second["already_applied"] is True
    assert second["redemption_id"] == first["redemption_id"]
    assert db._conn.execute(
        "SELECT COUNT(*) c FROM mgboost_wl_periods WHERE account_id=?", (account["id"],),
    ).fetchone()["c"] == 2  # base + exactly one promo period


def test_user_redeem_trial_requires_existing_account_no_bootstrap(db):
    """Self-service TRIAL_GRANT must not auto-create accounts -- that path
    routes through the admin-capability-gated AdminGrantStore bootstrap."""
    _define(db, _capability(db), code="USERTRY", effect_kind="TRIAL_GRANT",
           trial_class="WL_TRIAL", effect_params={"days": 1})
    with pytest.raises(PromoNotFound):
        db.promo.redeem_for_telegram_user(
            code="USERTRY", telegram_id=930000001,
            idempotency_key="promo-redeem-v1:930000001:33333", now=1_000,
        )
    assert db._conn.execute("SELECT COUNT(*) c FROM mgboost_accounts").fetchone()["c"] == 0


def test_user_redeem_trial_for_existing_inactive_subscription_identity(db):
    _define(db, _capability(db), code="USERTRY2", effect_kind="TRIAL_GRANT",
           trial_class="WL_TRIAL", effect_params={"days": 1})
    account = db.accounts.create_account("DIRECT", now=1)
    db.accounts.link_telegram_owner(account["id"], 930000002, provenance="ADMIN_REBIND",
                                    actor="test", now=1)
    result = db.promo.redeem_for_telegram_user(
        code="USERTRY2", telegram_id=930000002,
        idempotency_key="promo-redeem-v1:930000002:44444", now=1_000,
    )
    assert result["status"] == "REDEEMED"
    entitlement = db.entitlements.calculate(account_id=result["account_id"], now=1_000)
    assert entitlement["plan"]["code"] == "WL_TRIAL"


def test_user_redeem_standard_plan_extend_is_ineligible_needs_support_flow(db):
    _define(db, _capability(db), code="USERBASIC7", effect_kind="EXTEND_SUBSCRIPTION",
           effect_params={"days": 7})
    account = db.accounts.create_account("DIRECT", now=1)
    db.accounts.link_telegram_owner(account["id"], 920000003, provenance="ADMIN_REBIND",
                                    actor="test", now=1)
    db.subscription_renewal.apply_same_plan_purchase(
        account_id=account["id"], plan_code="BASIC", duration_days=30,
        payment_channel="TELEGRAM_STARS", mutation_source="DIRECT_PURCHASE",
        actor_type="TELEGRAM_USER", idempotency_key="promo-user-basic-base-01", now=1_000,
    )
    with pytest.raises(PromoIneligible):
        db.promo.redeem_for_telegram_user(
            code="USERBASIC7", telegram_id=920000003,
            idempotency_key="promo-redeem-v1:920000003:55555", now=2_000,
        )


def test_user_redeem_rejects_invalid_telegram_id(db):
    with pytest.raises(PromoError):
        db.promo.redeem_for_telegram_user(
            code="ANYCODE", telegram_id=-1,
            idempotency_key="promo-redeem-v1:-1:666666666", now=1_000,
        )
