"""Read-only presentation models for the account-centric admin panel.

This module deliberately composes existing domain stores and observability
helpers.  It does not make lifecycle decisions or mutate account state.
"""

from __future__ import annotations

import json
import time

from .internal_entitlements import InternalEntitlementError
from .legacy_grace_observability import account_grace_snapshot, classify_action
from .legacy_grace_migration import is_genesis_hwid_verifier


_MIGRATED_STATES = ("MIGRATED", "LEGACY_REVOKE_PENDING", "LEGACY_REVOKED")


def _aliases(connection, account_id: int, notes_by_alias: dict[str, str] | None = None) -> list[dict]:
    rows = connection.execute(
        "SELECT legacy_username,alias_role,ownership_provenance,legacy_status,"
        "legacy_expiry,observed_device_count,observed_hwid_count "
        "FROM mgboost_legacy_account_aliases WHERE account_id=? "
        "ORDER BY CASE alias_role WHEN 'PRIMARY' THEN 0 ELSE 1 END,id",
        (int(account_id),),
    ).fetchall()
    notes = notes_by_alias or {}
    result = []
    for row in rows:
        item = dict(row)
        note = notes.get(item["legacy_username"])
        item["note"] = note.strip() if isinstance(note, str) and note.strip() else None
        result.append(item)
    return result


def _display_identity(account: dict, aliases: list[dict]) -> dict:
    """Presentation identity only; never used for account linkage/authority."""
    primary = aliases[0] if aliases else None
    noted = next((alias for alias in aliases if alias.get("note")), None)
    return {
        "display_note": noted["note"] if noted else None,
        "display_note_source_alias": noted["legacy_username"] if noted else None,
        "primary_alias": primary["legacy_username"] if primary else None,
        "public_id": account["public_id"],
    }


def _is_technical_account(connection, account: dict) -> bool:
    """Classify explicit service canaries from durable structured evidence.

    No username, account id, note text or fuzzy matching participates. A
    customer grace member is never hidden, even if its source is INTERNAL.
    """
    if account["account_source"] != "INTERNAL":
        return False
    if connection.execute(
        "SELECT 1 FROM mgboost_legacy_grace_periods WHERE account_id=?", (account["id"],),
    ).fetchone():
        return False
    review = connection.execute(
        "SELECT ownership_evidence,evidence_json FROM mgboost_internal_account_reviews "
        "WHERE account_id=?", (account["id"],),
    ).fetchone()
    if review is None or review["ownership_evidence"] != "ABSENT":
        return False
    try:
        evidence = json.loads(review["evidence_json"])
    except (TypeError, ValueError):
        return False
    return isinstance(evidence, dict) and bool(str(evidence.get("purpose", "")).strip())


def _grace_progress(grace: dict | None, *, now: int) -> dict | None:
    if grace is None:
        return None
    start = int(grace["started_at"])
    end = int(grace["current_end_at"])
    duration = max(1, end - start)
    elapsed = min(duration, max(0, int(now) - start))
    elapsed_percent = round(elapsed * 100 / duration)
    return {
        **grace,
        "elapsed_percent": elapsed_percent,
        "remaining_percent": 100 - elapsed_percent,
    }


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


def _device_summaries(
    connection, account_id: int, *, device_slot_hmac_key: bytes | str = "",
) -> list[dict]:
    rows = connection.execute(
        "SELECT s.slot_number,s.slot_kind,s.desired_state,s.observed_state,s.created_at,s.updated_at,"
        "g.status AS generation_status,g.claimed_at,g.ended_at,g.end_reason,g.hwid_masked,g.hwid_verifier,"
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
        verifier = item.pop("hwid_verifier", None)
        item["proven_genesis_bootstrap"] = is_genesis_hwid_verifier(
            account_id, verifier, device_slot_hmac_key,
        )
        item["migration_evidence"] = (
            "REAL_DEVICE_LINEAGE" if item["migration_state"] is not None
            else "NO_REAL_DEVICE_LINEAGE"
        )
        item["real_migration_lineage"] = item["migration_state"] is not None
        result.append(item)
    return result


def _technical_summary(connection, account_id: int, public_id: str) -> dict:
    rows = connection.execute(
        "SELECT s.slot_number,g.id AS slot_generation_id,g.generation,g.status AS generation_status,g.hwid_verifier,"
        "c.id AS child_intent_id,c.public_id AS child_public_id,c.child_username,"
        "c.uuid_verifier,c.desired_state AS child_desired_state,"
        "c.observed_state AS child_observed_state,o.id AS outbox_id,o.operation_id,"
        "o.state AS outbox_state "
        "FROM mgboost_device_slots s "
        "LEFT JOIN mgboost_device_slot_generations g ON g.slot_id=s.id "
        "LEFT JOIN mgboost_child_user_intents c ON c.slot_generation_id=g.id "
        "LEFT JOIN mgboost_outbox o ON o.child_intent_id=c.id "
        "WHERE s.account_id=? ORDER BY s.slot_number,g.generation DESC",
        (int(account_id),),
    ).fetchall()
    return {"account_public_id": public_id, "device_lineage": [dict(row) for row in rows]}


def account_detail(
    db, account_id: int, *, now: int | None = None,
    notes_by_alias: dict[str, str] | None = None,
    device_slot_hmac_key: bytes | str = "",
) -> dict | None:
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
    aliases = _aliases(db._conn, account_id, notes_by_alias)
    identity = _display_identity(account, aliases)
    return {
        "account": account,
        "display_identity": identity,
        "aliases": aliases,
        "subscription": _subscription_summary(db, account_id, now=timestamp),
        "credential": _credential_summary(db._conn, account_id),
        "devices": _device_summaries(
            db._conn, account_id, device_slot_hmac_key=device_slot_hmac_key,
        ),
        "telegram": {
            "status": grace["telegram_status"],
            "identities": [dict(row) for row in identities],
        },
        "migration_grace": {
            **grace, "grace": _grace_progress(grace["grace"], now=timestamp),
            "action": classify_action(grace),
        },
        "technical": _technical_summary(db._conn, account_id, account["public_id"]),
    }


def account_summaries(
    db, *, now: int | None = None, notes_by_alias: dict[str, str] | None = None,
    include_technical: bool = False,
) -> list[dict]:
    timestamp = int(time.time()) if now is None else int(now)
    accounts = db._conn.execute("SELECT * FROM mgboost_accounts ORDER BY id").fetchall()
    result = []
    for row in accounts:
        account = dict(row)
        technical = _is_technical_account(db._conn, account)
        if technical and not include_technical:
            continue
        aliases = _aliases(db._conn, account["id"], notes_by_alias)
        identity = _display_identity(account, aliases)
        grace = account_grace_snapshot(db, account["id"], now=timestamp)
        subscription = _subscription_summary(db, account["id"], now=timestamp)
        result.append({
            "id": account["id"],
            "status": account["status"],
            "account_source": account["account_source"],
            "created_at": account["created_at"],
            **identity,
            "alias_count": len(aliases),
            "aliases": [alias["legacy_username"] for alias in aliases],
            "technical_account": technical,
            "subscription": subscription,
            "telegram_status": grace["telegram_status"],
            "active_devices": grace["active_devices"],
            "migrated_devices": grace["migrated_devices"],
            "parent_ready": grace["bridge_enabled"] and grace["active_devices"] > 0,
            "grace": grace["grace"],
            "migration_action": classify_action(grace),
        })
    return result


def migration_grace_summaries(
    db, *, now: int | None = None, notes_by_alias: dict[str, str] | None = None,
    include_technical: bool = False,
) -> dict:
    timestamp = int(time.time()) if now is None else int(now)
    account_ids = [row[0] for row in db._conn.execute(
        "SELECT id FROM mgboost_accounts ORDER BY id"
    ).fetchall()]
    rows = []
    for account_id in account_ids:
        account = db.accounts.get_account(account_id)
        technical = _is_technical_account(db._conn, account)
        if technical and not include_technical:
            continue
        snapshot = account_grace_snapshot(db, account_id, now=timestamp)
        # The default operational table is the actual grace campaign cohort.
        if snapshot["grace"] is None and not include_technical:
            continue
        aliases = _aliases(db._conn, account_id, notes_by_alias)
        rows.append({
            **snapshot,
            **_display_identity(account, aliases),
            "grace": _grace_progress(snapshot["grace"], now=timestamp),
            "technical_account": technical,
            "action": classify_action(snapshot),
        })
    cohort = [row for row in rows if row["grace"] is not None]
    telegram_counts = {
        status: sum(row["telegram_status"] == status for row in cohort)
        for status in ("BOUND", "UNREGISTERED", "PENDING_LINK", "AMBIGUOUS")
    }
    parent_ready = sum(row["bridge_enabled"] and row["active_devices"] > 0 for row in cohort)
    with_lineage = sum(sum(row["migration_state"].values()) > 0 for row in cohort)
    total_lineages = sum(sum(row["migration_state"].values()) for row in cohort)
    summary = {
        "cohort_accounts": len(cohort),
        "parent_ready": parent_ready,
        "telegram": telegram_counts,
        "accounts_with_real_lineage": with_lineage,
        "accounts_without_real_lineage": len(cohort) - with_lineage,
        "total_real_lineages": total_lineages,
        "active_slots": sum(row["active_devices"] for row in cohort),
        "legacy_active_accounts_72h": sum(row["raw_legacy_request_seen_72h"] for row in cohort),
        "legacy_requests_72h": sum(row["legacy_requests_72h"] for row in cohort),
        "error_reconcile": sum(row["migration_state"]["ERROR_RECONCILE"] for row in cohort),
        "compatibility_blockers": sum(row["action"] == "COMPATIBILITY_BLOCK" for row in cohort),
        "manual_review": sum(row["action"] == "MANUAL_REVIEW" for row in cohort),
    }
    return {"generated_at": timestamp, "summary": summary, "accounts": rows}


def dashboard_summary(
    db, *, now: int | None = None, notes_by_alias: dict[str, str] | None = None,
) -> dict:
    timestamp = int(time.time()) if now is None else int(now)
    migration_model = migration_grace_summaries(
        db, now=timestamp, notes_by_alias=notes_by_alias,
    )
    migration = migration_model["accounts"]
    campaign_rows = [row for row in migration if row["grace"]]
    campaign = None
    if campaign_rows:
        cohort_refs = sorted({row["grace"]["cohort_ref"] for row in campaign_rows})
        summary = migration_model["summary"]
        start = min(row["grace"]["started_at"] for row in campaign_rows)
        end = max(row["grace"]["current_end_at"] for row in campaign_rows)
        progress = _grace_progress({
            "started_at": start, "current_end_at": end,
        }, now=timestamp)
        campaign = {
            "cohort_refs": cohort_refs,
            "active": timestamp < end,
            "accounts_total": summary["cohort_accounts"],
            "day_of_14": max(row["grace"]["day_of_14"] for row in campaign_rows),
            "started_at": start,
            "ends_at": end,
            "seconds_remaining": max(0, end - timestamp),
            "elapsed_percent": progress["elapsed_percent"],
            "remaining_percent": progress["remaining_percent"],
            "telegram": summary["telegram"],
            "parent_ready": summary["parent_ready"],
            "active_slots": summary["active_slots"],
            "accounts_with_real_lineage": summary["accounts_with_real_lineage"],
            "accounts_without_real_lineage": summary["accounts_without_real_lineage"],
            "total_real_lineages": summary["total_real_lineages"],
            "still_legacy_active_72h": summary["legacy_active_accounts_72h"],
            "reconcile_blockers": summary["error_reconcile"],
            "compatibility_blockers": summary["compatibility_blockers"],
            "manual_review": summary["manual_review"],
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
        account = db.accounts.get_account(item["id"])
        aliases = _aliases(db._conn, item["id"], notes_by_alias)
        identity = _display_identity(account, aliases)
        item["label"] = identity["display_note"] or identity["primary_alias"] or identity["public_id"]
        item["primary_alias"] = identity["primary_alias"]
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
