"""Read-only presentation models for the account-centric admin panel.

This module deliberately composes existing domain stores and observability
helpers.  It does not make lifecycle decisions or mutate account state.
"""

from __future__ import annotations

import json
import time

from .account_consolidation import get_display_name, resolve_account_id
from .admin_audit_timeline import account_timeline
from .device_headers import PLATFORMS as _RAW_PLATFORM_LABELS
from .entitlement_engine import EntitlementNotFoundError
from .internal_entitlements import InternalEntitlementError
from .legacy_grace_observability import account_grace_snapshot, classify_action
from .legacy_grace_migration import is_genesis_hwid_verifier
from .device_real_projection import project_real_device
from .child_recovery import RECOVERABLE_ERROR_CLASSES


_MIGRATED_STATES = ("MIGRATED", "LEGACY_REVOKE_PENDING", "LEGACY_REVOKED")

# Cosmetic casing only, for the small set of VPN clients the owner named
# explicitly -- an unrecognized client_name is shown exactly as captured,
# never guessed into one of these.
_CLIENT_DISPLAY_NAMES = {
    "happ": "Happ",
    "v2raytun": "v2rayTun",
    "incy": "INCY",
}


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


def _display_identity(connection, account: dict, aliases: list[dict]) -> dict:
    """Presentation identity only; never used for account linkage/authority.

    DL-057: `display_name` (an owner-set PH7-13 label, e.g. "Megochel" for a
    merged account) outranks the ad-hoc per-alias `display_note` and the
    bare legacy `primary_alias` wherever a single human-facing title is
    needed -- absent one, behavior is unchanged."""
    primary = aliases[0] if aliases else None
    noted = next((alias for alias in aliases if alias.get("note")), None)
    display_name = get_display_name(_DisplayNameDb(connection), account["id"])
    return {
        "display_name": display_name,
        "display_note": noted["note"] if noted else None,
        "display_note_source_alias": noted["legacy_username"] if noted else None,
        "primary_alias": primary["legacy_username"] if primary else None,
        "public_id": account["public_id"],
    }


class _DisplayNameDb:
    """Adapts a bare connection to the tiny `db._conn` surface
    `account_consolidation.get_display_name()` needs, so this read-only
    presentation module never has to carry a full `Database` reference."""

    __slots__ = ("_conn",)

    def __init__(self, connection):
        self._conn = connection


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


def _telemetry_observations(db, account_id: int, aliases: list[dict]) -> list[dict]:
    """PH8-06 internal-only evidence feed for `project_real_device` -- never
    returned directly. Canonical PH8-06 opaque telemetry (keyed by CURRENT
    slot generation, `device_telemetry.record_observation`) is always
    preferred; the PH7-05 legacy `user_devices` evidence is included ONLY
    for a `hwid_verifier` that canonical telemetry does not already cover,
    so a slot with a canonical row is decided purely on that canonical
    evidence -- never a timestamp/similarity contest between the two
    sources. Every observation is tagged with THIS account's own
    `account_id` (never another account's), so a cross-account HWID can
    never even be considered a candidate.
    """
    canonical = db.device_telemetry.list_for_account(account_id)
    canonical_verifiers = {row["hwid_verifier"] for row in canonical}

    legacy = []
    for alias in aliases:
        username = alias.get("legacy_username")
        if not username:
            continue
        for row in db.get_user_devices_with_verifier(username):
            if not row.get("is_active") or not row.get("hwid_verifier"):
                continue
            if row["hwid_verifier"] in canonical_verifiers:
                continue
            legacy.append({
                "account_id": account_id,
                "hwid_verifier": row["hwid_verifier"],
                "observed_id": row.get("id"),
                "model": row.get("display_name") or row.get("device_name"),
                "platform": _humanize_platform(row.get("platform")),
                "client_name": _humanize_client_name(row.get("client_name")),
                "client_version": row.get("client_version") or None,
                "last_seen_at": row.get("last_seen"),
            })
    return [
        {
            **row,
            "platform": _humanize_platform(row.get("platform")),
            "client_name": _humanize_client_name(row.get("client_name")),
        }
        for row in canonical
    ] + legacy


def _device_summaries(
    connection, account_id: int, *, device_slot_hmac_key: bytes | str = "",
    telemetry_observations: list[dict] | None = None,
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
        # PH7-05: telemetry_observations only carries evidence once a real
        # request has hit the PH7-05 telemetry bridge (Database.
        # check_device_access(hwid_hmac_key=...)) for this account's own
        # username(s) -- see device_real_projection module docstring for the
        # exact proof-key contract.
        item["real_device"] = project_real_device(
            {
                "account_id": account_id,
                "generation_status": item["generation_status"],
                "is_genesis": item["proven_genesis_bootstrap"],
                "hwid_verifier": verifier,
            },
            telemetry_observations or [],
        )
        result.append(item)
    return result


def _humanize_platform(value: str | None) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    return _RAW_PLATFORM_LABELS.get(text.lower(), text)


def _humanize_client_name(value: str | None) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    return _CLIENT_DISPLAY_NAMES.get(text.lower(), text)


def _known_client_devices(db, aliases: list[dict]) -> list[dict]:
    """Real client-observed devices for this account's legacy username(s).

    Sourced from the already-existing, continuously-updated `user_devices`
    table (populated by `Database.check_device_access` on every real legacy
    `/sub/{token}` hit -- the same path every currently-migrated account's
    real traffic still runs through). This is genuine client evidence
    (device name/OS/VPN app+version/last activity), never a per-slot
    inference: a genesis/bootstrap placeholder slot never sends a real HTTP
    request, so it can never appear here by construction.
    """
    items = []
    for alias in aliases:
        username = alias.get("legacy_username")
        if not username:
            continue
        for row in db.get_user_devices(username):
            if not row.get("is_active"):
                continue
            items.append({
                "name": row.get("display_name") or row.get("device_name"),
                "platform": _humanize_platform(row.get("platform")),
                "client_name": _humanize_client_name(row.get("client_name")),
                "client_version": row.get("client_version") or None,
                "last_seen": row.get("last_seen"),
                "first_seen": row.get("first_seen"),
            })
    items.sort(key=lambda item: item["last_seen"] or 0, reverse=True)
    return items


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


def _device_action_availability(connection, account_id: int) -> dict[int, dict]:
    """Per-slot action availability, derived only from the existing durable
    lifecycle tables. Revoke/Free/Rebind follow DL-049; Disable/Enable reflect
    the reversible pause primitive (`DeviceSlotAdminStore`, PH7-05). This is
    presentation-only: every mutation route independently re-validates
    lifecycle state server-side and never trusts this view."""
    ops = connection.execute(
        "SELECT s.slot_number,o.operation_kind,o.state,o.last_error_class,"
        "o.id AS lifecycle_id,c.id AS child_intent_id,"
        "c.observed_state AS child_observed,s.desired_state AS slot_desired "
        "FROM mgboost_child_lifecycle_operations o "
        "JOIN mgboost_child_user_intents c ON c.id=o.old_child_intent_id "
        "JOIN mgboost_device_slot_generations g ON g.id=c.slot_generation_id "
        "JOIN mgboost_device_slots s ON s.id=g.slot_id "
        "WHERE o.account_id=? ORDER BY o.updated_at DESC", (int(account_id),),
    ).fetchall()
    per_slot: dict[int, list] = {}
    for row in ops:
        per_slot.setdefault(row["slot_number"], []).append(row)
    active_intents = {
        row["slot_number"]: row
        for row in connection.execute(
            "SELECT s.slot_number,c.id AS child_intent_id,c.observed_state,"
            "c.desired_state AS child_desired,c.uuid_verifier,"
            "s.desired_state AS slot_desired,s.observed_state AS slot_observed "
            "FROM mgboost_device_slots s "
            "JOIN mgboost_device_slot_generations g ON g.slot_id=s.id AND g.status='ACTIVE' "
            "JOIN mgboost_child_user_intents c ON c.slot_generation_id=g.id "
            "WHERE s.account_id=?", (int(account_id),),
        ).fetchall()
    }
    # Recoverable poisoned CHILD_USER_ENSURE outbox per slot (child_recovery
    # module's own narrowed RECOVERABLE_ERROR_CLASSES allowlist) -- this is
    # presentation-only, the recovery route re-validates everything fresh.
    recoverable_ops = {
        row["slot_number"]: row
        for row in connection.execute(
            "SELECT s.slot_number,o.operation_id,o.last_error_class "
            "FROM mgboost_outbox o "
            "JOIN mgboost_child_user_intents c ON c.id=o.child_intent_id "
            "JOIN mgboost_device_slot_generations g ON g.id=c.slot_generation_id AND g.status='ACTIVE' "
            "JOIN mgboost_device_slots s ON s.id=g.slot_id "
            "WHERE o.account_id=? AND o.operation_kind='CHILD_USER_ENSURE' AND o.state='ERROR'",
            (int(account_id),),
        ).fetchall()
    }
    result = {}
    for slot_number in sorted(set(per_slot) | set(active_intents)):
        rows = per_slot.get(slot_number, [])
        intent_row = active_intents.get(slot_number)
        child_observed = intent_row["observed_state"] if intent_row else None
        revoke_applied = any(
            r["operation_kind"] == "REVOKE" and r["state"] == "APPLIED"
            for r in rows if intent_row is not None and r["child_intent_id"] == intent_row["child_intent_id"]
        )
        pending_free = next(
            (r for r in rows if r["operation_kind"] == "FREE" and r["state"] != "APPLIED"),
            None,
        ) if intent_row else None
        blocking_rebind = next(
            (r for r in rows if r["operation_kind"] == "REBIND"),
            None,
        ) if intent_row else None
        slot_desired_value = next((r["slot_desired"] for r in rows), None)
        if slot_desired_value is None:
            latest_slot = connection.execute(
                "SELECT desired_state FROM mgboost_device_slots WHERE account_id=? AND slot_number=?",
                (int(account_id), slot_number),
            ).fetchone()
            slot_desired_value = latest_slot["desired_state"] if latest_slot else None
        entry: dict = {}
        pause_target_live = (
            intent_row is not None and child_observed != "REVOKED" and not revoke_applied
        )
        if pause_target_live and slot_desired_value == "ACTIVE":
            entry["disable"] = "available"
        elif slot_desired_value == "DISABLED":
            entry["disable"] = "done"
        else:
            entry["disable"] = "unavailable"
        if pause_target_live and slot_desired_value == "DISABLED":
            entry["enable"] = "available"
        elif slot_desired_value != "DISABLED" or not pause_target_live:
            entry["enable"] = "unavailable"
        if intent_row is not None and child_observed != "REVOKED" and not revoke_applied:
            entry["revoke"] = "available"
        elif revoke_applied:
            entry["revoke"] = "done"
        else:
            entry["revoke"] = "unavailable"
        # A paused slot still owns its active generation, so Free stays legal
        # after the confirmed Revoke exactly as for an ACTIVE slot.
        if revoke_applied and slot_desired_value in ("ACTIVE", "DISABLED"):
            entry["free"] = "available" if pending_free is None else f"PENDING:{pending_free['state']}"
        elif pending_free is not None:
            entry["free"] = f"PENDING:{pending_free['state']}"
        else:
            entry["free"] = "unavailable"
        if intent_row is not None and child_observed != "REVOKED":
            if blocking_rebind is None:
                entry["rebind"] = "available"
            elif blocking_rebind["state"] == "APPLIED":
                entry["rebind"] = "done"
            else:
                entry["rebind"] = f"PENDING:{blocking_rebind['state']}"
        elif blocking_rebind is not None and blocking_rebind["state"] == "APPLIED":
            entry["rebind"] = "done"
        else:
            entry["rebind"] = "unavailable"
        last_error = next((r["last_error_class"] for r in rows if r["last_error_class"]), None)
        if last_error:
            entry["last_error_class"] = last_error
        # Sync (normal STATE_SYNC convergence) vs Recover (audited broken-
        # child repair, `child_recovery.repair_child_ensure`) are disjoint
        # by construction: Sync is only ever offered for the exact identity
        # `parent_sync.enqueue_current_children` itself would select
        # (live generation, non-REVOKED, observed ACTIVE/DISABLED, a proven
        # `uuid_verifier`); Recover is only ever offered for a durably
        # ERROR'd CHILD_USER_ENSURE whose `last_error_class` is in the
        # child_recovery module's own narrowed recoverable-class allowlist.
        # A slot can show at most one of the two.
        recover_op = recoverable_ops.get(slot_number)
        if recover_op is not None and recover_op["last_error_class"] in RECOVERABLE_ERROR_CLASSES:
            entry["recover"] = "available"
        else:
            entry["recover"] = "unavailable"
        sync_eligible = (
            intent_row is not None
            and child_observed in ("ACTIVE", "DISABLED")
            and intent_row["child_desired"] != "REVOKED"
            and intent_row["uuid_verifier"] is not None
            and entry["recover"] != "available"
        )
        if sync_eligible:
            mismatch = (
                intent_row["slot_desired"] != intent_row["slot_observed"]
                or intent_row["child_desired"] != child_observed
            )
            entry["sync"] = "available" if mismatch else "not_needed"
        else:
            entry["sync"] = "unavailable"
        result[slot_number] = entry
    return result


def _manual_payments_summary(db, account_id: int, *, limit: int = 50) -> list[dict]:
    try:
        records = db.manual_payments.list_records(account_id=account_id, limit=limit)
    except Exception:
        return []
    conn = db._conn
    sync_states = {
        row["payment_record_id"]: dict(row) for row in conn.execute(
            "SELECT payment_record_id,state,attempts,last_error_class "
            "FROM mgboost_manual_payment_sync_jobs WHERE account_id=?", (int(account_id),),
        ).fetchall()
    }
    applications = {
        row["payment_record_id"]: dict(row) for row in conn.execute(
            "SELECT payment_record_id,applied_operation,applied_expiry,created_at AS applied_at "
            "FROM mgboost_manual_payment_applications WHERE account_id=?", (int(account_id),),
        ).fetchall()
    }
    result = []
    for record in records:
        item = {
            "id": record["id"], "public_id": record["public_id"], "kind": record["kind"],
            "status": record["status"],
            "plan_code": record["plan_code_snapshot"] or None,
            "package_sku": record["package_sku_snapshot"] or None,
            "duration_days": record["duration_days_snapshot"] or None,
            "package_bytes": record["package_bytes_snapshot"] or None,
            "amount_minor": record["expected_amount_minor"], "currency": record["currency"],
            "payment_method": record["payment_method"],
            "external_reference": record["external_reference"],
            "comment": record["comment"] or None,
            "created_at": record["created_at"], "updated_at": record["updated_at"],
            "cancelled_at": record["cancelled_at"] or None,
        }
        application = applications.get(record["id"])
        if application:
            item["application"] = application
        sync = sync_states.get(record["id"])
        item["sync_state"] = sync["state"] if sync else None
        item["sync_attempts"] = sync["attempts"] if sync else None
        item["sync_last_error_class"] = (sync["last_error_class"] if sync else None) or None
        result.append(item)
    return result


def _canonical_payments_summary(connection, account_id: int, *, limit: int = 10) -> list[dict]:
    rows = connection.execute(
        "SELECT public_id,payment_channel,record_status,amount_minor,currency,"
        "payment_method,external_reference,actor_type,created_at "
        "FROM mgboost_payment_records WHERE account_id=? "
        "ORDER BY created_at DESC,id DESC LIMIT ?", (int(account_id), limit),
    ).fetchall()
    return [dict(row) for row in rows]


def _legacy_stars_summary(db, aliases: list[dict], *, limit: int = 5) -> list[dict]:
    usernames = [alias["legacy_username"] for alias in aliases if alias.get("legacy_username")]
    if not usernames:
        return []
    placeholders = ",".join("?" * len(usernames))
    rows = db._conn.execute(
        f"SELECT id,marzban_username,tariff_name,duration_days,stars_price,status,target_expire,created_at "
        f"FROM stars_invoices WHERE marzban_username IN ({placeholders}) "
        f"ORDER BY created_at DESC,id DESC LIMIT ?", (*usernames, limit),
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["legacy_source"] = True
        result.append(item)
    return result


def _entitlement_detail(db, account_id: int, *, now: int):
    """PH5-04 is the authoritative read-only composition; never recompute
    entitlement client-side or in a second module."""
    try:
        return db.entitlements.calculate(account_id=int(account_id), now=int(now))
    except EntitlementNotFoundError:
        return None


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
    identity = _display_identity(db._conn, account, aliases)
    action_availability = _device_action_availability(db._conn, account_id)
    devices = _device_summaries(
        db._conn, account_id, device_slot_hmac_key=device_slot_hmac_key,
        telemetry_observations=_telemetry_observations(db, account_id, aliases),
    )
    for device in devices:
        device["actions"] = action_availability.get(device["slot_number"], {})
    return {
        "account": account,
        "display_identity": identity,
        "aliases": aliases,
        "subscription": _subscription_summary(db, account_id, now=timestamp),
        "entitlement": _entitlement_detail(db, account_id, now=timestamp),
        "credential": _credential_summary(db._conn, account_id),
        "devices": devices,
        "known_client_devices": _known_client_devices(db, aliases),
        "telegram": {
            "status": grace["telegram_status"],
            "identities": [dict(row) for row in identities],
        },
        "migration_grace": {
            **grace, "grace": _grace_progress(grace["grace"], now=timestamp),
            "action": classify_action(grace),
        },
        "manual_payments": _manual_payments_summary(db, account_id),
        "payment_records": _canonical_payments_summary(db._conn, account_id),
        "legacy_stars_invoices": _legacy_stars_summary(db, aliases),
        "timeline": account_timeline(db, account_id, limit_per_source=15),
        "technical": _technical_summary(db._conn, account_id, account["public_id"]),
        "consolidation": _consolidation_summary(db, account_id),
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
        identity = _display_identity(db._conn, account, aliases)
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
            **_display_identity(db._conn, account, aliases),
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


def _consolidation_summary(db, account_id: int) -> dict:
    """DL-057: surfaces PH7-13 merge state on both sides -- what this
    account absorbed (as canonical survivor) and, if it is itself an
    absorbed account, which survivor it now resolves to."""
    absorbed_into = db._conn.execute(
        "SELECT survivor_account_id,status,decision_ref,created_at "
        "FROM mgboost_account_merges WHERE absorbed_account_id=?",
        (int(account_id),),
    ).fetchone()
    absorbs_rows = db._conn.execute(
        "SELECT m.absorbed_account_id,m.status,m.decision_ref,m.created_at,"
        "(SELECT legacy_username FROM mgboost_legacy_account_aliases "
        " WHERE account_id=m.absorbed_account_id AND alias_role='PRIMARY') AS legacy_username "
        "FROM mgboost_account_merges m WHERE m.survivor_account_id=? ORDER BY m.created_at",
        (int(account_id),),
    ).fetchall()
    return {
        "absorbed_into": dict(absorbed_into) if absorbed_into else None,
        "absorbs": [dict(row) for row in absorbs_rows],
    }


def _queue_label(db, account_id: int) -> dict:
    account_id = resolve_account_id(db, account_id)
    account = db.accounts.get_account(account_id)
    if account is None:
        return {"account_id": account_id, "label": f"#{account_id}"}
    aliases = _aliases(db._conn, account_id)
    identity = _display_identity(db._conn, account, aliases)
    label = identity["display_name"] or identity["display_note"] or identity["primary_alias"] or identity["public_id"]
    return {"account_id": account_id, "label": label,
            "primary_alias": identity["primary_alias"]}


def _manual_payment_queues(db, now: int) -> dict:
    conn = db._conn
    try:
        records = db.manual_payments.list_records(limit=200)
    except Exception:
        records = []
    counts: dict[str, int] = {}
    pending_items: list[dict] = []
    review_items: list[dict] = []
    sync_items: list[dict] = []
    for record in records:
        status = record.get("status")
        counts[status] = counts.get(status, 0) + 1
        base = {
            "public_id": record.get("public_id"), "kind": record.get("kind"),
            "amount_minor": record.get("expected_amount_minor"),
            "currency": record.get("currency"), "created_at": record.get("created_at"),
            "plan_code": record.get("plan_code_snapshot"),
            "package_sku": record.get("package_sku_snapshot"),
            "duration_days": record.get("duration_days_snapshot"),
            **_queue_label(db, record.get("account_id")),
        }
        if status == "PENDING" and len(pending_items) < 8:
            pending_items.append(base)
        elif status == "MANUAL_REVIEW" and len(review_items) < 8:
            review_items.append(base)
    for row in db._conn.execute(
        "SELECT payment_record_id,account_id,state,last_error_class FROM "
        "mgboost_manual_payment_sync_jobs WHERE state!='SYNCED' ORDER BY updated_at LIMIT 8"
    ).fetchall():
        item = dict(row)
        item.update(_queue_label(db, row["account_id"]))
        sync_items.append(item)
    return {
        "counts_by_status": counts,
        "pending": pending_items,
        "manual_review": review_items,
        "sync_pending": sync_items,
    }


def _stars_manual_review_queue(db) -> tuple[int, list[dict]]:
    """Legacy Stars manual-review invoices also stay on their own existing
    screen; surfaced here so the operator queue is complete."""
    conn = db._conn
    total = conn.execute(
        "SELECT COUNT(*) FROM stars_invoices WHERE status='manual_review'"
    ).fetchone()[0]
    items = []
    for row in conn.execute(
        "SELECT id,marzban_username,tariff_name,stars_price,target_expire FROM "
        "stars_invoices WHERE status='manual_review' ORDER BY created_at DESC LIMIT 8"
    ).fetchall():
        item = dict(row)
        linked = conn.execute(
            "SELECT account_id FROM mgboost_legacy_account_aliases WHERE legacy_username=? LIMIT 1",
            (row["marzban_username"],),
        ).fetchone()
        if linked:
            item.update(_queue_label(db, linked["account_id"]))
        else:
            item["account_id"] = None
            item["label"] = row["marzban_username"]
        items.append(item)
    return int(total), items


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
        identity = _display_identity(db._conn, account, aliases)
        item["label"] = identity["display_name"] or identity["display_note"] or identity["primary_alias"] or identity["public_id"]
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
    stars_review_count, stars_review_items = _stars_manual_review_queue(db)
    parent_sync_pending = db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_parent_sync_operations WHERE state IN ('PENDING','RETRY')"
    ).fetchone()[0]
    queues = {
        **_manual_payment_queues(db, timestamp),
        "stars_manual_review": {"count": stars_review_count, "items": stars_review_items},
        "child_sync_pending_count": int(parent_sync_pending),
    }
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
        "queues": queues,
    }
