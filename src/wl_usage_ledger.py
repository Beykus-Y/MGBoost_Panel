"""PH6-03 -- durable monotonic WL usage ledger/collector.

Observe/accounting-only: this module never disables, resets, or otherwise
mutates anything on the Marzban side, and it never touches
`mgboost_wl_periods`/subscriptions/entitlements/inbounds -- it only reads
already-existing, already-safe usage endpoints (through the same
`ServiceMarzbanClient`/broker path every other read-only usage caller in
this codebase already uses -- `legacy.user.usage` accepts any
`[A-Za-z0-9_.@-]{1,128}` username, which every real `mgc_*` child username
already satisfies; no new broker surface was needed) and durably records
what it observed.

Real production Marzban semantics this design is built against (verified
2026-08-26 by reading the live Marzban 0.8.4 source over SSH --
`app/jobs/record_usages.py`, `app/db/crud.py`, `app/db/models.py` -- not
assumed):

- Marzban's own scheduler job reads each node's live xray-core stats with
  `reset=True` (the in-process counter is atomically zeroed on every read)
  and *adds* that delta into a durable per-(user, node, UTC-hour) row
  (`NodeUserUsage.used_traffic`) plus the user's own cumulative
  `used_traffic` column. A node restart therefore never causes a visible
  *decrease* at the API layer we read from -- at worst it causes bounded
  *under*-counting (traffic during the node's downtime is simply never
  polled by Marzban itself), which durable per-poll accumulation on our
  side cannot recover either, and isn't expected to.
- `GET /api/user/{username}/usage?start=&end=` (`crud.get_user_usages`)
  returns, per node, the *sum* of that user's hour-bucketed rows whose
  `created_at` falls in `[start, end]` -- an interval sum, so it can never
  itself report a negative delta for a single query.
- The one real decrease vector is `POST /api/user/{username}/reset` (or
  `next_plan` activation): `crud.reset_user_data_usage` calls
  `dbuser.node_usages.clear()`, and `User.node_usages` is declared
  `cascade="all, delete-orphan"` -- an admin reset does not just zero a
  counter, it *deletes every historical `NodeUserUsage` row for that user*.
  A query spanning through a reset can therefore genuinely report less
  than what we already durably recorded for an earlier, already-consumed
  window. This collector's cursor is a *last observed cumulative total*,
  not a subtractive running total, precisely so a reset is detected
  (`cursor_after < cursor_before`) rather than silently corrupting the
  ledger with a negative delta -- see `record_sample` below. The narrow,
  irreducible residual risk (a reset landing in the still-unconsumed
  window between two polls loses that window's real traffic, bounded by
  the poll interval) is a real limitation of Marzban's own reset semantics,
  not a bug in this module; it is why the design keeps the poll interval
  short and durably records every detected reset for operator visibility
  (`mgboost_wl_usage_sample_events.reset_detected`) instead of pretending
  it cannot happen.
- Children never have Marzban-side `data_limit`/reset strategy set
  (`child_contract.build_child_payload` always sends
  `data_limit=None`/`"no_reset"`) -- there is no scheduled, automatic,
  Marzban-internal reset vector to guard against beyond the admin-triggered
  one already covered above.

Attribution: a "child" here is a currently-live `mgboost_child_user_intents`
row (`observed_state='ACTIVE'` -- i.e. a real, currently-provisioned
Marzban user, the same liveness check `child_provisioning.py` itself uses
before creating an outbox operation). The exact live Marzban username is
already stored on that row (`child_username`, PH3-03) -- it is used only
transiently, in-memory, to make the read-only usage call; it is never
itself persisted into this ledger's own tables or logged. Every WL-period
boundary is exactly UTC-hour aligned (DL-020,
`subscription_renewal.align_to_utc_hour`), so a single UTC-hour sample
bucket can never straddle two periods -- `wl_period_id` attribution is
therefore unambiguous whenever a period exists to attribute to (today, in
production, none do yet -- PH6-03 stays fully dormant-safe, matching the
PH6-01/02 precedent, until a real purchase flow starts creating periods).
"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone

from .subscription_renewal import align_to_utc_hour
from .wl_topology import WL_NODE_IDS


_EPOCH_ISO = "1970-01-01T00:00:00+00:00"
_DEFAULT_LEASE_SECONDS = 300


class WLUsageLedgerError(RuntimeError):
    pass


class CollectorLeaseHeld(WLUsageLedgerError):
    """Another worker currently owns the single collector lease."""


def _iso(timestamp: int) -> str:
    return datetime.fromtimestamp(int(timestamp), tz=timezone.utc).isoformat()


def _usage_for_node(usages: list, node_id: int) -> int:
    for entry in usages or []:
        if entry.get("node_id") == node_id:
            value = entry.get("used_traffic", 0)
            return int(value) if value else 0
    return 0


class WLUsageLedgerStore:
    def __init__(self, connection: sqlite3.Connection, lock):
        self._conn = connection
        self._lock = lock

    # ------------------------------------------------------------------
    # Single-leader CAS lease (mirrors the PH3-03 `mgboost_outbox` lease
    # shape exactly: lease_owner/lease_expires_at/row_version). Any number
    # of processes/hosts may race to claim this; only one wins per window.
    # ------------------------------------------------------------------
    def claim_collector_lease(self, *, worker_id: str, now: int, lease_seconds: int = _DEFAULT_LEASE_SECONDS) -> bool:
        if not isinstance(worker_id, str) or not 1 <= len(worker_id) <= 128:
            raise WLUsageLedgerError("invalid collector worker identity")
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    "SELECT lease_owner, lease_expires_at, row_version "
                    "FROM mgboost_wl_usage_collector_lease WHERE id=1"
                ).fetchone()
                if row is None:
                    self._conn.rollback()
                    raise WLUsageLedgerError("collector lease row missing (schema not applied)")
                claimable = row["lease_owner"] is None or row["lease_expires_at"] <= now
                if not claimable:
                    self._conn.rollback()
                    return False
                updated = self._conn.execute(
                    "UPDATE mgboost_wl_usage_collector_lease SET lease_owner=?,"
                    "lease_expires_at=?,last_run_started_at=?,row_version=row_version+1,"
                    "updated_at=? WHERE id=1 AND row_version=?",
                    (worker_id, now + max(5, int(lease_seconds)), now, now, row["row_version"]),
                ).rowcount
                if updated != 1:
                    self._conn.rollback()
                    return False
                self._conn.commit()
                return True
            except Exception:
                self._conn.rollback()
                raise

    def release_collector_lease(
        self, *, worker_id: str, now: int, outcome: str = "OK", error_class: str | None = None,
    ) -> None:
        if outcome not in ("OK", "PARTIAL", "ERROR"):
            raise WLUsageLedgerError("invalid collector run outcome")
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    "SELECT lease_owner FROM mgboost_wl_usage_collector_lease WHERE id=1"
                ).fetchone()
                if row is None or row["lease_owner"] != worker_id:
                    self._conn.rollback()
                    raise WLUsageLedgerError("collector lease is not owned by this worker")
                self._conn.execute(
                    "UPDATE mgboost_wl_usage_collector_lease SET lease_owner=NULL,"
                    "lease_expires_at=NULL,last_run_completed_at=?,last_run_outcome=?,"
                    "last_run_error_class=?,row_version=row_version+1,updated_at=? WHERE id=1",
                    (now, outcome, error_class, now),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    # ------------------------------------------------------------------
    # Read-only attribution helpers
    # ------------------------------------------------------------------
    def list_live_children(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT id AS child_intent_id, account_id, child_username "
            "FROM mgboost_child_user_intents WHERE observed_state='ACTIVE'"
        ).fetchall()
        return [dict(row) for row in rows]

    def resolve_active_wl_period(self, account_id: int, at_timestamp: int) -> int | None:
        row = self._conn.execute(
            "SELECT id FROM mgboost_wl_periods WHERE account_id=? AND status='ACTIVE' "
            "AND starts_at <= ? AND ends_at > ? LIMIT 1",
            (int(account_id), int(at_timestamp), int(at_timestamp)),
        ).fetchone()
        return int(row["id"]) if row is not None else None

    def sync_wl_period_statuses(self, *, account_id: int, now: int) -> None:
        """Pure, idempotent, time-only advance of this account's own WL
        periods through the exact `PLANNED -> ACTIVE -> CLOSED` state
        machine `wl_period_lifecycle_schema.py`'s own docstring already
        named but deliberately left unbuilt ("Phase 6's own future runtime
        concern"). Never a policy decision -- a period becomes ACTIVE the
        instant its own `starts_at` arrives and CLOSED the instant its own
        `ends_at` passes, nothing else. `mgboost_wl_periods.status` is the
        one column PH5-02's immutability trigger deliberately left mutable
        for exactly this. Without this, `resolve_active_wl_period` (used
        both by PH6-03's own collector and PH6-04's shared-pool read model)
        can never find an ACTIVE period for a real purchase, since
        `apply_same_plan_purchase`/`WLPeriodAdminResetStore.reset_period`
        only ever create/leave rows `PLANNED`.

        Close-before-activate ordering handles two edge cases in one pass:
        a period whose entire window already fully elapsed (e.g. after a
        long collector gap) closes directly from PLANNED without ever
        needing to pass through ACTIVE, so it can never block a later
        sequential period in the same subscription from resolving; and the
        contiguous-boundary instant where one period's `ends_at` exactly
        equals the next period's `starts_at` closes the first and activates
        the second in the same, single, atomic transaction. A `CLOSED`
        period (including one closed early by ADMIN_RESET) is never touched
        again -- this never revives one.

        Ordering matters to callers: `mgboost_wl_usage_samples.wl_period_id`
        is fixed at the first write into a given (child, node, UTC-hour)
        bucket and is then immutable (the identity trigger guards it too) --
        if that first write ever happens before this sync has run for the
        period covering that hour, every later delta added to that same
        bucket stays permanently unattributed. Every real WL period boundary
        is exactly UTC-hour aligned (DL-020), so as long as this always runs
        immediately before `resolve_active_wl_period` on every collection
        (as `run_collection_cycle` already does), a period is always ACTIVE
        by the time its very first hour's very first sample is recorded --
        this is never actually reachable in the real collector path."""
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                self._conn.execute(
                    "UPDATE mgboost_wl_periods SET status='CLOSED' "
                    "WHERE account_id=? AND status IN ('PLANNED','ACTIVE') AND ends_at<=?",
                    (int(account_id), int(now)),
                )
                self._conn.execute(
                    "UPDATE mgboost_wl_periods SET status='ACTIVE' "
                    "WHERE account_id=? AND status='PLANNED' AND starts_at<=? AND ends_at>?",
                    (int(account_id), int(now), int(now)),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def get_cursor(self, *, account_id: int, child_intent_id: int, node_id: int) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM mgboost_wl_usage_cursors WHERE child_intent_id=? AND node_id=?",
            (int(child_intent_id), int(node_id)),
        ).fetchone()
        return dict(row) if row is not None else None

    # ------------------------------------------------------------------
    # The core idempotent write. Must run with an already-observed
    # (cursor_before, cursor_after) pair -- this function performs no
    # Marzban I/O itself, only the durable accounting.
    # ------------------------------------------------------------------
    def record_sample(
        self,
        *,
        account_id: int,
        child_intent_id: int,
        node_id: int,
        cursor_after: int,
        collector_id: str,
        collected_at: int,
        wl_period_id: int | None = None,
    ) -> dict:
        if cursor_after < 0:
            raise WLUsageLedgerError("observed cumulative usage cannot be negative")
        sample_hour = align_to_utc_hour(int(collected_at))
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                cursor_row = self._conn.execute(
                    "SELECT * FROM mgboost_wl_usage_cursors WHERE child_intent_id=? AND node_id=?",
                    (int(child_intent_id), int(node_id)),
                ).fetchone()
                if cursor_row is None:
                    self._conn.execute(
                        "INSERT INTO mgboost_wl_usage_cursors "
                        "(account_id, child_intent_id, node_id, last_observed_cumulative_bytes,"
                        " last_polled_at, created_at, updated_at) VALUES (?,?,?,0,0,?,?)",
                        (int(account_id), int(child_intent_id), int(node_id), collected_at, collected_at),
                    )
                    cursor_before = 0
                    cursor_row_version = 1
                else:
                    cursor_before = int(cursor_row["last_observed_cumulative_bytes"])
                    cursor_row_version = int(cursor_row["row_version"])

                reset_detected = cursor_after < cursor_before
                delta_bytes = cursor_after if reset_detected else (cursor_after - cursor_before)

                try:
                    self._conn.execute(
                        "INSERT INTO mgboost_wl_usage_sample_events "
                        "(account_id, child_intent_id, node_id, sample_hour, cursor_before,"
                        " cursor_after, delta_bytes, reset_detected, collector_id, collected_at,"
                        " created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            int(account_id), int(child_intent_id), int(node_id), sample_hour,
                            cursor_before, int(cursor_after), int(delta_bytes),
                            1 if reset_detected else 0, collector_id, int(collected_at),
                            int(collected_at),
                        ),
                    )
                except sqlite3.IntegrityError:
                    # This exact (child, node, cursor_before) transition was already
                    # durably recorded -- a retried/duplicated poll after a crash
                    # between the Marzban read and this commit. Idempotent no-op.
                    self._conn.rollback()
                    existing = self._conn.execute(
                        "SELECT * FROM mgboost_wl_usage_sample_events "
                        "WHERE child_intent_id=? AND node_id=? AND cursor_before=?",
                        (int(child_intent_id), int(node_id), cursor_before),
                    ).fetchone()
                    return dict(existing)

                existing_sample = self._conn.execute(
                    "SELECT * FROM mgboost_wl_usage_samples "
                    "WHERE child_intent_id=? AND node_id=? AND sample_hour=?",
                    (int(child_intent_id), int(node_id), sample_hour),
                ).fetchone()
                if existing_sample is None:
                    self._conn.execute(
                        "INSERT INTO mgboost_wl_usage_samples "
                        "(account_id, child_intent_id, node_id, wl_period_id, sample_hour,"
                        " bytes_delta, first_collected_at, last_collected_at, created_at, updated_at)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (
                            int(account_id), int(child_intent_id), int(node_id), wl_period_id,
                            sample_hour, int(delta_bytes), int(collected_at), int(collected_at),
                            int(collected_at), int(collected_at),
                        ),
                    )
                else:
                    self._conn.execute(
                        "UPDATE mgboost_wl_usage_samples SET bytes_delta=bytes_delta+?,"
                        "last_collected_at=?,updated_at=? "
                        "WHERE child_intent_id=? AND node_id=? AND sample_hour=?",
                        (
                            int(delta_bytes), int(collected_at), int(collected_at),
                            int(child_intent_id), int(node_id), sample_hour,
                        ),
                    )

                if cursor_row is None:
                    self._conn.execute(
                        "UPDATE mgboost_wl_usage_cursors SET last_observed_cumulative_bytes=?,"
                        "last_polled_at=?,reset_count=reset_count+?,row_version=row_version+1,"
                        "updated_at=? WHERE child_intent_id=? AND node_id=?",
                        (
                            int(cursor_after), int(collected_at), 1 if reset_detected else 0,
                            int(collected_at), int(child_intent_id), int(node_id),
                        ),
                    )
                else:
                    updated = self._conn.execute(
                        "UPDATE mgboost_wl_usage_cursors SET last_observed_cumulative_bytes=?,"
                        "last_polled_at=?,reset_count=reset_count+?,row_version=row_version+1,"
                        "updated_at=? WHERE child_intent_id=? AND node_id=? AND row_version=?",
                        (
                            int(cursor_after), int(collected_at), 1 if reset_detected else 0,
                            int(collected_at), int(child_intent_id), int(node_id), cursor_row_version,
                        ),
                    ).rowcount
                    if updated != 1:
                        raise WLUsageLedgerError(
                            "usage cursor changed concurrently -- collector lease was not exclusive"
                        )

                self._conn.commit()
                return {
                    "child_intent_id": int(child_intent_id),
                    "node_id": int(node_id),
                    "sample_hour": sample_hour,
                    "cursor_before": cursor_before,
                    "cursor_after": int(cursor_after),
                    "delta_bytes": int(delta_bytes),
                    "reset_detected": reset_detected,
                }
            except Exception:
                self._conn.rollback()
                raise


def run_collection_cycle(
    *,
    db,
    service_marzban,
    worker_id: str,
    now: int | None = None,
    lease_seconds: int = _DEFAULT_LEASE_SECONDS,
    node_ids: frozenset = WL_NODE_IDS,
) -> dict:
    """One observe-only collection pass across every live child and every
    configured WL node. Safe to call repeatedly/concurrently from any
    number of processes -- only the single process that wins the lease
    claim does any work; everyone else returns immediately as skipped.

    Never raises for a single child/node read failure -- those are counted
    and reported, not allowed to abort collection for every other child.
    Only raises for a structural problem (lease row missing, topology not
    confirmed OK) that should stop the whole cycle before it starts.
    """
    timestamp = int(time.time()) if now is None else int(now)
    db.wl_topology_guard.require_topology_ok()  # fail closed: reuse PH6-01, never duplicate

    ledger: WLUsageLedgerStore = db.wl_usage_ledger
    if not ledger.claim_collector_lease(worker_id=worker_id, now=timestamp, lease_seconds=lease_seconds):
        return {"skipped": "lease_held_by_other_collector"}

    summary = {"children_seen": 0, "samples_recorded": 0, "resets_detected": 0, "errors": []}
    outcome = "OK"
    try:
        children = ledger.list_live_children()
        summary["children_seen"] = len(children)
        for child in children:
            ledger.sync_wl_period_statuses(account_id=child["account_id"], now=timestamp)
            wl_period_id = ledger.resolve_active_wl_period(child["account_id"], timestamp)
            for node_id in sorted(node_ids):
                try:
                    usage = service_marzban.get_user_usage(
                        child["child_username"], start=_EPOCH_ISO, end=_iso(timestamp),
                    )
                    cursor_after = _usage_for_node(usage.get("usages", []), int(node_id))
                    result = ledger.record_sample(
                        account_id=child["account_id"],
                        child_intent_id=child["child_intent_id"],
                        node_id=int(node_id),
                        cursor_after=cursor_after,
                        collector_id=worker_id,
                        collected_at=timestamp,
                        wl_period_id=wl_period_id,
                    )
                    summary["samples_recorded"] += 1
                    if result["reset_detected"]:
                        summary["resets_detected"] += 1
                except Exception as exc:  # noqa: BLE001 -- deliberately broad, per-child isolation
                    summary["errors"].append(type(exc).__name__)
        if summary["errors"]:
            outcome = "PARTIAL" if summary["samples_recorded"] else "ERROR"
    except Exception as exc:
        outcome = "ERROR"
        summary["errors"].append(type(exc).__name__)
        ledger.release_collector_lease(
            worker_id=worker_id, now=int(time.time()), outcome=outcome,
            error_class=type(exc).__name__,
        )
        raise
    else:
        ledger.release_collector_lease(worker_id=worker_id, now=int(time.time()), outcome=outcome)
    summary["outcome"] = outcome
    return summary
