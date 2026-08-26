#!/usr/bin/env python3
"""PH4-05 dry-run legacy-grace-period eligibility report -- READ ONLY.

Produces exactly the decision table the owner asked for: one row per real
legacy/migrated account with `migration state / active devices / last legacy
activity / last opaque activity / compatibility / blockers /
recommendation (START_GRACE|HOLD)`.

Never starts a grace period, never writes a `mgboost_legacy_grace_periods`
row, never touches any account/slot/child/migration state. The only "write"
this script's `Database()` construction can ever perform is this project's
own additive/idempotent schema migration (identical to every `apply_*_schema`
call `Database.__init__` already makes on every normal startup) -- a no-op
if already applied, and never touches existing rows.

Usage (against a downloaded COPY of the production DB, never the live file):

    python -m scripts.ph4_05_grace_eligibility_report --db /path/to/copy.sqlite3

Recommendation heuristic (a starting point for the owner's decision, not an
automatic trigger): START_GRACE only when the account is ACTIVE, has at
least one MIGRATED device lineage, every currently active device is already
migrated (nothing still depends on the shared legacy URL), there is no
ERROR_RECONCILE lineage needing manual review, and no grace period exists
yet. Everything else is HOLD with an explicit reason.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time

sys.path.insert(0, ".")

from src.legacy_grace_observability import account_grace_snapshot  # noqa: E402


def _legacy_accounts(db) -> list[int]:
    rows = db._conn.execute(
        "SELECT DISTINCT account_id FROM mgboost_legacy_account_aliases ORDER BY account_id"
    ).fetchall()
    return [row[0] for row in rows]


def _account_status(db, account_id: int) -> str | None:
    row = db._conn.execute(
        "SELECT status FROM mgboost_accounts WHERE id=?", (account_id,),
    ).fetchone()
    return row[0] if row else None


def _compatibility_note(snapshot: dict) -> str:
    migration = snapshot["migration_state"]
    if migration["ERROR_RECONCILE"]:
        return "ERROR_RECONCILE lineage present"
    if migration["MIGRATED"] == 0:
        return "no migrated device yet"
    return "compatible"


def _evaluate(db, account_id: int, *, now: int) -> dict:
    snapshot = account_grace_snapshot(db, account_id, now=now)
    status = _account_status(db, account_id)
    migration = snapshot["migration_state"]
    migrated = migration["MIGRATED"]
    active_devices = snapshot["active_devices"]
    unmigrated_active = max(0, active_devices - migrated)

    blockers = []
    if status != "ACTIVE":
        blockers.append(f"account status is {status!r}, not ACTIVE")
    if snapshot["grace"] is not None:
        blockers.append("grace period already started for this account")
    if migrated == 0:
        blockers.append("no MIGRATED device lineage yet")
    if unmigrated_active:
        blockers.append(f"{unmigrated_active} active device(s) not yet migrated")
    if migration["ERROR_RECONCILE"]:
        blockers.append(f"{migration['ERROR_RECONCILE']} lineage(s) in ERROR_RECONCILE")

    recommendation = "HOLD" if blockers else "START_GRACE"

    return {
        "account_id": account_id,
        "account_status": status,
        "migration_state": migration,
        "active_devices": active_devices,
        "migrated_devices": migrated,
        "last_legacy_activity": snapshot["last_legacy_activity"],
        "last_opaque_activity": snapshot["last_opaque_activity"],
        "compatibility": _compatibility_note(snapshot),
        "blockers": blockers,
        "recommendation": recommendation,
    }


def build_report(db, *, now: int | None = None) -> list[dict]:
    now = int(time.time()) if now is None else int(now)
    return [_evaluate(db, account_id, now=now) for account_id in _legacy_accounts(db)]


def _print_table(rows: list[dict]) -> None:
    header = (
        "account", "status", "migrated/active", "last_legacy", "last_opaque",
        "compat", "recommendation", "blockers",
    )
    print(" | ".join(header))
    for row in rows:
        print(" | ".join([
            str(row["account_id"]),
            str(row["account_status"]),
            f"{row['migrated_devices']}/{row['active_devices']}",
            str(row["last_legacy_activity"]),
            str(row["last_opaque_activity"]),
            row["compatibility"],
            row["recommendation"],
            "; ".join(row["blockers"]) or "-",
        ]))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="path to a DB file (use a COPY, not the live production file)")
    parser.add_argument("--now", type=int, default=int(time.time()))
    parser.add_argument("--format", choices=("table", "json", "csv"), default="table")
    args = parser.parse_args()

    import src.database as database_module

    database_module.DB_PATH = args.db
    db = database_module.Database()
    try:
        rows = build_report(db, now=args.now)
    finally:
        db._conn.close()

    if args.format == "json":
        print(json.dumps(rows, sort_keys=True, indent=2))
    elif args.format == "csv":
        writer = csv.writer(sys.stdout)
        writer.writerow([
            "account_id", "account_status", "migrated_devices", "active_devices",
            "last_legacy_activity", "last_opaque_activity", "compatibility",
            "recommendation", "blockers",
        ])
        for row in rows:
            writer.writerow([
                row["account_id"], row["account_status"], row["migrated_devices"],
                row["active_devices"], row["last_legacy_activity"], row["last_opaque_activity"],
                row["compatibility"], row["recommendation"], "; ".join(row["blockers"]),
            ])
    else:
        _print_table(rows)


if __name__ == "__main__":
    main()
