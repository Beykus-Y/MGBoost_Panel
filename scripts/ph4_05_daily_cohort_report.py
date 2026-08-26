#!/usr/bin/env python3
"""PH4-05 daily grace-cohort monitoring report -- READ ONLY by default.

One row per cohort member (a `mgboost_legacy_grace_periods` row), meant to
be run once a day throughout the 14-day window so the owner has a concrete
list of who still needs a personal follow-up. Never prints a raw legacy/
opaque token, full subscription URL, UUID, full HWID, or Authorization/
Cookie value -- `legacy_user` is the account id, matching this project's
existing runbook convention (every example is by account id, never a real
username).

Usage:

    python -m scripts.ph4_05_daily_cohort_report --db <COPY-of-db.sqlite3> \
        [--cohort-ref PH4-05-...] [--format table|json|csv] [--now EPOCH]

`--catchup-bind` additionally (re)attempts `bind_telegram_after_registration`
for every `PENDING_LINK` member before reporting -- this is the only mode
that writes anything, and only ever calls the same idempotent, ambiguity-
checked primitive the bot's own linking handler already calls. Omit it to
keep the report itself strictly read-only (point `--db` at a copy, same
discipline as `ph4_05_grace_eligibility_report.py`).
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time

sys.path.insert(0, ".")

from src.legacy_grace_observability import account_grace_snapshot, classify_action  # noqa: E402


def _account_status(db, account_id: int) -> str | None:
    row = db._conn.execute(
        "SELECT status FROM mgboost_accounts WHERE id=?", (account_id,),
    ).fetchone()
    return row[0] if row else None


def _blocker(snapshot: dict, action: str) -> str:
    if action == "RECONCILE_REQUIRED":
        return f"{snapshot['migration_state']['ERROR_RECONCILE']} lineage(s) in ERROR_RECONCILE"
    if action == "MANUAL_REVIEW":
        return "ambiguous Telegram ownership -- more than one distinct ID linked"
    if action == "COMPATIBILITY_BLOCK":
        return "bridge enabled, legacy activity seen, but zero active devices"
    if action == "CONTACT_USER":
        return "grace ending soon and still unregistered"
    if action == "WAITING_FOR_REGISTRATION":
        return "-"
    return "-"


def _catchup_bind(db, member_row: dict, *, now: int) -> None:
    from src.legacy_grace_observability import telegram_status
    from src.legacy_grace_registration import bind_telegram_after_registration

    account_id = member_row["account_id"]
    if telegram_status(db, account_id) != "PENDING_LINK":
        return
    alias_rows = db._conn.execute(
        "SELECT legacy_username FROM mgboost_legacy_account_aliases WHERE account_id=?",
        (account_id,),
    ).fetchall()
    for alias in alias_rows:
        username = alias["legacy_username"]
        row = db._conn.execute(
            "SELECT DISTINCT telegram_id FROM tg_users WHERE marzban_username=?", (username,),
        ).fetchone()
        if row is None:
            continue
        bind_telegram_after_registration(
            db, legacy_username=username, telegram_id=int(row["telegram_id"]),
            actor="mgboost-daily-report-catchup", now=now,
        )


def build_report(db, *, now: int | None = None, cohort_ref: str | None = None, catchup_bind: bool = False) -> dict:
    now = int(time.time()) if now is None else int(now)
    members = db.legacy_grace.list_by_cohort(cohort_ref)

    if catchup_bind:
        for member in members:
            _catchup_bind(db, member, now=now)

    rows = []
    for member in members:
        account_id = member["account_id"]
        snapshot = account_grace_snapshot(db, account_id, now=now)
        action = classify_action(snapshot)
        rows.append({
            "legacy_user": account_id,
            "status": _account_status(db, account_id),
            "telegram_bound": snapshot["telegram_status"],
            "migration_state": snapshot["migration_state"],
            "active_devices": snapshot["active_devices"],
            "migrated_devices": snapshot["migrated_devices"],
            "last_legacy": snapshot["last_legacy_activity"],
            "last_opaque": snapshot["last_opaque_activity"],
            "last_child_fetch": snapshot["last_child_fetch"],
            "compatibility": "compatibility_block" if action == "COMPATIBILITY_BLOCK" else "ok",
            "grace_remaining_seconds": snapshot["grace"]["seconds_remaining"] if snapshot["grace"] else None,
            "grace_day_of_14": snapshot["grace"]["day_of_14"] if snapshot["grace"] else None,
            "blocker": _blocker(snapshot, action),
            "action": action,
        })

    cohort_start_ats = {m["started_at"] for m in members}
    cohort_end_ats = {m["original_end_at"] for m in members}
    aggregate = {
        "cohort_ref": cohort_ref,
        "member_count": len(members),
        "cohort_start_at": sorted(cohort_start_ats) if len(cohort_start_ats) != 1 else next(iter(cohort_start_ats), None),
        "cohort_end_at": sorted(cohort_end_ats) if len(cohort_end_ats) != 1 else next(iter(cohort_end_ats), None),
        "shared_boundary": len(cohort_start_ats) <= 1 and len(cohort_end_ats) <= 1,
        "by_action": {},
        "by_telegram_status": {},
        "total_active_devices": sum(r["active_devices"] for r in rows),
        "total_migrated_devices": sum(r["migrated_devices"] for r in rows),
    }
    for row in rows:
        aggregate["by_action"][row["action"]] = aggregate["by_action"].get(row["action"], 0) + 1
        aggregate["by_telegram_status"][row["telegram_bound"]] = (
            aggregate["by_telegram_status"].get(row["telegram_bound"], 0) + 1
        )

    return {"generated_at": now, "aggregate": aggregate, "rows": rows}


def _print_table(report: dict) -> None:
    agg = report["aggregate"]
    print(
        f"cohort_ref={agg['cohort_ref']} members={agg['member_count']} "
        f"start={agg['cohort_start_at']} end={agg['cohort_end_at']} "
        f"shared_boundary={agg['shared_boundary']}"
    )
    print(f"by_action={agg['by_action']}")
    print(f"by_telegram_status={agg['by_telegram_status']}")
    print(
        f"total_active_devices={agg['total_active_devices']} "
        f"total_migrated_devices={agg['total_migrated_devices']}"
    )
    print("-" * 80)
    header = (
        "legacy_user", "status", "telegram", "migrated/active", "last_legacy",
        "last_opaque", "last_child", "grace_day", "action", "blocker",
    )
    print(" | ".join(header))
    for row in report["rows"]:
        print(" | ".join([
            str(row["legacy_user"]), str(row["status"]), row["telegram_bound"],
            f"{row['migrated_devices']}/{row['active_devices']}",
            str(row["last_legacy"]), str(row["last_opaque"]), str(row["last_child_fetch"]),
            str(row["grace_day_of_14"]), row["action"], row["blocker"],
        ]))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--cohort-ref", default=None)
    parser.add_argument("--now", type=int, default=int(time.time()))
    parser.add_argument("--format", choices=("table", "json", "csv"), default="table")
    parser.add_argument("--catchup-bind", action="store_true")
    args = parser.parse_args()

    import src.database as database_module

    database_module.DB_PATH = args.db
    db = database_module.Database()
    try:
        report = build_report(db, now=args.now, cohort_ref=args.cohort_ref, catchup_bind=args.catchup_bind)
    finally:
        db._conn.close()

    if args.format == "json":
        print(json.dumps(report, sort_keys=True, indent=2))
    elif args.format == "csv":
        writer = csv.writer(sys.stdout)
        writer.writerow([
            "legacy_user", "status", "telegram_bound", "active_devices", "migrated_devices",
            "last_legacy", "last_opaque", "last_child_fetch", "grace_day_of_14",
            "action", "blocker",
        ])
        for row in report["rows"]:
            writer.writerow([
                row["legacy_user"], row["status"], row["telegram_bound"], row["active_devices"],
                row["migrated_devices"], row["last_legacy"], row["last_opaque"],
                row["last_child_fetch"], row["grace_day_of_14"], row["action"], row["blocker"],
            ])
    else:
        _print_table(report)


if __name__ == "__main__":
    main()
