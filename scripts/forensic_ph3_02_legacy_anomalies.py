#!/usr/bin/env python3
"""Read-only masked classification of PH3 legacy identity anomalies."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3


# Commit ed5bf99 introduced tg_bound/tg_rebound audit on 2026-08-21. Links
# registered before that deployment legitimately have no binding audit event.
TG_BIND_AUDIT_INTRODUCED_UTC = 1_787_326_428

def _utc(value):
    if value is None:
        return None
    return dt.datetime.fromtimestamp(int(value), tz=dt.timezone.utc).isoformat()


def _table_exists(connection, table):
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _count(connection, query, params):
    return int(connection.execute(query, params).fetchone()[0] or 0)


def _evidence(connection, username):
    result = {}
    if _table_exists(connection, "sub_requests"):
        row = connection.execute(
            "SELECT COUNT(*),MIN(timestamp),MAX(timestamp) FROM sub_requests WHERE username=?",
            (username,),
        ).fetchone()
        result["subscription_requests"] = {
            "count": int(row[0]), "first_utc": _utc(row[1]), "last_utc": _utc(row[2]),
        }
    if _table_exists(connection, "user_devices"):
        row = connection.execute(
            "SELECT COUNT(*),SUM(CASE WHEN is_active=1 THEN 1 ELSE 0 END),"
            "MIN(first_seen),MAX(last_seen) FROM user_devices WHERE username=?",
            (username,),
        ).fetchone()
        result["device_rows"] = {
            "count": int(row[0]), "active": int(row[1] or 0),
            "first_utc": _utc(row[2]), "last_utc": _utc(row[3]),
        }
    if _table_exists(connection, "tg_users"):
        rows = connection.execute(
            "SELECT telegram_id,registered_at FROM tg_users "
            "WHERE marzban_username=? ORDER BY registered_at,telegram_id",
            (username,),
        ).fetchall()
        result["telegram_links"] = {
            "count": len(rows),
            "registered_utc": [_utc(row[1]) for row in rows],
        }
        telegram_ids = [row[0] for row in rows]
    else:
        telegram_ids = []
    if _table_exists(connection, "audit_log"):
        parameters = [username]
        predicate = "marzban_username=?"
        if telegram_ids:
            predicate += " OR telegram_id IN (" + ",".join("?" for _ in telegram_ids) + ")"
            parameters.extend(telegram_ids)
        rows = connection.execute(
            "SELECT event_type,COUNT(*),MIN(timestamp),MAX(timestamp) FROM audit_log "
            f"WHERE {predicate} GROUP BY event_type ORDER BY event_type",
            parameters,
        ).fetchall()
        result["audit_events"] = [
            {"type": row[0], "count": int(row[1]),
             "first_utc": _utc(row[2]), "last_utc": _utc(row[3])}
            for row in rows
        ]
    if _table_exists(connection, "stars_invoices"):
        rows = connection.execute(
            "SELECT status,COUNT(*),MIN(created_at),MAX(created_at) "
            "FROM stars_invoices WHERE marzban_username=? "
            "GROUP BY status ORDER BY status",
            (username,),
        ).fetchall()
        result["stars_invoices"] = [
            {"status": row[0], "count": int(row[1]),
             "first_utc": _utc(row[2]), "last_utc": _utc(row[3])}
            for row in rows
        ]
    if _table_exists(connection, "tickets"):
        result["tickets"] = _count(
            connection, "SELECT COUNT(*) FROM tickets WHERE marzban_username=?", (username,)
        )
    if _table_exists(connection, "node_filters"):
        result["node_filter_rows"] = _count(
            connection, "SELECT COUNT(*) FROM node_filters WHERE username=?", (username,)
        )
    if _table_exists(connection, "per_user_configs"):
        result["per_user_config_rows"] = _count(
            connection, "SELECT COUNT(*) FROM per_user_configs WHERE username=?", (username,)
        )
    return result


def build_forensic(connection, live_usernames):
    local_sources = []
    for table, column in (
        ("tg_users", "marzban_username"),
        ("user_devices", "username"),
        ("sub_requests", "username"),
        ("stars_invoices", "marzban_username"),
    ):
        if _table_exists(connection, table):
            local_sources.append(
                f"SELECT {column} AS username FROM {table} WHERE {column} IS NOT NULL"
            )
    union = " UNION ".join(local_sources) if local_sources else "SELECT NULL WHERE 0"
    local = {
        row[0] for row in connection.execute(
            f"SELECT DISTINCT username FROM ({union}) WHERE username IS NOT NULL AND username!=''"
        )
    }
    live = {str(value) for value in live_usernames if value}
    local_only = sorted(local - live)
    multi = []
    if _table_exists(connection, "tg_users"):
        multi = [
            row[0] for row in connection.execute(
                "SELECT marzban_username FROM tg_users GROUP BY marzban_username "
                "HAVING COUNT(*)>1 ORDER BY marzban_username"
            )
        ]

    local_items = []
    for index, username in enumerate(local_only, 1):
        evidence = _evidence(connection, username)
        requests = evidence.get("subscription_requests", {}).get("count", 0)
        devices = evidence.get("device_rows", {}).get("count", 0)
        classification = (
            "ORPHANED_LOCAL_USAGE_DEVICE_EVIDENCE_FOR_NONLIVE_MARZBAN_USER"
            if requests or devices
            else "NONLIVE_LOCAL_REFERENCE_REQUIRES_MANUAL_REVIEW"
        )
        local_items.append({
            "label": f"LOCAL_ONLY_{index}",
            "classification": classification,
            "live_marzban_user": False,
            "evidence": evidence,
        })

    multi_items = []
    for index, username in enumerate(multi, 1):
        evidence = _evidence(connection, username)
        event_types = {row["type"] for row in evidence.get("audit_events", [])}
        registered = [
            row[1] for row in connection.execute(
                "SELECT telegram_id,registered_at FROM tg_users "
                "WHERE marzban_username=? ORDER BY registered_at,telegram_id",
                (username,),
            ).fetchall()
        ]
        if "tg_bound" in event_types:
            classification = "LEGACY_EXPLICIT_M_TO_1_TELEGRAM_BINDING"
        elif registered and max(registered) < TG_BIND_AUDIT_INTRODUCED_UTC:
            classification = "LEGACY_PRE_AUDIT_M_TO_1_TELEGRAM_BINDING"
        else:
            classification = "LEGACY_M_TO_1_BINDING_WITHOUT_COMPLETE_AUDIT_EVIDENCE"
        multi_items.append({
            "label": f"MULTI_TG_{index}",
            "classification": classification,
            "live_marzban_user": username in live,
            "evidence": evidence,
        })

    return {
        "mode": "READ_ONLY_MASKED_FORENSIC",
        "live_marzban_users": len(live),
        "local_distinct_usernames": len(local),
        "local_only_count": len(local_only),
        "local_only": local_items,
        "multi_telegram_username_count": len(multi),
        "multi_telegram": multi_items,
        "automatic_owner_or_account_assignment": 0,
        "deletions": 0,
        "raw_identifiers_emitted": False,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    args = parser.parse_args()

    from src.service_marzban import ServiceMarzbanClient

    client = ServiceMarzbanClient()
    sentinel = client.get_admin_token_from_env()
    users = []
    offset = 0
    while True:
        page = client.get_users(sentinel, limit=100, offset=offset)
        rows = page.get("users", []) if isinstance(page, dict) else page
        if not rows:
            break
        users.extend(rows)
        if len(rows) < 100:
            break
        offset += len(rows)
    connection = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    try:
        result = build_forensic(
            connection, [str(row.get("username") or "") for row in users]
        )
    finally:
        connection.close()
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
