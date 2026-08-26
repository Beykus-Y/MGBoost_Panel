"""Read-only presentation models for the account-centric admin panel.

This module deliberately composes existing domain stores and observability
helpers.  It does not make lifecycle decisions or mutate account state.
"""

from __future__ import annotations

import time

from .internal_entitlements import InternalEntitlementError
from .legacy_grace_observability import account_grace_snapshot, classify_action


_MIGRATED_STATES = ("MIGRATED", "LEGACY_REVOKE_PENDING", "LEGACY_REVOKED")


def _aliases(connection, account_id: int) -> list[dict]:
    rows = connection.execute(
        "SELECT legacy_username,alias_role,ownership_provenance,legacy_status,"
        "legacy_expiry,observed_device_count,observed_hwid_count "
        "FROM mgboost_legacy_account_aliases WHERE account_id=? "
        "ORDER BY CASE alias_role WHEN 'PRIMARY' THEN 0 ELSE 1 END,id",
        (int(account_id),),
    ).fetchall()
    return [dict(row) for row in rows]


def _subscription_summary(db, account_id: int, *, now: int) -> dict | None:
    row = db._conn.execute(
        "SELECT s.id,s.status,s.started_at,s.current_expiry,s.created_at,s.updated_at,"
        "p.plan_code,p.version AS plan_version,p.display_name,p.plan_kind,"
        "p.billing_required,p.device_limit_mode,p.device_limit,p.wl_mode,p.wl_quota_bytes "
        "FROM mgboost_subscriptions s LEFT JOIN mgboost_plan_versions p "
        "ON p.id=s.current_plan_version_id WHERE s.account_id=? "
        "ORDER BY CASE WHEN s.status IN ('PENDING','ACTIVE','DISABLED','UNLIMITED','UNKNOWN_LEGACY') "
        "THEN 0 ELSE 1 END,s.id DESC LIMIT 1",
        (int(account_id),),
    ).fetchone()
    if row is None:
        return None
    result = dict(row)
    result["billing_required"] = bool(result["billing_required"]) if result["billing_required"] is not None else None
    result["expired"] = result["current_expiry"] is not None and result["current_expiry"] <= now
    account = db.accounts.get_account(account_id)
    if account and account["account_source"] == "INTERNAL":
        try:
            effective = db.internal_entitlements.effective_entitlements(account_id, now=now)
        except InternalEntitlementError:
            effective = None
        result["effective"] = effective
    else:
        result["effective"] = {
            "device_limit_mode": result["device_limit_mode"],
            "device_limit": result["device_limit"],
            "wl_mode": result["wl_mode"],
            "wl_quota_bytes": result["wl_quota_bytes"],
            "override_mode": "AUTO",
        }
    return result


def _credential_summary(connection, account_id: int) -> dict | None:
    row = connection.execute(
        "SELECT generation,status,created_at,activated_at,revoked_at,revoke_reason,last_used_at "
        "FROM mgboost_subscription_credentials WHERE account_id=? "
        "ORDER BY generation DESC LIMIT 1",
        (int(account_id),),
    ).fetchone()
    return dict(row) if row else None


def _device_summaries(connection, account_id: int) -> list[dict]:
    rows = connection.execute(
        "SELECT s.slot_number,s.slot_kind,s.desired_state,s.observed_state,s.created_at,s.updated_at,"
        "g.status AS generation_status,g.claimed_at,g.ended_at,g.end_reason,g.hwid_masked,"
        "c.desired_state AS child_desired_state,c.observed_state AS child_observed_state,"
        "c.uuid_masked,m.state AS migration_state,m.updated_at AS migration_updated_at "
        "FROM mgboost_device_slots s "
        "LEFT JOIN mgboost_device_slot_generations g ON g.slot_id=s.id AND g.status='ACTIVE' "
        "LEFT JOIN mgboost_child_user_intents c ON c.slot_generation_id=g.id "
        "LEFT JOIN mgboost_migration_bindings m ON m.account_id=s.account_id "
        "AND m.slot_generation_id=g.id "
        "WHERE s.account_id=? ORDER BY s.slot_number",
        (int(account_id),),
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["migration_evidence"] = (
            "REAL_DEVICE_LINEAGE" if item["migration_state"] is not None
            else "NO_REAL_DEVICE_LINEAGE"
        )
        item["real_migration_lineage"] = item["migration_state"] is not None
        result.append(item)
    return result


def _technical_summary(connection, account_id: int, public_id: str) -> dict:
    rows = connection.execute(
        "SELECT s.slot_number,g.id AS slot_generation_id,g.generation,g.hwid_verifier,"
        "c.id AS child_intent_id,c.public_id AS child_public_id,c.child_username,"
        "c.uuid_verifier,o.id AS outbox_id,o.operation_id "
        "FROM mgboost_device_slots s "
        "LEFT JOIN mgboost_device_slot_generations g ON g.slot_id=s.id "
        "LEFT JOIN mgboost_child_user_intents c ON c.slot_generation_id=g.id "
        "LEFT JOIN mgboost_outbox o ON o.child_intent_id=c.id "
        "WHERE s.account_id=? ORDER BY s.slot_number,g.generation",
        (int(account_id),),
    ).fetchall()
    return {"account_public_id": public_id, "device_lineage": [dict(row) for row in rows]}


def account_detail(db, account_id: int, *, now: int | None = None) -> dict | None:
    timestamp = int(time.time()) if now is None else int(now)
    account = db.accounts.get_account(int(account_id))
    if account is None:
        return None
    grace = account_grace_snapshot(db, account_id, now=timestamp)
    identities = db._conn.execute(
        "SELECT telegram_id,role,provenance,linked_at,revoked_at,revoke_reason "
        "FROM mgboost_telegram_identities WHERE account_id=? ORDER BY linked_at,id",
        (int(account_id),),
    ).fetchall()
    return {
        "account": account,
        "aliases": _aliases(db._conn, account_id),
        "subscription": _subscription_summary(db, account_id, now=timestamp),
        "credential": _credential_summary(db._conn, account_id),
        "devices": _device_summaries(db._conn, account_id),
        "telegram": {
            "status": grace["telegram_status"],
            "identities": [dict(row) for row in identities],
        },
        "migration_grace": {**grace, "action": classify_action(grace)},
        "technical": _technical_summary(db._conn, account_id, account["public_id"]),
    }


def account_summaries(db, *, now: int | None = None) -> list[dict]:
    timestamp = int(time.time()) if now is None else int(now)
    accounts = db._conn.execute("SELECT * FROM mgboost_accounts ORDER BY id").fetchall()
    result = []
    for row in accounts:
        account = dict(row)
        aliases = _aliases(db._conn, account["id"])
        grace = account_grace_snapshot(db, account["id"], now=timestamp)
        subscription = _subscription_summary(db, account["id"], now=timestamp)
        result.append({
            "id": account["id"],
            "status": account["status"],
            "account_source": account["account_source"],
            "created_at": account["created_at"],
            "primary_alias": aliases[0]["legacy_username"] if aliases else None,
            "alias_count": len(aliases),
            "subscription": subscription,
            "telegram_status": grace["telegram_status"],
            "active_devices": grace["active_devices"],
            "migrated_devices": grace["migrated_devices"],
            "parent_ready": grace["bridge_enabled"] and grace["active_devices"] > 0,
            "grace": grace["grace"],
            "migration_action": classify_action(grace),
        })
    return result


def migration_grace_summaries(db, *, now: int | None = None) -> dict:
    timestamp = int(time.time()) if now is None else int(now)
    account_ids = [row[0] for row in db._conn.execute(
        "SELECT id FROM mgboost_accounts ORDER BY id"
    ).fetchall()]
    rows = []
    for account_id in account_ids:
        snapshot = account_grace_snapshot(db, account_id, now=timestamp)
        aliases = _aliases(db._conn, account_id)
        rows.append({
            **snapshot,
            "primary_alias": aliases[0]["legacy_username"] if aliases else None,
            "action": classify_action(snapshot),
        })
    return {"generated_at": timestamp, "accounts": rows}


def dashboard_summary(db, *, now: int | None = None) -> dict:
    timestamp = int(time.time()) if now is None else int(now)
    migration = migration_grace_summaries(db, now=timestamp)["accounts"]
    active_campaign = [row for row in migration if row["grace"] and row["grace"]["active"]]
    campaign = None
    if active_campaign:
        cohort_refs = sorted({row["grace"]["cohort_ref"] for row in active_campaign})
        campaign = {
            "cohort_refs": cohort_refs,
            "accounts_total": len(active_campaign),
            "day_of_14": max(row["grace"]["day_of_14"] for row in active_campaign),
            "ends_at": max(row["grace"]["current_end_at"] for row in active_campaign),
            "seconds_remaining": max(row["grace"]["seconds_remaining"] for row in active_campaign),
            "telegram_bound": sum(row["telegram_status"] == "BOUND" for row in active_campaign),
            "waiting_for_registration": sum(row["action"] == "WAITING_FOR_REGISTRATION" for row in active_campaign),
            "parent_ready": sum(row["bridge_enabled"] and row["active_devices"] > 0 for row in active_campaign),
            "active_slots": sum(row["active_devices"] for row in active_campaign),
            "real_device_lineages": sum(sum(row["migration_state"].values()) for row in active_campaign),
            "real_devices_child_backed": sum(row["migrated_devices"] for row in active_campaign),
            "still_legacy_active_72h": sum(row["raw_legacy_request_seen_72h"] for row in active_campaign),
            "reconcile_blockers": sum(row["action"] == "RECONCILE_REQUIRED" for row in active_campaign),
            "compatibility_blockers": sum(row["action"] == "COMPATIBILITY_BLOCK" for row in active_campaign),
        }

    expiries = db._conn.execute(
        "SELECT a.id,COALESCE((SELECT legacy_username FROM mgboost_legacy_account_aliases "
        "WHERE account_id=a.id ORDER BY CASE alias_role WHEN 'PRIMARY' THEN 0 ELSE 1 END,id LIMIT 1),"
        "a.public_id) AS label,s.current_expiry,s.status FROM mgboost_accounts a "
        "JOIN mgboost_subscriptions s ON s.account_id=a.id "
        "WHERE s.current_expiry IS NOT NULL AND s.current_expiry>=? "
        "ORDER BY s.current_expiry LIMIT 12",
        (timestamp,),
    ).fetchall()
    expiry_rows = []
    buckets = {"today": 0, "three_days": 0, "seven_days": 0, "thirty_days": 0}
    for row in expiries:
        item = dict(row)
        seconds = item["current_expiry"] - timestamp
        item["seconds_remaining"] = seconds
        expiry_rows.append(item)
        if seconds <= 86400:
            buckets["today"] += 1
        if seconds <= 3 * 86400:
            buckets["three_days"] += 1
        if seconds <= 7 * 86400:
            buckets["seven_days"] += 1
        if seconds <= 30 * 86400:
            buckets["thirty_days"] += 1

    tickets = db._conn.execute(
        "SELECT COUNT(*) AS open_count,"
        "SUM(CASE WHEN status IN ('waiting_human','new_user') THEN 1 ELSE 0 END) AS unanswered "
        "FROM tickets WHERE status!='closed'"
    ).fetchone()
    slot_mismatch = db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_device_slots WHERE desired_state!=observed_state"
    ).fetchone()[0]
    child_mismatch = db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_child_user_intents WHERE desired_state!=observed_state"
    ).fetchone()[0]
    return {
        "generated_at": timestamp,
        "grace_campaign": campaign,
        "health": {
            "error_reconcile": sum(row["migration_state"]["ERROR_RECONCILE"] for row in migration),
            "resolver_errors_72h": sum(row["resolver_errors_72h"] for row in migration),
            "slot_state_mismatches": int(slot_mismatch),
            "child_state_mismatches": int(child_mismatch),
        },
        "expiring": {"buckets": buckets, "accounts": expiry_rows},
        "tickets": {"open": int(tickets["open_count"] or 0), "unanswered": int(tickets["unanswered"] or 0)},
    }
