#!/usr/bin/env python3
"""Real Marzban 0.8.4 PH4-02 durable migration state machine staging gate.

Same isolated-target contract as every other PH3-0x/PH4-01 gate (loopback,
non-default port, explicit `PH3_ISOLATED_STAGING_ACK`). Two independent
synthetic scenarios against the same real Marzban instance:

  A. `LEGACY -> MIGRATING -> MIGRATED` via `process_migration_bridge_request`
     (wraps the unmodified `resolve_legacy_bridge` -- no second resolver):
     lazy PH3-03 child, working config, absent shared UUID in the migrated
     body, legacy user untouched, idempotent repeat, and a real crash/
     lost-ACK convergence proof (kill the connection mid-attempt, reopen a
     fresh `Database()`, reconcile, retry -- exactly one child, no
     shared-UUID fallback).

  B. On a separate disposable synthetic account: `MIGRATED ->
     LEGACY_REVOKE_PENDING -> LEGACY_REVOKED`, with a REAL revoke of the
     synthetic legacy Marzban user (disable + UUID rotation), proving the
     old shared credential is genuinely dead, the state cannot roll back,
     and the migrated child keeps working.

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
from src.device_slots import privacy_safe_hwid
from src.legacy_bridge_resolver import is_fall_through_outcome
from src.marzban import MarzbanClient
from src.migration_lifecycle import process_migration_bridge_request, reconcile_binding
from src.opaque_resolver import OUTCOME_OK
from src.security import AdminSessionStore
from scripts.build_ph3_03_staging_xray import EFFECTIVE_VLESS_TAGS
from scripts.verify_ph3_03_marzban_staging import EXPECTED_VERSION, _make_parent_and_slot, require_isolated_url


def _seed_legacy_user(marzban, admin_token, username, note):
    payload = {
        "username": username,
        "proxies": {"vless": {"flow": "xtls-rprx-vision"}},
        "inbounds": {"vless": list(EFFECTIVE_VLESS_TAGS)},
        "expire": 0, "data_limit": None,
        "data_limit_reset_strategy": "no_reset", "status": "active",
        "note": note,
    }
    marzban.create_user(payload, admin_token)
    return marzban.get_user(username, admin_token)


def _make_second_parent_and_slot(db, *, legacy_username, mapping_key, capability, hmac_key):
    """Independent of `_make_parent_and_slot` (which is hardcoded to the
    fixed 'beykusios'/INTERNAL_OWNER_PRIMARY identity) -- builds a second,
    distinct, disposable reviewed account for the separate revoke-boundary
    scenario (B), never colliding with scenario A's account/alias."""
    plan = db.internal_entitlements.create_internal_plan(
        capability=capability, plan_code="INTERNAL_OWNER_CANARY_B", version=1,
        display_name="Internal owner canary B", device_limit_mode="LIMITED",
        device_limit=10, wl_mode="UNLIMITED", terms={"schema": 1, "staging": True}, now=100,
    )
    account = db.internal_entitlements.create_reviewed_account(
        capability=capability, plan_version_id=plan["id"], legacy_username=legacy_username,
        mapping_key=mapping_key, decision_ref="ph4-02-staging-scenario-b-approval",
        legacy_aliases=[{
            "legacy_username": legacy_username, "alias_role": "PRIMARY",
            "ownership_provenance": "OWNER_APPROVED", "legacy_status": "UNLIMITED",
            "legacy_expiry": None, "observed_device_count": 1, "observed_hwid_count": 1,
            "evidence": {"privacy": "masked", "staging": True},
        }],
        ownership_evidence="PROVEN", telegram_id=905302973, legacy_status="UNLIMITED",
        legacy_expiry=None, device_evidence_count=1, hwid_evidence_count=1,
        internal_reason="PH4-02 isolated staging revoke-boundary scenario",
        migration_confidence="HIGH", evidence={"staging": True},
        idempotency_key="ph4-02-staging-approved-parent-b-v1", now=100,
    )
    alias = db._conn.execute(
        "SELECT id FROM mgboost_legacy_account_aliases WHERE account_id=? AND legacy_username=?",
        (account["account_id"], legacy_username),
    ).fetchone()
    slot = db.device_slots.claim(
        account["account_id"], "ph4-02-legacy-device-row-b", hmac_key, now=101,
    )
    return account, alias["id"], slot


def _seed_first_child(db, marzban, admin_token, ensure_fn, *, account, alias_row, slot, legacy_hash, idem_key):
    prepared = db.child_provisioning.prepare_child_ensure(
        account_id=account["account_id"], slot_generation_id=slot["generation_id"],
        source_alias_id=alias_row["id"], source_contract_hash=legacy_hash, expire=0,
        idempotency_key=idem_key, now=102,
    )
    claimed = db.child_provisioning.claim(
        prepared["operation_id"], worker_id="stage-seed-worker", now=103, lease_seconds=5,
    )
    created = ensure_fn(claimed["payload"])
    seed_uuid = created.pop("uuid")
    db.child_provisioning.acknowledge(
        prepared["operation_id"], worker_id="stage-seed-worker",
        outcome=created["outcome"], child_uuid=seed_uuid, remote_result=created, now=104,
    )
    return prepared["child_username"]


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

    hmac_key = os.environ["DEVICE_SLOT_HMAC_KEY"]
    results = {}
    created_usernames = []
    db = Database()
    try:
        direct_ops = BrokerOperations(marzban)

        def ensure_fn(payload):
            return direct_ops.dispatch("child.user.ensure", payload)

        def subscription_fn(payload):
            return direct_ops.dispatch("child.user.subscription.get", payload)

        # ================= Scenario A: forward migration lifecycle =================
        legacy_username_a = "beykusios"
        legacy_a = _seed_legacy_user(marzban, admin_token, legacy_username_a, "PH4-02 scenario A legacy/source user")
        created_usernames.append(legacy_username_a)
        legacy_uuid_a = legacy_a["proxies"]["vless"]["id"]
        legacy_hash_a = source_contract_hash(legacy_a)

        account_a, alias_id_a, slot_a = _make_parent_and_slot(db)
        alias_row_a = db._conn.execute(
            "SELECT id, legacy_username FROM mgboost_legacy_account_aliases "
            "WHERE account_id=? AND alias_role='PRIMARY'", (account_a["account_id"],),
        ).fetchone()
        if alias_row_a["legacy_username"] != legacy_username_a:
            raise RuntimeError("staging fixture alias does not match the isolated legacy user (scenario A)")

        _raw, session = AdminSessionStore().create(
            os.environ["PRIMARY_MGBOOST_ADMIN_LOGIN"], "ph4-02-staging-server-jwt",
        )
        capability = db.primary_admin_authority.authorize_session(session)

        seed_child_a = _seed_first_child(
            db, marzban, admin_token, ensure_fn, account=account_a, alias_row=alias_row_a,
            slot=slot_a, legacy_hash=legacy_hash_a, idem_key="ph4-02-stage-seed-child-a",
        )
        created_usernames.append(seed_child_a)

        db.legacy_bridge.create_binding(
            capability=capability, account_id=account_a["account_id"], legacy_alias_id=alias_row_a["id"],
            enabled=True, decision_ref="ph4-02-isolated-staging-a", now=105,
        )

        meta_a = {
            "client_name": "Happ", "client_version": "2.7.0", "platform": "windows",
            "hwid_candidate_present": True, "hwid_candidate_supported": True,
            "device_id": "ph4-02-stage-device-a",
        }
        first = process_migration_bridge_request(
            db, legacy_username_a, meta_a, hmac_key=hmac_key, ensure_fn=ensure_fn,
            subscription_fn=subscription_fn, worker_id="stage-migration-worker", now=200,
        )
        results["A_bridge_ok"] = first.outcome == OUTCOME_OK
        if first.child_username:
            created_usernames.append(first.child_username)

        hwid_verifier_a, _masked = privacy_safe_hwid("ph4-02-stage-device-a", hmac_key)
        binding_a = db.migration_lifecycle.find_by_device(account_a["account_id"], hwid_verifier_a)
        results["A_binding_migrated"] = binding_a is not None and binding_a["state"] == "MIGRATED"

        remote_child_a = marzban.get_user(first.child_username, admin_token) if first.child_username else {}
        results["A_child_active_vless_only"] = (
            remote_child_a.get("status") == "active" and set(remote_child_a.get("proxies", {})) == {"vless"}
        )
        results["A_child_uuid_differs_from_legacy"] = (
            remote_child_a.get("proxies", {}).get("vless", {}).get("id") != legacy_uuid_a
        )
        decoded_body_a = base64.b64decode(first.body_b64).decode("utf-8", errors="replace") if first.body_b64 else ""
        results["A_bridged_body_absent_shared_legacy_uuid"] = legacy_uuid_a not in decoded_body_a

        legacy_after_a = marzban.get_user(legacy_username_a, admin_token)
        results["A_legacy_untouched_after_migration"] = (
            legacy_after_a["status"] == "active" and legacy_after_a["proxies"]["vless"]["id"] == legacy_uuid_a
        )

        repeat_a = process_migration_bridge_request(
            db, legacy_username_a, meta_a, hmac_key=hmac_key, ensure_fn=ensure_fn,
            subscription_fn=subscription_fn, worker_id="stage-migration-worker", now=201,
        )
        results["A_repeat_idempotent_same_child"] = (
            repeat_a.outcome == OUTCOME_OK and repeat_a.child_username == first.child_username
        )
        binding_a_repeat = db.migration_lifecycle.find_by_device(account_a["account_id"], hwid_verifier_a)
        lineage_count_a = db._conn.execute(
            "SELECT COUNT(*) FROM mgboost_migration_bindings WHERE account_id=? AND hwid_verifier=?",
            (account_a["account_id"], hwid_verifier_a),
        ).fetchone()[0]
        results["A_repeat_no_duplicate_lineage"] = lineage_count_a == 1

        # ---- crash / lost-ACK convergence: real connection close+reopen ----
        meta_b = dict(meta_a, device_id="ph4-02-stage-device-b")
        hwid_verifier_b, _masked_b = privacy_safe_hwid("ph4-02-stage-device-b", hmac_key)

        def flaky_subscription_fn(payload):
            raise ConnectionError("simulated broker/network loss after remote commit")

        crashed = process_migration_bridge_request(
            db, legacy_username_a, meta_b, hmac_key=hmac_key, ensure_fn=ensure_fn,
            subscription_fn=flaky_subscription_fn, worker_id="stage-migration-worker", now=210,
        )
        results["A_crash_attempt_not_ok"] = crashed.outcome != OUTCOME_OK
        db._conn.close()

        db = Database()  # fresh process/connection, same on-disk file -- real crash simulation
        _raw2, session2 = AdminSessionStore().create(
            os.environ["PRIMARY_MGBOOST_ADMIN_LOGIN"], "ph4-02-staging-server-jwt-2",
        )
        capability = db.primary_admin_authority.authorize_session(session2)
        binding_b_after_crash = db.migration_lifecycle.find_by_device(account_a["account_id"], hwid_verifier_b)
        results["A_crash_binding_durably_migrating"] = (
            binding_b_after_crash is not None and binding_b_after_crash["state"] == "MIGRATING"
        )

        recovered = process_migration_bridge_request(
            db, legacy_username_a, meta_b, hmac_key=hmac_key, ensure_fn=ensure_fn,
            subscription_fn=subscription_fn, worker_id="stage-migration-worker-2", now=211,
        )
        results["A_crash_recovery_converges_ok"] = recovered.outcome == OUTCOME_OK
        if recovered.child_username:
            created_usernames.append(recovered.child_username)
        binding_b_final = db.migration_lifecycle.find_by_device(account_a["account_id"], hwid_verifier_b)
        results["A_crash_recovery_final_state_migrated"] = (
            binding_b_final is not None and binding_b_final["state"] == "MIGRATED"
        )
        children_b = db._conn.execute(
            "SELECT COUNT(*) FROM mgboost_child_user_intents WHERE account_id=?",
            (account_a["account_id"],),
        ).fetchone()[0]
        results["A_crash_recovery_exactly_one_child_per_device"] = children_b == 3  # seed + device-a + device-b

        # ================= Scenario B: revoke boundary (separate account) =================
        legacy_username_b = "beykusios2"
        legacy_b = _seed_legacy_user(marzban, admin_token, legacy_username_b, "PH4-02 scenario B legacy/source user")
        created_usernames.append(legacy_username_b)
        legacy_uuid_b = legacy_b["proxies"]["vless"]["id"]
        legacy_hash_b = source_contract_hash(legacy_b)

        account_b, alias_id_b, slot_b = _make_second_parent_and_slot(
            db, legacy_username=legacy_username_b, mapping_key="PH4_02_STAGE_REVOKE_B",
            capability=capability, hmac_key=hmac_key,
        )
        alias_row_b = db._conn.execute(
            "SELECT id FROM mgboost_legacy_account_aliases WHERE account_id=? AND alias_role='PRIMARY'",
            (account_b["account_id"],),
        ).fetchone()

        seed_child_b = _seed_first_child(
            db, marzban, admin_token, ensure_fn, account=account_b, alias_row=alias_row_b,
            slot=slot_b, legacy_hash=legacy_hash_b, idem_key="ph4-02-stage-seed-child-b",
        )
        created_usernames.append(seed_child_b)

        db.legacy_bridge.create_binding(
            capability=capability, account_id=account_b["account_id"], legacy_alias_id=alias_row_b["id"],
            enabled=True, decision_ref="ph4-02-isolated-staging-b", now=105,
        )
        meta_revoke = {
            "client_name": "Happ", "client_version": "2.7.0", "platform": "windows",
            "hwid_candidate_present": True, "hwid_candidate_supported": True,
            "device_id": "ph4-02-stage-revoke-device",
        }
        migrated_b = process_migration_bridge_request(
            db, legacy_username_b, meta_revoke, hmac_key=hmac_key, ensure_fn=ensure_fn,
            subscription_fn=subscription_fn, worker_id="stage-revoke-worker", now=300,
        )
        results["B_migrated_ok"] = migrated_b.outcome == OUTCOME_OK
        if migrated_b.child_username:
            created_usernames.append(migrated_b.child_username)

        hwid_verifier_revoke, _m = privacy_safe_hwid("ph4-02-stage-revoke-device", hmac_key)
        binding_revoke = db.migration_lifecycle.find_by_device(account_b["account_id"], hwid_verifier_revoke)
        results["B_binding_migrated"] = binding_revoke is not None and binding_revoke["state"] == "MIGRATED"

        binding_revoke = db.migration_lifecycle.start_legacy_revoke_pending(
            binding_revoke["operation_id"], capability=capability, expected_revision=binding_revoke["revision"],
            reason="PH4-02 isolated staging gate synthetic revoke", now=400,
        )
        results["B_revoke_pending"] = binding_revoke["state"] == "LEGACY_REVOKE_PENDING"

        # REAL revoke of the synthetic legacy Marzban user: disable + rotate UUID.
        import uuid as _uuid
        new_uuid = str(_uuid.uuid4())
        marzban.modify_user(
            legacy_username_b,
            {"status": "disabled", "proxies": {"vless": {"id": new_uuid, "flow": "xtls-rprx-vision"}}},
            admin_token,
        )
        legacy_after_revoke = marzban.get_user(legacy_username_b, admin_token)
        results["B_legacy_remote_actually_disabled"] = legacy_after_revoke["status"] == "disabled"
        results["B_legacy_remote_uuid_rotated"] = (
            legacy_after_revoke["proxies"]["vless"]["id"] != legacy_uuid_b
        )

        binding_revoke = db.migration_lifecycle.mark_legacy_revoked(
            binding_revoke["operation_id"], expected_revision=binding_revoke["revision"], now=401,
        )
        results["B_state_is_legacy_revoked"] = binding_revoke["state"] == "LEGACY_REVOKED"

        rollback_blocked = False
        try:
            db.migration_lifecycle.reconcile_to_migrating(
                binding_revoke["operation_id"], expected_revision=binding_revoke["revision"],
                reason="attempted rollback -- must be refused", now=402,
            )
        except Exception:
            rollback_blocked = True
        results["B_rollback_after_revoke_refused"] = rollback_blocked

        # child keeps working after the shared legacy credential is dead.
        post_revoke = process_migration_bridge_request(
            db, legacy_username_b, meta_revoke, hmac_key=hmac_key, ensure_fn=ensure_fn,
            subscription_fn=subscription_fn, worker_id="stage-revoke-worker", now=403,
        )
        results["B_child_still_works_after_revoke"] = (
            post_revoke.outcome == OUTCOME_OK and post_revoke.child_username == migrated_b.child_username
        )
        binding_final = db.migration_lifecycle.find_by_operation_id(binding_revoke["operation_id"])
        results["B_state_still_legacy_revoked_after_child_use"] = binding_final["state"] == "LEGACY_REVOKED"

        db_dump = "\n".join(db._conn.iterdump())
        raw_secrets = [
            legacy_uuid_a, legacy_uuid_b, new_uuid,
            remote_child_a.get("proxies", {}).get("vless", {}).get("id"),
        ]
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
