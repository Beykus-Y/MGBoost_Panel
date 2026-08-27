#!/usr/bin/env python3
"""DL-057: execute the single owner-approved Megochel account consolidation.

No configurable target. Hardcoded to exactly the two reviewed accounts
(absorbed=MegochelPC, survivor=MegochelAndroid) and refuses to run against
anything else -- every identifying fact (account ids, legacy usernames,
Telegram owner id, genesis HWID) is asserted against the live database
before any write, and the script aborts if any assertion fails.

Every mutation goes through an existing or newly-reviewed canonical
primitive (`child_lifecycle.process_revoke/process_free`,
`account_consolidation.close_account/create_merge/set_display_name`,
`legacy_paid_compat.increase_device_limit`) -- no raw SQL write anywhere in
this file. Real Marzban credentials are used only to mint a genuine
PrimaryAdminCapability (the same pattern `run_ph3_03_production_canary.py`
uses); no raw credential/token/UUID is ever printed.

Usage: python3 scripts/dl057_megochel_consolidation.py [--dry-run]
`--dry-run` runs every preflight assertion and prints the exact plan, but
performs zero writes.
"""

from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

from dotenv import dotenv_values

from src.account_consolidation import (
    AccountConsolidationError,
    close_account,
    create_merge,
    set_display_name,
)
from src.admin_authority import PrimaryAdminAuthorizationError
from src.child_lifecycle import process_free, process_revoke
from src.config import MARZBAN_URL, PRIMARY_MGBOOST_ADMIN_LOGIN
from src.database import Database
from src.legacy_grace_migration import is_genesis_hwid_verifier
from src.legacy_paid_compat import increase_device_limit
from src.marzban import MarzbanClient
from src.security import AdminSessionStore
from src.service_marzban import ServiceMarzbanClient

DECISION_REF = "DL-057-megochel-consolidation-2026-08-27"

EXPECTED_ABSORBED_ACCOUNT_ID = 5
EXPECTED_ABSORBED_USERNAME = "MegochelPC"
EXPECTED_SURVIVOR_ACCOUNT_ID = 6
EXPECTED_SURVIVOR_USERNAME = "MegochelAndroid"
EXPECTED_SURVIVOR_TELEGRAM_ID = 1623120036
DISPLAY_NAME = "Megochel"
APPROVED_EXTRA_DEVICE_SLOTS = 3  # 3 (default) + 3 = D6, per owner decision


class PreflightFailed(RuntimeError):
    pass


def _protected_auth(path: Path) -> tuple[str, str]:
    import stat

    info = path.stat()
    if info.st_uid != os.geteuid() or info.st_mode & (
        stat.S_IWGRP | stat.S_IXGRP | stat.S_IRWXO
    ):
        raise PermissionError("broker auth environment is not sufficiently protected")
    values = dotenv_values(path)
    username = str(values.get("MARZBAN_ADMIN_USER") or "").strip()
    password = str(values.get("MARZBAN_ADMIN_PASS") or "")
    if not username or not password:
        raise RuntimeError("protected admin authentication is incomplete")
    return username, password


def _real_primary_capability(db: Database, auth_path: Path):
    username, password = _protected_auth(auth_path)
    if username != PRIMARY_MGBOOST_ADMIN_LOGIN:
        raise RuntimeError("protected admin login differs from primary mapping")
    token = MarzbanClient(MARZBAN_URL).get_token(username, password)
    password = None
    if not token:
        raise RuntimeError("real Marzban primary-admin authentication failed")
    store = AdminSessionStore()
    raw_session_id, session = store.create(username, token)
    token = None
    try:
        capability = db.primary_admin_authority.authorize_session(session)
        try:
            db.primary_admin_authority.authorize_session(object())
        except PrimaryAdminAuthorizationError:
            forged_rejected = True
        else:
            forged_rejected = False
        if not forged_rejected:
            raise RuntimeError("forged primary-admin session was accepted")
        return capability, store, raw_session_id
    except Exception:
        store.revoke(raw_session_id)
        raise


def _preflight(db: Database, hmac_key: str) -> dict:
    conn = db._conn
    absorbed = db.accounts.get_account(EXPECTED_ABSORBED_ACCOUNT_ID)
    survivor = db.accounts.get_account(EXPECTED_SURVIVOR_ACCOUNT_ID)
    if absorbed is None or survivor is None:
        raise PreflightFailed("expected accounts do not exist")
    if absorbed["status"] != "ACTIVE" or survivor["status"] != "ACTIVE":
        raise PreflightFailed(
            f"expected both accounts ACTIVE, got absorbed={absorbed['status']!r} "
            f"survivor={survivor['status']!r}"
        )

    absorbed_alias = conn.execute(
        "SELECT legacy_username FROM mgboost_legacy_account_aliases "
        "WHERE account_id=? AND alias_role='PRIMARY'", (EXPECTED_ABSORBED_ACCOUNT_ID,),
    ).fetchone()
    survivor_alias = conn.execute(
        "SELECT legacy_username FROM mgboost_legacy_account_aliases "
        "WHERE account_id=? AND alias_role='PRIMARY'", (EXPECTED_SURVIVOR_ACCOUNT_ID,),
    ).fetchone()
    if not absorbed_alias or absorbed_alias["legacy_username"] != EXPECTED_ABSORBED_USERNAME:
        raise PreflightFailed("absorbed account's PRIMARY alias does not match expectation")
    if not survivor_alias or survivor_alias["legacy_username"] != EXPECTED_SURVIVOR_USERNAME:
        raise PreflightFailed("survivor account's PRIMARY alias does not match expectation")

    owner = conn.execute(
        "SELECT telegram_id FROM mgboost_telegram_identities "
        "WHERE account_id=? AND role='OWNER' AND revoked_at IS NULL",
        (EXPECTED_SURVIVOR_ACCOUNT_ID,),
    ).fetchone()
    if not owner or int(owner["telegram_id"]) != EXPECTED_SURVIVOR_TELEGRAM_ID:
        raise PreflightFailed("survivor account's Telegram OWNER does not match expectation")

    absorbed_owner = conn.execute(
        "SELECT 1 FROM mgboost_telegram_identities "
        "WHERE account_id=? AND role='OWNER' AND revoked_at IS NULL",
        (EXPECTED_ABSORBED_ACCOUNT_ID,),
    ).fetchone()
    if absorbed_owner is not None:
        raise PreflightFailed(
            "absorbed account unexpectedly has an active Telegram OWNER identity"
        )

    existing_merge = conn.execute(
        "SELECT status FROM mgboost_account_merges WHERE absorbed_account_id=?",
        (EXPECTED_ABSORBED_ACCOUNT_ID,),
    ).fetchone()
    if existing_merge is not None:
        raise PreflightFailed(
            f"a merge already exists for the absorbed account (status={existing_merge['status']!r})"
        )

    non_terminal_children = conn.execute(
        "SELECT id,slot_generation_id,child_username FROM mgboost_child_user_intents "
        "WHERE account_id=? AND (desired_state!='REVOKED' OR observed_state NOT IN ('REVOKED','NOT_CREATED'))",
        (EXPECTED_ABSORBED_ACCOUNT_ID,),
    ).fetchall()
    if len(non_terminal_children) != 1:
        raise PreflightFailed(
            f"expected exactly one non-terminal child intent on the absorbed account, found "
            f"{len(non_terminal_children)} -- refusing to guess which one is the genesis placeholder"
        )
    child = non_terminal_children[0]

    generation = conn.execute(
        "SELECT hwid_verifier FROM mgboost_device_slot_generations WHERE id=?",
        (child["slot_generation_id"],),
    ).fetchone()
    if not is_genesis_hwid_verifier(EXPECTED_ABSORBED_ACCOUNT_ID, generation["hwid_verifier"], hmac_key):
        raise PreflightFailed(
            "the absorbed account's only non-terminal child is NOT provably the synthetic "
            "genesis placeholder -- refusing to revoke what may be a real customer device"
        )

    sub = conn.execute(
        "SELECT id FROM mgboost_subscriptions WHERE account_id=? AND status='ACTIVE'",
        (EXPECTED_SURVIVOR_ACCOUNT_ID,),
    ).fetchone()
    if sub is None:
        raise PreflightFailed("survivor account has no live ACTIVE subscription")

    return {
        "absorbed_child_intent_id": child["id"],
        "absorbed_child_username": child["child_username"],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--auth-path", type=Path, default=Path("/etc/mgboost/marzban-broker.env"),
    )
    args = parser.parse_args()

    db = Database()
    hmac_key = os.getenv("DEVICE_SLOT_HMAC_KEY", "")
    if not hmac_key:
        from src.config import DEVICE_SLOT_HMAC_KEY

        hmac_key = DEVICE_SLOT_HMAC_KEY

    plan = _preflight(db, hmac_key)
    print(
        "PREFLIGHT OK: absorbed_account_id=%d (%s), survivor_account_id=%d (%s), "
        "genesis_child_intent_id=%d, genesis_child_username=%s"
        % (
            EXPECTED_ABSORBED_ACCOUNT_ID, EXPECTED_ABSORBED_USERNAME,
            EXPECTED_SURVIVOR_ACCOUNT_ID, EXPECTED_SURVIVOR_USERNAME,
            plan["absorbed_child_intent_id"], plan["absorbed_child_username"],
        )
    )
    if args.dry_run:
        print("--dry-run: no writes performed.")
        return

    capability, session_store, session_id = _real_primary_capability(db, args.auth_path)
    try:
        worker_id = f"dl057-consolidation:{os.getpid()}"
        now = int(time.time())

        marzban = ServiceMarzbanClient(
            broker_client_id=os.getenv("MARZBAN_BROKER_CLIENT_ID", "mgboost-main"),
        )
        marzban.assert_credential_boundary()

        def _revoke_fn(payload: dict) -> dict:
            result = marzban.revoke_child_user(payload)
            if not isinstance(result, dict) or "outcome" not in result:
                raise RuntimeError("invalid revoke outcome contract")
            return result

        # 1) Revoke the absorbed account's genesis child.
        revoke_prepared = db.child_lifecycle.prepare_revoke(
            account_id=EXPECTED_ABSORBED_ACCOUNT_ID,
            old_child_intent_id=plan["absorbed_child_intent_id"],
            reason="DL-057 Megochel consolidation: retiring absorbed account's genesis placeholder",
            idempotency_key=f"dl057-revoke-v1:{EXPECTED_ABSORBED_ACCOUNT_ID}", now=now,
        )
        revoke_result = process_revoke(
            db, revoke_prepared["operation_id"], worker_id=worker_id,
            revoke_fn=_revoke_fn, now=now,
        )
        print(f"REVOKE: state={revoke_result['state'] if revoke_result else 'ALREADY_CLAIMED'}")

        # 2) Free the slot.
        free_prepared = db.child_lifecycle.prepare_free(
            account_id=EXPECTED_ABSORBED_ACCOUNT_ID,
            old_child_intent_id=plan["absorbed_child_intent_id"],
            reason="DL-057 Megochel consolidation: freeing absorbed account's genesis slot",
            idempotency_key=f"dl057-free-v1:{EXPECTED_ABSORBED_ACCOUNT_ID}", now=now,
        )
        free_result = process_free(
            db, free_prepared["operation_id"], worker_id=worker_id, now=now,
        )
        print(f"FREE: state={free_result['state'] if free_result else 'ALREADY_CLAIMED'}")

        # 3) Close the absorbed account.
        close_result = close_account(
            db, capability=capability, account_id=EXPECTED_ABSORBED_ACCOUNT_ID,
            decision_ref=DECISION_REF,
            reason="DL-057: MegochelPC consolidated into Megochel (MegochelAndroid), same real person",
            now=now,
        )
        print(f"CLOSE: {close_result}")

        # 4) Create the merge.
        merge_result = create_merge(
            db, capability=capability, absorbed_account_id=EXPECTED_ABSORBED_ACCOUNT_ID,
            survivor_account_id=EXPECTED_SURVIVOR_ACCOUNT_ID, decision_ref=DECISION_REF,
            reason="DL-057: owner-approved consolidation, same real person (MegochelPC + MegochelAndroid)",
            now=now,
        )
        print(f"MERGE: id={merge_result['id']} status={merge_result['status']} "
              f"already_applied={merge_result['already_applied']}")

        # 5) Set the survivor's human-facing display name.
        name_result = set_display_name(
            db, capability=capability, account_id=EXPECTED_SURVIVOR_ACCOUNT_ID,
            display_name=DISPLAY_NAME, decision_ref=DECISION_REF, now=now,
        )
        print(f"DISPLAY_NAME: {name_result}")

        # 6) D3 -> D6 on the survivor's already-provisioned legacy-compat subscription.
        limit_result = increase_device_limit(
            db, capability=capability, account_id=EXPECTED_SURVIVOR_ACCOUNT_ID,
            approved_extra_device_slots=APPROVED_EXTRA_DEVICE_SLOTS, decision_ref=DECISION_REF,
            evidence={
                "trusted_user": True,
                "owner_decision": "DL-057: trusted user, additional device slots explicitly approved",
            },
            now=now,
        )
        print(
            f"DEVICE_LIMIT: subscription_id={limit_result['id']} "
            f"plan_code={limit_result['_plan']['plan_code']} "
            f"already_applied={limit_result['already_applied']}"
        )

        print("DL-057 Megochel consolidation completed successfully.")
    except (AccountConsolidationError, RuntimeError) as exc:
        print(f"ABORTED: {exc}")
        raise
    finally:
        session_store.revoke(session_id)


if __name__ == "__main__":
    main()
