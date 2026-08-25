#!/usr/bin/env python3
"""Real Marzban 0.8.4 PH2-05 Telegram ownership rebind staging gate.

Same isolated-target contract as every other PH3-0x/PH2-01/PH4-01 gate.
Ownership rebind itself never calls Marzban -- this gate's job is to prove
that a real child, once created via the unmodified PH3-03 pipeline, is
completely untouched (same username/generation/UUID) across both an
ordinary and a suspected-compromise rebind, and that the compromise mode's
mandatory opaque-token rotation is exactly one new generation.

Prints no raw UUID/token/admin credential.
"""

from __future__ import annotations

import argparse
import json
import os

from src.broker_operations import BrokerOperations
from src.child_contract import source_contract_hash
from src.database import Database
from src.marzban import MarzbanClient
from src.ownership_rebind import process_rebind
from src.security import AdminSessionStore
from scripts.build_ph3_03_staging_xray import EFFECTIVE_VLESS_TAGS
from scripts.verify_ph3_03_marzban_staging import EXPECTED_VERSION, require_isolated_url


def _make_parent(db, *, mapping, tg, source_username, source_hash):
    _raw, session = AdminSessionStore().create(
        os.environ["PRIMARY_MGBOOST_ADMIN_LOGIN"], "staging-server-jwt",
    )
    capability = db.primary_admin_authority.authorize_session(session)
    plan = db.internal_entitlements.create_internal_plan(
        capability=capability, plan_code=f"PH2_05_CANARY_{mapping}", version=1,
        display_name="PH2-05 canary", device_limit_mode="LIMITED", device_limit=10,
        wl_mode="UNLIMITED", terms={"schema": 1, "staging": True}, now=100,
    )
    account = db.internal_entitlements.create_reviewed_account(
        capability=capability, plan_version_id=plan["id"], legacy_username=source_username,
        mapping_key=mapping, decision_ref=f"ph2-05-isolated-staging-{mapping}",
        legacy_aliases=[{
            "legacy_username": source_username, "alias_role": "PRIMARY",
            "ownership_provenance": "OWNER_APPROVED", "legacy_status": "UNLIMITED",
            "legacy_expiry": None, "observed_device_count": 1, "observed_hwid_count": 1,
            "evidence": {"staging": True},
        }],
        ownership_evidence="PROVEN", telegram_id=tg, legacy_status="UNLIMITED", legacy_expiry=None,
        device_evidence_count=1, hwid_evidence_count=1,
        internal_reason="PH2-05 isolated ownership-rebind canary", migration_confidence="HIGH",
        evidence={"schema": 1}, idempotency_key=f"ph2-05-canary-{mapping}", now=100,
    )
    alias = db._conn.execute(
        "SELECT id FROM mgboost_legacy_account_aliases WHERE account_id=?",
        (account["account_id"],),
    ).fetchone()
    slot = db.device_slots.claim(
        account["account_id"], f"ph2-05-{mapping}-hwid", os.environ["DEVICE_SLOT_HMAC_KEY"], now=101,
    )
    return account, alias["id"], slot, capability


def _seed_child(db, marzban, direct_ops, account, alias_id, slot, source_hash, idem):
    prepared = db.child_provisioning.prepare_child_ensure(
        account_id=account["account_id"], slot_generation_id=slot["generation_id"],
        source_alias_id=alias_id, source_contract_hash=source_hash, expire=0,
        idempotency_key=idem, now=102,
    )
    claimed = db.child_provisioning.claim(
        prepared["operation_id"], worker_id="stage-seed-worker", now=103, lease_seconds=5,
    )
    created = direct_ops.dispatch("child.user.ensure", claimed["payload"])
    raw_uuid = created.pop("uuid")
    db.child_provisioning.acknowledge(
        prepared["operation_id"], worker_id="stage-seed-worker",
        outcome=created["outcome"], child_uuid=raw_uuid, remote_result=created, now=104,
    )
    return prepared["child_username"], raw_uuid


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

    results = {}
    created_usernames = []
    db = Database()
    try:
        direct_ops = BrokerOperations(marzban)

        def _make_source(username):
            payload = {
                "username": username,
                "proxies": {"vless": {"flow": "xtls-rprx-vision"}},
                "inbounds": {"vless": list(EFFECTIVE_VLESS_TAGS)},
                "expire": 0, "data_limit": None,
                "data_limit_reset_strategy": "no_reset", "status": "active",
                "note": "PH2-05 ownership rebind isolated source template",
            }
            marzban.create_user(payload, admin_token)
            created_usernames.append(username)
            return source_contract_hash(marzban.get_user(username, admin_token))

        # ================= scenario A: ORDINARY rebind =================
        source_username_a = "ph2-05-stage-source-a"
        source_hash_a = _make_source(source_username_a)
        account_a, alias_a, slot_a, cap_a = _make_parent(
            db, mapping="ORD", tg=700001, source_username=source_username_a, source_hash=source_hash_a,
        )
        child_username_a, child_uuid_a = _seed_child(
            db, marzban, direct_ops, account_a, alias_a, slot_a, source_hash_a, "ph2-05-ord-child-v1",
        )
        created_usernames.append(child_username_a)
        prepared_cred_a = db.subscription_credentials.prepare(
            account_id=account_a["account_id"], actor_ref="stage-primary-admin",
            reason="ph2-05 ordinary staging credential", idempotency_key="ph2-05-ord-cred-prepare-v1",
            now=110,
        )
        db.subscription_credentials.activate(
            credential_id=prepared_cred_a["id"], account_id=account_a["account_id"],
            expected_generation=prepared_cred_a["generation"], actor_ref="stage-primary-admin",
            idempotency_key="ph2-05-ord-cred-activate-v1", now=111,
        )
        token_a = prepared_cred_a["raw_token"]

        rebind_a = db.ownership_rebind.prepare(
            capability=cap_a, account_id=account_a["account_id"], expected_old_telegram_id=700001,
            new_telegram_id=700002, mode="ORDINARY", reason="ph2-05 isolated ordinary staging rebind",
            idempotency_key="ph2-05-ord-rebind-v1", now=120,
        )
        result_a = process_rebind(db, rebind_a["operation_id"], worker_id="stage-rebind-worker", now=121)
        results["ordinary_applied"] = result_a["state"] == "APPLIED"

        owner_a_new = db._conn.execute(
            "SELECT telegram_id FROM mgboost_telegram_identities WHERE account_id=? "
            "AND role='OWNER' AND revoked_at IS NULL", (account_a["account_id"],),
        ).fetchone()
        results["ordinary_new_owner_active"] = owner_a_new["telegram_id"] == 700002
        old_owner_a = db._conn.execute(
            "SELECT revoked_at FROM mgboost_telegram_identities WHERE account_id=? AND telegram_id=700001",
            (account_a["account_id"],),
        ).fetchone()
        results["ordinary_old_owner_revoked"] = old_owner_a["revoked_at"] is not None

        results["ordinary_token_unchanged"] = db.subscription_credentials.resolve(token_a, now=122) is not None

        remote_child_a = marzban.get_user(child_username_a, admin_token)
        results["ordinary_child_untouched"] = (
            remote_child_a["username"] == child_username_a
            and remote_child_a["proxies"]["vless"]["id"] == child_uuid_a
            and remote_child_a["status"] == "active"
        )

        # ================= scenario B: COMPROMISE rebind (fresh setup) =================
        source_username_b = "ph2-05-stage-source-b"
        source_hash_b = _make_source(source_username_b)
        account_b, alias_b, slot_b, cap_b = _make_parent(
            db, mapping="CMP", tg=700010, source_username=source_username_b, source_hash=source_hash_b,
        )
        child_username_b, child_uuid_b = _seed_child(
            db, marzban, direct_ops, account_b, alias_b, slot_b, source_hash_b, "ph2-05-cmp-child-v1",
        )
        created_usernames.append(child_username_b)
        prepared_cred_b = db.subscription_credentials.prepare(
            account_id=account_b["account_id"], actor_ref="stage-primary-admin",
            reason="ph2-05 compromise staging credential", idempotency_key="ph2-05-cmp-cred-prepare-v1",
            now=130,
        )
        db.subscription_credentials.activate(
            credential_id=prepared_cred_b["id"], account_id=account_b["account_id"],
            expected_generation=prepared_cred_b["generation"], actor_ref="stage-primary-admin",
            idempotency_key="ph2-05-cmp-cred-activate-v1", now=131,
        )
        token_b_old = prepared_cred_b["raw_token"]

        rebind_b = db.ownership_rebind.prepare(
            capability=cap_b, account_id=account_b["account_id"], expected_old_telegram_id=700010,
            new_telegram_id=700011, mode="COMPROMISE", reason="ph2-05 isolated compromise staging rebind",
            idempotency_key="ph2-05-cmp-rebind-v1", now=140,
        )
        result_b = process_rebind(db, rebind_b["operation_id"], worker_id="stage-rebind-worker", now=141)
        results["compromise_applied"] = result_b["state"] == "APPLIED"

        results["compromise_old_token_denied"] = db.subscription_credentials.resolve(token_b_old, now=142) is None
        new_credential_row = db._conn.execute(
            "SELECT generation FROM mgboost_subscription_credentials WHERE account_id=? AND status='ACTIVE'",
            (account_b["account_id"],),
        ).fetchone()
        results["compromise_exactly_one_new_generation"] = new_credential_row["generation"] == 2

        owner_b_new = db._conn.execute(
            "SELECT telegram_id FROM mgboost_telegram_identities WHERE account_id=? "
            "AND role='OWNER' AND revoked_at IS NULL", (account_b["account_id"],),
        ).fetchone()
        results["compromise_new_owner_active"] = owner_b_new["telegram_id"] == 700011
        old_owner_b = db._conn.execute(
            "SELECT revoked_at FROM mgboost_telegram_identities WHERE account_id=? AND telegram_id=700010",
            (account_b["account_id"],),
        ).fetchone()
        results["compromise_old_owner_revoked"] = old_owner_b["revoked_at"] is not None

        remote_child_b = marzban.get_user(child_username_b, admin_token)
        results["compromise_child_uuid_unchanged"] = (
            remote_child_b["proxies"]["vless"]["id"] == child_uuid_b
            and remote_child_b["status"] == "active"
        )

        db_dump = "\n".join(db._conn.iterdump())
        raw_secrets = [token_a, token_b_old, child_uuid_a, child_uuid_b]
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
