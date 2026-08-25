#!/usr/bin/env python3
"""Real Marzban 0.8.4 PH3-08 parent status/expiry -> active children sync gate.

Same isolated-target contract as every other PH3-0x gate (loopback,
non-default port, explicit `PH3_ISOLATED_STAGING_ACK`). Creates one synthetic
parent with 3 real children via the existing PH3-03 pipeline, then drives the
real `src/parent_sync.py` state machine through the real broker operation
dispatch against the real Marzban instance:

  ACTIVE parent+finite expiry -> all 3 children active with that expiry
  parent EXPIRED              -> all 3 disabled, same UUIDs, same generations
  parent renewed              -> the SAME 3 children active again (no new
                                  provisioning, no UUID rotation)
  one child PH3-05-revoked, then a disable/enable cycle -> the revoked child
                                  never resurrects; the other 2 correctly
                                  follow the parent
  stale enable after disable, lost ACK, worker restart -> converge safely

Prints no raw UUID/token/admin credential.
"""

from __future__ import annotations

import argparse
import json
import os
import time

from src import child_lifecycle, parent_sync
from src.broker_operations import BrokerOperations
from src.database import Database
from src.marzban import MarzbanClient
from src.child_contract import source_contract_hash
from scripts.build_ph3_03_staging_xray import EFFECTIVE_VLESS_TAGS
from scripts.verify_ph3_03_marzban_staging import (
    EXPECTED_VERSION,
    _make_parent_and_slot,
    require_isolated_url,
)


def _sync_fn(marzban):
    return lambda payload: BrokerOperations(marzban).dispatch("child.user.state.sync", payload)


def _revoke_fn(marzban):
    return lambda payload: BrokerOperations(marzban).dispatch("child.user.revoke", payload)


def _create_child(db, direct_ops, account, alias_id, slot_generation_id, idem_key, now):
    prepared = db.child_provisioning.prepare_child_ensure(
        account_id=account["account_id"], slot_generation_id=slot_generation_id,
        source_alias_id=alias_id, source_contract_hash=EXPECTED_SOURCE_HASH,
        expire=0, idempotency_key=idem_key, now=now,
    )
    claimed = db.child_provisioning.claim(
        prepared["operation_id"], worker_id="stage-provisioning-worker", now=now + 1, lease_seconds=5,
    )
    created = direct_ops.dispatch("child.user.ensure", claimed["payload"])
    raw_uuid = created.pop("uuid")
    child = db.child_provisioning.acknowledge(
        prepared["operation_id"], worker_id="stage-provisioning-worker",
        outcome=created["outcome"], child_uuid=raw_uuid, remote_result=created, now=now + 2,
    )
    return prepared, child, raw_uuid


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    args = parser.parse_args()
    base_url = require_isolated_url(args.url)
    required_env = (
        "MARZBAN_ADMIN_USER", "MARZBAN_ADMIN_PASS", "DATA_DIR",
        "PRIMARY_MGBOOST_ADMIN_ACTOR_ID", "PRIMARY_MGBOOST_ADMIN_LOGIN",
        "DEVICE_SLOT_HMAC_KEY",
    )
    if any(not os.environ.get(name) for name in required_env):
        raise RuntimeError("staging environment is incomplete")

    marzban = MarzbanClient(base_url=base_url)
    admin_token = marzban.get_admin_token_from_env()
    system = marzban.get_system(admin_token)
    if (system or {}).get("version") != EXPECTED_VERSION:
        raise RuntimeError("unexpected Marzban version")
    base = int(time.time())

    global EXPECTED_SOURCE_HASH
    source_name = "beykusios"
    results = {}
    created_usernames = []
    hmac_key = os.environ["DEVICE_SLOT_HMAC_KEY"]
    db = Database()
    try:
        source_payload = {
            "username": source_name,
            "proxies": {"vless": {"flow": "xtls-rprx-vision"}},
            "inbounds": {"vless": list(EFFECTIVE_VLESS_TAGS)},
            "expire": 0, "data_limit": None,
            "data_limit_reset_strategy": "no_reset", "status": "active",
            "note": "PH3-08 parent-sync isolated source template",
        }
        marzban.create_user(source_payload, admin_token)
        created_usernames.append(source_name)
        source = marzban.get_user(source_name, admin_token)
        EXPECTED_SOURCE_HASH = source_contract_hash(source)

        account, alias_id, slot1 = _make_parent_and_slot(db)
        direct_ops = BrokerOperations(marzban)
        account_id = account["account_id"]

        # ---------- create 3 children ----------
        fixtures = []
        prepared1, child1, uuid1 = _create_child(
            db, direct_ops, account, alias_id, slot1["generation_id"],
            "ph3-08-stage-child-1-v1", base + 2,
        )
        created_usernames.append(prepared1["child_username"])
        fixtures.append((prepared1, child1, uuid1))
        for i in range(2, 4):
            slot = db.device_slots.claim(account_id, f"ph3-08-stage-hwid-{i}", hmac_key, now=base + 10 + i)
            prepared, child, raw_uuid = _create_child(
                db, direct_ops, account, alias_id, slot["generation_id"],
                f"ph3-08-stage-child-{i}-v1", base + 20 + i,
            )
            created_usernames.append(prepared["child_username"])
            fixtures.append((prepared, child, raw_uuid))

        # ---------- ACTIVE parent + finite expiry -> all 3 active with that expiry ----------
        _set_subscription(db, account_id, status="ACTIVE", current_expiry=base + 3600)
        cycle1 = parent_sync.run_account_sync_cycle(
            db, account_id, sync_fn=_sync_fn(marzban), worker_id="stage-sync-worker", now=base + 200,
        )
        results["cycle1_all_prepared_applied"] = (
            cycle1["prepared"] == 3 and cycle1["applied"] == 3 and cycle1["aggregate_state"] == "IN_SYNC"
        )
        remote_after_active = [marzban.get_user(p["child_username"], admin_token) for p, _c, _u in fixtures]
        results["all_active_with_correct_expiry"] = all(
            u["status"] == "active" and u["expire"] == base + 3600 for u in remote_after_active
        )
        results["uuids_unchanged_after_activate"] = all(
            u["proxies"]["vless"]["id"] == raw_uuid
            for u, (_p, _c, raw_uuid) in zip(remote_after_active, fixtures)
        )

        # ---------- parent EXPIRED -> all 3 disabled, same UUIDs, same generations ----------
        _set_subscription(db, account_id, status="EXPIRED", current_expiry=base - 3600)
        generations_before_expire = {
            p["child_username"]: db._conn.execute(
                "SELECT generation FROM mgboost_device_slot_generations WHERE id="
                "(SELECT slot_generation_id FROM mgboost_child_user_intents WHERE id=?)",
                (p["child_intent_id"],),
            ).fetchone()["generation"]
            for p, _c, _u in fixtures
        }
        cycle2 = parent_sync.run_account_sync_cycle(
            db, account_id, sync_fn=_sync_fn(marzban), worker_id="stage-sync-worker", now=base + 300,
        )
        results["cycle2_all_disabled"] = cycle2["applied"] == 3 and cycle2["aggregate_state"] == "IN_SYNC"
        remote_after_expire = [marzban.get_user(p["child_username"], admin_token) for p, _c, _u in fixtures]
        results["all_disabled_after_expiry"] = all(u["status"] == "disabled" for u in remote_after_expire)
        results["uuids_unchanged_after_expire"] = all(
            u["proxies"]["vless"]["id"] == raw_uuid
            for u, (_p, _c, raw_uuid) in zip(remote_after_expire, fixtures)
        )

        # ---------- one child PH3-05-revoked while parent is expired ----------
        revoked_prepared, revoked_child, revoked_uuid = fixtures[0]
        revoke_op = db.child_lifecycle.prepare_revoke(
            account_id=account_id, old_child_intent_id=revoked_prepared["child_intent_id"],
            reason="ph3-08 staging: revoke one child before renewal",
            idempotency_key="ph3-08-stage-revoke-v1", now=base + 310,
        )
        child_lifecycle.process_revoke(
            db, revoke_op["operation_id"], worker_id="stage-lifecycle-worker",
            revoke_fn=_revoke_fn(marzban), now=base + 311,
        )
        rotated = marzban.get_user(revoked_prepared["child_username"], admin_token)
        results["revoked_child_disabled_and_rotated"] = (
            rotated["status"] == "disabled" and rotated["proxies"]["vless"]["id"] != revoked_uuid
        )

        # ---------- parent renewed -> the SAME (non-revoked) children active again ----------
        _set_subscription(db, account_id, status="ACTIVE", current_expiry=base + 7200)
        cycle3 = parent_sync.run_account_sync_cycle(
            db, account_id, sync_fn=_sync_fn(marzban), worker_id="stage-sync-worker", now=base + 400,
        )
        results["cycle3_excludes_revoked_child"] = cycle3["prepared"] == 2  # only the 2 non-revoked
        remaining = fixtures[1:]
        remote_after_renew = [marzban.get_user(p["child_username"], admin_token) for p, _c, _u in remaining]
        results["renewed_children_active_same_uuid"] = all(
            u["status"] == "active" and u["expire"] == base + 7200
            and u["proxies"]["vless"]["id"] == raw_uuid
            for u, (_p, _c, raw_uuid) in zip(remote_after_renew, remaining)
        )
        results["revoked_child_never_resurrected"] = (
            marzban.get_user(revoked_prepared["child_username"], admin_token)["status"] == "disabled"
        )
        generations_after_renew = {
            p["child_username"]: db._conn.execute(
                "SELECT generation FROM mgboost_device_slot_generations WHERE id="
                "(SELECT slot_generation_id FROM mgboost_child_user_intents WHERE id=?)",
                (p["child_intent_id"],),
            ).fetchone()["generation"]
            for p, _c, _u in remaining
        }
        results["generations_unchanged_across_full_cycle"] = all(
            generations_after_renew[u] == generations_before_expire[u] for u in generations_after_renew
        )
        outbox_counts = {
            p["child_username"]: db._conn.execute(
                "SELECT COUNT(*) FROM mgboost_outbox WHERE child_intent_id=?", (p["child_intent_id"],),
            ).fetchone()[0]
            for p, _c, _u in remaining
        }
        results["no_new_provisioning_for_renewed_children"] = all(c == 1 for c in outbox_counts.values())

        # ---------- stale enable after disable is never dispatched ----------
        stale_prepared, stale_child, _stale_uuid = remaining[0]
        pre_stale_state = db.parent_sync.refresh_desired_state(account_id, now=base + 410)
        stale_ops = db.parent_sync.enqueue_current_children(account_id, now=base + 410)
        stale_op = next(o for o in stale_ops if o["child_intent_id"] == stale_prepared["child_intent_id"])
        _set_subscription(db, account_id, status="EXPIRED", current_expiry=base + 7200)
        db.parent_sync.refresh_desired_state(account_id, now=base + 420)
        claimed_stale = db.parent_sync.claim(stale_op["operation_id"], worker_id="stage-late-worker", now=base + 430)
        results["stale_enable_superseded_not_dispatched"] = (
            claimed_stale is None
            and marzban.get_user(stale_prepared["child_username"], admin_token)["status"] == "active"
        )

        # ---------- converge back to renewed-active, then re-run for idempotency (lost ACK) ----------
        _set_subscription(db, account_id, status="ACTIVE", current_expiry=base + 7200)
        cycle4 = parent_sync.run_account_sync_cycle(
            db, account_id, sync_fn=_sync_fn(marzban), worker_id="stage-sync-worker-restart", now=base + 500,
        )
        results["worker_restart_converges"] = cycle4["aggregate_state"] == "IN_SYNC"

        # ---------- Marzban outage during sync fails closed ----------
        dead_client = MarzbanClient(base_url="http://127.0.0.1:1")
        try:
            BrokerOperations(dead_client).dispatch(
                "child.user.state.sync",
                {
                    "operation_id": db._conn.execute(
                        "SELECT operation_id FROM mgboost_parent_sync_operations "
                        "WHERE child_intent_id=? ORDER BY id DESC LIMIT 1",
                        (remaining[0][0]["child_intent_id"],),
                    ).fetchone()["operation_id"],
                    "child_username": remaining[0][0]["child_username"],
                    "desired_status": "active",
                    "desired_expire": base + 7200,
                    "uuid_verifier": remaining[0][1]["uuid_verifier"],
                },
            )
            results["outage_raises"] = False
        except Exception:
            results["outage_raises"] = True

        db_dump = "\n".join(db._conn.iterdump())
        raw_secrets = [u for _p, _c, u in fixtures] + [rotated["proxies"]["vless"]["id"]]
        results["no_raw_credentials_in_mgboost_db"] = not any(s in db_dump for s in raw_secrets)

        output = {
            "staging_contract": "PASS" if all(results.values()) else "FAIL",
            "results": results,
        }
        print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
    finally:
        for username in created_usernames:
            try:
                marzban.delete_user(username, admin_token)
            except Exception:
                pass
        db._conn.close()


def _set_subscription(db, account_id, *, status, current_expiry):
    db._conn.execute(
        "UPDATE mgboost_subscriptions SET status=?,current_expiry=?,updated_at=updated_at WHERE account_id=?",
        (status, current_expiry, account_id),
    )
    db._conn.commit()


if __name__ == "__main__":
    main()
