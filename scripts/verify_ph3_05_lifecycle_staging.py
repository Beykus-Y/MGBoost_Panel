#!/usr/bin/env python3
"""Real Marzban 0.8.4 PH3-05 device revoke/free/rebind lifecycle staging gate.

Reuses the same isolated-target contract as the other PH3-03 gates (loopback,
non-default port, explicit `PH3_ISOLATED_STAGING_ACK`). Creates one source
user and drives the real `src/child_lifecycle.py` state machine, through the
real broker operation dispatch, against the real Marzban instance:

  slot 1: create child -> REVOKE -> reread proves disabled+rotated UUID
          -> duplicate revoke converges -> FREE only succeeds after that
  slot 2: create a second child -> REBIND (revoke old, swap generation,
          hand off to the existing PH3-03 provisioning pipeline) -> exactly
          one new remote child, old child stays revoked

Prints no raw UUID/token/admin credential.
"""

from __future__ import annotations

import argparse
import json
import os

from src import child_lifecycle
from src.broker_operations import BrokerOperations
from src.child_contract import derive_lifecycle_operation_id, source_contract_hash
from src.database import Database
from src.marzban import MarzbanClient
from scripts.build_ph3_03_staging_xray import EFFECTIVE_VLESS_TAGS
from scripts.verify_ph3_03_marzban_staging import (
    EXPECTED_SOURCE_HASH,
    EXPECTED_VERSION,
    _make_parent_and_slot,
    require_isolated_url,
)


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
            "note": "PH3-05 lifecycle isolated source template",
        }
        marzban.create_user(source_payload, admin_token)
        created_usernames.append(source_name)
        source = marzban.get_user(source_name, admin_token)
        if source_contract_hash(source) != EXPECTED_SOURCE_HASH:
            raise AssertionError("staging source differs from approved production shape")

        account, alias_id, slot1 = _make_parent_and_slot(db)
        direct_ops = BrokerOperations(marzban)

        # ---------- slot 1: create -> revoke -> retry -> free ----------
        prepared1, child1, raw_uuid1 = _create_child(
            db, direct_ops, account, alias_id, slot1["generation_id"],
            "ph3-05-lifecycle-stage-child-1-v1", 102,
        )
        created_usernames.append(prepared1["child_username"])
        results["child_created_and_active"] = (
            marzban.get_user(prepared1["child_username"], admin_token)["status"] == "active"
        )

        revoke_prepared = db.child_lifecycle.prepare_revoke(
            account_id=account["account_id"], old_child_intent_id=prepared1["child_intent_id"],
            reason="ph3-05 isolated staging revoke", idempotency_key="ph3-05-stage-revoke-v1",
            now=200,
        )
        revoked = child_lifecycle.process_revoke(
            db, revoke_prepared["operation_id"], worker_id="stage-lifecycle-worker",
            revoke_fn=_revoke_fn(marzban), now=201,
        )
        results["revoke_applied"] = revoked["state"] == "APPLIED"
        after_revoke = marzban.get_user(prepared1["child_username"], admin_token)
        results["remote_disabled_after_revoke"] = after_revoke["status"] == "disabled"
        rotated_uuid = after_revoke["proxies"]["vless"]["id"]
        results["remote_uuid_rotated"] = rotated_uuid != raw_uuid1

        retry_prepared = db.child_lifecycle.prepare_revoke(
            account_id=account["account_id"], old_child_intent_id=prepared1["child_intent_id"],
            reason="ph3-05 isolated staging revoke", idempotency_key="ph3-05-stage-revoke-v1",
            now=202,
        )
        results["duplicate_revoke_returns_same_operation"] = (
            retry_prepared["operation_id"] == revoke_prepared["operation_id"]
        )
        results["duplicate_revoke_cannot_reclaim_applied_lease"] = (
            db.child_lifecycle.claim(revoke_prepared["operation_id"], worker_id="stage-retry-worker", now=203)
            is None
        )
        # Idempotent-against-already-disabled-remote: a fresh independent
        # dispatch of the exact same typed revoke call must not re-rotate.
        second_dispatch = direct_ops.dispatch("child.user.revoke", {
            "operation_id": derive_lifecycle_operation_id(prepared1["child_username"], "REVOKE"),
            "child_username": prepared1["child_username"],
            "uuid_verifier": child1["uuid_verifier"],
        })
        results["already_revoked_dispatch_is_idempotent"] = (
            second_dispatch["outcome"] == "ALREADY_REVOKED"
        )
        results["no_second_rotation_after_already_revoked"] = (
            marzban.get_user(prepared1["child_username"], admin_token)["proxies"]["vless"]["id"]
            == rotated_uuid
        )

        free_prepared = db.child_lifecycle.prepare_free(
            account_id=account["account_id"], old_child_intent_id=prepared1["child_intent_id"],
            reason="ph3-05 isolated staging free", idempotency_key="ph3-05-stage-free-v1", now=210,
        )
        freed = child_lifecycle.process_free(
            db, free_prepared["operation_id"], worker_id="stage-lifecycle-worker", now=211,
        )
        results["free_applied"] = freed["state"] == "APPLIED"
        slot1_row = db._conn.execute(
            "SELECT desired_state FROM mgboost_device_slots WHERE id=?", (slot1["slot_id"],),
        ).fetchone()
        results["slot_freed_locally_only_after_revoke"] = slot1_row["desired_state"] == "FREE"

        # ---------- slot 2: create -> rebind (reinstall) ----------
        slot2 = db.device_slots.claim(
            account["account_id"], "ph3-05-stage-original-hwid-slot2", hmac_key, now=220,
        )
        prepared2, child2, raw_uuid2 = _create_child(
            db, direct_ops, account, alias_id, slot2["generation_id"],
            "ph3-05-lifecycle-stage-child-2-v1", 221,
        )
        created_usernames.append(prepared2["child_username"])

        rebind_prepared = db.child_lifecycle.prepare_rebind(
            account_id=account["account_id"], old_child_intent_id=prepared2["child_intent_id"],
            reason="ph3-05 isolated staging rebind (reinstall)",
            idempotency_key="ph3-05-stage-rebind-v1", now=230,
        )
        rebind_result = child_lifecycle.process_rebind(
            db, rebind_prepared["operation_id"], worker_id="stage-lifecycle-worker",
            revoke_fn=_revoke_fn(marzban), new_raw_hwid="ph3-05-stage-rebind-new-hwid",
            hmac_key=hmac_key, now=231,
        )
        results["rebind_applied"] = rebind_result["state"] == "APPLIED"
        new_child_intent = db._conn.execute(
            "SELECT child_username FROM mgboost_child_user_intents WHERE id=?",
            (rebind_result["new_child_intent_id"],),
        ).fetchone()
        results["rebind_new_child_username_differs"] = (
            new_child_intent["child_username"] != prepared2["child_username"]
        )
        old_child2_remote = marzban.get_user(prepared2["child_username"], admin_token)
        results["rebind_old_child_revoked_before_new_provisioning"] = (
            old_child2_remote["status"] == "disabled"
        )

        # The rebind lifecycle op only hands off to PH3-03's existing durable
        # outbox; drive that pipeline exactly as PH3-03's own gate does.
        new_outbox_row = db._conn.execute(
            "SELECT operation_id FROM mgboost_outbox WHERE child_intent_id=?",
            (rebind_result["new_child_intent_id"],),
        ).fetchone()
        claimed3 = db.child_provisioning.claim(
            new_outbox_row["operation_id"], worker_id="stage-provisioning-worker-3",
            now=232, lease_seconds=5,
        )
        created3 = direct_ops.dispatch("child.user.ensure", claimed3["payload"])
        created_usernames.append(new_child_intent["child_username"])
        raw_uuid3 = created3.pop("uuid")
        db.child_provisioning.acknowledge(
            new_outbox_row["operation_id"], worker_id="stage-provisioning-worker-3",
            outcome=created3["outcome"], child_uuid=raw_uuid3, remote_result=created3, now=233,
        )
        repeat = direct_ops.dispatch("child.user.ensure", claimed3["payload"])
        results["rebind_exactly_one_new_remote_child"] = repeat["outcome"] == "EXISTING"
        repeat.pop("uuid", None)
        new_child_remote = marzban.get_user(new_child_intent["child_username"], admin_token)
        results["rebind_new_child_active"] = new_child_remote["status"] == "active"
        old_child2_remote_final = marzban.get_user(prepared2["child_username"], admin_token)
        results["rebind_old_child_stays_revoked_after_new_child_live"] = (
            old_child2_remote_final["status"] == "disabled"
        )

        # ---------- Marzban outage during revoke fails closed ----------
        dead_client = MarzbanClient(base_url="http://127.0.0.1:1")
        try:
            BrokerOperations(dead_client).dispatch(
                "child.user.revoke",
                {
                    "operation_id": derive_lifecycle_operation_id(prepared1["child_username"], "REVOKE"),
                    "child_username": prepared1["child_username"],
                    "uuid_verifier": child1["uuid_verifier"],
                },
            )
            results["outage_raises"] = False
        except Exception:
            results["outage_raises"] = True

        db_dump = "\n".join(db._conn.iterdump())
        raw_secrets = [raw_uuid1, rotated_uuid, raw_uuid2, raw_uuid3]
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


if __name__ == "__main__":
    main()
