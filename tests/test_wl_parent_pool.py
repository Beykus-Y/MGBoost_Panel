"""PH6-04 -- default shared parent WL pool (accounting/read model only).

Every scenario the corrective-slice instruction named: one parent/several
children, sum across both WL nodes, one child through both nodes, duplicate
ledger observations, a revoked/rebound generation that keeps its already-
consumed current-period traffic, the boundary between two WL periods,
sequential 30d/60d periods, a non-WL account, concurrency/restart/idempotent
recomputation, and the total absence of any enforcement/config side effect.
"""

import importlib
import os
import tempfile

import pytest

from src.broker_operations import BrokerOperations
from src.child_contract import source_contract_hash
from src.wl_parent_pool import (
    WLPeriodNotFound,
    compute_parent_wl_pool,
    resolve_current_parent_wl_pool,
)
from src.wl_usage_ledger import run_collection_cycle
from src.wl_topology import WL_NODES

from tests.test_marzban_broker import FakeMarzban
from tests.test_wl_usage_ledger import FakeServiceMarzban, _clean_topology_ok

HWID_KEY = "pool-test-hwid-key-that-is-at-least-32-bytes-long"


@pytest.fixture
def db(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("DATA_DIR", tmp)
    monkeypatch.setenv("PRIMARY_MGBOOST_ADMIN_ACTOR_ID", "owner:mgboost-primary:v1")
    monkeypatch.setenv("PRIMARY_MGBOOST_ADMIN_LOGIN", "authenticated-primary-login")
    import src.config as config
    import src.database as database
    importlib.reload(config)
    importlib.reload(database)
    database.DB_PATH = os.path.join(tmp, "db.sqlite3")
    instance = database.Database()
    yield instance
    instance._conn.close()


@pytest.fixture(autouse=True)
def seeded_catalog(db):
    from src.plan_catalog import seed_plan_catalog
    seed_plan_catalog(db.plan_catalog, now=1)
    return db


# --- fixtures: a real DIRECT parent account, a real commercial WL/FAMILY --
# purchase (real subscription_renewal engine, real periods -- never hand-
# invented), and real child devices via the real child_provisioning pipeline.

def _direct_account_with_alias(db, *, now, mapping_key, alias_username):
    account = db.accounts.create_account("DIRECT", now=now)
    db._conn.execute(
        "INSERT INTO mgboost_legacy_alias_groups "
        "(account_id,mapping_key,decision_ref,created_by_actor,created_at) "
        "VALUES (?,?,?,?,?)",
        (account["id"], mapping_key, "wl-parent-pool-test-fixture", "TEST", now),
    )
    alias_id = db._conn.execute(
        "INSERT INTO mgboost_legacy_account_aliases "
        "(account_id,legacy_username,alias_role,ownership_provenance,legacy_status,"
        "legacy_expiry,observed_device_count,observed_hwid_count,evidence_json,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (account["id"], alias_username, "PRIMARY", "OWNER_APPROVED", "ACTIVE",
         None, 1, 1, "{}", now),
    ).lastrowid
    db._conn.commit()
    return account, alias_id


def _purchase(db, account_id, *, plan_code, duration_days, key, now):
    return db.subscription_renewal.apply_same_plan_purchase(
        account_id=account_id, plan_code=plan_code, duration_days=duration_days,
        payment_channel="TELEGRAM_STARS", mutation_source="DIRECT_PURCHASE",
        actor_type="TELEGRAM_USER", idempotency_key=f"purchase-idem-{key}", now=now,
    )


def _add_child(db, account_id, remote, alias_username, alias_id, *, hwid_suffix, now):
    slot = db.device_slots.claim(account_id, f"pool-hwid-{hwid_suffix}", HWID_KEY, now=now)
    request_hash = source_contract_hash(remote.users[alias_username])
    prepared = db.child_provisioning.prepare_child_ensure(
        account_id=account_id, slot_generation_id=slot["generation_id"],
        source_alias_id=alias_id, source_contract_hash=request_hash, expire=0,
        idempotency_key=f"pool-child-idem-{account_id}-{hwid_suffix}", now=now,
    )
    claimed = db.child_provisioning.claim(
        prepared["operation_id"], worker_id="pool-fixture-worker", now=now + 1, lease_seconds=5,
    )
    created = BrokerOperations(remote).dispatch("child.user.ensure", claimed["payload"])
    child_uuid = created.pop("uuid")
    db.child_provisioning.acknowledge(
        prepared["operation_id"], worker_id="pool-fixture-worker",
        outcome=created["outcome"], child_uuid=child_uuid, remote_result=created, now=now + 2,
    )
    return prepared["child_intent_id"]


def _family(db, *, mapping_key="family-pool-test", alias_username="family_parent", now=1_000):
    account, alias_id = _direct_account_with_alias(
        db, now=now, mapping_key=mapping_key, alias_username=alias_username,
    )
    purchase = _purchase(
        db, account["id"], plan_code="FAMILY", duration_days=30,
        key=f"{mapping_key}-purchase-key", now=now,
    )
    remote = FakeMarzban()
    remote.users[alias_username] = remote.users.pop("alice")
    remote.users[alias_username]["username"] = alias_username
    return account, alias_id, purchase, remote


NODE_A, NODE_B = sorted(node["id"] for node in WL_NODES)


# --- compute_parent_wl_pool: pure sum over an already-known period id ----

def test_one_parent_several_children_sum_into_one_shared_pool(db):
    account, alias_id, purchase, remote = _family(db)
    period_id = db._conn.execute(
        "SELECT id FROM mgboost_wl_periods WHERE account_id=? AND sequence_no=1",
        (account["id"],),
    ).fetchone()["id"]
    db.wl_usage_ledger.sync_wl_period_statuses(account_id=account["id"], now=1_000)

    child_a = _add_child(db, account["id"], remote, "family_parent", alias_id, hwid_suffix="a", now=1_000)
    child_b = _add_child(db, account["id"], remote, "family_parent", alias_id, hwid_suffix="b", now=1_000)
    child_c = _add_child(db, account["id"], remote, "family_parent", alias_id, hwid_suffix="c", now=1_000)

    db.wl_usage_ledger.record_sample(
        account_id=account["id"], child_intent_id=child_a, node_id=NODE_A,
        cursor_after=60_000_000_000, collector_id="w1", collected_at=1_000, wl_period_id=period_id,
    )
    db.wl_usage_ledger.record_sample(
        account_id=account["id"], child_intent_id=child_b, node_id=NODE_A,
        cursor_after=20_000_000_000, collector_id="w1", collected_at=1_000, wl_period_id=period_id,
    )
    db.wl_usage_ledger.record_sample(
        account_id=account["id"], child_intent_id=child_c, node_id=NODE_A,
        cursor_after=10_000_000_000, collector_id="w1", collected_at=1_000, wl_period_id=period_id,
    )

    pool = compute_parent_wl_pool(db._conn, account_id=account["id"], wl_period_id=period_id)
    assert pool["consumed_bytes"] == 90_000_000_000  # 60+20+10 = 90 decimal-GB, roadmap's own example
    assert pool["base_quota_bytes"] == 150_000_000_000  # FAMILY plan: 150 decimal-GB
    assert pool["remaining_bytes"] == 60_000_000_000
    assert pool["exceeded"] is False
    assert pool["contributing_children"] == 3
    assert pool["quota_mode"] == "LIMITED"


def test_pool_exceeded_at_quota_reported_but_never_enforced(db):
    account, alias_id, purchase, remote = _family(db)
    period_id = db._conn.execute(
        "SELECT id FROM mgboost_wl_periods WHERE account_id=? AND sequence_no=1",
        (account["id"],),
    ).fetchone()["id"]
    db.wl_usage_ledger.sync_wl_period_statuses(account_id=account["id"], now=1_000)
    child = _add_child(db, account["id"], remote, "family_parent", alias_id, hwid_suffix="x", now=1_000)

    db.wl_usage_ledger.record_sample(
        account_id=account["id"], child_intent_id=child, node_id=NODE_A,
        cursor_after=150_000_000_000, collector_id="w1", collected_at=1_000, wl_period_id=period_id,
    )
    pool = compute_parent_wl_pool(db._conn, account_id=account["id"], wl_period_id=period_id)
    assert pool["consumed_bytes"] == 150_000_000_000
    assert pool["remaining_bytes"] == 0
    assert pool["exceeded"] is True

    # No enforcement side effect of any kind: the child/account/period rows
    # are exactly what this fixture created, nothing else touched them.
    intent = db._conn.execute(
        "SELECT desired_state, observed_state FROM mgboost_child_user_intents WHERE id=?", (child,),
    ).fetchone()
    assert intent["desired_state"] == "ACTIVE"
    assert intent["observed_state"] == "ACTIVE"


def test_one_child_through_both_wl_nodes_is_summed_together(db):
    account, alias_id, purchase, remote = _family(db)
    period_id = db._conn.execute(
        "SELECT id FROM mgboost_wl_periods WHERE account_id=? AND sequence_no=1",
        (account["id"],),
    ).fetchone()["id"]
    child = _add_child(db, account["id"], remote, "family_parent", alias_id, hwid_suffix="both", now=1_000)

    db.wl_usage_ledger.record_sample(
        account_id=account["id"], child_intent_id=child, node_id=NODE_A,
        cursor_after=5_000_000_000, collector_id="w1", collected_at=1_000, wl_period_id=period_id,
    )
    db.wl_usage_ledger.record_sample(
        account_id=account["id"], child_intent_id=child, node_id=NODE_B,
        cursor_after=7_000_000_000, collector_id="w1", collected_at=1_000, wl_period_id=period_id,
    )
    pool = compute_parent_wl_pool(db._conn, account_id=account["id"], wl_period_id=period_id)
    assert pool["consumed_bytes"] == 12_000_000_000
    assert pool["contributing_children"] == 1


def test_duplicate_ledger_observation_never_double_counts(db):
    account, alias_id, purchase, remote = _family(db)
    period_id = db._conn.execute(
        "SELECT id FROM mgboost_wl_periods WHERE account_id=? AND sequence_no=1",
        (account["id"],),
    ).fetchone()["id"]
    child = _add_child(db, account["id"], remote, "family_parent", alias_id, hwid_suffix="dup", now=1_000)

    for _ in range(3):  # simulate a crash-retry / racing duplicate collector poll
        db.wl_usage_ledger.record_sample(
            account_id=account["id"], child_intent_id=child, node_id=NODE_A,
            cursor_after=3_000_000_000, collector_id="w1", collected_at=1_000, wl_period_id=period_id,
        )
    pool = compute_parent_wl_pool(db._conn, account_id=account["id"], wl_period_id=period_id)
    assert pool["consumed_bytes"] == 3_000_000_000  # not 9_000_000_000


def test_revoked_generation_keeps_its_already_consumed_current_period_traffic(db):
    account, alias_id, purchase, remote = _family(db)
    period_id = db._conn.execute(
        "SELECT id FROM mgboost_wl_periods WHERE account_id=? AND sequence_no=1",
        (account["id"],),
    ).fetchone()["id"]
    old_child = _add_child(db, account["id"], remote, "family_parent", alias_id, hwid_suffix="old", now=1_000)
    new_child = _add_child(db, account["id"], remote, "family_parent", alias_id, hwid_suffix="new", now=1_000)

    db.wl_usage_ledger.record_sample(
        account_id=account["id"], child_intent_id=old_child, node_id=NODE_A,
        cursor_after=4_000_000_000, collector_id="w1", collected_at=1_000, wl_period_id=period_id,
    )
    # Simulate the real rebind/revoke lifecycle's own terminal effect on the
    # old generation's row (child_lifecycle.py sets exactly this) -- the
    # samples/events tables are immutable regardless, this just proves the
    # pool sum doesn't filter on observed_state.
    db._conn.execute(
        "UPDATE mgboost_child_user_intents SET desired_state='REVOKED',"
        "observed_state='REVOKED' WHERE id=?", (old_child,),
    )
    db._conn.commit()

    db.wl_usage_ledger.record_sample(
        account_id=account["id"], child_intent_id=new_child, node_id=NODE_A,
        cursor_after=1_000_000_000, collector_id="w1", collected_at=1_000, wl_period_id=period_id,
    )
    pool = compute_parent_wl_pool(db._conn, account_id=account["id"], wl_period_id=period_id)
    assert pool["consumed_bytes"] == 5_000_000_000  # old (revoked) 4 + new 1, neither lost
    assert pool["contributing_children"] == 2


def test_wl_period_boundary_never_leaks_usage_across_periods(db):
    account, alias_id = _direct_account_with_alias(
        db, now=0, mapping_key="boundary-test", alias_username="boundary_parent",
    )
    purchase = _purchase(db, account["id"], plan_code="WL", duration_days=60, key="boundary-key", now=0)
    assert len(purchase["wl_periods"]) == 2
    p1 = purchase["wl_periods"][0]
    p2 = purchase["wl_periods"][1]
    assert p1["ends_at"] == p2["starts_at"]  # contiguous, no gap/overlap
    p1_id = db._conn.execute(
        "SELECT id FROM mgboost_wl_periods WHERE account_id=? AND sequence_no=1", (account["id"],),
    ).fetchone()["id"]
    p2_id = db._conn.execute(
        "SELECT id FROM mgboost_wl_periods WHERE account_id=? AND sequence_no=2", (account["id"],),
    ).fetchone()["id"]

    remote = FakeMarzban()
    remote.users["boundary_parent"] = remote.users.pop("alice")
    remote.users["boundary_parent"]["username"] = "boundary_parent"
    child = _add_child(db, account["id"], remote, "boundary_parent", alias_id, hwid_suffix="b", now=0)

    # A sample explicitly attributed to period 1 vs. one explicitly
    # attributed to period 2 (exactly what PH6-03's own UTC-hour-aligned
    # collection-time attribution always produces -- never straddled).
    db.wl_usage_ledger.record_sample(
        account_id=account["id"], child_intent_id=child, node_id=NODE_A,
        cursor_after=10_000_000_000, collector_id="w1", collected_at=p1["starts_at"] + 3600,
        wl_period_id=p1_id,
    )
    db.wl_usage_ledger.record_sample(
        account_id=account["id"], child_intent_id=child, node_id=NODE_A,
        cursor_after=17_000_000_000, collector_id="w1", collected_at=p2["starts_at"] + 3600,
        wl_period_id=p2_id,
    )

    pool1 = compute_parent_wl_pool(db._conn, account_id=account["id"], wl_period_id=p1_id)
    pool2 = compute_parent_wl_pool(db._conn, account_id=account["id"], wl_period_id=p2_id)
    assert pool1["consumed_bytes"] == 10_000_000_000
    assert pool2["consumed_bytes"] == 7_000_000_000  # 17 - 10, this period's own delta only
    assert pool1["base_quota_bytes"] == pool2["base_quota_bytes"] == 100_000_000_000  # WL: 100GB/period


def test_30d_and_60d_sequential_purchases_never_merge_quota(db):
    account, alias_id = _direct_account_with_alias(
        db, now=0, mapping_key="seq-purchase-test", alias_username="seq_parent",
    )
    _purchase(db, account["id"], plan_code="WL", duration_days=30, key="seq-key-1", now=0)
    periods = db._conn.execute(
        "SELECT id, sequence_no, base_quota_bytes FROM mgboost_wl_periods "
        "WHERE account_id=? ORDER BY sequence_no", (account["id"],),
    ).fetchall()
    assert len(periods) == 1
    # A second, later purchase extends the subscription and schedules its
    # own further sequential period(s) -- never retroactively changes the
    # already-scheduled first one (PH5-02's own immutability contract).
    _purchase(db, account["id"], plan_code="WL", duration_days=30, key="seq-key-2", now=30 * 86400)
    periods = db._conn.execute(
        "SELECT id, sequence_no, base_quota_bytes FROM mgboost_wl_periods "
        "WHERE account_id=? ORDER BY sequence_no", (account["id"],),
    ).fetchall()
    assert len(periods) == 2
    assert periods[0]["base_quota_bytes"] == periods[1]["base_quota_bytes"] == 100_000_000_000

    remote = FakeMarzban()
    remote.users["seq_parent"] = remote.users.pop("alice")
    remote.users["seq_parent"]["username"] = "seq_parent"
    child = _add_child(db, account["id"], remote, "seq_parent", alias_id, hwid_suffix="s", now=0)

    db.wl_usage_ledger.record_sample(
        account_id=account["id"], child_intent_id=child, node_id=NODE_A,
        cursor_after=30_000_000_000, collector_id="w1", collected_at=1_000, wl_period_id=periods[0]["id"],
    )
    pool1 = compute_parent_wl_pool(db._conn, account_id=account["id"], wl_period_id=periods[0]["id"])
    pool2 = compute_parent_wl_pool(db._conn, account_id=account["id"], wl_period_id=periods[1]["id"])
    assert pool1["consumed_bytes"] == 30_000_000_000
    assert pool2["consumed_bytes"] == 0  # nothing recorded against the second period yet


def test_unknown_period_for_account_raises_not_a_fabricated_zero(db):
    account, _alias_id = _direct_account_with_alias(
        db, now=0, mapping_key="unknown-period-test", alias_username="unk_parent",
    )
    with pytest.raises(WLPeriodNotFound):
        compute_parent_wl_pool(db._conn, account_id=account["id"], wl_period_id=999_999)


def test_period_belonging_to_a_different_account_is_rejected(db):
    account_a, _ = _direct_account_with_alias(db, now=0, mapping_key="acct-a", alias_username="a_parent")
    _purchase(db, account_a["id"], plan_code="WL", duration_days=30, key="acct-a-key", now=0)
    period_a = db._conn.execute(
        "SELECT id FROM mgboost_wl_periods WHERE account_id=?", (account_a["id"],),
    ).fetchone()["id"]

    account_b, _ = _direct_account_with_alias(db, now=0, mapping_key="acct-b", alias_username="b_parent")
    with pytest.raises(WLPeriodNotFound):
        compute_parent_wl_pool(db._conn, account_id=account_b["id"], wl_period_id=period_a)


# --- resolve_current_parent_wl_pool: the time-aware entrypoint -----------

def test_non_wl_account_has_no_current_pool(db):
    account, _alias_id = _direct_account_with_alias(
        db, now=0, mapping_key="non-wl-test", alias_username="basic_parent",
    )
    _purchase(db, account["id"], plan_code="BASIC", duration_days=30, key="non-wl-key", now=0)
    assert resolve_current_parent_wl_pool(db, account_id=account["id"], now=100) is None


def test_account_with_zero_purchases_has_no_current_pool(db):
    account, _alias_id = _direct_account_with_alias(
        db, now=0, mapping_key="never-purchased-test", alias_username="new_parent",
    )
    assert resolve_current_parent_wl_pool(db, account_id=account["id"], now=100) is None


def test_between_two_periods_has_no_current_pool(db):
    # An account whose only period already fully elapsed and no successor
    # was purchased yet -- distinct from "no WL period ever existed", but
    # the same "nothing to report" result, by design.
    account, alias_id = _direct_account_with_alias(
        db, now=0, mapping_key="between-periods-test", alias_username="gap_parent",
    )
    _purchase(db, account["id"], plan_code="WL", duration_days=30, key="between-key", now=0)
    result = resolve_current_parent_wl_pool(db, account_id=account["id"], now=31 * 86400)
    assert result is None
    period = db._conn.execute(
        "SELECT status FROM mgboost_wl_periods WHERE account_id=?", (account["id"],),
    ).fetchone()
    assert period["status"] == "CLOSED"  # closed by the time-only sync, not fabricated activity


def test_resolve_current_pool_promotes_planned_to_active_and_sums_real_samples(db):
    account, alias_id = _direct_account_with_alias(
        db, now=0, mapping_key="resolve-current-test", alias_username="cur_parent",
    )
    _purchase(db, account["id"], plan_code="WL", duration_days=30, key="resolve-key", now=0)
    period = db._conn.execute(
        "SELECT id, status FROM mgboost_wl_periods WHERE account_id=?", (account["id"],),
    ).fetchone()
    assert period["status"] == "PLANNED"  # real purchase always creates PLANNED, never ACTIVE

    remote = FakeMarzban()
    remote.users["cur_parent"] = remote.users.pop("alice")
    remote.users["cur_parent"]["username"] = "cur_parent"
    child = _add_child(db, account["id"], remote, "cur_parent", alias_id, hwid_suffix="c", now=0)

    # Nothing promotes PLANNED->ACTIVE on its own -- resolve_current_parent_
    # wl_pool is the one that does it, via sync_wl_period_statuses, before
    # ever trying to resolve/sum anything.
    pre_sync_status = db._conn.execute(
        "SELECT status FROM mgboost_wl_periods WHERE id=?", (period["id"],),
    ).fetchone()["status"]
    assert pre_sync_status == "PLANNED"

    db.wl_usage_ledger.record_sample(
        account_id=account["id"], child_intent_id=child, node_id=NODE_A,
        cursor_after=5_000_000_000, collector_id="w1", collected_at=100,
        wl_period_id=period["id"],
    )
    pool = resolve_current_parent_wl_pool(db, account_id=account["id"], now=100)
    assert pool is not None
    assert pool["wl_period_id"] == period["id"]
    assert pool["consumed_bytes"] == 5_000_000_000
    assert pool["status"] == "ACTIVE"

    status_after = db._conn.execute(
        "SELECT status FROM mgboost_wl_periods WHERE id=?", (period["id"],),
    ).fetchone()["status"]
    assert status_after == "ACTIVE"


def test_run_collection_cycle_now_attributes_a_real_wl_period_end_to_end(db):
    """End-to-end proof the gap this session closed is real: a genuine
    purchase (always PLANNED) followed by a real `run_collection_cycle`
    call (PH6-03's own collector, now calling `sync_wl_period_statuses`
    internally) attributes the recorded sample's `wl_period_id` to the
    live period, and the PH6-04 pool sum picks it up with zero extra
    plumbing -- one accounting path, not two."""
    _clean_topology_ok(db, now=1)
    account, alias_id = _direct_account_with_alias(
        db, now=0, mapping_key="e2e-collection-test", alias_username="e2e_parent",
    )
    _purchase(db, account["id"], plan_code="WL", duration_days=30, key="e2e-key", now=0)

    remote = FakeMarzban()
    remote.users["e2e_parent"] = remote.users.pop("alice")
    remote.users["e2e_parent"]["username"] = "e2e_parent"
    child_id = _add_child(db, account["id"], remote, "e2e_parent", alias_id, hwid_suffix="e", now=0)
    child_username = db._conn.execute(
        "SELECT child_username FROM mgboost_child_user_intents WHERE id=?", (child_id,),
    ).fetchone()["child_username"]

    fake_marzban = FakeServiceMarzban()
    fake_marzban.set_usage(child_username, {NODE_A: 8_000_000_000, NODE_B: 1_000_000_000})

    summary = run_collection_cycle(db=db, service_marzban=fake_marzban, worker_id="e2e-worker", now=100)
    assert summary["outcome"] == "OK"
    assert summary["samples_recorded"] == 2

    sample_period_ids = {
        row["wl_period_id"] for row in db._conn.execute(
            "SELECT wl_period_id FROM mgboost_wl_usage_samples WHERE child_intent_id=?", (child_id,),
        )
    }
    assert sample_period_ids != {None}  # the real gap: this used to always be {None}

    pool = resolve_current_parent_wl_pool(db, account_id=account["id"], now=100)
    assert pool is not None
    assert pool["consumed_bytes"] == 9_000_000_000
    assert pool["contributing_children"] == 1


# --- concurrency / restart / idempotent recomputation ---------------------

def test_recomputation_is_idempotent_across_repeated_calls_and_restart(db):
    account, alias_id, purchase, remote = _family(db)
    period_id = db._conn.execute(
        "SELECT id FROM mgboost_wl_periods WHERE account_id=? AND sequence_no=1", (account["id"],),
    ).fetchone()["id"]
    child = _add_child(db, account["id"], remote, "family_parent", alias_id, hwid_suffix="r", now=1_000)
    db.wl_usage_ledger.record_sample(
        account_id=account["id"], child_intent_id=child, node_id=NODE_A,
        cursor_after=4_000_000_000, collector_id="w1", collected_at=1_000, wl_period_id=period_id,
    )

    first = compute_parent_wl_pool(db._conn, account_id=account["id"], wl_period_id=period_id)
    second = compute_parent_wl_pool(db._conn, account_id=account["id"], wl_period_id=period_id)
    assert first == second

    # "Restart" -- nothing in this module holds any in-memory state to lose;
    # a fresh call against the same durable connection reproduces the exact
    # same result.
    third = resolve_current_parent_wl_pool(db, account_id=account["id"], now=1_000)
    assert third["consumed_bytes"] == first["consumed_bytes"]

    # Concurrent collector activity: two workers racing the same
    # (child, node) transition -- PH6-03's own idempotency key means only
    # one of them actually advances the ledger; recomputation afterwards is
    # still exactly correct, never double-counted.
    db.wl_usage_ledger.record_sample(
        account_id=account["id"], child_intent_id=child, node_id=NODE_A,
        cursor_after=6_000_000_000, collector_id="w2", collected_at=1_000 + 3600, wl_period_id=period_id,
    )
    db.wl_usage_ledger.record_sample(
        account_id=account["id"], child_intent_id=child, node_id=NODE_A,
        cursor_after=6_000_000_000, collector_id="w1", collected_at=1_000 + 3600, wl_period_id=period_id,
    )
    fourth = compute_parent_wl_pool(db._conn, account_id=account["id"], wl_period_id=period_id)
    assert fourth["consumed_bytes"] == 6_000_000_000  # not 8_000_000_000


# --- no raw identifiers, no enforcement/config mutation -------------------

def test_pool_read_models_never_expose_raw_identifiers(db):
    account, alias_id, purchase, remote = _family(db)
    period_id = db._conn.execute(
        "SELECT id FROM mgboost_wl_periods WHERE account_id=? AND sequence_no=1", (account["id"],),
    ).fetchone()["id"]
    child = _add_child(db, account["id"], remote, "family_parent", alias_id, hwid_suffix="priv", now=1_000)
    db.wl_usage_ledger.record_sample(
        account_id=account["id"], child_intent_id=child, node_id=NODE_A,
        cursor_after=1_000_000_000, collector_id="w1", collected_at=1_000, wl_period_id=period_id,
    )
    pool = compute_parent_wl_pool(db._conn, account_id=account["id"], wl_period_id=period_id)
    banned_substrings = ("mgc_", "uuid_", "sha256:", "family_parent")
    serialized = " ".join(f"{k}={v}" for k, v in pool.items())
    for needle in banned_substrings:
        assert needle not in serialized


def test_no_marzban_or_config_mutation_from_pool_computation(db):
    account, alias_id, purchase, remote = _family(db)
    period_id = db._conn.execute(
        "SELECT id FROM mgboost_wl_periods WHERE account_id=? AND sequence_no=1", (account["id"],),
    ).fetchone()["id"]
    child = _add_child(db, account["id"], remote, "family_parent", alias_id, hwid_suffix="m", now=1_000)
    db.wl_usage_ledger.record_sample(
        account_id=account["id"], child_intent_id=child, node_id=NODE_A,
        cursor_after=1_000_000_000, collector_id="w1", collected_at=1_000, wl_period_id=period_id,
    )
    calls_before = list(remote.calls)
    compute_parent_wl_pool(db._conn, account_id=account["id"], wl_period_id=period_id)
    resolve_current_parent_wl_pool(db, account_id=account["id"], now=1_000)
    assert remote.calls == calls_before  # zero additional Marzban interaction of any kind

    intent = db._conn.execute(
        "SELECT desired_state, observed_state FROM mgboost_child_user_intents WHERE id=?", (child,),
    ).fetchone()
    assert intent["desired_state"] == "ACTIVE"
    assert intent["observed_state"] == "ACTIVE"
    period = db._conn.execute(
        "SELECT quota_mode, base_quota_bytes FROM mgboost_wl_periods WHERE id=?", (period_id,),
    ).fetchone()
    assert period["quota_mode"] == "LIMITED"
    assert period["base_quota_bytes"] == 150_000_000_000
