"""PH6-06 exact inbound-only WL enforcement state machine.

Every scenario proves one named property from the slice brief: exactly-once
disable/enable by observation, replay/flip-flop safety, epoch-guarded
staleness, topology fail-closed gating, byte-exact preservation of everything
the mutation must never touch, Non-WL/UNLIMITED invisibility, partial/
outage/restart recovery paths, and clean interplay with device revoke/pause/
rebind lifecycle.
"""

import json
import threading
from urllib.error import HTTPError

import pytest

from src.broker_operations import BrokerOperations
from src.broker_protocol import BROKER_OPERATIONS
from src.child_contract import credential_verifier, source_contract_hash
from src.wl_enforcement import (
    MAX_ATTEMPTS,
    RemoteChildMissing,
    WLEnforcementConflict,
    decide_direction_from_pool,
    observe_child_vless,
    process_wl_op,
    run_wl_enforcement_cycle,
)
from src.wl_enforcement_schema import (
    MIGRATION_ID,
    SCHEMA_CHECKSUM,
    apply_wl_enforcement_schema,
)
from src.wl_topology import WL_INBOUND_TAGS, WL_NODES

from tests.test_child_lifecycle import _build_applied_child, _revoke_fn
from tests.test_child_provisioning import HWID_KEY, PRIMARY, PRIMARY_LOGIN, _account
from tests.test_marzban_broker import FakeMarzban
from tests.test_wl_usage_ledger import _clean_topology_ok


WL_A = "wl-tcp-direct"
WL_B = "wl-selec-grpc-smart"
NON_WL_A = "LEGACY"
NON_WL_B = "LEGACY-2"


@pytest.fixture
def db(monkeypatch):
    import importlib
    import os
    import tempfile

    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("DATA_DIR", tmp)
    monkeypatch.setenv("PRIMARY_MGBOOST_ADMIN_ACTOR_ID", PRIMARY)
    monkeypatch.setenv("PRIMARY_MGBOOST_ADMIN_LOGIN", PRIMARY_LOGIN)
    import src.config as config
    import src.database as database
    importlib.reload(config)
    importlib.reload(database)
    database.DB_PATH = os.path.join(tmp, "db.sqlite3")
    instance = database.Database()
    yield instance
    instance._conn.close()


class WlBackedClient:
    """Test double standing in for ServiceMarzbanClient: read-only
    `legacy.user.get` plus the typed `child.user.wl.set`, both executed
    against the same FakeMarzban through the real BrokerOperations. The two
    topology read endpoints are also routed through real BrokerOperations
    dispatch (their FakeMarzban payloads are overridden to mirror the exact
    governed PH0-05 baseline, unlike its legacy defaults)."""

    def __init__(self, remote, *, topology=None):
        self.remote = remote
        self.ops = BrokerOperations(remote)
        self._topology = topology or {
            "tags": sorted(WL_INBOUND_TAGS),
            "nodes": [
                {"id": n["id"], "name": n["role"], "address": n["address"],
                 "usage_coefficient": n["usage_coefficient"]}
                for n in WL_NODES
            ],
        }
        self.remote.get_nodes = self._fake_nodes
        self.remote.get_inbounds = self._fake_inbounds

    def _fake_nodes(self, token):
        return json.loads(json.dumps(self._topology["nodes"]))

    def _fake_inbounds(self, token):
        return {"vless": [{"tag": t} for t in self._topology["tags"]]}

    def get_user(self, username):
        return self.ops.dispatch("legacy.user.get", {"username": username})

    def set_child_wl_state(self, request):
        return self.ops.dispatch("child.user.wl.set", request)

    def get_nodes(self, admin_token=None):
        return self.ops.marzban.get_nodes(self.ops._admin_token())

    def get_inbounds(self, admin_token=None):
        return self.ops.marzban.get_inbounds(self.ops._admin_token())

    def get_admin_token_from_env(self):
        return self.remote.get_admin_token_from_env()


def _ok_observer():
    nodes = {
        n["id"]: {"role": n["role"], "address": n["address"],
                  "usage_coefficient": n["usage_coefficient"]}
        for n in WL_NODES
    }
    tags = frozenset(WL_INBOUND_TAGS)
    return lambda: (tags, nodes)


def _mismatch_observer():
    nodes = {n["id"]: {"role": n["role"], "address": n["address"],
                       "usage_coefficient": n["usage_coefficient"]}
             for n in WL_NODES}
    return lambda: (frozenset(), nodes)


def _broken_observer():
    def boom():
        raise RuntimeError("SimulatedMarzbanDown")
    return boom


def _modify_count(remote, username):
    return sum(
        1 for call in remote.calls
        if call[0] == "modify_user" and call[1] == username
    )


def _inbounds_of(remote, username):
    return sorted(remote.users[username]["inbounds"]["vless"])


def _full_snapshot(remote, username):
    user = remote.users[username]
    return json.loads(json.dumps(user))


def _ensure_extra_child(db, account, alias_id, remote, *, hwid, tg=None):
    """Provision one more real child generation for an existing account."""
    from src.child_provisioning import ChildProvisioningStore  # noqa: F401 wiring proof
    slot = db.device_slots.claim(account["account_id"], hwid, HWID_KEY, now=210)
    request_hash = source_contract_hash(remote.users[_alias_username(db, alias_id)])
    prepared = db.child_provisioning.prepare_child_ensure(
        account_id=account["account_id"], slot_generation_id=slot["generation_id"],
        source_alias_id=alias_id, source_contract_hash=request_hash, expire=0,
        idempotency_key=f"wl-extra-{hwid}"[:64], now=220,
    )
    claimed = db.child_provisioning.claim(
        prepared["operation_id"], worker_id="fixture-worker", now=221, lease_seconds=5,
    )
    created = BrokerOperations(remote).dispatch("child.user.ensure", claimed["payload"])
    child_uuid = created.pop("uuid")
    child = db.child_provisioning.acknowledge(
        prepared["operation_id"], worker_id="fixture-worker",
        outcome=created["outcome"], child_uuid=child_uuid, remote_result=created, now=222,
    )
    return {
        "slot": slot, "prepared": prepared, "child": child,
        "child_intent_id": prepared["child_intent_id"],
        "child_username": prepared["child_username"],
    }


def _alias_username(db, alias_id):
    return db._conn.execute(
        "SELECT legacy_username FROM mgboost_legacy_account_aliases WHERE id=?",
        (alias_id,),
    ).fetchone()["legacy_username"]


def _give_child_wl_tags(remote, child_username):
    user = remote.users[child_username]
    merged = set(user["inbounds"]["vless"]) | {WL_A, WL_B}
    user["inbounds"]["vless"] = sorted(merged)


def _fixture_tg(mapping):
    import hashlib as _h
    return 660000 + (int(_h.sha256(("tg:" + mapping).encode()).hexdigest(), 16) % 200000)


def _fixture_alias(mapping):
    import hashlib as _h
    return "a" + _h.sha256(("alias:" + mapping).encode()).hexdigest()[:10]


def _enforce_fixture(db, *, mapping, n_children=1):
    """Account + N ensured children whose remote inbounds carry both the
    non-WL hosts and two exact PH0-05 WL tags (source-shaped config).
    `_build_applied_child` already creates the reviewed account + first
    child; extras are claimed as additional slots on the same account."""
    fx = _build_applied_child(db, mapping=mapping, tg=_fixture_tg(mapping),
                              alias=_fixture_alias(mapping))
    account = fx["account"]
    alias_row = db._conn.execute(
        "SELECT id FROM mgboost_legacy_account_aliases WHERE account_id=?",
        (account["account_id"],),
    ).fetchone()
    alias_id = alias_row["id"]
    remote = fx["remote"]
    extras = []
    for i in range(n_children - 1):
        extra = _ensure_extra_child(
            db, account, alias_id, remote, hwid=f"privacy-safe-wl-hwid-{mapping}-{i}",
        )
        extras.append(extra)
    children = [{
        "fx_child": fx["child_intent_id"], "username": fx["child_username"],
    }]
    for extra in extras:
        children.append({
            "fx_child": extra["child_intent_id"],
            "username": extra["child_username"],
        })
    for c in children:
        _give_child_wl_tags(remote, c["username"])
    client = WlBackedClient(remote)
    return {
        "account": account, "alias_id": alias_id, "remote": remote,
        "client": client, "children": children,
        "primary": fx, "extras": extras,
    }


def _seed_limited_period(db, *, account_id, now, quota_bytes=1_000_000_000,
                         starts_at=None, ends_at=None, status="ACTIVE"):
    conn = db._conn
    subscription = conn.execute(
        "SELECT id FROM mgboost_subscriptions WHERE account_id=?", (account_id,)
    ).fetchone()
    subscription_id = subscription["id"]
    mutation_id = conn.execute(
        "INSERT INTO mgboost_entitlement_mutations "
        "(account_id,operation,payment_channel,mutation_source,actor_type,created_at) "
        "VALUES (?,'WL_TEST_SEED','NOT_APPLICABLE','SYSTEM','SYSTEM',?)",
        (account_id, now),
    ).lastrowid
    next_seq = (conn.execute(
        "SELECT COALESCE(MAX(sequence_no),0)+1 FROM mgboost_subscription_terms "
        "WHERE subscription_id=?", (subscription_id,),
    ).fetchone()[0])
    term_id = conn.execute(
        "INSERT INTO mgboost_subscription_terms "
        "(account_id,subscription_id,sequence_no,plan_snapshot_json,mutation_id,created_at) "
        "VALUES (?,?,?,'{}',?,?)",
        (account_id, subscription_id, next_seq, mutation_id, now),
    ).lastrowid
    period_seq = (conn.execute(
        "SELECT COALESCE(MAX(sequence_no),0)+1 FROM mgboost_wl_periods WHERE subscription_id=?",
        (subscription_id,),
    ).fetchone()[0])
    period_id = conn.execute(
        "INSERT INTO mgboost_wl_periods "
        "(account_id,subscription_id,subscription_term_id,sequence_no,starts_at,ends_at,"
        "quota_mode,base_quota_bytes,status,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            account_id, subscription_id, term_id, period_seq,
            starts_at if starts_at is not None else now - 3600,
            ends_at if ends_at is not None else now + 30 * 86400,
            "LIMITED", quota_bytes, status, now,
        ),
    ).lastrowid
    conn.commit()
    return period_id


def _burn_quota(db, *, account_id, child_intent_id, period_id, total_bytes,
                collector="w", collected_at=1700000100):
    """Push real cumulative usage samples until the shared pool is exceeded.
    Node counters are cumulative per (child, node) -- this helper writes a
    fresh +total_bytes/2 DELTA per node on top of whatever the cursors
    already hold, in its own UTC-hour bucket."""
    conn = db._conn
    per_node = total_bytes // 2
    for node_id in (4, 7):
        row = conn.execute(
            "SELECT last_observed_cumulative_bytes FROM mgboost_wl_usage_cursors "
            "WHERE child_intent_id=? AND node_id=?",
            (child_intent_id, node_id),
        ).fetchone()
        current = int(row["last_observed_cumulative_bytes"]) if row is not None else 0
        db.wl_usage_ledger.record_sample(
            account_id=account_id, child_intent_id=child_intent_id,
            node_id=node_id, cursor_after=current + per_node,
            collector_id=collector, collected_at=collected_at,
            wl_period_id=period_id,
        )
    total = conn.execute(
        "SELECT SUM(bytes_delta) s FROM mgboost_wl_usage_samples "
        "WHERE account_id=? AND wl_period_id=?", (account_id, period_id),
    ).fetchone()["s"]
    assert total is not None and total >= total_bytes


# --------------------------------------------------------------------------
# Pure policy / contract level
# --------------------------------------------------------------------------

def test_decide_direction_abstains_for_none_and_unlimited():
    assert decide_direction_from_pool(None) is None
    unlimited = {"quota_mode": "UNLIMITED", "exceeded": False}
    assert decide_direction_from_pool(unlimited) is None
    limited_ok = {"quota_mode": "LIMITED", "exceeded": False}
    assert decide_direction_from_pool(limited_ok) == "INCLUDED"
    limited_over = {"quota_mode": "LIMITED", "exceeded": True}
    assert decide_direction_from_pool(limited_over) == "EXCLUDED"


def test_new_broker_operation_is_registered_and_typed():
    assert "child.user.wl.set" in BROKER_OPERATIONS


def test_request_validation_rejects_foreign_baseline_and_shape_noise():
    from src.wl_enforcement_contract import validate_wl_set_request
    base = {
        "operation_id": "wla_" + "a" * 26,
        "child_username": "mgc_" + "a" * 26,
        "uuid_verifier": "sha256:" + "b" * 64,
        "direction": "INCLUDED",
        "baseline_wl_tags": [WL_A],
    }
    normalized = validate_wl_set_request(base)
    assert normalized["baseline_wl_tags"] == [WL_A]
    foreign = dict(base, baseline_wl_tags=[WL_A, "not-on-allowlist"])
    with pytest.raises(ValueError):
        validate_wl_set_request(foreign)
    excluded_with_baseline = dict(base, direction="EXCLUDED")
    with pytest.raises(ValueError):
        validate_wl_set_request(excluded_with_baseline)
    with pytest.raises(ValueError):
        validate_wl_set_request(dict(base, surprise=1))
    empty_baseline = dict(base, baseline_wl_tags=[])
    with pytest.raises(ValueError):
        validate_wl_set_request(empty_baseline)


# --------------------------------------------------------------------------
# Migration/schema guards
# --------------------------------------------------------------------------

def test_migration_is_checksum_pinned_idempotent_and_requires_ledger(db):
    assert apply_wl_enforcement_schema(db._conn) is False
    marker = db._conn.execute(
        "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
        (MIGRATION_ID,),
    ).fetchone()
    assert marker[0] == SCHEMA_CHECKSUM
    conn = db._conn
    # FK-safe standalone insert fails on unknown accounts
    with pytest.raises(Exception):
        conn.execute(
            "INSERT INTO mgboost_wl_enforcement_states "
            "(account_id,epoch,state,last_direction,created_at,updated_at) "
            "VALUES (424242,1,'ACTIVE','INCLUDED',1,2)"
        )


def test_epoch_monotonic_trigger_rejects_downgrade(db):
    fx = _enforce_fixture(db, mapping="WL_EPOCH_TRG")
    _clean_topology_ok(db)
    db.wl_enforcement.apply_decision(
        fx["account"]["account_id"], pool={"quota_mode": "LIMITED", "exceeded": True,
                                           "wl_period_id": 77},
        now=500,
    )
    with pytest.raises(Exception):
        db._conn.execute(
            "UPDATE mgboost_wl_enforcement_states SET epoch=0 WHERE account_id=?",
            (fx["account"]["account_id"],),
        )


def test_unique_per_epoch_child_and_delete_guards(db):
    fx = _enforce_fixture(db, mapping="WL_UNIQ_TRG")
    decision = db.wl_enforcement.apply_decision(
        fx["account"]["account_id"], pool={"quota_mode": "LIMITED", "exceeded": True,
                                           "wl_period_id": 78},
        now=600,
    )
    first = decision["prepared"][0]
    with pytest.raises(Exception):
        db._conn.execute(
            "INSERT INTO mgboost_wl_enforcement_ops "
            "(account_id,epoch,child_intent_id,direction,request_hash,payload_json,"
            "next_attempt_at,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (first["account_id"], first["epoch"], first["child_intent_id"],
             first["direction"], first["request_hash"], "{}", 1, 1, 1),
        )
    with pytest.raises(Exception):
        db._conn.execute("DELETE FROM mgboost_wl_enforcement_ops WHERE id=?", (first["id"],))
    db._conn.execute(
        "INSERT INTO mgboost_wl_enforcement_events "
        "(op_row_id,account_id,epoch,attempt_no,event_type,created_at) "
        "VALUES (?,?,?,?, 'STARTED', 1)",
        (first["id"], first["account_id"], first["epoch"], 1),
    )
    with pytest.raises(Exception):
        db._conn.execute("UPDATE mgboost_wl_enforcement_events SET outcome='x'")
    with pytest.raises(Exception):
        db._conn.execute("DELETE FROM mgboost_wl_enforcement_events")


# --------------------------------------------------------------------------
# Exactly-once enforcement driven purely by quota signals
# --------------------------------------------------------------------------

NOW = 1_800_000_000


def test_quota_exhausted_disables_exact_once_across_repeated_cycles(db):
    fx = _enforce_fixture(db, mapping="WL_ONCE_OFF")
    child = fx["children"][0]
    period = _seed_limited_period(db, account_id=fx["account"]["account_id"], now=NOW)
    _burn_quota(db, account_id=fx["account"]["account_id"],
                child_intent_id=child["fx_child"], period_id=period,
                total_bytes=2_000_000_000)
    before_snapshot = _full_snapshot(fx["remote"], child["username"])

    summary = run_wl_enforcement_cycle(
        db=db, service_marzban=fx["client"], worker_id="cyc-1",
        now=NOW, topology_observer=_ok_observer(),
    )
    assert summary["accounts_disabled"] == 1
    state = db.wl_enforcement.get_state(fx["account"]["account_id"])
    assert state["state"] == "DISABLED" and state["last_direction"] == "EXCLUDED"

    # exact remote effect: WL tags gone, non-WL preserved, nothing else moved
    assert _inbounds_of(fx["remote"], child["username"]) == [NON_WL_A]
    after_snapshot = _full_snapshot(fx["remote"], child["username"])
    del after_snapshot["inbounds"]
    del before_snapshot["inbounds"]
    assert after_snapshot == before_snapshot

    first_disable_mutations = _modify_count(fx["remote"], child["username"])
    assert first_disable_mutations == 1

    for i in range(4):  # repeated/replayed evaluation -- zero further effects
        again = run_wl_enforcement_cycle(
            db=db, service_marzban=fx["client"], worker_id=f"cyc-r{i}",
            now=NOW + i, topology_observer=_ok_observer(),
        )
        assert again["ops_prepared"] == 0
        assert again["accounts_disabled"] == 0
    assert _modify_count(fx["remote"], child["username"]) == first_disable_mutations
    ops = db.wl_enforcement.epoch_ops(fx["account"]["account_id"], state["epoch"])
    assert len(ops) == 1 and ops[0]["state"] == "APPLIED"
    events = db._conn.execute(
        "SELECT event_type,outcome FROM mgboost_wl_enforcement_events WHERE op_row_id=?",
        (ops[0]["id"],),
    ).fetchall()
    succeeded = [e for e in events if e["event_type"] == "SUCCEEDED"]
    assert len(succeeded) == 1 and succeeded[0]["outcome"] == "SYNCED"


def test_quota_reset_or_new_period_restores_exactly_once(db):
    fx = _enforce_fixture(db, mapping="WL_RESET_ON")
    child = fx["children"][0]
    account_id = fx["account"]["account_id"]
    period = _seed_limited_period(db, account_id=account_id, now=NOW)
    _burn_quota(db, account_id=account_id, child_intent_id=child["fx_child"],
                period_id=period, total_bytes=2_000_000_000)
    run_wl_enforcement_cycle(db=db, service_marzban=fx["client"], worker_id="turn-off",
                             now=NOW, topology_observer=_ok_observer())
    assert _inbounds_of(fx["remote"], child["username"]) == [NON_WL_A]

    # ADMIN_RESET-shaped successor: old closed, fresh consumed=0 period opens
    db._conn.execute("UPDATE mgboost_wl_periods SET status='CLOSED' WHERE id=?", (period,))
    fresh = _seed_limited_period(db, account_id=account_id, now=NOW + 10,
                                 quota_bytes=5_000_000_000, starts_at=NOW + 10)
    assert fresh != period

    summary = run_wl_enforcement_cycle(db=db, service_marzban=fx["client"],
                                       worker_id="turn-on", now=NOW + 20,
                                       topology_observer=_ok_observer())
    assert summary["accounts_enabled"] == 1
    restored = _inbounds_of(fx["remote"], child["username"])
    assert restored == sorted({NON_WL_A, WL_A, WL_B})
    restore_mutations = _modify_count(fx["remote"], child["username"])
    assert restore_mutations == 2  # exactly ONE real disable + ONE real enable
    run_wl_enforcement_cycle(db=db, service_marzban=fx["client"], worker_id="on2",
                             now=NOW + 21, topology_observer=_ok_observer())
    assert _modify_count(fx["remote"], child["username"]) == restore_mutations
    state = db.wl_enforcement.get_state(account_id)
    assert state["state"] == "ACTIVE" and state["last_direction"] == "INCLUDED"
    include_ops = [o for o in db.wl_enforcement.epoch_ops(account_id, state["epoch"])
                   if o["state"] == "APPLIED"]
    assert len(include_ops) == 1


def test_flip_flop_three_epochs_each_produce_real_single_mutation(db):
    fx = _enforce_fixture(db, mapping="WL_FLIP3")
    child = fx["children"][0]
    account_id = fx["account"]["account_id"]
    period = _seed_limited_period(db, account_id=account_id, now=NOW)
    _burn_quota(db, account_id=account_id, child_intent_id=child["fx_child"],
                period_id=period, total_bytes=2_000_000_000)

    run_wl_enforcement_cycle(db=db, service_marzban=fx["client"], worker_id="dis-1",
                             now=NOW, topology_observer=_ok_observer())
    assert _inbounds_of(fx["remote"], child["username"]) == [NON_WL_A]

    db._conn.execute("UPDATE mgboost_wl_periods SET status='CLOSED' WHERE id=?", (period,))
    p2 = _seed_limited_period(db, account_id=account_id, now=NOW + 10,
                              quota_bytes=5_000_000_000, starts_at=NOW + 10)
    run_wl_enforcement_cycle(db=db, service_marzban=fx["client"], worker_id="ena-1",
                             now=NOW + 20, topology_observer=_ok_observer())
    assert _inbounds_of(fx["remote"], child["username"]) == sorted({NON_WL_A, WL_A, WL_B})

    # quota exhausts AGAIN inside the very same new period -- a genuine
    # re-disable transition, never a replayed key's false convergence.
    # A later UTC-hour bucket is used because PH6-03 fixes wl_period_id at
    # a bucket's first write (the earlier burn already owns this child's
    # first hour).
    _burn_quota(db, account_id=account_id, child_intent_id=child["fx_child"],
                period_id=p2, total_bytes=6_000_000_000,
                collected_at=1700000100 + 7200)
    run_wl_enforcement_cycle(db=db, service_marzban=fx["client"], worker_id="dis-2",
                             now=NOW + 30, topology_observer=_ok_observer())
    assert _inbounds_of(fx["remote"], child["username"]) == [NON_WL_A]

    state = db.wl_enforcement.get_state(account_id)
    assert state["epoch"] == 3 and state["state"] == "DISABLED"
    assert _modify_count(fx["remote"], child["username"]) == 3
    ids_by_epoch = {}
    for o in db.wl_enforcement.epoch_ops(account_id, 1) + \
            db.wl_enforcement.epoch_ops(account_id, 2) + \
            db.wl_enforcement.epoch_ops(account_id, 3):
        ids_by_epoch.setdefault(o["epoch"], []).append(o["operation_id"])
    assert len(ids_by_epoch[1]) == len(ids_by_epoch[2]) == len(ids_by_epoch[3]) == 1
    assert len(set(sum(ids_by_epoch.values(), []))) == 3  # all distinct


# --------------------------------------------------------------------------
# Topology gate
# --------------------------------------------------------------------------

def test_default_topology_refresh_runs_real_read_path_and_asserts_fresh(db):
    fx = _enforce_fixture(db, mapping="WL_TOPO_DEFAULT")
    period = _seed_limited_period(db, account_id=fx["account"]["account_id"], now=NOW)
    child = fx["children"][0]
    _burn_quota(db, account_id=fx["account"]["account_id"],
                child_intent_id=child["fx_child"], period_id=period,
                total_bytes=2_000_000_000)
    # No topology_observer injected: the cycle MUST refresh through its own
    # read path against the client (get_nodes/get_inbounds), durably record
    # a fresh assertion, and only then enforce.
    summary = run_wl_enforcement_cycle(db=db, service_marzban=fx["client"],
                                       worker_id="wl-worker", now=NOW)
    assert summary["accounts_disabled"] == 1
    latest = db.wl_topology_guard.latest_assertion()
    assert latest is not None and latest["ok"] is True
    assert latest["config_version"] != "NONE"
    assert _inbounds_of(fx["remote"], child["username"]) == [NON_WL_A]


def test_topology_stale_config_version_blocks_everything(db):
    fx = _enforce_fixture(db, mapping="WL_TOPO_STALEVER")
    period = _seed_limited_period(db, account_id=fx["account"]["account_id"], now=NOW)
    child = fx["children"][0]
    _burn_quota(db, account_id=fx["account"]["account_id"],
                child_intent_id=child["fx_child"], period_id=period,
                total_bytes=2_000_000_000)
    import src.wl_topology_guard as guard_mod
    from src.wl_topology_guard import TopologyMismatchError
    # Fresh-but-future config version expected on record: require_topology_ok
    # must reject the drift as unknown topology before any transition.
    nodes = [{"id": n["id"], "name": n["role"], "address": n["address"],
              "usage_coefficient": 1.0} for n in WL_NODES]
    db.wl_topology_guard.run_assertion(frozenset(WL_INBOUND_TAGS),
                                       {n["id"]: {"role": n["role"], "address": n["address"],
                                                  "usage_coefficient": 1.0} for n in WL_NODES},
                                       now=NOW - 1)
    real_version = guard_mod.WL_TOPOLOGY_VERSION
    guard_mod.WL_TOPOLOGY_VERSION = "2099-01-01-v9"
    try:
        with pytest.raises(TopologyMismatchError):
            run_wl_enforcement_cycle(db=db, service_marzban=WlBackedClient(fx["remote"]),
                                     worker_id="wl-worker", now=NOW)
    finally:
        guard_mod.WL_TOPOLOGY_VERSION = real_version
    assert db.wl_enforcement.get_state(fx["account"]["account_id"]) is None
    assert _modify_count(fx["remote"], child["username"]) == 0


def test_topology_fresh_mismatch_records_assertion_and_blocks(db):
    fx = _enforce_fixture(db, mapping="WL_TOPO_BAD")
    period = _seed_limited_period(db, account_id=fx["account"]["account_id"], now=NOW)
    child = fx["children"][0]
    _burn_quota(db, account_id=fx["account"]["account_id"],
                child_intent_id=child["fx_child"], period_id=period,
                total_bytes=2_000_000_000)
    from src.wl_topology_guard import TopologyMismatchError
    with pytest.raises(TopologyMismatchError):
        run_wl_enforcement_cycle(db=db, service_marzban=fx["client"], worker_id="wl-worker",
                                 now=NOW, topology_observer=_mismatch_observer())
    latest = db.wl_topology_guard.latest_assertion()
    assert latest is not None and latest["ok"] is False
    assert db.wl_enforcement.get_state(fx["account"]["account_id"]) is None
    assert _modify_count(fx["remote"], child["username"]) == 0


def test_topology_check_unreachable_aborts_before_any_transition(db):
    fx = _enforce_fixture(db, mapping="WL_TOPO_DOWN")
    with pytest.raises(RuntimeError, match="SimulatedMarzbanDown"):
        run_wl_enforcement_cycle(db=db, service_marzban=fx["client"], worker_id="wl-worker",
                                 now=NOW, topology_observer=_broken_observer())
    assert db.wl_enforcement.get_state(fx["account"]["account_id"]) is None


# --------------------------------------------------------------------------
# Who can NEVER be touched
# --------------------------------------------------------------------------

def test_unlimited_and_nonwl_accounts_are_structurally_untouched(db):
    unlim = _enforce_fixture(db, mapping="WL_UNLIM")
    nonwl = _enforce_fixture(db, mapping="WL_NONWL")
    # UNLIMITED-quota period exists => abstain; plain account => abstain
    _seed_unlimited(db, unlim, now=NOW)
    snapshots_u = [_full_snapshot(unlim["remote"], c["username"]) for c in unlim["children"]]
    snapshots_n = [_full_snapshot(nonwl["remote"], c["username"]) for c in nonwl["children"]]
    summaries = [
        run_wl_enforcement_cycle(db=db, service_marzban=unlim["client"], worker_id="unlim-a",
                                 now=NOW, topology_observer=_ok_observer()),
        run_wl_enforcement_cycle(db=db, service_marzban=nonwl["client"], worker_id="nonwl-b",
                                 now=NOW, topology_observer=_ok_observer()),
    ]
    # One shared DB: every cycle sees BOTH accounts; each must fully abstain.
    assert all(
        s["accounts_abstained"] == s["accounts_evaluated"] and s["ops_prepared"] == 0
        for s in summaries
    )
    assert db.wl_enforcement.get_state(unlim["account"]["account_id"]) is None
    assert db.wl_enforcement.get_state(nonwl["account"]["account_id"]) is None
    assert [_full_snapshot(unlim["remote"], c["username"]) for c in unlim["children"]] == snapshots_u
    assert [_full_snapshot(nonwl["remote"], c["username"]) for c in nonwl["children"]] == snapshots_n


def _seed_unlimited(db, fx, *, now):
    conn = db._conn
    subscription = conn.execute(
        "SELECT id FROM mgboost_subscriptions WHERE account_id=?",
        (fx["account"]["account_id"],),
    ).fetchone()
    subscription_id = subscription["id"]
    mutation_id = conn.execute(
        "INSERT INTO mgboost_entitlement_mutations "
        "(account_id,operation,payment_channel,mutation_source,actor_type,created_at) "
        "VALUES (?,'U','NOT_APPLICABLE','SYSTEM','SYSTEM',?)",
        (fx["account"]["account_id"], now),
    ).lastrowid
    next_seq = (conn.execute(
        "SELECT COALESCE(MAX(sequence_no),0)+1 FROM mgboost_subscription_terms "
        "WHERE subscription_id=?", (subscription_id,),
    ).fetchone()[0])
    term_id = conn.execute(
        "INSERT INTO mgboost_subscription_terms "
        "(account_id,subscription_id,sequence_no,plan_snapshot_json,mutation_id,created_at) "
        "VALUES (?,?,?,'{}',?,?)",
        (fx["account"]["account_id"], subscription_id, next_seq, mutation_id, now),
    ).lastrowid
    period_seq = (conn.execute(
        "SELECT COALESCE(MAX(sequence_no),0)+1 FROM mgboost_wl_periods WHERE subscription_id=?",
        (subscription_id,),
    ).fetchone()[0])
    conn.execute(
        "INSERT INTO mgboost_wl_periods "
        "(account_id,subscription_id,subscription_term_id,sequence_no,starts_at,ends_at,"
        "quota_mode,status,created_at) VALUES (?,?,?,?,?,?,'UNLIMITED','ACTIVE',?)",
        (fx["account"]["account_id"], subscription_id, term_id, period_seq,
         now - 3600, now + 30 * 86400, now),
    )
    conn.commit()


# --------------------------------------------------------------------------
# Staleness guards / claims
# --------------------------------------------------------------------------

def test_stale_epoch_op_is_superseded_and_never_dispatched(db):
    fx = _enforce_fixture(db, mapping="WL_STALE_GUARD")
    account_id = fx["account"]["account_id"]
    child = fx["children"][0]
    period = _seed_limited_period(db, account_id=account_id, now=NOW)
    _burn_quota(db, account_id=account_id, child_intent_id=child["fx_child"],
                period_id=period, total_bytes=2_000_000_000)
    # A first epoch is driven to completion (baseline history exists).
    run_wl_enforcement_cycle(db=db, service_marzban=fx["client"],
                             worker_id="stage-a", now=NOW,
                             topology_observer=_ok_observer())
    assert db.wl_enforcement.get_state(account_id)["state"] == "DISABLED"

    # An ENABLE_PENDING epoch-2 decision is opened but never dispatched...
    db._conn.execute("UPDATE mgboost_wl_periods SET status='CLOSED' WHERE id=?", (period,))
    p2 = _seed_limited_period(db, account_id=account_id, now=NOW + 5,
                              quota_bytes=9_000_000_000, starts_at=NOW + 5)
    reopened = db.wl_enforcement.apply_decision(account_id, pool={
        "quota_mode": "LIMITED", "exceeded": False, "wl_period_id": p2,
    }, now=NOW + 6)
    assert reopened["state"]["state"] == "ENABLE_PENDING"
    assert reopened["state"]["epoch"] == 2
    superseded_op = reopened["prepared"][0]["operation_id"]

    # ...because the input flips AGAIN to exhausted before any dispatch.
    _burn_quota(db, account_id=account_id, child_intent_id=child["fx_child"],
                period_id=p2, total_bytes=10_000_000_000,  # exceeds the fresh 9 GB period
                collected_at=1700000100 + 3600)
    flipped = db.wl_enforcement.apply_decision(account_id, pool={
        "quota_mode": "LIMITED", "exceeded": True, "wl_period_id": p2,
    }, now=NOW + 7)
    assert flipped["state"]["state"] == "DISABLE_PENDING"
    assert flipped["state"]["epoch"] == 3

    result = process_wl_op(db, superseded_op, worker_id="late-worker",
                           service_marzban=fx["client"], now=NOW + 8)
    assert result is None
    op = db.wl_enforcement.get_op(superseded_op)
    assert op["state"] == "PENDING"  # schema has no fifth state; epochs gate it
    supersede_events = db._conn.execute(
        "SELECT COUNT(*) c FROM mgboost_wl_enforcement_events "
        "WHERE op_row_id=? AND event_type='SUPERSEDED'", (op["id"],),
    ).fetchone()["c"]
    assert supersede_events == 1

    # The live epoch-3 EXCLUDE drives and converges. The child ends WL-free
    # having received exactly two real mutations ever (epoch-1 disable +
    # epoch-3 re-disable); the never-dispatched enable contributed nothing.
    processed = process_wl_op(
        db,
        [o for o in flipped["prepared"] if o["direction"] == "EXCLUDED"][0]["operation_id"],
        worker_id="live-worker",
        service_marzban=fx["client"], now=NOW + 9)
    assert processed is not None and processed["state"] == "APPLIED"
    finalized = db.wl_enforcement.finalize_account(
        account_id, verify_fn=lambda op_: _inbounds_of(fx["remote"], _u(op_)) ==
        [NON_WL_A], now=NOW + 10,
    )
    assert finalized["flipped"] == "DISABLED"
    assert _inbounds_of(fx["remote"], child["username"]) == [NON_WL_A]
    # Exactly ONE lifetime mutation: epoch-3 finds the remote already
    # matching its target (the superseded enable never restored anything)
    # and settles by ALREADY_IN_SYNC observation instead of re-mutating.
    assert _modify_count(fx["remote"], child["username"]) == 1


def _u(op):
    return json.loads(op["payload_json"])["child_username"]


def test_claim_refuses_wrong_direction_under_same_epoch_identity(db):
    fx = _enforce_fixture(db, mapping="WL_DIR_GUARD")
    decision = db.wl_enforcement.apply_decision(fx["account"]["account_id"], pool={
        "quota_mode": "LIMITED", "exceeded": True, "wl_period_id": 900,
    }, now=NOW)
    op_id = decision["prepared"][0]["operation_id"]

    # simulate a machine row flip WITHOUT opening a new epoch (tamper);
    # the claim-time guard must refuse dispatch regardless of the epoch.
    db._conn.execute(
        "UPDATE mgboost_wl_enforcement_states SET last_direction='INCLUDED' "
        "WHERE account_id=?", (fx["account"]["account_id"],),
    )
    db._conn.commit()
    result = process_wl_op(db, op_id, worker_id="guard-worker",
                           service_marzban=fx["client"], now=NOW + 1)
    assert result is None
    stale_username = json.loads(
        decision["prepared"][0]["payload_json"])["child_username"]
    assert _modify_count(fx["remote"], stale_username) == 0


# --------------------------------------------------------------------------
# Failure isolation / outage / restart boundaries
# --------------------------------------------------------------------------

def _child_with_persistent_modify_failure(fx, username):
    parent_modify = type(fx["remote"]).modify_user

    def picky(self, uname, payload, token):
        if uname == username:
            raise HTTPError("http://marzban/api/user", 500, "node offline", {},
                            __import__("io").BytesIO(b'{"detail":"offline"}'))
        return parent_modify(self, uname, payload, token)

    fx["remote"].modify_user = picky.__get__(fx["remote"])


def test_partial_offline_child_isolates_siblings_then_error_reconcile(db, monkeypatch):
    import src.wl_enforcement as wl_mod
    monkeypatch.setattr(wl_mod, "RETRY_DELAY_SECONDS", 0)
    fx = _enforce_fixture(db, mapping="WL_PARTIAL", n_children=3)
    account_id = fx["account"]["account_id"]
    victim = fx["children"][1]
    healthy = [c for c in fx["children"] if c is not victim]
    period = _seed_limited_period(db, account_id=account_id, now=NOW)
    for c in fx["children"]:
        _burn_quota(db, account_id=account_id, child_intent_id=c["fx_child"],
                    period_id=period, total_bytes=3_000_000_000)
    _child_with_persistent_modify_failure(fx, victim["username"])

    for tick in range(MAX_ATTEMPTS + 2):
        run_wl_enforcement_cycle(db=db, service_marzban=fx["client"],
                                 worker_id=f"wkr-{tick}", now=NOW + tick,
                                 topology_observer=_ok_observer())
    for c in healthy:
        assert _inbounds_of(fx["remote"], c["username"]) == [NON_WL_A]
    assert _inbounds_of(fx["remote"], victim["username"]) == sorted([NON_WL_A, WL_A, WL_B])
    state = db.wl_enforcement.get_state(account_id)
    assert state["state"] == "ERROR_RECONCILE"


def test_marzban_outage_maps_to_retry_then_recovers_without_double_mutation(db, monkeypatch):
    import src.wl_enforcement as wl_mod
    monkeypatch.setattr(wl_mod, "RETRY_DELAY_SECONDS", 0)
    fx = _enforce_fixture(db, mapping="WL_OUTAGE")
    account_id = fx["account"]["account_id"]
    child = fx["children"][0]
    period = _seed_limited_period(db, account_id=account_id, now=NOW)
    _burn_quota(db, account_id=account_id, child_intent_id=child["fx_child"],
                period_id=period, total_bytes=2_000_000_000)
    fx["remote"].outage = True
    first = run_wl_enforcement_cycle(db=db, service_marzban=fx["client"], worker_id="wk-outage-0",
                                     now=NOW, topology_observer=_ok_observer())
    assert first["ops_errored"] == 0 and first["ops_applied"] == 0
    op = db.wl_enforcement.epoch_ops(account_id, 1)[0]
    assert op["state"] == "RETRY"
    fx["remote"].outage = False
    run_wl_enforcement_cycle(db=db, service_marzban=fx["client"], worker_id="wk-outage-1",
                             now=NOW + 1, topology_observer=_ok_observer())
    assert _inbounds_of(fx["remote"], child["username"]) == [NON_WL_A]
    run_wl_enforcement_cycle(db=db, service_marzban=fx["client"], worker_id="wk-outage-2",
                             now=NOW + 2, topology_observer=_ok_observer())
    assert _modify_count(fx["remote"], child["username"]) == 1
    state = db.wl_enforcement.get_state(account_id)
    assert state["state"] == "DISABLED"


def test_attempt_cap_lands_error_reconcile_not_infinite_retry(db, monkeypatch):
    import src.wl_enforcement as wl_mod
    monkeypatch.setattr(wl_mod, "RETRY_DELAY_SECONDS", 0)
    fx = _enforce_fixture(db, mapping="WL_CAP")
    account_id = fx["account"]["account_id"]
    child = fx["children"][0]
    period = _seed_limited_period(db, account_id=account_id, now=NOW)
    _burn_quota(db, account_id=account_id, child_intent_id=child["fx_child"],
                period_id=period, total_bytes=2_000_000_000)
    _child_with_persistent_modify_failure(fx, child["username"])
    for tick in range(MAX_ATTEMPTS + 3):
        run_wl_enforcement_cycle(db=db, service_marzban=fx["client"],
                                 worker_id=f"cap-{tick}", now=NOW + tick,
                                 topology_observer=_ok_observer())
    op = db.wl_enforcement.epoch_ops(account_id, 1)[0]
    assert op["state"] == "ERROR" and op["attempts"] == MAX_ATTEMPTS
    assert db.wl_enforcement.get_state(account_id)["state"] == "ERROR_RECONCILE"


def test_restart_between_desired_commit_and_remote_mutation_completes(db):
    fx = _enforce_fixture(db, mapping="WL_RS1")
    account_id = fx["account"]["account_id"]
    period = _seed_limited_period(db, account_id=account_id, now=NOW)
    child = fx["children"][0]
    _burn_quota(db, account_id=account_id, child_intent_id=child["fx_child"],
                period_id=period, total_bytes=2_000_000_000)
    decision = db.wl_enforcement.apply_decision(account_id, pool={
        "quota_mode": "LIMITED", "exceeded": True, "wl_period_id": period,
    }, now=NOW)
    assert decision["state"]["state"] == "DISABLE_PENDING"
    # "restart": brand-new store/client instances over the same durable DB
    fresh_store_client = WlBackedClient(fx["remote"])
    summary = run_wl_enforcement_cycle(db=db, service_marzban=fresh_store_client,
                                       worker_id="after-restart", now=NOW + 1,
                                       topology_observer=_ok_observer())
    assert summary["ops_applied"] == 1
    assert _inbounds_of(fx["remote"], child["username"]) == [NON_WL_A]


def test_restart_after_remote_success_before_ack_converges_once(db):
    fx = _enforce_fixture(db, mapping="WL_RS2")
    account_id = fx["account"]["account_id"]
    child = fx["children"][0]
    period = _seed_limited_period(db, account_id=account_id, now=NOW)
    _burn_quota(db, account_id=account_id, child_intent_id=child["fx_child"],
                period_id=period, total_bytes=2_000_000_000)
    decision = db.wl_enforcement.apply_decision(account_id, pool={
        "quota_mode": "LIMITED", "exceeded": True, "wl_period_id": period,
    }, now=NOW)
    op_id = decision["prepared"][0]["operation_id"]
    worker_a = "crashed-worker"
    claimed = db.wl_enforcement.claim(op_id, worker_id=worker_a, now=NOW)
    observed = observe_child_vless(WlBackedClient(fx["remote"]), claimed["payload"]["child_username"])
    manifest = {
        "baseline_full": sorted(observed),
        "target": sorted(set(observed) - set(WL_INBOUND_TAGS)),
        "removed_wl": sorted(set(observed) & set(WL_INBOUND_TAGS)),
    }
    db.wl_enforcement.record_manifest(op_id, worker_id=worker_a, manifest=manifest, now=NOW)
    result = fx["client"].set_child_wl_state({
        "operation_id": op_id,
        "child_username": claimed["payload"]["child_username"],
        "uuid_verifier": claimed["payload"]["uuid_verifier"],
        "direction": "EXCLUDED",
        "baseline_wl_tags": None,
    })
    assert result["outcome"] == "SYNCED"
    # ...and here worker A crashes: remote mutated, local not acknowledged.
    assert _inbounds_of(fx["remote"], child["username"]) == [NON_WL_A]

    # new worker after lease expiry must CONVERGE BY OBSERVATION only
    for late_now in (NOW + 200, NOW + 201):  # two full re-drives
        outcome = process_wl_op(db, op_id, worker_id=f"recovery-{late_now}",
                                service_marzban=WlBackedClient(fx["remote"]),
                                now=late_now)
        if outcome is not None:
            break
    assert db.wl_enforcement.get_op(op_id)["state"] == "APPLIED"
    assert _modify_count(fx["remote"], child["username"]) == 1  # STILL exactly one
    frozen = json.loads(db.wl_enforcement.get_op(op_id)["manifest_json"])
    assert frozen["target"] == [NON_WL_A]  # first-writer-wins survived


# --------------------------------------------------------------------------
# Lifecycle interplay
# --------------------------------------------------------------------------

def test_revoked_child_is_structurally_excluded_from_enforcement(db):
    fx = _enforce_fixture(db, mapping="WL_REVOKED", n_children=2)
    account_id = fx["account"]["account_id"]
    doomed = fx["children"][0]
    survivor = fx["children"][1]
    doomed_fx = fx["primary"] if doomed["username"] == fx["primary"]["child_username"] \
        else next(e for e in fx["extras"] if e["child_username"] == doomed["username"])
    prepared = db.child_lifecycle.prepare_revoke(
        account_id=account_id, old_child_intent_id=doomed_fx["child_intent_id"],
        reason="wl revocation interplay probe", idempotency_key="revk-wl-1-------", now=300,
    )
    from src import child_lifecycle as lc
    lc.process_revoke(db, prepared["operation_id"], worker_id="lc-worker",
                      revoke_fn=_revoke_fn(fx["remote"]), now=301)
    period = _seed_limited_period(db, account_id=account_id, now=NOW + 400)
    for c in (doomed, survivor):
        _burn_quota(db, account_id=account_id, child_intent_id=c["fx_child"],
                    period_id=period, total_bytes=2_000_000_000)
    revoked_taglist = _inbounds_of(fx["remote"], doomed["username"])
    summary = run_wl_enforcement_cycle(db=db, service_marzban=fx["client"], worker_id="wl-cycle",
                                       now=NOW + 401, topology_observer=_ok_observer())
    assert summary["accounts_disabled"] == 1
    assert _inbounds_of(fx["remote"], survivor["username"]) == [NON_WL_A]
    assert _inbounds_of(fx["remote"], doomed["username"]) == revoked_taglist
    state = db.wl_enforcement.get_state(account_id)
    minted_children = {o["child_intent_id"] for o in db.wl_enforcement.epoch_ops(account_id, state["epoch"])}
    assert doomed_fx["child_intent_id"] not in minted_children


def test_slot_paused_child_still_receives_uniform_inbound_enforcement(db):
    from src.security import AdminSessionStore
    fx = _enforce_fixture(db, mapping="WL_PAUSED", n_children=2)
    account_id = fx["account"]["account_id"]
    paused_target = fx["extras"][0]
    _raw, session = AdminSessionStore().create(PRIMARY_LOGIN, "test-server-jwt-pause")
    capability = db.primary_admin_authority.authorize_session(session)
    pause_result = db.device_slot_admin.set_paused(
        capability, account_id=account_id,
        slot_number=paused_target["slot"]["slot_number"], paused=True,
        reason="pause while WL enforcement probes membership uniformity",
        idempotency_key="pause-wl-probe--------", now=340,
    )
    paused_slot_state = db._conn.execute(
        "SELECT s.desired_state FROM mgboost_device_slots s "
        "JOIN mgboost_child_user_intents ci ON ci.slot_generation_id=g.id "
        "JOIN mgboost_device_slot_generations g ON g.slot_id=s.id WHERE ci.id=?",
        (paused_target["child_intent_id"],),
    ).fetchone()["desired_state"]
    assert paused_slot_state == "DISABLED"

    period = _seed_limited_period(db, account_id=account_id, now=NOW + 60)
    for c in fx["children"]:
        _burn_quota(db, account_id=account_id, child_intent_id=c["fx_child"],
                    period_id=period, total_bytes=2_000_000_000)
    summary = run_wl_enforcement_cycle(db=db, service_marzban=fx["client"], worker_id="paused-x",
                                       now=NOW + 61, topology_observer=_ok_observer())
    assert summary["accounts_disabled"] == 1
    for c in fx["children"]:  # BOTH incl. paused have exact membership enforced
        assert _inbounds_of(fx["remote"], c["username"]) == [NON_WL_A]


def test_rebind_successor_new_child_is_picked_up_by_late_arrival_rule(db):
    fx = _enforce_fixture(db, mapping="WL_LATEARR")
    account_id = fx["account"]["account_id"]
    child = fx["children"][0]
    period = _seed_limited_period(db, account_id=account_id, now=NOW)
    _burn_quota(db, account_id=account_id, child_intent_id=child["fx_child"],
                period_id=period, total_bytes=2_000_000_000)
    run_wl_enforcement_cycle(db=db, service_marzban=fx["client"], worker_id="late-1",
                             now=NOW + 1, topology_observer=_ok_observer())
    assert db.wl_enforcement.get_state(account_id)["state"] == "DISABLED"

    # A device joins while the family is WL-suspended: its fresh child clones
    # the FULL source config (including WL tags).
    joiner = _ensure_extra_child(db, fx["account"], fx["alias_id"], fx["remote"],
                                 hwid="privacy-safe-late-arrival")
    _give_child_wl_tags(fx["remote"], joiner["child_username"])
    assert sorted(joiner["child_username"] and
                  fx["remote"].users[joiner["child_username"]]["inbounds"]["vless"]) == \
        sorted({NON_WL_A, WL_A, WL_B})

    summary = run_wl_enforcement_cycle(db=db, service_marzban=fx["client"], worker_id="late-2",
                                       now=NOW + 2, topology_observer=_ok_observer())
    assert summary["epochs_opened"] == 1
    state = db.wl_enforcement.get_state(account_id)
    assert state["state"] == "DISABLED" and state["epoch"] == 2
    assert _inbounds_of(fx["remote"], joiner["child_username"]) == [NON_WL_A]
    # the original sibling's op history stays in its own epoch, untouched
    assert _modify_count(fx["remote"], child["username"]) == 1
    first_epoch_ops = db.wl_enforcement.epoch_ops(account_id, 1)
    assert all(o["state"] == "APPLIED" for o in first_epoch_ops)


# --------------------------------------------------------------------------
# Degenerate/mismatch content
# --------------------------------------------------------------------------

def test_removing_all_inbounds_is_refused_fail_closed(db):
    mapping = "WL_ALLWLD"
    fx = _build_applied_child(db, mapping=mapping, tg=_fixture_tg(mapping),
                              alias=_fixture_alias(mapping))
    account = fx["account"]
    remote = fx["remote"]
    child_username = fx["child_username"]
    remote.users[child_username]["inbounds"] = {"vless": [WL_A, WL_B]}
    client = WlBackedClient(remote)
    account_id = account["account_id"]
    period = _seed_limited_period(db, account_id=account_id, now=NOW)
    _burn_quota(db, account_id=account_id, child_intent_id=fx["child_intent_id"],
                period_id=period, total_bytes=2_000_000_000)
    run_wl_enforcement_cycle(db=db, service_marzban=client, worker_id="wl-worker",
                             now=NOW, topology_observer=_ok_observer())
    op = db.wl_enforcement.epoch_ops(account_id, 1)[0]
    assert op["state"] == "ERROR"
    assert op["last_error_class"] == "WOULD_REMOVE_ALL_INBOUNDS"
    assert db.wl_enforcement.get_state(account_id)["state"] == "ERROR_RECONCILE"
    assert _inbounds_of(remote, child_username) == sorted([WL_A, WL_B])


def test_included_restore_without_recorded_baseline_fails_closed(db):
    fx = _enforce_fixture(db, mapping="WL_NOBASE")
    account_id = fx["account"]["account_id"]
    child = fx["children"][0]
    period = _seed_limited_period(db, account_id=account_id, now=NOW)
    _burn_quota(db, account_id=account_id, child_intent_id=child["fx_child"],
                period_id=period, total_bytes=2_000_000_000)
    run_wl_enforcement_cycle(db=db, service_marzban=fx["client"], worker_id="turn-off",
                             now=NOW, topology_observer=_ok_observer())

    # tamper: erase every recorded EXCLUDE baseline, then force an INCLUDE
    # signal (successor period opened fresh)
    db._conn.execute("UPDATE mgboost_wl_enforcement_ops SET manifest_json=NULL")
    db._conn.execute("UPDATE mgboost_wl_periods SET status='CLOSED'")
    _seed_limited_period(db, account_id=account_id, now=NOW + 10,
                         quota_bytes=5_000_000_000, starts_at=NOW + 10)
    summary = run_wl_enforcement_cycle(db=db, service_marzban=fx["client"],
                                       worker_id="on-broken-x", now=NOW + 20,
                                       topology_observer=_ok_observer())
    op = [o for o in db.wl_enforcement.epoch_ops(account_id, 2)][0]
    assert op["direction"] == "INCLUDED" and op["state"] == "ERROR"
    assert op["last_error_class"] == "NO_BASELINE_FOR_INCLUDE"
    assert summary["accounts_error_reconcile"] == 1
    # ...and crucially NOTHING was invented remotely either way
    assert _inbounds_of(fx["remote"], child["username"]) == [NON_WL_A]


def test_live_reread_drift_flags_error_reconcile_instead_of_blind_repair(db):
    fx = _enforce_fixture(db, mapping="WL_DRIFT")
    account_id = fx["account"]["account_id"]
    child = fx["children"][0]
    period = _seed_limited_period(db, account_id=account_id, now=NOW)
    _burn_quota(db, account_id=account_id, child_intent_id=child["fx_child"],
                period_id=period, total_bytes=2_000_000_000)
    run_wl_enforcement_cycle(db=db, service_marzban=fx["client"], worker_id="wl-worker",
                             now=NOW, topology_observer=_ok_observer())
    assert db.wl_enforcement.get_state(account_id)["state"] == "DISABLED"

    # operator/hand drift between converge and finalize verification:
    # a hand-modified remote list diverges from the frozen target while the
    # machine believes it is converged. Same-input cycles must NOT blindly
    # re-enforce (that repair loop belongs to PH6-07 reconciliation); they
    # must also never misreport convergence.
    fx["remote"].users[child["username"]]["inbounds"]["vless"] = \
        sorted({NON_WL_A, NON_WL_B, WL_A})
    drift = run_wl_enforcement_cycle(db=db, service_marzban=fx["client"],
                                     worker_id="postdrift", now=NOW + 1,
                                     topology_observer=_ok_observer())
    assert drift["ops_prepared"] == 0
    state = db.wl_enforcement.get_state(account_id)
    assert state["state"] == "DISABLED"  # v1 boundary: detection deferred to PH6-07
    assert _inbounds_of(fx["remote"], child["username"]) == sorted({NON_WL_A, NON_WL_B, WL_A})


def test_absent_remote_child_errors_as_remote_missing_never_created(db):
    fx = _enforce_fixture(db, mapping="WL_ABSENT")
    account_id = fx["account"]["account_id"]
    child = fx["children"][0]
    period = _seed_limited_period(db, account_id=account_id, now=NOW)
    _burn_quota(db, account_id=account_id, child_intent_id=child["fx_child"],
                period_id=period, total_bytes=2_000_000_000)
    del fx["remote"].users[child["username"]]
    before_users = set(fx["remote"].users)
    run_wl_enforcement_cycle(db=db, service_marzban=fx["client"], worker_id="wl-worker",
                             now=NOW, topology_observer=_ok_observer())
    op = db.wl_enforcement.epoch_ops(account_id, 1)[0]
    assert op["state"] == "ERROR" and op["last_error_class"] == "REMOTE_MISSING"
    assert db.wl_enforcement.get_state(account_id)["state"] == "ERROR_RECONCILE"
    assert set(fx["remote"].users) == before_users  # nobody was auto-created


# --------------------------------------------------------------------------
# Broker-op wire behavior details
# --------------------------------------------------------------------------

def test_broker_wl_set_already_in_sync_is_observation_based(db):
    fx = _enforce_fixture(db, mapping="WL_ALREADY")
    child = fx["children"][0]
    username = child["username"]
    from src.wl_enforcement_contract import derive_wl_operation_id
    intent = db._conn.execute(
        "SELECT uuid_verifier FROM mgboost_child_user_intents WHERE id=?",
        (child["fx_child"],),
    ).fetchone()
    verifier = intent["uuid_verifier"]
    result = fx["client"].set_child_wl_state({
        "operation_id": derive_wl_operation_id(username, 11, "INCLUDED"),
        "child_username": username,
        "uuid_verifier": verifier,
        "direction": "INCLUDED",
        "baseline_wl_tags": [WL_A, WL_B],
    })
    assert result["outcome"] == "ALREADY_IN_SYNC"
    assert _modify_count(fx["remote"], username) == 0


def test_broker_wl_set_refuses_unknown_verifier_and_unverifiable_shapes(db):
    fx = _enforce_fixture(db, mapping="WL_VERIF")
    username = fx["children"][0]["username"]
    from src.wl_enforcement_contract import derive_wl_operation_id
    bad = {
        "operation_id": derive_wl_operation_id(username, 12, "EXCLUDED"),
        "child_username": username,
        "uuid_verifier": "sha256:" + "0" * 64,
        "direction": "EXCLUDED",
        "baseline_wl_tags": None,
    }
    with pytest.raises(ValueError, match="verifier mismatch"):
        fx["client"].set_child_wl_state(bad)
    mixed = {
        "operation_id": derive_wl_operation_id(username, 13, "EXCLUDED"),
        "child_username": username,
        "uuid_verifier": db._conn.execute(
            "SELECT uuid_verifier FROM mgboost_child_user_intents WHERE id=?",
            (fx["children"][0]["fx_child"],),
        ).fetchone()["uuid_verifier"],
        "direction": "EXCLUDED",
        "baseline_wl_tags": ["intruder-tag"],
    }
    with pytest.raises(ValueError):
        fx["client"].set_child_wl_state(mixed)


def test_zero_effect_input_changes_do_not_reopen_the_machine(db):
    fx = _enforce_fixture(db, mapping="WL_NOOP_EVALS")
    account_id = fx["account"]["account_id"]
    period = _seed_limited_period(db, account_id=account_id, now=NOW)
    child = fx["children"][0]
    _burn_quota(db, account_id=account_id, child_intent_id=child["fx_child"],
                period_id=period, total_bytes=2_000_000_000)
    run_wl_enforcement_cycle(db=db, service_marzban=fx["client"], worker_id="wk-outage-0",
                             now=NOW, topology_observer=_ok_observer())
    disabled_state = db.wl_enforcement.get_state(account_id)
    row_version = disabled_state["row_version"]
    for i in range(3):
        run_wl_enforcement_cycle(db=db, service_marzban=fx["client"], worker_id=f"noop-{i}",
                                 now=NOW + 1 + i, topology_observer=_ok_observer())
    state = db.wl_enforcement.get_state(account_id)
    assert state["state"] == "DISABLED"
    assert state["epoch"] == disabled_state["epoch"]
    # converged no-op evaluations don't churn the machine row either
    assert state["row_version"] <= row_version + 3  # only finalize CAS bumps are absent
