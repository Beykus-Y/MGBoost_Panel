import os
import tempfile

import pytest


@pytest.fixture
def db():
    tmp = tempfile.mkdtemp()
    os.environ["DATA_DIR"] = tmp
    from importlib import reload
    import src.config as cfg
    reload(cfg)
    cfg.DATA_DIR = tmp
    import src.database as db_mod
    reload(db_mod)
    db_mod.DB_PATH = os.path.join(tmp, "db.sqlite3")
    instance = db_mod.Database()
    yield instance
    instance._conn.close()


@pytest.fixture(autouse=True)
def seeded_catalog(db):
    from src.plan_catalog import seed_plan_catalog
    seed_plan_catalog(db.plan_catalog, now=100)
    return db


def _purchase(db, account_id, *, plan_code="BASIC", duration_days=30, key, now):
    return db.subscription_renewal.apply_same_plan_purchase(
        account_id=account_id, plan_code=plan_code, duration_days=duration_days,
        payment_channel="TELEGRAM_STARS", mutation_source="DIRECT_PURCHASE",
        actor_type="TELEGRAM_USER", idempotency_key=key, now=now,
    )


def test_compute_new_expiry_active_vs_expired_boundary():
    from src.subscription_renewal import compute_new_expiry

    # Active: current_expiry in the future -> extends from current_expiry.
    anchor, expiry = compute_new_expiry(1_000_000, 30, now=500_000)
    assert anchor == 1_000_000
    assert expiry == 1_000_000 + 30 * 86400

    # Expired: current_expiry in the past -> extends from now.
    anchor, expiry = compute_new_expiry(100, 30, now=500_000)
    assert anchor == 500_000
    assert expiry == 500_000 + 30 * 86400

    # Exact boundary: current_expiry == now is not "still active" (project
    # convention: `now < current_end_at` is active, `now == end` already ended).
    anchor, expiry = compute_new_expiry(500_000, 30, now=500_000)
    assert anchor == 500_000

    # No prior subscription at all -> extends from now, same formula.
    anchor, expiry = compute_new_expiry(None, 30, now=500_000)
    assert anchor == 500_000
    assert expiry == 500_000 + 30 * 86400


def test_schedule_wl_period_windows_60_days_is_two_contiguous_30_day_periods():
    from src.subscription_renewal import schedule_wl_period_windows

    windows = schedule_wl_period_windows(anchor=1_000, duration_days=60, wl_period_days=30)
    assert windows == [(1_000, 1_000 + 30 * 86400), (1_000 + 30 * 86400, 1_000 + 60 * 86400)]
    assert windows[0][1] == windows[1][0]  # contiguous, no gap/overlap


def test_schedule_wl_period_windows_rejects_non_exact_multiple():
    from src.subscription_renewal import RenewalError, schedule_wl_period_windows

    with pytest.raises(RenewalError):
        schedule_wl_period_windows(anchor=0, duration_days=45, wl_period_days=30)


def test_first_purchase_of_wl_plan_creates_subscription_and_periods(db):
    account = db.accounts.create_account("DIRECT", now=1)
    result = _purchase(db, account["id"], plan_code="WL", duration_days=30, key="purchase-key-0001", now=1_000)

    assert result["already_applied"] is False
    assert result["anchor"] == 1_000
    assert result["new_expiry"] == 1_000 + 30 * 86400
    assert len(result["wl_periods"]) == 1
    # WL period start is UTC-hour-floored (DL-020); subscription anchor/expiry above stay exact-second.
    assert result["wl_periods"][0]["starts_at"] == 0
    assert result["wl_periods"][0]["ends_at"] == 30 * 86400

    sub = db._conn.execute(
        "SELECT * FROM mgboost_subscriptions WHERE account_id=?", (account["id"],)
    ).fetchone()
    assert sub["status"] == "ACTIVE"
    assert sub["current_expiry"] == 1_000 + 30 * 86400


def test_60_day_purchase_creates_exactly_two_sequential_periods_never_merged(db):
    account = db.accounts.create_account("DIRECT", now=1)
    result = _purchase(db, account["id"], plan_code="WL", duration_days=60, key="purchase-key-0002", now=1_000)

    assert len(result["wl_periods"]) == 2
    assert result["wl_periods"][0]["sequence_no"] == 1
    assert result["wl_periods"][1]["sequence_no"] == 2
    assert result["wl_periods"][0]["ends_at"] == result["wl_periods"][1]["starts_at"]
    periods = db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_wl_periods WHERE subscription_id=?",
        (result["subscription_id"],),
    ).fetchone()[0]
    assert periods == 2


def test_non_wl_plan_creates_zero_periods_because_non_wl_is_unlimited(db):
    account = db.accounts.create_account("DIRECT", now=1)
    result = _purchase(db, account["id"], plan_code="BASIC", duration_days=60, key="purchase-key-0003", now=1_000)

    assert result["wl_periods"] == []
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_wl_periods WHERE subscription_id=?",
        (result["subscription_id"],),
    ).fetchone()[0] == 0


def test_repeated_equal_duration_purchases_stack_and_periods_keep_incrementing(db):
    account = db.accounts.create_account("DIRECT", now=1)
    first = _purchase(db, account["id"], plan_code="WL", duration_days=30, key="stack-key-0001-xx", now=1_000)
    second = _purchase(db, account["id"], plan_code="WL", duration_days=30, key="stack-key-0002-xx", now=1_100)

    assert second["anchor"] == first["new_expiry"]  # still-active subscription extends forward
    assert second["new_expiry"] == first["new_expiry"] + 30 * 86400
    assert second["wl_periods"][0]["sequence_no"] == 2  # continues, never restarts at 1
    # Both anchors floor to the same UTC hour boundary + one duration's worth
    # of whole days (a multiple of 3600s), so the second purchase's floored
    # WL-period start lines up exactly with the first purchase's own floored
    # period end -- still gapless/non-overlapping even though the
    # subscription's own exact-second expiry (`first["new_expiry"]`) isn't
    # itself hour-aligned.
    from src.subscription_renewal import align_to_utc_hour
    assert second["wl_periods"][0]["starts_at"] == align_to_utc_hour(first["new_expiry"])
    assert second["wl_periods"][0]["starts_at"] == first["wl_periods"][0]["ends_at"]

    sub = db._conn.execute(
        "SELECT current_expiry, row_version FROM mgboost_subscriptions WHERE id=?",
        (first["subscription_id"],),
    ).fetchone()
    assert sub["current_expiry"] == second["new_expiry"]
    assert sub["row_version"] == 2


def test_idempotency_key_applies_exactly_once(db):
    account = db.accounts.create_account("DIRECT", now=1)
    first = _purchase(db, account["id"], plan_code="WL", duration_days=30, key="idem-key-0001-xxxx", now=1_000)
    replay = _purchase(db, account["id"], plan_code="WL", duration_days=30, key="idem-key-0001-xxxx", now=9_999)

    assert replay["already_applied"] is True
    assert replay["new_expiry"] == first["new_expiry"]
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_entitlement_mutations WHERE account_id=?",
        (account["id"],),
    ).fetchone()[0] == 1
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_wl_periods WHERE subscription_id=?",
        (first["subscription_id"],),
    ).fetchone()[0] == 1


def test_different_plan_purchase_is_refused_not_stacked(db):
    from src.subscription_renewal import PlanMismatch

    account = db.accounts.create_account("DIRECT", now=1)
    _purchase(db, account["id"], plan_code="BASIC", duration_days=30, key="mismatch-key-0001", now=1_000)
    with pytest.raises(PlanMismatch):
        _purchase(db, account["id"], plan_code="WL", duration_days=30, key="mismatch-key-0002", now=1_100)


def test_unlimited_subscription_is_never_overwritten_by_a_commercial_purchase(db):
    from src.subscription_renewal import UnlimitedSubscriptionConflict

    account = db.accounts.create_account("INTERNAL", now=1)
    internal_plan = db.accounts.create_plan_version({
        "plan_code": "INTERNAL_TEST", "version": 1, "display_name": "Internal",
        "plan_kind": "INTERNAL", "billing_required": False,
        "device_limit_mode": "UNLIMITED", "device_limit": None,
        "wl_mode": "UNLIMITED", "wl_quota_bytes": None, "wl_period_days": None,
        "terms": {},
    }, now=100)
    db._conn.execute(
        "INSERT INTO mgboost_subscriptions "
        "(account_id,current_plan_version_id,status,started_at,current_expiry,"
        "created_at,updated_at) VALUES (?,?,'UNLIMITED',?,NULL,?,?)",
        (account["id"], internal_plan["id"], 100, 100, 100),
    )
    db._conn.commit()

    with pytest.raises(UnlimitedSubscriptionConflict):
        _purchase(db, account["id"], plan_code="BASIC", duration_days=30, key="unlimited-key-0001", now=1_000)


# --- PH5-13 promo: expired free-trial plan must not block a real purchase ---


def _install_trial_subscription(db, account_id, *, current_expiry, now=100):
    trial_plan = db.accounts.create_plan_version({
        "plan_code": "WL_TRIAL_TEST", "version": 1, "display_name": "WL Trial",
        "plan_kind": "COMMERCIAL", "billing_required": False,
        "device_limit_mode": "LIMITED", "device_limit": 1,
        "wl_mode": "LIMITED", "wl_quota_bytes": 10_000_000_000, "wl_period_days": 1,
        "terms": {},
    }, now=now)
    db._conn.execute(
        "INSERT INTO mgboost_subscriptions "
        "(account_id,current_plan_version_id,status,started_at,current_expiry,"
        "created_at,updated_at) VALUES (?,?,'ACTIVE',?,?,?,?)",
        (account_id, trial_plan["id"], now, current_expiry, now, now),
    )
    db._conn.commit()
    return trial_plan


def test_expired_free_trial_does_not_block_a_real_purchase(db):
    account = db.accounts.create_account("DIRECT", now=1)
    _install_trial_subscription(db, account["id"], current_expiry=500, now=100)

    result = _purchase(db, account["id"], plan_code="WL", duration_days=30,
                       key="trial-expired-purchase-0001", now=1_000)
    assert result["plan_code"] == "WL"
    row = db._conn.execute(
        "SELECT current_plan_version_id FROM mgboost_subscriptions WHERE account_id=?",
        (account["id"],),
    ).fetchone()
    real_wl = db.plan_catalog.get_plan_version("WL")
    assert row["current_plan_version_id"] == real_wl["id"]


def test_still_active_free_trial_still_blocks_a_different_purchase(db):
    from src.subscription_renewal import PlanMismatch

    account = db.accounts.create_account("DIRECT", now=1)
    _install_trial_subscription(db, account["id"], current_expiry=1_000_000, now=100)

    with pytest.raises(PlanMismatch):
        _purchase(db, account["id"], plan_code="WL", duration_days=30,
                 key="trial-active-purchase-0001", now=1_000)


def test_expired_billed_plan_still_blocks_a_different_purchase(db):
    """Regression pin: the expired-free-trial exception must never widen to
    billing_required=1 plans -- an expired REAL (paid) plan still requires
    the same-plan-only guard (renewal, not silent plan-switch)."""
    from src.subscription_renewal import PlanMismatch

    account = db.accounts.create_account("DIRECT", now=1)
    _purchase(db, account["id"], plan_code="BASIC", duration_days=30,
             key="billed-expired-key-0001", now=100)
    with pytest.raises(PlanMismatch):
        _purchase(db, account["id"], plan_code="WL", duration_days=30,
                 key="billed-expired-key-0002", now=100 + 31 * 86400)


def test_unknown_plan_and_unknown_duration_are_rejected(db):
    from src.subscription_renewal import RenewalError

    account = db.accounts.create_account("DIRECT", now=1)
    with pytest.raises(RenewalError):
        _purchase(db, account["id"], plan_code="NOT_A_REAL_PLAN", duration_days=30, key="bad-key-0001", now=1_000)
    with pytest.raises(RenewalError):
        _purchase(db, account["id"], plan_code="BASIC", duration_days=45, key="bad-key-0002", now=1_000)


def test_timestamps_are_pure_utc_epoch_seconds_no_calendar_semantics(db):
    account = db.accounts.create_account("DIRECT", now=1)
    result = _purchase(db, account["id"], plan_code="BASIC", duration_days=30, key="tz-key-0001-xxxxxx", now=0)
    assert result["new_expiry"] == 30 * 86400  # exact seconds, no month-length ambiguity


def test_align_to_utc_hour_floors_partial_hour():
    from src.subscription_renewal import align_to_utc_hour

    assert align_to_utc_hour(0) == 0
    assert align_to_utc_hour(3599) == 0
    assert align_to_utc_hour(3600) == 3600
    assert align_to_utc_hour(3601) == 3600
    assert align_to_utc_hour(7199) == 3600


def test_wl_period_start_is_utc_hour_aligned_for_a_partial_hour_purchase(db):
    account = db.accounts.create_account("DIRECT", now=1)
    # now=5_000 is mid-hour (1h23m20s into the epoch) -- PH6-02 "partial-hour" case.
    result = _purchase(db, account["id"], plan_code="WL", duration_days=30, key="partial-hour-key-01", now=5_000)

    assert result["anchor"] == 5_000  # subscription anchor itself stays exact
    assert result["wl_periods"][0]["starts_at"] == 3_600  # WL period floors to the hour
    assert result["wl_periods"][0]["starts_at"] % 3600 == 0
    assert result["wl_periods"][0]["ends_at"] % 3600 == 0


# --- PH5-13 promo: append_promo_wl_period ------------------------------------


def _append_promo(db, account_id, *, days, quota_bytes, key, now, plan_version_id=None,
                  operation="PROMO_EXTEND_WL_PERIOD"):
    return db.subscription_renewal.append_promo_wl_period(
        account_id=account_id, days=days, quota_bytes=quota_bytes,
        operation=operation, plan_version_id=plan_version_id,
        mutation_source="ADMIN", payment_channel="ADMIN_GRANT",
        actor_type="PRIMARY_ADMIN", actor_ref="owner:test",
        reason="promo test", idempotency_key=key, now=now,
    )


def test_append_promo_wl_period_extends_existing_wl_subscription_without_touching_plan(db):
    account = db.accounts.create_account("DIRECT", now=1)
    base = _purchase(db, account["id"], plan_code="WL", duration_days=30,
                     key="promo-base-purchase-0001", now=1_000)
    base_period = base["wl_periods"][0]

    result = _append_promo(db, account["id"], days=7, quota_bytes=30_000_000_000,
                           key="promo-extend-key-0001", now=2_000)

    assert result["wl_periods"][0]["starts_at"] == base_period["ends_at"]  # no gap
    assert result["wl_periods"][0]["ends_at"] == base_period["ends_at"] + 7 * 86400

    sub = db._conn.execute(
        "SELECT current_plan_version_id FROM mgboost_subscriptions WHERE account_id=?",
        (account["id"],),
    ).fetchone()
    wl_plan = db.plan_catalog.get_plan_version("WL")
    assert sub["current_plan_version_id"] == wl_plan["id"]  # untouched

    # Original period's own row is completely untouched.
    original = db._conn.execute(
        "SELECT starts_at,ends_at,base_quota_bytes FROM mgboost_wl_periods "
        "WHERE sequence_no=1 AND account_id=?", (account["id"],),
    ).fetchone()
    assert (original["starts_at"], original["ends_at"], original["base_quota_bytes"]) == (
        base_period["starts_at"], base_period["ends_at"], 100_000_000_000,
    )


def test_append_promo_wl_period_anchors_on_max_period_ends_at_not_current_expiry(db):
    """After an ADMIN_EXPIRY_ADJUSTMENT (which moves ONLY current_expiry, never
    WL periods), a promo period must still anchor on the real WL chronology
    (MAX(ends_at)), not the now-diverged current_expiry."""
    account = db.accounts.create_account("DIRECT", now=1)
    base = _purchase(db, account["id"], plan_code="WL", duration_days=30,
                     key="promo-anchor-base-0001", now=1_000)
    base_period = base["wl_periods"][0]

    # Simulate exactly what ADMIN_EXPIRY_ADJUSTMENT (PH7-01) does: move ONLY
    # current_expiry, never touch WL periods (that store's own writer is
    # exercised separately in tests/test_admin_operational_admin.py; here we
    # only need the resulting divergent DB state, not its auth plumbing).
    moved_expiry = base["new_expiry"] + 10 * 86400
    db._conn.execute(
        "UPDATE mgboost_subscriptions SET current_expiry=?,row_version=row_version+1 "
        "WHERE account_id=?", (moved_expiry, account["id"]),
    )
    db._conn.commit()

    result = _append_promo(db, account["id"], days=7, quota_bytes=30_000_000_000,
                           key="promo-anchor-key-0001", now=3_000)
    assert result["wl_periods"][0]["starts_at"] == base_period["ends_at"]
    assert result["wl_periods"][0]["starts_at"] != moved_expiry


def test_append_promo_wl_period_is_idempotent(db):
    account = db.accounts.create_account("DIRECT", now=1)
    _purchase(db, account["id"], plan_code="WL", duration_days=30,
             key="promo-idem-base-0001", now=1_000)

    first = _append_promo(db, account["id"], days=7, quota_bytes=30_000_000_000,
                          key="promo-idem-key-0001", now=2_000)
    second = _append_promo(db, account["id"], days=7, quota_bytes=30_000_000_000,
                           key="promo-idem-key-0001", now=5_000)
    assert second["already_applied"] is True
    assert second["wl_periods"] == first["wl_periods"]
    assert db._conn.execute(
        "SELECT COUNT(*) c FROM mgboost_wl_periods WHERE account_id=?", (account["id"],),
    ).fetchone()["c"] == 2  # base + exactly one promo period, no duplicate


def test_append_promo_wl_period_creates_fresh_subscription_with_real_plan_version_for_trial(db):
    account = db.accounts.create_account("DIRECT", now=1)
    trial_plan = db.accounts.create_plan_version({
        "plan_code": "WL_TRIAL", "version": 1, "display_name": "WL Trial",
        "plan_kind": "COMMERCIAL", "billing_required": False,
        "device_limit_mode": "LIMITED", "device_limit": 1,
        "wl_mode": "LIMITED", "wl_quota_bytes": 10_000_000_000, "wl_period_days": 1,
        "terms": {},
    }, now=100)

    result = _append_promo(db, account["id"], days=1, quota_bytes=10_000_000_000,
                           key="promo-trial-key-0001", now=1_000,
                           plan_version_id=trial_plan["id"], operation="PROMO_TRIAL_GRANT")

    sub = db._conn.execute(
        "SELECT current_plan_version_id,status FROM mgboost_subscriptions WHERE account_id=?",
        (account["id"],),
    ).fetchone()
    assert sub["current_plan_version_id"] == trial_plan["id"]
    assert sub["status"] == "ACTIVE"

    entitlement = db.entitlements.calculate(account_id=account["id"], now=1_000)
    assert entitlement["plan"]["code"] == "WL_TRIAL"
    assert entitlement["device"]["limit"] == 1
    assert entitlement["wl"]["base_quota_bytes"] == 10_000_000_000


def test_append_promo_wl_period_requires_plan_version_for_brand_new_subscription(db):
    from src.subscription_renewal import RenewalError

    account = db.accounts.create_account("DIRECT", now=1)
    with pytest.raises(RenewalError):
        _append_promo(db, account["id"], days=1, quota_bytes=10_000_000_000,
                      key="promo-trial-noplan-key-01", now=1_000)
