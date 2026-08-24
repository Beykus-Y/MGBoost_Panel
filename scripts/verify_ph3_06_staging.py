#!/usr/bin/env python3
"""Apply and verify dormant PH3-06 on a disposable production DB copy."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3

from src.internal_entitlement_schema import (
    MIGRATION_ID,
    NEW_RUNTIME_TABLES,
    apply_internal_entitlement_schema,
)


def _digest(connection, tables):
    digest = hashlib.sha256()
    for table in tables:
        digest.update(table.encode())
        for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid'):
            digest.update(repr(tuple(row)).encode())
    return digest.hexdigest()


def _count(connection, table):
    return int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    args = parser.parse_args()
    connection = sqlite3.connect(args.db)
    try:
        existing = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' AND name!='mgboost_schema_migrations' "
            "ORDER BY name"
        )]
        before = _digest(connection, existing)
        state_before = {
            "users": 25,
            "devices": _count(connection, "user_devices"),
            "hwid_locks": _count(connection, "hwid_lock"),
            "accounts": _count(connection, "mgboost_accounts"),
            "slots": _count(connection, "mgboost_device_slots"),
            "generations": _count(connection, "mgboost_device_slot_generations"),
        }
        first = apply_internal_entitlement_schema(connection, now=1)
        second = apply_internal_entitlement_schema(connection, now=2)
        after = _digest(connection, existing)
        state_after = {
            **state_before,
            "devices": _count(connection, "user_devices"),
            "hwid_locks": _count(connection, "hwid_lock"),
            "accounts": _count(connection, "mgboost_accounts"),
            "slots": _count(connection, "mgboost_device_slots"),
            "generations": _count(connection, "mgboost_device_slot_generations"),
        }
        if before != after or state_before != state_after:
            raise SystemExit("pre-existing runtime state changed")
        if any(_count(connection, table) for table in NEW_RUNTIME_TABLES):
            raise SystemExit("PH3-06 tables are not dormant/empty")
        marker = connection.execute(
            "SELECT COUNT(*) FROM mgboost_schema_migrations WHERE migration_id=?",
            (MIGRATION_ID,),
        ).fetchone()[0]
        result = {
            "first_apply": first,
            "second_apply": second,
            "preexisting_table_count": len(existing),
            "preexisting_digest_preserved": before == after,
            "state": state_after,
            "new_table_rows": {table: _count(connection, table) for table in NEW_RUNTIME_TABLES},
            "migration_marker_count": marker,
            "quick_check": connection.execute("PRAGMA quick_check").fetchone()[0],
            "foreign_key_violations": len(connection.execute("PRAGMA foreign_key_check").fetchall()),
        }
        print(json.dumps(result, indent=2, sort_keys=True))
    finally:
        connection.close()


if __name__ == "__main__":
    main()
