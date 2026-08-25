#!/usr/bin/env python3
"""Real Marzban 0.8.4 PH4-01 legacy subscription alias bridge staging gate.

Same isolated-target contract as every other PH3-0x/PH2-01 gate (loopback,
non-default port, explicit `PH3_ISOLATED_STAGING_ACK`). Proves, against a
real Marzban instance:

  1. before any binding exists, a legacy request for the synthetic legacy
     user returns the real shared legacy credential unchanged;
  2. a reviewed parent + explicit enabled binding is created for that same
     legacy username;
  3. the SAME legacy authority + a supported HWID now bridges to a real
     PH3-03 child -- the shared legacy UUID is absent from the bridged
     response, and the legacy remote user itself is untouched/still active;
  4. repeat request -> same child; a second, distinct HWID -> its own
     second child; no duplicates;
  5. full capacity and missing/malformed HWID both fall through to the
     unmodified legacy response (never denied outright);
  6. zero raw credential persistence in the MGBoost DB dump.

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
from src.legacy_bridge_resolver import resolve_legacy_bridge
from src.marzban import MarzbanClient
from src.opaque_resolver import OUTCOME_OK
from src.security import AdminSessionStore
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

    legacy_username = "beykusios"
    results = {}
    created_usernames = []
    hmac_key = os.environ["DEVICE_SLOT_HMAC_KEY"]
    db = Database()
    try:
        legacy_payload = {
            "username": legacy_username,
            "proxies": {"vless": {"flow": "xtls-rprx-vision"}},
            "inbounds": {"vless": list(EFFECTIVE_VLESS_TAGS)},
            "expire": 0, "data_limit": None,
            "data_limit_reset_strategy": "no_reset", "status": "active",
            "note": "PH4-01 legacy bridge isolated legacy/source user",
        }
        marzban.create_user(legacy_payload, admin_token)
        created_usernames.append(legacy_username)
        legacy_user = marzban.get_user(legacy_username, admin_token)
        legacy_uuid = legacy_user["proxies"]["vless"]["id"]
        legacy_hash = source_contract_hash(legacy_user)

        # ---------- 1. before any binding: legacy remains authoritative ----------
        pre_binding_user = marzban.get_user(legacy_username, admin_token)
        results["pre_binding_legacy_unchanged"] = (
            pre_binding_user["status"] == "active"
            and pre_binding_user["proxies"]["vless"]["id"] == legacy_uuid
        )

        # ---------- 2. reviewed parent + explicit enabled binding ----------
        account, alias_id, slot1 = _make_parent_and_slot(db)
        # _make_parent_and_slot's own fixture alias uses "beykusios" already
        # (matching production's real primary alias) -- reuse it directly.
        alias_row = db._conn.execute(
            "SELECT id, legacy_username FROM mgboost_legacy_account_aliases "
            "WHERE account_id=? AND alias_role='PRIMARY'", (account["account_id"],),
        ).fetchone()
        if alias_row["legacy_username"] != legacy_username:
            raise RuntimeError("staging fixture alias does not match the isolated legacy user")

        _raw, session = AdminSessionStore().create(
            os.environ["PRIMARY_MGBOOST_ADMIN_LOGIN"], "staging-server-jwt",
        )
        capability = db.primary_admin_authority.authorize_session(session)

        direct_ops = BrokerOperations(marzban)

        def ensure_fn(payload):
            return direct_ops.dispatch("child.user.ensure", payload)

        def subscription_fn(payload):
            return direct_ops.dispatch("child.user.subscription.get", payload)

        # The bridge engine deliberately never discovers a brand-new source
        # template on its own (matching PH2-01's own documented limitation)
        # -- seed the account's first child via the existing, unmodified
        # PH3-03 pipeline first, exactly like every prior PH3-0x gate does.
        seed_prepared = db.child_provisioning.prepare_child_ensure(
            account_id=account["account_id"], slot_generation_id=slot1["generation_id"],
            source_alias_id=alias_row["id"], source_contract_hash=legacy_hash, expire=0,
            idempotency_key="ph4-01-stage-seed-child-v1", now=102,
        )
        seed_claimed = db.child_provisioning.claim(
            seed_prepared["operation_id"], worker_id="stage-seed-worker", now=103, lease_seconds=5,
        )
        seed_created = ensure_fn(seed_claimed["payload"])
        seed_uuid = seed_created.pop("uuid")
        db.child_provisioning.acknowledge(
            seed_prepared["operation_id"], worker_id="stage-seed-worker",
            outcome=seed_created["outcome"], child_uuid=seed_uuid, remote_result=seed_created, now=104,
        )
        created_usernames.append(seed_prepared["child_username"])

        db.legacy_bridge.create_binding(
            capability=capability, account_id=account["account_id"], legacy_alias_id=alias_row["id"],
            enabled=True, decision_ref="ph4-01-isolated-staging-v1", now=105,
        )

        # ---------- 3. same legacy authority + supported HWID -> bridged child ----------
        meta_known = {
            "client_name": "Happ", "client_version": "2.7.0", "platform": "windows",
            "hwid_candidate_present": True, "hwid_candidate_supported": True,
            "device_id": "ph4-01-stage-device-a",
        }
        first = resolve_legacy_bridge(
            db, legacy_username, meta_known, hmac_key=hmac_key, ensure_fn=ensure_fn,
            subscription_fn=subscription_fn, worker_id="stage-bridge-worker", now=200,
        )
        results["bridge_ok"] = first.outcome == OUTCOME_OK
        if first.child_username:
            created_usernames.append(first.child_username)

        remote_child = marzban.get_user(first.child_username, admin_token) if first.child_username else {}
        results["child_created_active_vless_only"] = (
            remote_child.get("status") == "active" and set(remote_child.get("proxies", {})) == {"vless"}
        )
        results["child_uuid_differs_from_shared_legacy_uuid"] = (
            remote_child.get("proxies", {}).get("vless", {}).get("id") != legacy_uuid
        )

        decoded_body = base64.b64decode(first.body_b64).decode("utf-8", errors="replace") if first.body_b64 else ""
        results["bridged_body_absent_shared_legacy_uuid"] = legacy_uuid not in decoded_body

        legacy_after_bridge = marzban.get_user(legacy_username, admin_token)
        results["legacy_remote_user_untouched_after_bridge"] = (
            legacy_after_bridge["status"] == "active"
            and legacy_after_bridge["proxies"]["vless"]["id"] == legacy_uuid
        )

        # ---------- 4. repeat -> same child; second device -> own child ----------
        repeat = resolve_legacy_bridge(
            db, legacy_username, meta_known, hmac_key=hmac_key, ensure_fn=ensure_fn,
            subscription_fn=subscription_fn, worker_id="stage-bridge-worker", now=201,
        )
        results["repeat_is_idempotent_same_child"] = (
            repeat.outcome == OUTCOME_OK and repeat.child_username == first.child_username
            and repeat.slot_number == first.slot_number
        )

        meta_second = dict(meta_known, device_id="ph4-01-stage-device-b")
        second = resolve_legacy_bridge(
            db, legacy_username, meta_second, hmac_key=hmac_key, ensure_fn=ensure_fn,
            subscription_fn=subscription_fn, worker_id="stage-bridge-worker", now=210,
        )
        results["second_device_gets_distinct_child"] = (
            second.outcome == OUTCOME_OK and second.child_username != first.child_username
        )
        if second.child_username:
            created_usernames.append(second.child_username)

        # ---------- 5. missing HWID and full slots fall through to legacy ----------
        meta_missing = {**meta_known, "hwid_candidate_present": False,
                         "hwid_candidate_supported": False, "device_id": None}
        missing_result = resolve_legacy_bridge(
            db, legacy_username, meta_missing, hmac_key=hmac_key, ensure_fn=ensure_fn,
            subscription_fn=subscription_fn, worker_id="stage-bridge-worker", now=220,
        )
        from src.legacy_bridge_resolver import is_fall_through_outcome
        results["missing_hwid_falls_through"] = is_fall_through_outcome(missing_result.outcome)

        for i in range(7):
            db.device_slots.claim(account["account_id"], f"ph4-01-stage-filler-{i}", hmac_key, now=230 + i)
        full_result = resolve_legacy_bridge(
            db, legacy_username, dict(meta_known, device_id="ph4-01-stage-overflow"),
            hmac_key=hmac_key, ensure_fn=ensure_fn, subscription_fn=subscription_fn,
            worker_id="stage-bridge-worker", now=240,
        )
        results["full_slots_falls_through"] = is_fall_through_outcome(full_result.outcome)

        # ---------- 6. unmapped legacy username unaffected ----------
        unmapped_result = resolve_legacy_bridge(
            db, "not-a-mapped-username", meta_known, hmac_key=hmac_key, ensure_fn=ensure_fn,
            subscription_fn=subscription_fn, worker_id="stage-bridge-worker", now=250,
        )
        results["unmapped_username_falls_through"] = is_fall_through_outcome(unmapped_result.outcome)

        db_dump = "\n".join(db._conn.iterdump())
        raw_secrets = [legacy_uuid, remote_child.get("proxies", {}).get("vless", {}).get("id")]
        results["no_raw_credentials_in_mgboost_db"] = not any(s and s in db_dump for s in raw_secrets)

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
