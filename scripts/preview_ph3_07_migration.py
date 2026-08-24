#!/usr/bin/env python3
"""Aggregate-only PH3-07 additive migration/dormancy preview."""

from __future__ import annotations

import argparse
import json
import sqlite3


def _count(connection, table):
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) if exists else None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--assert-initial-empty", action="store_true")
    args = parser.parse_args()
    connection = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    try:
        result = {
            "mode": "READ_ONLY_AGGREGATE_PREVIEW",
            "telemetry_subject_rows": _count(connection, "mgboost_hwid_compat_subjects"),
            "telemetry_daily_rows": _count(connection, "mgboost_hwid_compat_daily"),
            "parent_accounts": _count(connection, "mgboost_accounts"),
            "slot_rows": _count(connection, "mgboost_device_slots"),
            "generation_rows": _count(connection, "mgboost_device_slot_generations"),
            "legacy_device_rows": _count(connection, "user_devices"),
            "legacy_hwid_lock_rows": _count(connection, "hwid_lock"),
            "raw_identifiers_emitted": False,
        }
    finally:
        connection.close()
    if args.assert_initial_empty and any(
        result[key] != 0
        for key in (
            "telemetry_subject_rows", "telemetry_daily_rows", "parent_accounts",
            "slot_rows", "generation_rows",
        )
    ):
        raise SystemExit("PH3-07 initial runtime tables are not empty")
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
