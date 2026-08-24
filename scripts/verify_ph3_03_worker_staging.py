#!/usr/bin/env python3
"""Real isolated Marzban 0.8.4 gate for the durable PH3-03 worker."""

from __future__ import annotations

import argparse
import hashlib
import json
import os

from src.broker_operations import BrokerOperations
from src.child_contract import source_contract_hash
from src.child_worker import ChildProvisioningWorker
from src.database import Database
from src.marzban import MarzbanClient
from src.service_marzban import ServiceMarzbanClient
from scripts.build_ph3_03_staging_xray import EFFECTIVE_VLESS_TAGS
from scripts.verify_ph3_03_marzban_staging import (
    AUTH_KEY,
    CLIENT_ID,
    EXPECTED_CHILD_USERNAME,
    EXPECTED_OPERATION_ID,
    EXPECTED_SOURCE_HASH,
    EXPECTED_VERSION,
    InstrumentedMarzban,
    _make_parent_and_slot,
    _unused_port,
    require_isolated_url,
    running_broker,
)


class Clock:
    def __init__(self, value=1_000):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


def _worker(db, service, clock, worker_id, *, mode="active", crash_hook=None):
    return ChildProvisioningWorker(
        db, service, worker_id=worker_id,
        allowed_operation_ids=[EXPECTED_OPERATION_ID], mode=mode,
        max_attempts=8, lease_seconds=5, retry_base_seconds=1,
        retry_cap_seconds=4, reconcile_interval_seconds=5,
        clock=clock, crash_hook=crash_hook,
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    args = parser.parse_args()
    base_url = require_isolated_url(args.url)
    marzban = MarzbanClient(base_url=base_url)
    token = marzban.get_admin_token_from_env()
    if marzban.get_system(token).get("version") != EXPECTED_VERSION:
        raise RuntimeError("isolated target is not Marzban 0.8.4")

    source_name = "beykusios"
    child_name = EXPECTED_CHILD_USERNAME
    created = []
    db = Database()
    results = {}
    raw_uuid = None
    try:
        source = marzban.create_user({
            "username": source_name,
            "proxies": {"vless": {"flow": "xtls-rprx-vision"}},
            "inbounds": {"vless": list(EFFECTIVE_VLESS_TAGS)},
            "expire": 0,
            "data_limit": None,
            "data_limit_reset_strategy": "no_reset",
            "status": "active",
            "note": "PH3-03 durable worker isolated source",
        }, token)
        created.append(source_name)
        source = marzban.get_user(source_name, token)
        if source_contract_hash(source) != EXPECTED_SOURCE_HASH:
            raise AssertionError("real staging source contract drift")
        account, alias_id, slot = _make_parent_and_slot(db)
        prepared = db.child_provisioning.prepare_child_ensure(
            account_id=account["account_id"],
            slot_generation_id=slot["generation_id"],
            source_alias_id=alias_id,
            source_contract_hash=EXPECTED_SOURCE_HASH,
            expire=0,
            idempotency_key="ph3-worker-staging-child-ensure-v1",
            now=900,
        )
        if prepared["operation_id"] != EXPECTED_OPERATION_ID:
            raise AssertionError("worker staging operation identity drift")

        clock = Clock()
        instrumented = InstrumentedMarzban(marzban)
        operations = BrokerOperations(instrumented)
        crashed = {"done": False}

        def lost_ack(stage, _operation):
            if stage == "after_remote_create_before_ack" and not crashed["done"]:
                crashed["done"] = True
                raise RuntimeError("TEST_CRASH_REMOTE_CREATED_LOCAL_ACK_LOST")

        with running_broker(operations) as service:
            try:
                _worker(db, service, clock, "stage-worker-crash", crash_hook=lost_ack).run_once()
            except RuntimeError as exc:
                if str(exc) != "TEST_CRASH_REMOTE_CREATED_LOCAL_ACK_LOST":
                    raise
            else:
                raise AssertionError("lost-ACK crash injection did not fire")
            created.append(child_name)
            if instrumented.child_create_calls != 1:
                raise AssertionError("real worker did not create exactly one child")
            clock.advance(6)
            recovered = _worker(db, service, clock, "stage-worker-restarted").run_once()
            if recovered["provisioned"] != 1 or instrumented.child_create_calls != 1:
                raise AssertionError("lost-ACK worker recovery did not converge")
            results["remote_created_local_ack_failed"] = "PASS"
            results["worker_restart"] = "PASS"

        # Broker restart: a new authenticated server/client pair must reconcile
        # the APPLIED child read-only.
        clock.advance(6)
        with running_broker(BrokerOperations(instrumented)) as restarted_service:
            after_broker_restart = _worker(
                db, restarted_service, clock, "stage-after-broker-restart",
                mode="reconcile_only",
            ).run_once()
            if after_broker_restart["reconciled"] != 1:
                raise AssertionError("broker restart reconciliation failed")
        results["broker_restart"] = "PASS"

        # Stale reconciliation lease is recovered by another worker instance.
        clock.advance(6)
        db._conn.execute(
            "UPDATE mgboost_child_workflow_state SET lease_owner='dead-stage-worker',"
            "lease_expires_at=?,next_check_at=?",
            (clock.value - 1, clock.value),
        )
        db._conn.commit()
        with running_broker(BrokerOperations(instrumented)) as service:
            stale = _worker(
                db, service, clock, "stage-stale-lease-recovery",
                mode="reconcile_only",
            ).run_once()
            if stale["reconciled"] != 1:
                raise AssertionError("stale lease recovery failed")
        results["stale_lease_recovery"] = "PASS"

        # Broker/Marzban unavailable never becomes success, then a fresh broker
        # converges without changing the child.
        clock.advance(6)
        unavailable = ServiceMarzbanClient(
            mode="broker", broker_url=f"http://127.0.0.1:{_unused_port()}",
            broker_key=AUTH_KEY, broker_client_id=CLIENT_ID, broker_timeout=0.2,
        )
        failed = _worker(
            db, unavailable, clock, "stage-outage", mode="reconcile_only"
        ).run_once()
        if failed["retried"] != 1:
            raise AssertionError("broker outage produced false success")
        clock.advance(2)
        with running_broker(BrokerOperations(instrumented)) as service:
            recovered = _worker(
                db, service, clock, "stage-outage-recovered",
                mode="reconcile_only",
            ).run_once()
            if recovered["reconciled"] != 1:
                raise AssertionError("post-outage reconciliation failed")
        results["broker_marzban_outage"] = "PASS"
        results["recovery_after_outage"] = "PASS"

        child = marzban.get_user(child_name, token)
        raw_uuid = child["proxies"]["vless"]["id"]
        if set(child["proxies"]) != {"vless"}:
            raise AssertionError("worker child is not VLESS-only")
        if sorted(child["inbounds"]["vless"]) != sorted(EFFECTIVE_VLESS_TAGS):
            raise AssertionError("worker child exact inbound set drift")
        if child["proxies"]["vless"].get("flow") != "xtls-rprx-vision":
            raise AssertionError("worker child flow drift")
        if instrumented.child_create_calls != 1:
            raise AssertionError("retry/restart paths created a duplicate child")
        if len([name for name in marzban.get_users(token, limit=100, offset=0)["users"]
                if name["username"] == child_name]) != 1:
            raise AssertionError("remote child count is not exactly one")
        dump = "\n".join(db._conn.iterdump())
        if raw_uuid in dump:
            raise AssertionError("raw child UUID persisted in MGBoost DB")
        workflow = db._conn.execute(
            "SELECT reconcile_state,failure_count,last_error_class "
            "FROM mgboost_child_workflow_state"
        ).fetchone()
        if tuple(workflow) != ("IN_SYNC", 0, None):
            raise AssertionError("final worker state is not in sync")
        results["exactly_one_remote_child"] = "PASS"
        results["raw_uuid_not_persisted"] = "PASS"
        results["final_in_sync"] = "PASS"
        print(json.dumps({
            "staging_worker_gate": "PASS",
            "marzban_version": EXPECTED_VERSION,
            "operation_id": EXPECTED_OPERATION_ID,
            "child_username": EXPECTED_CHILD_USERNAME,
            "uuid_masked": "uuid_" + hashlib.sha256(
                ("mask\0" + raw_uuid).encode("utf-8")
            ).hexdigest()[:8],
            "effective_vless_count": len(EFFECTIVE_VLESS_TAGS),
            "child_create_calls": instrumented.child_create_calls,
            "results": results,
        }, indent=2, sort_keys=True))
    finally:
        for username in (child_name, source_name):
            if username not in created:
                continue
            try:
                marzban.delete_user(username, token)
            except Exception:
                pass
        db._conn.close()


if __name__ == "__main__":
    main()
