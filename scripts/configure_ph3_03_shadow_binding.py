#!/usr/bin/env python3
"""Root-only tool: create/enable/disable the ONE approved PH3-03 SHADOW
resolver binding for the single owner-approved dormant canary.

This tool is deliberately narrow. It has no configurable account, alias,
device, slot or child target -- everything is checked against the fixed,
already-approved manifest below. It never scans legacy users, never creates
more than one binding, and is never imported by application startup or any
HTTP route. It prints no raw HWID, UUID or subscription token -- only row
ids, booleans and the fixed public identifiers already published in
ROADMAP.md/docs.
"""

from __future__ import annotations

import argparse
import json
import sqlite3

from src.database import Database


MAPPING_KEY = "INTERNAL_OWNER_PRIMARY"
SOURCE_USERNAME = "beykusios"
DEVICE_ROW_ID = 56
EXPECTED_ACCOUNT_PUBLIC_ID = "acct_435p4hjeoxeq3bzg4ifkdut4veower4r"
EXPECTED_CHILD_USERNAME = "mgc_sgg6v7t6he43yytsqmkdczzfpa"
EXPECTED_OPERATION_ID = "op_lw33pjhqhnvorrgh4p754bnc34"
EXPECTED_SOURCE_HASH = "52bd127165402fd429e47b4fa485a53566f8870af2514f6c82d4de204287ff47"
EXPECTED_SLOT_NUMBER = 1
EXPECTED_GENERATION = 1
DECISION_REF = "ph3-03-shadow-canary-v1"
EXPECTED_DEVICE = {
    "username": SOURCE_USERNAME,
    "device_name": "iPhone 17",
    "platform": "iOS",
    "client_name": "INCY",
    "client_version": "2.5.2",
    "is_active": 1,
}


class ShadowBindingToolError(RuntimeError):
    pass


def _resolve_approved_manifest(conn: sqlite3.Connection) -> dict:
    """Resolve every id the fixed canary manifest needs, verifying each
    field against the approved constants above. Raises on any mismatch --
    never guesses, never widens scope, never auto-corrects."""

    group = conn.execute(
        "SELECT account_id FROM mgboost_legacy_alias_groups WHERE mapping_key=?",
        (MAPPING_KEY,),
    ).fetchone()
    if not group:
        raise ShadowBindingToolError(f"no alias group for mapping_key={MAPPING_KEY!r}")
    account_id = group["account_id"]

    account = conn.execute(
        "SELECT id, public_id, account_source, status FROM mgboost_accounts WHERE id=?",
        (account_id,),
    ).fetchone()
    if not account or account["public_id"] != EXPECTED_ACCOUNT_PUBLIC_ID:
        raise ShadowBindingToolError("resolved account public id does not match approved manifest")
    if account["status"] != "ACTIVE":
        raise ShadowBindingToolError("approved account is not ACTIVE")

    alias = conn.execute(
        "SELECT id FROM mgboost_legacy_account_aliases WHERE account_id=? AND legacy_username=?",
        (account_id, SOURCE_USERNAME),
    ).fetchone()
    if not alias:
        raise ShadowBindingToolError(
            f"legacy alias {SOURCE_USERNAME!r} does not belong to account {account_id}"
        )
    alias_id = alias["id"]

    alias_count = conn.execute(
        "SELECT COUNT(*) FROM mgboost_legacy_account_aliases WHERE account_id=?",
        (account_id,),
    ).fetchone()[0]
    if alias_count != 3:
        raise ShadowBindingToolError(f"unexpected alias cardinality: {alias_count} (expected 3)")

    device = conn.execute(
        "SELECT id, username, request_key, device_name, platform, client_name, "
        "client_version, is_active FROM user_devices WHERE id=?",
        (DEVICE_ROW_ID,),
    ).fetchone()
    if not device:
        raise ShadowBindingToolError(f"legacy device row {DEVICE_ROW_ID} does not exist")
    if device["username"] != SOURCE_USERNAME:
        raise ShadowBindingToolError("legacy device row does not belong to the approved alias")
    if not (device["request_key"] or "").startswith("hwid:"):
        raise ShadowBindingToolError("legacy device row is not HWID-backed")
    observed_device = {
        "username": device["username"], "device_name": device["device_name"],
        "platform": device["platform"], "client_name": device["client_name"],
        "client_version": device["client_version"], "is_active": device["is_active"],
    }
    if observed_device != EXPECTED_DEVICE:
        raise ShadowBindingToolError("legacy device row does not match the approved device contract")

    slot_generation = conn.execute(
        "SELECT g.id AS generation_id, g.status AS generation_status, "
        "s.desired_state AS slot_desired_state, s.current_generation "
        "FROM mgboost_device_slot_generations g "
        "JOIN mgboost_device_slots s ON s.id=g.slot_id AND s.account_id=g.account_id "
        "WHERE g.account_id=? AND g.slot_number=? AND g.generation=?",
        (account_id, EXPECTED_SLOT_NUMBER, EXPECTED_GENERATION),
    ).fetchone()
    if not slot_generation:
        raise ShadowBindingToolError("approved slot/generation does not exist for this account")
    if (
        slot_generation["generation_status"] != "ACTIVE"
        or slot_generation["slot_desired_state"] != "ACTIVE"
        or slot_generation["current_generation"] != EXPECTED_GENERATION
    ):
        raise ShadowBindingToolError("approved slot/generation is not the current active generation")
    slot_generation_id = slot_generation["generation_id"]

    intent = conn.execute(
        "SELECT id, child_username, source_contract_hash FROM mgboost_child_user_intents "
        "WHERE account_id=? AND slot_generation_id=?",
        (account_id, slot_generation_id),
    ).fetchone()
    if not intent:
        raise ShadowBindingToolError("no child intent for the approved account/slot generation")
    if intent["child_username"] != EXPECTED_CHILD_USERNAME:
        raise ShadowBindingToolError("child intent username does not match approved manifest")
    if intent["source_contract_hash"] != EXPECTED_SOURCE_HASH:
        raise ShadowBindingToolError("child intent source contract hash does not match approved manifest")
    child_intent_id = intent["id"]

    outbox = conn.execute(
        "SELECT id, state FROM mgboost_outbox "
        "WHERE operation_id=? AND account_id=? AND child_intent_id=?",
        (EXPECTED_OPERATION_ID, account_id, child_intent_id),
    ).fetchone()
    if not outbox:
        raise ShadowBindingToolError("approved outbox operation does not exist for this child intent")
    if outbox["state"] != "APPLIED":
        raise ShadowBindingToolError(f"approved outbox operation is not APPLIED (state={outbox['state']!r})")

    workflow = conn.execute(
        "SELECT reconcile_state FROM mgboost_child_workflow_state WHERE outbox_id=?",
        (outbox["id"],),
    ).fetchone()
    if not workflow or workflow["reconcile_state"] != "IN_SYNC":
        raise ShadowBindingToolError(
            "child reconciliation is not IN_SYNC "
            f"(state={workflow['reconcile_state'] if workflow else 'MISSING'})"
        )

    return {
        "account_id": account_id, "legacy_alias_id": alias_id,
        "legacy_device_id": DEVICE_ROW_ID, "slot_generation_id": slot_generation_id,
        "child_intent_id": child_intent_id, "operation_id": EXPECTED_OPERATION_ID,
    }


def _identity_fields(binding: dict) -> dict:
    return {
        key: binding[key] for key in (
            "account_id", "legacy_alias_id", "legacy_device_id",
            "slot_generation_id", "child_intent_id", "operation_id", "mode",
        )
    }


def create(db: Database) -> dict:
    manifest = _resolve_approved_manifest(db._conn)
    total_bindings = db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_shadow_resolver_bindings"
    ).fetchone()[0]
    existing = db.shadow_resolver_bindings.get_binding_by_device(manifest["legacy_device_id"])
    if existing is None:
        if total_bindings != 0:
            raise ShadowBindingToolError(
                f"unexpected pre-existing shadow binding cardinality: {total_bindings} "
                "(refusing to create a second binding for a different device)"
            )
        created = db.shadow_resolver_bindings.create_binding(
            decision_ref=DECISION_REF, enabled=False, **manifest,
        )
        return {"outcome": "CREATED", "binding_id": created["id"], "enabled": bool(created["enabled"])}

    expected_identity = {**manifest, "mode": "SHADOW"}
    if _identity_fields(existing) != expected_identity:
        raise ShadowBindingToolError(
            "an existing shadow binding for this legacy device has conflicting identity; "
            "refusing to silently update an immutable row"
        )
    return {"outcome": "EXISTING", "binding_id": existing["id"], "enabled": bool(existing["enabled"])}


def _set_enabled(db: Database, enabled: bool) -> dict:
    manifest = _resolve_approved_manifest(db._conn)
    existing = db.shadow_resolver_bindings.get_binding_by_device(manifest["legacy_device_id"])
    if existing is None:
        raise ShadowBindingToolError("no shadow binding exists yet -- run --action create first")
    if bool(existing["enabled"]) == enabled:
        return {"outcome": "NO_OP", "binding_id": existing["id"], "enabled": enabled}
    db.shadow_resolver_bindings.set_enabled(existing["id"], enabled)
    return {"outcome": "UPDATED", "binding_id": existing["id"], "enabled": enabled}


def status(db: Database) -> dict:
    total = db._conn.execute("SELECT COUNT(*) FROM mgboost_shadow_resolver_bindings").fetchone()[0]
    enabled_count = db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_shadow_resolver_bindings WHERE enabled=1"
    ).fetchone()[0]
    row = db.shadow_resolver_bindings.get_binding_by_device(DEVICE_ROW_ID)
    return {
        "total_bindings": total, "enabled_bindings": enabled_count,
        "approved_canary_binding_id": row["id"] if row else None,
        "approved_canary_enabled": bool(row["enabled"]) if row else None,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--action", required=True, choices=("create", "enable", "disable", "status"),
    )
    args = parser.parse_args()
    db = Database()
    try:
        if args.action == "create":
            result = create(db)
        elif args.action == "enable":
            result = _set_enabled(db, True)
        elif args.action == "disable":
            result = _set_enabled(db, False)
        else:
            result = status(db)
        print(json.dumps(result, sort_keys=True))
    finally:
        db._conn.close()


if __name__ == "__main__":
    main()
