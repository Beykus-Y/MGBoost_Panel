#!/usr/bin/env python3
"""Aggregate-only PH3-02 dormant slot migration gate."""

from __future__ import annotations

import argparse
import json
import sqlite3


def _has_table(connection, name):
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def build_preview(connection):
    result = {
        "mode": "READ_ONLY_AGGREGATE_PREVIEW",
        "slot_rows": None,
        "generation_rows": None,
        "parent_account_rows": None,
        "legacy_device_rows": 0,
        "legacy_hwid_lock_rows": 0,
        "automatic_backfill": 0,
        "raw_identifiers_emitted": False,
    }
    mappings = {
        "slot_rows": "mgboost_device_slots",
        "generation_rows": "mgboost_device_slot_generations",
        "parent_account_rows": "mgboost_accounts",
        "legacy_device_rows": "user_devices",
        "legacy_hwid_lock_rows": "hwid_lock",
    }
    for key, table in mappings.items():
        if _has_table(connection, table):
            result[key] = int(connection.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0])
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--assert-dormant-empty", action="store_true")
    args = parser.parse_args()
    connection = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    try:
        result = build_preview(connection)
    finally:
        connection.close()
    if args.assert_dormant_empty and (
        result["slot_rows"] != 0 or result["generation_rows"] != 0
        or result["parent_account_rows"] != 0
    ):
        raise SystemExit("PH3 runtime tables are not dormant/empty")
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
