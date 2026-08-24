#!/usr/bin/env python3
"""Apply and verify PH3-07 on a disposable production DB copy."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3

from src.compat_telemetry import record_observation
from src.compat_telemetry_schema import apply_compat_telemetry_schema


CANARY_TOKEN = "ph3-07-staging-raw-token-canary"
CANARY_HWID = "ph3-07-staging-raw-hwid-canary"
CANARY_KEY = "ph3-07-staging-hmac-key-material-at-least-32-bytes"


def table_digest(connection, tables):
    digest = hashlib.sha256()
    for table in tables:
        digest.update(table.encode())
        for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid'):
            digest.update(repr(tuple(row)).encode("utf-8"))
    return digest.hexdigest()


def count(connection, table):
    return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    args = parser.parse_args()
    connection = sqlite3.connect(args.db)
    try:
        existing_tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' AND name!='mgboost_schema_migrations' "
                "ORDER BY name"
            )
        ]
        before = table_digest(connection, existing_tables)
        before_legacy = {
            "devices": count(connection, "user_devices"),
            "hwid_locks": count(connection, "hwid_lock"),
            "accounts": count(connection, "mgboost_accounts"),
            "slots": count(connection, "mgboost_device_slots"),
            "generations": count(connection, "mgboost_device_slot_generations"),
        }
        first = apply_compat_telemetry_schema(connection, now=1)
        second = apply_compat_telemetry_schema(connection, now=2)
        after = table_digest(connection, existing_tables)
        if before != after:
            raise SystemExit("pre-existing table digest changed")
        record_observation(
            args.db,
            CANARY_TOKEN,
            {
                "device_id": CANARY_HWID,
                "client_name": "Happ",
                "client_version": "stage",
                "platform": "Android",
            },
            CANARY_KEY,
            now=100,
            timeout_seconds=2,
        )
        connection.close()
        connection = sqlite3.connect(args.db)
        after_runtime = {
            "devices": count(connection, "user_devices"),
            "hwid_locks": count(connection, "hwid_lock"),
            "accounts": count(connection, "mgboost_accounts"),
            "slots": count(connection, "mgboost_device_slots"),
            "generations": count(connection, "mgboost_device_slot_generations"),
        }
        if before_legacy != after_runtime:
            raise SystemExit("legacy/parent/slot state changed")
        raw = open(args.db, "rb").read()
        if any(value.encode() in raw for value in (CANARY_TOKEN, CANARY_HWID, CANARY_KEY)):
            raise SystemExit("raw canary persisted")
        result = {
            "first_apply": first,
            "second_apply": second,
            "preexisting_table_count": len(existing_tables),
            "preexisting_digest_preserved": before == after,
            "legacy_state": after_runtime,
            "telemetry_subject_rows": count(connection, "mgboost_hwid_compat_subjects"),
            "telemetry_daily_rows": count(connection, "mgboost_hwid_compat_daily"),
            "quick_check": connection.execute("PRAGMA quick_check").fetchone()[0],
            "foreign_key_violations": len(connection.execute("PRAGMA foreign_key_check").fetchall()),
            "raw_canaries_persisted": False,
        }
        print(json.dumps(result, indent=2, sort_keys=True))
    finally:
        connection.close()


if __name__ == "__main__":
    main()
