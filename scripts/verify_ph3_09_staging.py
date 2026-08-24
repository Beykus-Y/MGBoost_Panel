#!/usr/bin/env python3
"""Apply and verify dormant PH3-09 on a disposable production DB copy."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3

from src.provenance_schema import MIGRATION_ID, NEW_RUNTIME_TABLES, apply_provenance_schema


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
        state_before = {name: _count(connection, name) for name in (
            "user_devices", "hwid_lock", "mgboost_accounts",
            "mgboost_device_slots", "mgboost_device_slot_generations",
            "mgboost_internal_account_reviews", "mgboost_internal_entitlement_revisions",
        )}
        first = apply_provenance_schema(connection, now=1)
        second = apply_provenance_schema(connection, now=2)
        after = _digest(connection, existing)
        state_after = {name: _count(connection, name) for name in state_before}
        if before != after or state_before != state_after:
            raise SystemExit("pre-existing runtime state changed")
        if any(_count(connection, table) for table in NEW_RUNTIME_TABLES):
            raise SystemExit("PH3-09 tables are not dormant/empty")
        result = {
            "first_apply": first,
            "second_apply": second,
            "preexisting_table_count": len(existing),
            "preexisting_digest_preserved": before == after,
            "state": state_after,
            "new_table_rows": {table: _count(connection, table) for table in NEW_RUNTIME_TABLES},
            "migration_marker_count": connection.execute(
                "SELECT COUNT(*) FROM mgboost_schema_migrations WHERE migration_id=?",
                (MIGRATION_ID,),
            ).fetchone()[0],
            "quick_check": connection.execute("PRAGMA quick_check").fetchone()[0],
            "foreign_key_violations": len(connection.execute("PRAGMA foreign_key_check").fetchall()),
        }
        print(json.dumps(result, indent=2, sort_keys=True))
    finally:
        connection.close()


if __name__ == "__main__":
    main()
