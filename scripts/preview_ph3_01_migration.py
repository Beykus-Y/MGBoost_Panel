#!/usr/bin/env python3
"""Read-only, aggregate-only PH3-01 legacy migration preview.

The script never emits Telegram IDs, usernames, subscription tokens, HWIDs or
payment references.  It intentionally proposes zero automatic account writes:
classification and ownership decisions belong to the staged Phase 4 migration.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3


def _has_table(connection: sqlite3.Connection, name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _scalar(connection: sqlite3.Connection, query: str, params=()) -> int:
    return int(connection.execute(query, params).fetchone()[0] or 0)


def build_preview(connection: sqlite3.Connection, *, authoritative_users: int | None = None) -> dict:
    sources = []
    for table, column in (
        ("tg_users", "marzban_username"),
        ("user_devices", "username"),
        ("sub_requests", "username"),
        ("stars_invoices", "marzban_username"),
    ):
        if _has_table(connection, table):
            sources.append(f"SELECT {column} AS username FROM {table} WHERE {column} IS NOT NULL")
    union = " UNION ".join(sources) if sources else "SELECT NULL AS username WHERE 0"
    connection.execute("DROP TABLE IF EXISTS temp.ph3_preview_usernames")
    connection.execute(
        "CREATE TEMP TABLE ph3_preview_usernames AS "
        f"SELECT DISTINCT username FROM ({union}) WHERE username!=''"
    )
    observed = _scalar(connection, "SELECT COUNT(*) FROM ph3_preview_usernames")

    tg = {"links": 0, "linked_usernames": 0, "single_link_candidates": 0,
          "multiple_link_review": 0, "without_link": observed}
    if _has_table(connection, "tg_users"):
        tg["links"] = _scalar(connection, "SELECT COUNT(*) FROM tg_users")
        tg["linked_usernames"] = _scalar(
            connection, "SELECT COUNT(DISTINCT marzban_username) FROM tg_users"
        )
        tg["single_link_candidates"] = _scalar(
            connection,
            "SELECT COUNT(*) FROM (SELECT marzban_username FROM tg_users "
            "GROUP BY marzban_username HAVING COUNT(*)=1)",
        )
        tg["multiple_link_review"] = _scalar(
            connection,
            "SELECT COUNT(*) FROM (SELECT marzban_username FROM tg_users "
            "GROUP BY marzban_username HAVING COUNT(*)>1)",
        )
        tg["without_link"] = _scalar(
            connection,
            "SELECT COUNT(*) FROM ph3_preview_usernames p WHERE NOT EXISTS "
            "(SELECT 1 FROM tg_users t WHERE t.marzban_username=p.username)",
        )

    devices = {"rows": 0, "active_rows": 0, "linked_usernames": 0, "hwid_locks": 0}
    if _has_table(connection, "user_devices"):
        devices.update({
            "rows": _scalar(connection, "SELECT COUNT(*) FROM user_devices"),
            "active_rows": _scalar(
                connection, "SELECT COUNT(*) FROM user_devices WHERE is_active=1"
            ),
            "linked_usernames": _scalar(
                connection, "SELECT COUNT(DISTINCT username) FROM user_devices"
            ),
        })
    if _has_table(connection, "hwid_lock"):
        devices["hwid_locks"] = _scalar(connection, "SELECT COUNT(*) FROM hwid_lock")

    stars = {
        "invoice_rows": 0,
        "durably_paid_or_applied_events": 0,
        "usernames_with_stars_event_evidence": 0,
        "event_payment_channel_provable": "TELEGRAM_STARS",
        "current_plan_provable_from_legacy_invoice": 0,
    }
    if _has_table(connection, "stars_invoices"):
        stars["invoice_rows"] = _scalar(connection, "SELECT COUNT(*) FROM stars_invoices")
        evidence_where = (
            "telegram_payment_charge_id IS NOT NULL AND status IN "
            "('paid','plan_committed','applied','refund_pending','refund_unknown','refunded')"
        )
        stars["durably_paid_or_applied_events"] = _scalar(
            connection, f"SELECT COUNT(*) FROM stars_invoices WHERE {evidence_where}"
        )
        stars["usernames_with_stars_event_evidence"] = _scalar(
            connection,
            "SELECT COUNT(DISTINCT marzban_username) FROM stars_invoices WHERE "
            + evidence_where,
        )

    new_rows = {}
    for table in (
        "mgboost_accounts", "mgboost_telegram_identities", "mgboost_plan_versions",
        "mgboost_plan_durations", "mgboost_subscriptions",
        "mgboost_entitlement_mutations", "mgboost_subscription_terms",
        "mgboost_entitlement_state", "mgboost_entitlement_overrides",
        "mgboost_wl_periods",
    ):
        new_rows[table] = _scalar(connection, f"SELECT COUNT(*) FROM {table}") if _has_table(connection, table) else None

    return {
        "mode": "READ_ONLY_AGGREGATE_PREVIEW",
        "authoritative_marzban_user_count": authoritative_users,
        "legacy_usernames_observed_in_local_db": observed,
        "telegram_binding_evidence": tg,
        "legacy_device_evidence": devices,
        "stars_event_evidence": stars,
        "automatic_backfill": {
            "accounts": 0,
            "identity_links": 0,
            "plan_assignments": 0,
            "subscriptions": 0,
            "entitlements": 0,
            "reason": (
                "Legacy username is not account identity; TG M:1 bindings do not "
                "prove a single owner; old free-form tariff/invoice values do not "
                "prove a new versioned plan. Stars charge rows prove individual "
                "payment events only. UNKNOWN_LEGACY is required until reviewed."
            ),
        },
        "post_schema_new_runtime_rows": new_rows,
        "sensitive_values_emitted": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--authoritative-marzban-users", type=int)
    parser.add_argument("--assert-new-runtime-empty", action="store_true")
    args = parser.parse_args()

    path = os.path.abspath(args.db)
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        preview = build_preview(
            connection, authoritative_users=args.authoritative_marzban_users
        )
    finally:
        connection.close()
    if args.assert_new_runtime_empty:
        populated = {
            key: value for key, value in preview["post_schema_new_runtime_rows"].items()
            if value not in (None, 0)
        }
        if populated:
            raise SystemExit("new PH3 runtime tables are not empty")
    print(json.dumps(preview, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
