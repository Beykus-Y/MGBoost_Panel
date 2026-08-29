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


# --- PURCHASE_DISCOUNT: reservation lifecycle ---------------------------------


def _define_discount(db, code, *, percent=None, minor=None):
    cap = _capability(db)
    params = {"discount_percent": percent} if percent is not None else {"discount_minor": minor}
    return _define(db, cap, code=code, effect_kind="PURCHASE_DISCOUNT", effect_params=params)


def _make_reservation(db, code, telegram_id, *, key_suffix="1", now=1_000, ttl=3600):
    return db.promo.reserve_purchase_for_telegram_user(
        code=code, telegram_id=telegram_id, ttl_seconds=ttl,
        idempotency_key=f"promo-reserve-v1:{telegram_id}:{key_suffix}", now=now,
    )


def test_reserve_rejects_non_discount_and_unknown_codes(db):
    cap = _capability(db)
    _define(db, cap, code="NOTDISC", effect_kind="EXTEND_SUBSCRIPTION", effect_params={"days": 7})
    account, _ = _wl_account(db, telegram_id=950000001, now=1_000)
    with pytest.raises(PromoError):
        _make_reservation(db, "NOTDISC", 950000001)
    with pytest.raises(PromoNotFound):
        _make_reservation(db, "DOESNOTEXIST9", 950000001)


def test_reserve_requires_existing_account_and_replays_same_key(db):
    _define_discount(db, "DISC10P", percent=10)
    with pytest.raises(PromoNotFound):
        _make_reservation(db, "DISC10P", 960000001)
    account, _ = _wl_account(db, telegram_id=960000001, now=1_000)

    first = _make_reservation(db, "DISC10P", 960000001)
    assert first["status"] == "RESERVED"
    assert first["reserved_until"] == 1_000 + 3_600
    replay = _make_reservation(db, "DISC10P", 960000001)
    assert replay["redemption_id"] == first["redemption_id"]
    assert replay["already"] is True
    assert db._conn.execute(
        "SELECT COUNT(*) c FROM mgboost_promo_redemptions").fetchone()["c"] == 1


def test_per_user_limit_blocks_second_reservation_for_same_code(db):
    _define_discount(db, "DISCONCE", minor=5)
    account, _ = _wl_account(db, telegram_id=960000002, now=1_000)
    _make_reservation(db, "DISCONCE", 960000002, key_suffix="a")
    with pytest.raises(PromoConflict):
        _make_reservation(db, "DISCONCE", 960000002, key_suffix="b")


def test_stars_invoice_carries_discounted_price_and_snapshot(db):
    _define_discount(db, "STARDISC", percent=50)
    account, _ = _wl_account(db, telegram_id=960000003, now=1_000)
    reservation = _make_reservation(db, "STARDISC", 960000003)

    invoice = db.stars_purchases.create_invoice(
        telegram_id=960000003, plan_code="WL", duration_days=30, ttl_seconds=3600,
        now=1_100, promo_redemption_id=reservation["redemption_id"],
    )
    assert invoice["original_stars_price"] == invoice["price_amount_snapshot"]
    assert invoice["discount_minor"] == invoice["price_amount_snapshot"] // 2
    assert invoice["stars_price"] == invoice["price_amount_snapshot"] - invoice["discount_minor"]
    row = db._conn.execute(
        "SELECT status,bound_kind,bound_invoice_id FROM mgboost_promo_redemptions WHERE id=?",
        (reservation["redemption_id"],),
    ).fetchone()
    assert row["status"] == "RESERVED"  # binding does not commit
    assert row["bound_kind"] == "STARS" and row["bound_invoice_id"] == invoice["id"]

    # Snapshot is immutable once written.
    import sqlite3 as _sq
    with pytest.raises(_sq.IntegrityError):
        db._conn.execute(
            "UPDATE stars_invoices SET discount_minor=1 WHERE id=?", (invoice["id"],))
        db._conn.commit()
    db._conn.rollback()


def test_checkout_commits_reservation_and_cleanup_cannot_cancel_it(db):
    _define_discount(db, "STARDISC2", percent=50)
    account, _ = _wl_account(db, telegram_id=960000004, now=1_000)
    reservation = _make_reservation(db, "STARDISC2", 960000004)
    # reservation TTL ends at 1_100+3_600=4_700 -- the same instant the
    # invoice expires. Sweep at 4_000: nothing expired yet.
    invoice = db.stars_purchases.create_invoice(
        telegram_id=960000004, plan_code="WL", duration_days=30, ttl_seconds=3600,
        now=1_100, promo_redemption_id=reservation["redemption_id"],
    )
    db.promo.release_expired_reservations(now=4_000)
    row = db._conn.execute(
        "SELECT status FROM mgboost_promo_redemptions WHERE id=?",
        (reservation["redemption_id"],)).fetchone()
    assert row["status"] == "RESERVED"  # live bound invoice: TTL never wins

    db.stars_purchases.validate_invoice_for_checkout(invoice["id"], 960000004, now=4_050)
    status = db._conn.execute(
        "SELECT status FROM mgboost_promo_redemptions WHERE id=?",
        (reservation["redemption_id"],)).fetchone()["status"]
    assert status == "COMMITTED"
    # After COMMITTED even an everything-expired sweep cannot cancel it.
    db.promo.release_expired_reservations(now=9_000_000)
    still = db._conn.execute(
        "SELECT status FROM mgboost_promo_redemptions WHERE id=?",
        (reservation["redemption_id"],)).fetchone()["status"]
    assert still == "COMMITTED"


def test_cleanup_cancels_bound_reservation_once_invoice_expires(db):
    _define_discount(db, "STARDISC5", percent=10)
    account, _ = _wl_account(db, telegram_id=960000007, now=1_000)
    reservation = _make_reservation(db, "STARDISC5", 960000007, ttl=600)
    invoice = db.stars_purchases.create_invoice(
        telegram_id=960000007, plan_code="WL", duration_days=30, ttl_seconds=600,
        now=1_100, promo_redemption_id=reservation["redemption_id"],
    )
    # Abandoned checkout: the invoice expires -> the reservation must be
    # released, never held forever (single-use code back to the pool).
    db.promo.release_expired_reservations(now=1_100 + 601)
    row = db._conn.execute(
        "SELECT status FROM mgboost_promo_redemptions WHERE id=?",
        (reservation["redemption_id"],)).fetchone()
    assert row["status"] == "CANCELLED"


def test_capture_flips_reservation_to_redeemed_exactly_once(db):
    _define_discount(db, "STARDISC3", percent=25)
    account, _ = _wl_account(db, telegram_id=960000005, now=1_000)
    reservation = _make_reservation(db, "STARDISC3", 960000005)
    invoice = db.stars_purchases.create_invoice(
        telegram_id=960000005, plan_code="WL", duration_days=30, ttl_seconds=3600,
        now=1_100, promo_redemption_id=reservation["redemption_id"],
    )
    db.stars_purchases.validate_invoice_for_checkout(invoice["id"], 960000005, now=1_150)
    result = db.stars_purchases.capture_paid(
        invoice["id"], charge_id="charge-disc-1", provider_charge_id=None,
        payer_telegram_id=960000005, currency="XTR", amount=invoice["stars_price"], now=1_200,
    )
    assert result == "paid"
    row = db._conn.execute(
        "SELECT status FROM mgboost_promo_redemptions WHERE id=?",
        (reservation["redemption_id"],)).fetchone()
    assert row["status"] == "REDEEMED"

    duplicate = db.stars_purchases.capture_paid(
        invoice["id"], charge_id="charge-disc-1", provider_charge_id=None,
        payer_telegram_id=960000005, currency="XTR", amount=invoice["stars_price"], now=1_300,
    )
    assert duplicate == "duplicate"


def test_cleanup_cancels_unbound_reservation_and_returns_code_to_pool(db):
    _define_discount(db, "STARDISC4", minor=3)
    account, _ = _wl_account(db, telegram_id=960000006, now=1_000)
    reservation = _make_reservation(db, "STARDISC4", 960000006, ttl=600)
    db.promo.release_expired_reservations(now=1_000 + 601)
    row = db._conn.execute(
        "SELECT status FROM mgboost_promo_redemptions WHERE id=?",
        (reservation["redemption_id"],)).fetchone()
    assert row["status"] == "CANCELLED"
    # cancelled single-use attempt does not block a fresh reservation
    again = _make_reservation(db, "STARDISC4", 960000006, key_suffix="2", now=1_700)
    assert again["status"] == "RESERVED"


def test_discount_floor_is_one_star_never_zero(db):
    from src.promo import _discount_from_effect_params
    assert _discount_from_effect_params({"discount_percent": 100}, 100) == 1
    assert _discount_from_effect_params({"discount_minor": 999999}, 5) == 1


def test_checkout_rejected_when_reservation_lost_the_race(db):
    """If the reservation got cancelled between invoice creation and checkout
    (the exact race the COMMITTED gate exists for), checkout must be refused
    -- never charge money against a pool-returned promo."""
    import pytest as _pytest
    from src.stars_purchase import StarsPurchaseError
    _define_discount(db, "STARDISC6", percent=10)
    account, _ = _wl_account(db, telegram_id=960000008, now=1_000)
    reservation = _make_reservation(db, "STARDISC6", 960000008, ttl=600)
    invoice = db.stars_purchases.create_invoice(
        telegram_id=960000008, plan_code="WL", duration_days=30, ttl_seconds=600,
        now=1_100, promo_redemption_id=reservation["redemption_id"],
    )
    # Simulate the lost race: cleanup cancelled the bound reservation right
    # before the delayed checkout arrives (direct SQL for the race window).
    db._conn.execute(
        "UPDATE mgboost_promo_redemptions SET status='CANCELLED' WHERE id=?",
        (reservation["redemption_id"],))
    db._conn.commit()
    with _pytest.raises(StarsPurchaseError):
        db.stars_purchases.validate_invoice_for_checkout(invoice["id"], 960000008, now=1_200)
    invoice_row = db.get_invoice(invoice["id"])
    assert invoice_row["status"] == "created"  # unchanged, no money moved
