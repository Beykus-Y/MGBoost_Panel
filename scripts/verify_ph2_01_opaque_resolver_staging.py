#!/usr/bin/env python3
"""Real Marzban 0.8.4 PH2-01 opaque subscription resolver staging gate.

Same isolated-target contract as every other PH3-0x gate (loopback,
non-default port, explicit `PH3_ISOLATED_STAGING_ACK`). Creates one reviewed
parent, seeds its first child via the existing PH3-03 pipeline, issues one
opaque credential, then drives the real `src/opaque_resolver.py` engine
through the real typed broker operations against the real Marzban instance.

Prints no raw UUID/token/admin credential.
"""

from __future__ import annotations

import argparse
import base64
import json
import os

from src.broker_operations import BrokerOperations
from src.child_contract import source_contract_hash
from src.database import Database
from src.marzban import MarzbanClient
from src.opaque_resolver import OUTCOME_OK, resolve_opaque_subscription
from scripts.build_ph3_03_staging_xray import EFFECTIVE_VLESS_TAGS
from scripts.verify_ph3_03_marzban_staging import (
    EXPECTED_VERSION,
    _make_parent_and_slot,
    require_isolated_url,
)


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
            "note": "PH2-01 opaque resolver isolated source template",
        }
        marzban.create_user(source_payload, admin_token)
        created_usernames.append(source_name)
        source = marzban.get_user(source_name, admin_token)
        source_hash = source_contract_hash(source)

        account, alias_id, slot1 = _make_parent_and_slot(db)
        direct_ops = BrokerOperations(marzban)

        def ensure_fn(payload):
            return direct_ops.dispatch("child.user.ensure", payload)

        def subscription_fn(payload):
            return direct_ops.dispatch("child.user.subscription.get", payload)

        # ---------- seed the account's first child (existing PH3-03 pipeline) ----------
        prepared1 = db.child_provisioning.prepare_child_ensure(
            account_id=account["account_id"], slot_generation_id=slot1["generation_id"],
            source_alias_id=alias_id, source_contract_hash=source_hash, expire=0,
            idempotency_key="ph2-01-stage-seed-child-v1", now=102,
        )
        claimed1 = db.child_provisioning.claim(
            prepared1["operation_id"], worker_id="stage-seed-worker", now=103, lease_seconds=5,
        )
        created1 = ensure_fn(claimed1["payload"])
        raw_uuid1 = created1.pop("uuid")
        db.child_provisioning.acknowledge(
            prepared1["operation_id"], worker_id="stage-seed-worker",
            outcome=created1["outcome"], child_uuid=raw_uuid1, remote_result=created1, now=104,
        )
        created_usernames.append(prepared1["child_username"])

        # ---------- issue one opaque credential ----------
        prepared_cred = db.subscription_credentials.prepare(
            account_id=account["account_id"], actor_ref="stage-primary-admin",
            reason="ph2-01 isolated staging issuance", idempotency_key="ph2-01-stage-cred-prepare-v1",
            now=110,
        )
        db.subscription_credentials.activate(
            credential_id=prepared_cred["id"], account_id=account["account_id"],
            expected_generation=prepared_cred["generation"], actor_ref="stage-primary-admin",
            idempotency_key="ph2-01-stage-cred-activate-v1", now=111,
        )
        raw_token = prepared_cred["raw_token"]

        # ---------- new HWID assigns a fresh slot, idempotent retry reuses it ----------
        known_meta = {
            "client_name": "Happ", "client_version": "2.7.0", "platform": "windows",
            "hwid_candidate_present": True, "hwid_candidate_supported": True,
            "device_id": "ph2-01-stage-known-hwid",
        }
        # "known slot" is proven by requesting the SAME device twice below,
        # not by reusing the seed child's own slot (a distinct, unrelated
        # device claimed purely to exercise the existing PH3-03 pipeline).
        first = resolve_opaque_subscription(
            db, raw_token, known_meta, hmac_key=hmac_key, ensure_fn=ensure_fn,
            subscription_fn=subscription_fn, worker_id="stage-resolver-worker", now=200,
        )
        results["first_request_ok"] = first.outcome == OUTCOME_OK
        results["bridged_body_present"] = bool(first.body_b64)
        first_username = first.child_username
        if first_username:
            created_usernames.append(first_username)

        second = resolve_opaque_subscription(
            db, raw_token, known_meta, hmac_key=hmac_key, ensure_fn=ensure_fn,
            subscription_fn=subscription_fn, worker_id="stage-resolver-worker", now=201,
        )
        results["retry_is_idempotent_same_child"] = (
            second.outcome == OUTCOME_OK and second.child_username == first.child_username
            and second.slot_number == first.slot_number and second.generation == first.generation
        )

        remote_child = marzban.get_user(first_username, admin_token)
        results["bridged_child_active_vless_only"] = (
            remote_child["status"] == "active" and set(remote_child["proxies"]) == {"vless"}
        )
        results["bridged_child_uuid_differs_from_source"] = (
            remote_child["proxies"]["vless"]["id"] != source["proxies"]["vless"]["id"]
        )

        decoded_body = base64.b64decode(first.body_b64).decode("utf-8", errors="replace")
        results["bridged_body_absent_shared_source_uuid"] = (
            source["proxies"]["vless"]["id"] not in decoded_body
        )

        # ---------- second, distinct HWID gets its own second child ----------
        second_device_meta = dict(known_meta, device_id="ph2-01-stage-second-hwid")
        second_device = resolve_opaque_subscription(
            db, raw_token, second_device_meta, hmac_key=hmac_key, ensure_fn=ensure_fn,
            subscription_fn=subscription_fn, worker_id="stage-resolver-worker", now=210,
        )
        results["second_device_gets_distinct_child"] = (
            second_device.outcome == OUTCOME_OK
            and second_device.child_username != first.child_username
        )
        if second_device.child_username:
            created_usernames.append(second_device.child_username)

        # ---------- missing/malformed HWID denied ----------
        missing_meta = dict(known_meta, hwid_candidate_present=False, hwid_candidate_supported=False, device_id=None)
        missing_result = resolve_opaque_subscription(
            db, raw_token, missing_meta, hmac_key=hmac_key, ensure_fn=ensure_fn,
            subscription_fn=subscription_fn, worker_id="stage-resolver-worker", now=220,
        )
        results["missing_hwid_denied"] = missing_result.outcome != OUTCOME_OK

        # ---------- invalid opaque token denied (uniform, no dispatch) ----------
        invalid_result = resolve_opaque_subscription(
            db, "not-a-real-token-at-all", known_meta, hmac_key=hmac_key, ensure_fn=ensure_fn,
            subscription_fn=subscription_fn, worker_id="stage-resolver-worker", now=230,
        )
        results["invalid_token_denied"] = invalid_result.outcome != OUTCOME_OK

        # ---------- revoked credential denied ----------
        db.subscription_credentials.revoke(
            credential_id=prepared_cred["id"], account_id=account["account_id"],
            reason_code="ADMIN_MANUAL", actor_ref="stage-primary-admin",
            idempotency_key="ph2-01-stage-cred-revoke-v1", now=240,
        )
        revoked_result = resolve_opaque_subscription(
            db, raw_token, known_meta, hmac_key=hmac_key, ensure_fn=ensure_fn,
            subscription_fn=subscription_fn, worker_id="stage-resolver-worker", now=241,
        )
        results["revoked_token_denied"] = revoked_result.outcome != OUTCOME_OK

        db_dump = "\n".join(db._conn.iterdump())
        raw_secrets = [raw_token, raw_uuid1, remote_child["proxies"]["vless"]["id"]]
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
