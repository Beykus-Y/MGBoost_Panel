#!/usr/bin/env python3
"""Aggregate-only PH3-07 compatibility report; emits no subject refs."""

from __future__ import annotations

import argparse
import json
import sqlite3
import time

from src.compat_telemetry import SECONDS_PER_DAY


def build_report(connection: sqlite3.Connection, *, now: int, days: int) -> dict:
    cutoff = int(now) - int(days) * SECONDS_PER_DAY
    rows = connection.execute(
        "SELECT client_name,client_version,platform,compatibility_category,"
        "SUM(request_count),SUM(correlated_subject_count),SUM(repeat_request_count) "
        "FROM mgboost_hwid_compat_daily WHERE day_start>=? "
        "GROUP BY client_name,client_version,platform,compatibility_category "
        "ORDER BY SUM(request_count) DESC,client_name,client_version,platform",
        (cutoff - cutoff % SECONDS_PER_DAY,),
    ).fetchall()
    clients = [
        {
            "client": row[0],
            "version": row[1],
            "platform": row[2],
            "category": row[3],
            "requests": int(row[4]),
            "correlated_subjects": int(row[5]),
            "repeat_requests": int(row[6]),
        }
        for row in rows
    ]
    categories: dict[str, int] = {}
    for row in clients:
        categories[row["category"]] = categories.get(row["category"], 0) + row["requests"]
    total = sum(categories.values())
    supported = categories.get("SUPPORTED_HWID_PRESENT", 0)
    unsafe = total - supported
    return {
        "mode": "AGGREGATE_ONLY",
        "window_days": int(days),
        "requests": total,
        "categories": categories,
        "supported_hwid_percent": round(supported * 100 / total, 2) if total else None,
        "future_fail_closed_unsafe_percent": round(unsafe * 100 / total, 2) if total else None,
        "clients": clients,
        "raw_identifiers_emitted": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--days", type=int, default=30, choices=range(1, 61))
    parser.add_argument("--now", type=int, default=int(time.time()))
    args = parser.parse_args()
    connection = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    try:
        report = build_report(connection, now=args.now, days=args.days)
    finally:
        connection.close()
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
