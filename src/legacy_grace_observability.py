"""PH4-05 read-only grace-period observability.

Pure query/assembly functions -- no mutation, no route wiring, no
gating/denial decision anywhere in this module. Composes the picture from
already-existing durable tables wherever one exists (PH4-02 migration
lineage/events, PH3-02 device slots, PH2-01 opaque credential `last_used_at`)
plus the new PH4-05 grace-period and grace-activity tables, rather than
duplicating any of them. Used by both `scripts/ph4_05_grace_eligibility_report.py`
(pre-start dry run, `grace` is always `None`) and any future admin/support
surface once grace periods are actually running.

Privacy: every field here is either an internal integer id, a bounded safe
enum/string already used elsewhere in this project's own audit trail, or a
UTC timestamp -- never a raw token, full subscription URL, UUID, full HWID,
cookie/auth value or bearer path."""

from __future__ import annotations

import sqlite3

from .legacy_grace import day_index, grace_active, seconds_remaining
from .legacy_grace_activity import count_since, last_seen

_MIGRATION_STATES = (
    "MIGRATING", "MIGRATED", "LEGACY_REVOKE_PENDING", "LEGACY_REVOKED", "ERROR_RECONCILE",
)

_DAY_SECONDS = 86400


def _migration_state_counts(connection: sqlite3.Connection, account_id: int) -> dict[str, int]:
    counts = {state: 0 for state in _MIGRATION_STATES}
    rows = connection.execute(
        "SELECT state, COUNT(*) AS n FROM mgboost_migration_bindings "
        "WHERE account_id=? GROUP BY state",
        (int(account_id),),
    ).fetchall()
    for row in rows:
        if row["state"] in counts:
            counts[row["state"]] = row["n"]
    return counts


def _active_device_count(connection: sqlite3.Connection, account_id: int) -> int:
    row = connection.execute(
        "SELECT COUNT(*) FROM mgboost_device_slot_generations "
        "WHERE account_id=? AND status='ACTIVE'",
        (int(account_id),),
    ).fetchone()
    return int(row[0]) if row else 0


def _recent_resolver_errors(connection: sqlite3.Connection, account_id: int, *, since: int) -> int:
    """RETRY/ERROR_RECONCILE events on this account's migration bindings --
    the existing PH4-02 audit trail, not a new error log."""
    row = connection.execute(
        "SELECT COUNT(*) FROM mgboost_migration_binding_events "
        "WHERE account_id=? AND event_type IN ('RETRY','ERROR_RECONCILE') AND created_at>=?",
        (int(account_id), int(since)),
    ).fetchone()
    return int(row[0]) if row else 0


def _reconciliation_failures(connection: sqlite3.Connection, account_id: int, *, since: int) -> int:
    row = connection.execute(
        "SELECT COUNT(*) FROM mgboost_migration_binding_events "
        "WHERE account_id=? AND event_type='RECONCILE_STALE' AND created_at>=?",
        (int(account_id), int(since)),
    ).fetchone()
    return int(row[0]) if row else 0


def _revoke_rebind_events(connection: sqlite3.Connection, account_id: int, *, since: int) -> int:
    row = connection.execute(
        "SELECT COUNT(*) FROM mgboost_migration_binding_events "
        "WHERE account_id=? AND event_type IN ('REVOKE_PENDING_STARTED','LEGACY_REVOKED') "
        "AND created_at>=?",
        (int(account_id), int(since)),
    ).fetchone()
    return int(row[0]) if row else 0


def _opaque_last_used(connection: sqlite3.Connection, account_id: int) -> int | None:
    row = connection.execute(
        "SELECT MAX(last_used_at) FROM mgboost_subscription_credentials "
        "WHERE account_id=? AND status='ACTIVE'",
        (int(account_id),),
    ).fetchone()
    return int(row[0]) if row and row[0] is not None else None


def _last_child_fetch(connection: sqlite3.Connection, account_id: int) -> int | None:
    """Proxy for "successful child subscription delivery": the most recent
    time this account's PH4-02 migration lineage moved to a state that only
    happens after a real, successful child fetch (`mark_migrated()` is only
    called on `OUTCOME_OK`). Honest proxy, not a literal per-request signal
    -- documented as such in the grace-period runbook."""
    row = connection.execute(
        "SELECT MAX(updated_at) FROM mgboost_migration_bindings "
        "WHERE account_id=? AND state IN "
        "('MIGRATED','LEGACY_REVOKE_PENDING','LEGACY_REVOKED')",
        (int(account_id),),
    ).fetchone()
    return int(row[0]) if row and row[0] is not None else None


def telegram_status(db, account_id: int) -> str:
    """One of `BOUND` (an active OWNER identity exists), `AMBIGUOUS` (no
    owner yet, but more than one distinct Telegram ID has linked one of
    this account's legacy usernames via the existing bot flow),
    `PENDING_LINK` (exactly one distinct Telegram ID has registered via the
    bot but `bind_telegram_after_registration` has not run for it yet --
    the daily report's catch-up sweep should retry it) or `UNREGISTERED`."""
    connection = db._conn
    account_id = int(account_id)
    owner = connection.execute(
        "SELECT 1 FROM mgboost_telegram_identities "
        "WHERE account_id=? AND role='OWNER' AND revoked_at IS NULL",
        (account_id,),
    ).fetchone()
    if owner is not None:
        return "BOUND"
    row = connection.execute(
        "SELECT COUNT(DISTINCT t.telegram_id) FROM tg_users t "
        "JOIN mgboost_legacy_account_aliases a ON a.legacy_username=t.marzban_username "
        "WHERE a.account_id=?",
        (account_id,),
    ).fetchone()
    distinct_count = int(row[0]) if row and row[0] is not None else 0
    if distinct_count > 1:
        return "AMBIGUOUS"
    if distinct_count == 1:
        return "PENDING_LINK"
    return "UNREGISTERED"


def _bridge_enabled(connection: sqlite3.Connection, account_id: int) -> bool:
    row = connection.execute(
        "SELECT 1 FROM mgboost_legacy_bridge_bindings WHERE account_id=? AND enabled=1",
        (int(account_id),),
    ).fetchone()
    return row is not None


def _raw_legacy_request_seen(connection: sqlite3.Connection, account_id: int, *, since: int) -> bool:
    """Uses the pre-existing, unrelated `sub_requests` log (raw
    Marzban-username-keyed, present since before PH4-05) to detect "a
    client is still actively hitting this account's legacy subscription" --
    independent of whether a bridge binding exists yet. Never stores or
    returns anything from that table beyond a boolean."""
    row = connection.execute(
        "SELECT 1 FROM sub_requests WHERE timestamp>=? AND username IN "
        "(SELECT legacy_username FROM mgboost_legacy_account_aliases WHERE account_id=?) LIMIT 1",
        (int(since), int(account_id)),
    ).fetchone()
    return row is not None


ACTION_OK_MIGRATED = "OK_MIGRATED"
# Device-migration semantics only: the account has no real-device migration
# lineage yet (not a single `mgboost_migration_bindings` row), so it is
# waiting for its first real device connection. Telegram ownership is NOT an
# input to this state -- it is reported separately by `telegram_status()`.
ACTION_WAITING_FIRST_DEVICE = "WAITING_FIRST_DEVICE"
ACTION_CONTACT_USER = "CONTACT_USER"
ACTION_MANUAL_REVIEW = "MANUAL_REVIEW"
ACTION_COMPATIBILITY_BLOCK = "COMPATIBILITY_BLOCK"
ACTION_RECONCILE_REQUIRED = "RECONCILE_REQUIRED"

CONTACT_USER_DAYS_REMAINING_THRESHOLD = 3


def classify_action(snapshot: dict) -> str:
    """Pure decision over an already-assembled `account_grace_snapshot()`
    result -- no query, no mutation, trivially unit-testable. Order matters:
    the most actionable/urgent category wins. `OK_MIGRATED` means a real
    device migration lineage exists at all (`MIGRATING`, `MIGRATED`,
    `LEGACY_REVOKE_PENDING` or `LEGACY_REVOKED` -- the first connection
    already happened and migration is proceeding normally); Telegram absence
    never gates it and never produces its own pseudo migration state here --
    AMBIGUOUS/CONTACT_USER below are the campaign's operational outreach
    categories, not device-migration states."""
    migration_state = snapshot["migration_state"]
    if migration_state["ERROR_RECONCILE"] > 0:
        return ACTION_RECONCILE_REQUIRED
    if snapshot["telegram_status"] == "AMBIGUOUS":
        return ACTION_MANUAL_REVIEW
    if (
        snapshot["bridge_enabled"]
        and snapshot["active_devices"] == 0
        and snapshot["raw_legacy_request_seen_72h"]
    ):
        return ACTION_COMPATIBILITY_BLOCK
    if sum(migration_state.values()) > 0:
        return ACTION_OK_MIGRATED
    grace = snapshot["grace"]
    if grace is not None and grace["active"]:
        days_remaining = grace["seconds_remaining"] / 86400
        if days_remaining <= CONTACT_USER_DAYS_REMAINING_THRESHOLD and snapshot["telegram_status"] != "BOUND":
            return ACTION_CONTACT_USER
    return ACTION_WAITING_FIRST_DEVICE


def account_grace_snapshot(db, account_id: int, *, now: int) -> dict:
    """Assembles the full per-account visibility set required by PH4-05's
    accept criteria. `grace` is `None` for an account that has not started
    yet (the normal dry-run/eligibility case)."""
    connection = db._conn
    account_id = int(account_id)
    grace_row = db.legacy_grace.find_by_account(account_id)

    grace_summary = None
    if grace_row is not None:
        grace_summary = {
            "cohort_ref": grace_row["cohort_ref"],
            "started_at": grace_row["started_at"],
            "original_end_at": grace_row["original_end_at"],
            "current_end_at": grace_row["current_end_at"],
            "revision": grace_row["revision"],
            "active": grace_active(grace_row["current_end_at"], now=now),
            "day_of_14": day_index(grace_row["started_at"], now=now),
            "seconds_remaining": seconds_remaining(grace_row["current_end_at"], now=now),
            "extended": grace_row["current_end_at"] != grace_row["original_end_at"],
        }

    since_24h = now - _DAY_SECONDS
    since_72h = now - 3 * _DAY_SECONDS
    legacy_last = last_seen(connection, account_id, "LEGACY")
    opaque_last = _opaque_last_used(connection, account_id)
    if opaque_last is None:
        opaque_last = last_seen(connection, account_id, "OPAQUE")

    inactive_since_grace_start = None
    if grace_row is not None:
        started = grace_row["started_at"]
        newest = max(filter(None, [legacy_last, opaque_last]), default=None)
        inactive_since_grace_start = newest is None or newest < started

    migration_state = _migration_state_counts(connection, account_id)

    return {
        "account_id": account_id,
        "grace": grace_summary,
        "migration_state": migration_state,
        "migrated_devices": migration_state["MIGRATED"],
        "active_devices": _active_device_count(connection, account_id),
        "telegram_status": telegram_status(db, account_id),
        "last_legacy_activity": legacy_last,
        "last_opaque_activity": opaque_last,
        "last_child_fetch": _last_child_fetch(connection, account_id),
        "legacy_requests_24h": count_since(connection, account_id, "LEGACY", since=since_24h, now=now),
        "legacy_requests_72h": count_since(connection, account_id, "LEGACY", since=since_72h, now=now),
        "opaque_requests_24h": count_since(connection, account_id, "OPAQUE", since=since_24h, now=now),
        "opaque_requests_72h": count_since(connection, account_id, "OPAQUE", since=since_72h, now=now),
        "resolver_errors_72h": _recent_resolver_errors(connection, account_id, since=since_72h),
        "reconciliation_failures_72h": _reconciliation_failures(connection, account_id, since=since_72h),
        "revoke_rebind_events_72h": _revoke_rebind_events(connection, account_id, since=since_72h),
        "inactive_since_grace_start": inactive_since_grace_start,
        "bridge_enabled": _bridge_enabled(connection, account_id),
        "raw_legacy_request_seen_72h": _raw_legacy_request_seen(connection, account_id, since=since_72h),
    }
