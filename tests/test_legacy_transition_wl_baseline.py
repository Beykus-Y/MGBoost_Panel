"""Focused contract for conservative legacy->commercial WL attribution.

The first observation after an hour-aligned LEGACY_PAID_COMPAT transition is
an explicit baseline, never a commercial charge.  This deliberately forgives
one crossing collector interval so traffic before the boundary can never be
charged to the new LIMITED period.
"""

from tests.test_child_lifecycle import _build_applied_child
from tests.test_legacy_commercial_transition import _add_child
from tests.test_wl_usage_ledger import (
    db, _clean_topology_ok, _seed_active_wl_period, FakeServiceMarzban,
)
from src.wl_usage_ledger import run_collection_cycle
from src.wl_parent_pool import compute_parent_wl_pool


def _transition(db, account_id, *, number):
    """Minimal durable parent for the ledger-only baseline contract."""
    from src.plan_catalog import seed_plan_catalog, RUB_PRICES
    seed_plan_catalog(db.plan_catalog, now=1)
    conn = db._conn
    target = conn.execute("SELECT id FROM mgboost_plan_versions WHERE plan_code='WL'").fetchone()[0]
    duration = conn.execute(
        "SELECT id FROM mgboost_plan_durations WHERE plan_version_id=? AND duration_days=30",
        (target,),
    ).fetchone()[0]
    catalog = conn.execute(
        "SELECT id,catalog_version FROM mgboost_price_catalog_versions WHERE channel='RUB' AND status='ACTIVE'"
    ).fetchone()
    price = conn.execute(
        "SELECT id FROM mgboost_plan_prices WHERE catalog_version_id=? AND plan_version_id=? AND duration_id=?",
        (catalog["id"], target, duration),
    ).fetchone()[0]
    payment = conn.execute(
        "INSERT INTO mgboost_manual_payment_records (public_id,kind,status,account_id,plan_version_id,duration_id,catalog_version_id,catalog_version_snapshot,plan_price_id,plan_code_snapshot,plan_version_snapshot,duration_days_snapshot,expected_amount_minor,recorded_amount_minor,currency,payment_method,external_reference,actor_ref,idempotency_key_hash,request_hash,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"mpay_baseline_{number}", "PLAN_PRODUCT", "PENDING", account_id, target, duration,
         catalog["id"], catalog["catalog_version"], price, "WL", 1, 30, RUB_PRICES[("WL", 30)],
         RUB_PRICES[("WL", 30)], "RUB", "test", f"baseline-ref-{number}", "test-owner",
         f"baseline-key-{number}", f"baseline-request-{number}", 1, 1),
    ).lastrowid
    source = conn.execute("SELECT current_plan_version_id FROM mgboost_subscriptions WHERE account_id=?", (account_id,)).fetchone()[0]
    tid = conn.execute(
        "INSERT INTO mgboost_legacy_commercial_transitions (public_id,account_id,payment_record_id,state,source_plan_version_id,source_subscription_status,aligned_source_expiry,target_plan_version_id,duration_days,catalog_version_id,plan_price_id,expected_amount_minor,actor_ref,reason,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"lct_baseline_{number}", account_id, payment, "PENDING_PAYMENT", source, "ACTIVE", 3600,
         target, 30, catalog["id"], price, RUB_PRICES[("WL", 30)], "test-owner", "baseline test", 1, 1),
    ).lastrowid
    conn.commit()
    return tid


def test_transition_baseline_forgives_crossing_delta_then_charges_once(db):
    _clean_topology_ok(db)
    fx = _build_applied_child(db)
    account_id = fx["account"]["account_id"]
    period_id = _seed_active_wl_period(
        db, account_id=account_id, starts_at=3600, ends_at=3600 + 86400,
        now=3600, quota_mode="LIMITED", base_quota_bytes=10_000,
    )
    fake = FakeServiceMarzban()
    fake.set_usage(fx["child_username"], {4: 100, 7: 200})
    run_collection_cycle(db=db, service_marzban=fake, worker_id="before", now=3599)

    db.wl_usage_ledger.register_transition_baseline(
        transition_id=_transition(db, account_id, number=77), account_id=account_id, wl_period_id=period_id,
        child_intent_ids=[fx["child_intent_id"]], node_ids=[4, 7], now=3600,
    )
    fake.set_usage(fx["child_username"], {4: 250, 7: 450})
    run_collection_cycle(db=db, service_marzban=fake, worker_id="crossing", now=3601)
    assert compute_parent_wl_pool(db._conn, account_id=account_id, wl_period_id=period_id)["consumed_bytes"] == 0

    fake.set_usage(fx["child_username"], {4: 280, 7: 500})
    run_collection_cycle(db=db, service_marzban=fake, worker_id="after", now=3660)
    assert compute_parent_wl_pool(db._conn, account_id=account_id, wl_period_id=period_id)["consumed_bytes"] == 80


def test_transition_baseline_is_idempotent_and_reset_safe(db):
    _clean_topology_ok(db)
    fx = _build_applied_child(db)
    account_id = fx["account"]["account_id"]
    period_id = _seed_active_wl_period(
        db, account_id=account_id, starts_at=3600, ends_at=3600 + 86400,
        now=3600, quota_mode="LIMITED", base_quota_bytes=10_000,
    )
    fake = FakeServiceMarzban()
    fake.set_usage(fx["child_username"], {4: 100, 7: 100})
    run_collection_cycle(db=db, service_marzban=fake, worker_id="before", now=3599)
    transition_id = _transition(db, account_id, number=78)
    db.wl_usage_ledger.register_transition_baseline(
        transition_id=transition_id, account_id=account_id, wl_period_id=period_id,
        child_intent_ids=[fx["child_intent_id"]], node_ids=[4, 7], now=3600,
    )
    db.wl_usage_ledger.register_transition_baseline(
        transition_id=transition_id, account_id=account_id, wl_period_id=period_id,
        child_intent_ids=[fx["child_intent_id"]], node_ids=[4, 7], now=3600,
    )
    fake.set_usage(fx["child_username"], {4: 900, 7: 800})
    first = run_collection_cycle(db=db, service_marzban=fake, worker_id="first", now=3601)
    # A reset after the forgiven crossing interval is a normal, charged
    # post-transition delta; it must not create a second baseline.
    fake.set_usage(fx["child_username"], {4: 20, 7: 10})
    reset = run_collection_cycle(db=db, service_marzban=fake, worker_id="reset", now=3660)
    assert first["errors"] == [] and reset["errors"] == []
    assert compute_parent_wl_pool(db._conn, account_id=account_id, wl_period_id=period_id)["consumed_bytes"] == 30


def test_baseline_identity_is_transition_child_generation_and_node(db):
    _clean_topology_ok(db)
    fx = _build_applied_child(db)
    account_id = fx["account"]["account_id"]
    period_id = _seed_active_wl_period(db, account_id=account_id, starts_at=3600,
                                       ends_at=90000, now=3600, quota_mode="LIMITED",
                                       base_quota_bytes=10_000)
    tid = _transition(db, account_id, number=79)
    db.wl_usage_ledger.register_transition_baseline(
        transition_id=tid, account_id=account_id, wl_period_id=period_id,
        child_intent_ids=[fx["child_intent_id"]], node_ids=[4, 7], now=3600,
    )
    rows = db._conn.execute(
        "SELECT child_intent_id,node_id FROM mgboost_wl_transition_baselines WHERE transition_id=? ORDER BY node_id",
        (tid,),
    ).fetchall()
    assert [tuple(row) for row in rows] == [(fx["child_intent_id"], 4), (fx["child_intent_id"], 7)]
    # Re-registration is a durable replay, not a second free observation.
    db.wl_usage_ledger.register_transition_baseline(
        transition_id=tid, account_id=account_id, wl_period_id=period_id,
        child_intent_ids=[fx["child_intent_id"]], node_ids=[4, 7], now=3601,
    )
    assert db._conn.execute("SELECT COUNT(*) FROM mgboost_wl_transition_baselines WHERE transition_id=?", (tid,)).fetchone()[0] == 2


def test_transition_baseline_covers_multiple_surviving_children_and_both_nodes(db):
    _clean_topology_ok(db)
    fx = _build_applied_child(db)
    account_id = fx["account"]["account_id"]
    second = _add_child(db, account_id, suffix="baseline-second", now=300)
    second_name = db._conn.execute(
        "SELECT child_username FROM mgboost_child_user_intents WHERE id=?", (second["child_intent_id"],),
    ).fetchone()[0]
    period_id = _seed_active_wl_period(db, account_id=account_id, starts_at=3600,
                                       ends_at=90000, now=3600, quota_mode="LIMITED",
                                       base_quota_bytes=100_000)
    tid = _transition(db, account_id, number=80)
    child_ids = [fx["child_intent_id"], second["child_intent_id"]]
    db.wl_usage_ledger.register_transition_baseline(
        transition_id=tid, account_id=account_id, wl_period_id=period_id,
        child_intent_ids=child_ids, node_ids=[4, 7], now=3600,
    )
    fake = FakeServiceMarzban()
    fake.set_usage(fx["child_username"], {4: 500, 7: 700})
    fake.set_usage(second_name, {4: 300, 7: 400})
    run_collection_cycle(db=db, service_marzban=fake, worker_id="multi-crossing", now=3601)
    assert compute_parent_wl_pool(db._conn, account_id=account_id, wl_period_id=period_id)["consumed_bytes"] == 0
    fake.set_usage(fx["child_username"], {4: 510, 7: 720})
    fake.set_usage(second_name, {4: 330, 7: 440})
    run_collection_cycle(db=db, service_marzban=fake, worker_id="multi-after", now=3660)
    assert compute_parent_wl_pool(db._conn, account_id=account_id, wl_period_id=period_id)["consumed_bytes"] == 100
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_wl_transition_baselines WHERE transition_id=? AND state='CONSUMED'", (tid,),
    ).fetchone()[0] == 4


def test_repeated_first_observation_does_not_forgive_a_second_interval(db):
    _clean_topology_ok(db)
    fx = _build_applied_child(db)
    account_id = fx["account"]["account_id"]
    period_id = _seed_active_wl_period(db, account_id=account_id, starts_at=3600,
                                       ends_at=90000, now=3600, quota_mode="LIMITED",
                                       base_quota_bytes=100_000)
    tid = _transition(db, account_id, number=81)
    db.wl_usage_ledger.register_transition_baseline(
        transition_id=tid, account_id=account_id, wl_period_id=period_id,
        child_intent_ids=[fx["child_intent_id"]], node_ids=[4], now=3600,
    )
    first = db.wl_usage_ledger.record_sample(
        account_id=account_id, child_intent_id=fx["child_intent_id"], node_id=4,
        cursor_after=1000, collector_id="worker-a", collected_at=3601, wl_period_id=period_id,
    )
    replay = db.wl_usage_ledger.record_sample(
        account_id=account_id, child_intent_id=fx["child_intent_id"], node_id=4,
        cursor_after=1000, collector_id="worker-b", collected_at=3601, wl_period_id=period_id,
    )
    charged = db.wl_usage_ledger.record_sample(
        account_id=account_id, child_intent_id=fx["child_intent_id"], node_id=4,
        cursor_after=1050, collector_id="worker-a", collected_at=3660, wl_period_id=period_id,
    )
    assert first["delta_bytes"] == 0 and first["transition_baseline"] is True
    assert replay["delta_bytes"] == 0
    assert charged["delta_bytes"] == 50
    assert compute_parent_wl_pool(db._conn, account_id=account_id, wl_period_id=period_id)["consumed_bytes"] == 50


def test_ordinary_limited_observation_has_no_transition_forgiveness(db):
    _clean_topology_ok(db)
    fx = _build_applied_child(db)
    account_id = fx["account"]["account_id"]
    period_id = _seed_active_wl_period(db, account_id=account_id, starts_at=3600,
                                       ends_at=90000, now=3600, quota_mode="LIMITED",
                                       base_quota_bytes=100_000)
    sample = db.wl_usage_ledger.record_sample(
        account_id=account_id, child_intent_id=fx["child_intent_id"], node_id=4,
        cursor_after=75, collector_id="ordinary-renewal", collected_at=3601, wl_period_id=period_id,
    )
    assert sample["delta_bytes"] == 75
    assert compute_parent_wl_pool(db._conn, account_id=account_id, wl_period_id=period_id)["consumed_bytes"] == 75


import pytest


@pytest.mark.parametrize("period_number", [2, 3])
def test_transition_baseline_consumes_in_later_commercial_period_once(db, period_number):
    _clean_topology_ok(db)
    fx = _build_applied_child(db)
    account_id = fx["account"]["account_id"]
    first = _seed_active_wl_period(
        db, account_id=account_id, starts_at=3600, ends_at=7200, now=3600,
        quota_mode="LIMITED", base_quota_bytes=100_000,
    )
    current_start = 3600 * period_number
    current = _seed_active_wl_period(
        db, account_id=account_id, starts_at=current_start, ends_at=current_start + 3600, now=current_start,
        quota_mode="LIMITED", base_quota_bytes=100_000,
    )
    db._conn.execute("UPDATE mgboost_wl_periods SET status='CLOSED' WHERE id=?", (first,))
    db._conn.commit()
    tid = _transition(db, account_id, number=82)
    db.wl_usage_ledger.register_transition_baseline(
        transition_id=tid, account_id=account_id, wl_period_id=first,
        child_intent_ids=[fx["child_intent_id"]], node_ids=[4], now=3600,
    )
    forgiven = db.wl_usage_ledger.record_sample(
        account_id=account_id, child_intent_id=fx["child_intent_id"], node_id=4,
        cursor_after=5000, collector_id="late-period", collected_at=current_start + 1,
        wl_period_id=current,
    )
    charged = db.wl_usage_ledger.record_sample(
        account_id=account_id, child_intent_id=fx["child_intent_id"], node_id=4,
        cursor_after=5075, collector_id="late-period", collected_at=current_start + 60,
        wl_period_id=current,
    )
    assert forgiven["transition_baseline"] is True and forgiven["delta_bytes"] == 0
    assert charged["delta_bytes"] == 75
    assert compute_parent_wl_pool(db._conn, account_id=account_id, wl_period_id=current)["consumed_bytes"] == 75
