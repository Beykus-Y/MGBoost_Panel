#!/usr/bin/env python3
"""Fail-closed production preflight for the dormant PH3-03 reconciler."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3

from src.child_contract import credential_verifier
from src.child_workflow_schema import MIGRATION_ID, SCHEMA_CHECKSUM
from src.service_marzban import ServiceMarzbanClient


EXPECTED_OPERATION = "op_lw33pjhqhnvorrgh4p754bnc34"
EXPECTED_CHILD = "mgc_sgg6v7t6he43yytsqmkdczzfpa"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=os.path.join(os.getenv("DATA_DIR", "./data"), "db.sqlite3"))
    parser.add_argument("--operation-id", default=EXPECTED_OPERATION)
    args = parser.parse_args()
    if args.operation_id != EXPECTED_OPERATION:
        raise RuntimeError("production gate permits only the approved dormant operation")
    if os.getenv("CHILD_WORKER_MODE") != "reconcile_only":
        raise RuntimeError("production worker must be reconcile_only")
    allowed = [item.strip() for item in os.getenv(
        "CHILD_WORKER_ALLOWED_OPERATION_IDS", ""
    ).split(",") if item.strip()]
    if allowed != [EXPECTED_OPERATION]:
        raise RuntimeError("production worker allowlist is not the exact canary operation")

    connection = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        marker = connection.execute(
            "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
            (MIGRATION_ID,),
        ).fetchone()
        if not marker or marker[0] != SCHEMA_CHECKSUM:
            raise RuntimeError("durable child workflow migration is absent or incompatible")
        expected_counts = {
            "mgboost_accounts": 1,
            "mgboost_legacy_account_aliases": 3,
            "mgboost_device_slots": 1,
            "mgboost_device_slot_generations": 1,
            "mgboost_child_user_intents": 1,
            "mgboost_outbox": 1,
        }
        counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in expected_counts
        }
        if counts != expected_counts:
            raise RuntimeError("dormant production row counts differ from approved baseline")
        operation = connection.execute(
            "SELECT o.id,o.account_id,o.child_intent_id,o.operation_id,o.state,o.attempts,"
            "c.child_username,c.slot_number,c.generation,c.uuid_verifier,c.uuid_masked "
            "FROM mgboost_outbox o JOIN mgboost_child_user_intents c "
            "ON c.id=o.child_intent_id AND c.account_id=o.account_id"
        ).fetchone()
        if not operation or (
            operation["operation_id"], operation["state"], operation["child_username"],
            operation["slot_number"], operation["generation"]
        ) != (EXPECTED_OPERATION, "APPLIED", EXPECTED_CHILD, 1, 1):
            raise RuntimeError("approved dormant operation state drift")
        pending = connection.execute(
            "SELECT COUNT(*) FROM mgboost_outbox WHERE state IN ('PENDING','RETRY','IN_FLIGHT')"
        ).fetchone()[0]
        if pending:
            raise RuntimeError("activation would encounter a non-applied outbox row")
        payload = json.loads(connection.execute(
            "SELECT payload_json FROM mgboost_outbox WHERE operation_id=?", (EXPECTED_OPERATION,)
        ).fetchone()[0])
    finally:
        connection.close()

    client = ServiceMarzbanClient()
    client.assert_credential_boundary()
    observed = client.observe_child_user(payload)
    if observed.get("presence") != "MATCH":
        raise RuntimeError("approved remote child does not match the durable contract")
    if credential_verifier(observed.pop("uuid")) != operation["uuid_verifier"]:
        raise RuntimeError("approved remote child credential verifier drift")
    users = client.get_users(client.get_admin_token_from_env(), limit=1000, offset=0)
    remote_users = users.get("users", users) if isinstance(users, dict) else users
    child_count = sum(1 for user in remote_users if user.get("username", "").startswith("mgc_"))
    inbounds = client.get_inbounds(client.get_admin_token_from_env())
    vless_count = len((inbounds or {}).get("vless", []))
    ss_count = len((inbounds or {}).get("shadowsocks", []))
    if (child_count, vless_count, ss_count) != (1, 25, 0):
        raise RuntimeError("production Marzban child/topology baseline drift")
    print(json.dumps({
        "preflight": "PASS",
        "activation_plan": "READ_RECONCILE_ONLY",
        "operation_id": operation["operation_id"],
        "account_id": operation["account_id"],
        "child_intent_id": operation["child_intent_id"],
        "child_username": operation["child_username"],
        "slot": operation["slot_number"],
        "generation": operation["generation"],
        "uuid_masked": operation["uuid_masked"],
        "pending_outbox": pending,
        "remote_child_count": child_count,
        "vless_inbound_count": vless_count,
        "shadowsocks_inbound_count": ss_count,
        "new_intents_created_by_preflight": 0,
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
