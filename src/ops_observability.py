"""PH8-04 -- operator health/observability read model.

Same idiom as `wl_reconciliation.backlog_snapshot()` and
`admin_read_models.dashboard_summary()`: synchronous, read-only, pure
SQL-composing functions over existing durable tables, returning
JSON-safe dicts of counts/timestamps/enums -- never a raw token, HWID,
UUID, username, or password. Every new column any future PH8-04 table
adds must stay within that same discipline: fixed-enum TEXT or INTEGER
only, never free text or an exception message.

This module composes signals that already exist durably (collector
freshness/outbox age/drift via `backlog_snapshot()`, WL usage-cursor
monotonicity, `ERROR_RECONCILE` migration backlog, `MANUAL_REVIEW`
legacy-commercial-transition backlog) into one operator health
snapshot. It introduces **no new tables and no new writes** -- this is
deliberately the "free signals" step. Hot-path counters (auth failures,
resolver outcomes) and the permanent acquisition-milestone fact table
are separate, later additions with their own fail-open write path --
they do not belong in this module's read-only composition.

Fail-open composition: `health_snapshot()` is the only entry point an
admin route calls, and it must never let one broken signal (missing
table, locked DB, malformed row) take down the whole operator surface.
Each signal is computed through `_safe_source()`, which turns any
exception into `{"status": "UNKNOWN", "error_class": type(exc).__name__}`
for that signal alone -- never the raw exception text (same redaction
discipline as everywhere else) -- and the top-level `status` becomes
`DEGRADED` rather than raising. This module is read *only*: it has no
write path of its own, so "fail-open" here means "never 500 the
endpoint," not "never block a mutation" (there is no mutation here to
block).
"""

from __future__ import annotations

import time

from .wl_reconciliation import backlog_snapshot


def _safe_source(sources: dict, name: str, fn):
    """Run one independent observability signal; on any exception, record
    `name` as UNKNOWN in `sources` and return a same-shaped UNKNOWN stub
    instead of propagating -- so one broken signal degrades only itself,
    never the rest of the snapshot. `error_class` only, never the raw
    exception object, per the PH8-04 Step 1 redaction precedent."""
    try:
        result = fn()
        sources[name] = "OK"
        return result
    except Exception as exc:
        sources[name] = "UNKNOWN"
        return {"status": "UNKNOWN", "error_class": type(exc).__name__}


# Default lookback window for the monotonicity signal: 24h. This is a
# dashboard display window, not an alert threshold -- how far back to
# *show* reset activity, not a judgment about when it becomes
# actionable (that threshold is an explicit PH8-04 open question).
DEFAULT_MONOTONICITY_LOOKBACK_SECONDS = 86400


def monotonicity_snapshot(db, *, now: int | None = None,
                           lookback_seconds: int = DEFAULT_MONOTONICITY_LOOKBACK_SECONDS) -> dict:
    """Identifier-free count of usage-cursor regressions (a counter going
    backwards) in the lookback window, read from the durable, immutable
    `mgboost_wl_usage_sample_events` table the PH6-03 collector already
    writes inside its own transaction. Distinct from `usage_freshness()`:
    that answers "is the ledger stale," this answers "did any counter
    regress" -- two different failure modes, never conflated."""
    timestamp = int(time.time()) if now is None else int(now)
    since = timestamp - max(0, int(lookback_seconds))
    conn = db._conn
    reset_count = conn.execute(
        "SELECT COUNT(*) AS n FROM mgboost_wl_usage_sample_events "
        "WHERE reset_detected=1 AND created_at >= ?",
        (since,),
    ).fetchone()["n"]
    affected_cursors = conn.execute(
        "SELECT COUNT(DISTINCT child_intent_id || ':' || node_id) AS n "
        "FROM mgboost_wl_usage_sample_events "
        "WHERE reset_detected=1 AND created_at >= ?",
        (since,),
    ).fetchone()["n"]
    return {
        "lookback_seconds": int(lookback_seconds),
        "since": since,
        "reset_events": int(reset_count),
        "distinct_cursors_affected": int(affected_cursors),
    }


def error_reconcile_snapshot(db, *, now: int | None = None) -> dict:
    """Identifier-free aggregate over the PH4-02 migration-lifecycle
    binding table: how many bindings are stuck in ERROR_RECONCILE right
    now, how old the oldest one is, and how often the reconciler has
    re-observed the same binding as still-stuck (RECONCILE_STALE) --
    read-only, no new storage. No alert threshold is set here: there is
    no technical backoff cadence in `reconcile_binding()` to anchor one
    on, per the PH8-04 plan's threshold-provenance section -- age/backlog
    thresholds are an explicit open question for the owner."""
    timestamp = int(time.time()) if now is None else int(now)
    conn = db._conn
    count_row = conn.execute(
        "SELECT COUNT(*) AS n, MIN(updated_at) AS oldest_updated_at "
        "FROM mgboost_migration_bindings WHERE state='ERROR_RECONCILE'"
    ).fetchone()
    count = int(count_row["n"])
    oldest_updated_at = count_row["oldest_updated_at"]
    oldest_age_seconds = (
        max(0, timestamp - int(oldest_updated_at)) if oldest_updated_at is not None else None
    )
    stale_recurrences = conn.execute(
        "SELECT COUNT(*) AS n FROM mgboost_migration_binding_events "
        "WHERE event_type='RECONCILE_STALE'"
    ).fetchone()["n"]
    return {
        "count_in_state": count,
        "oldest_updated_at": int(oldest_updated_at) if oldest_updated_at is not None else None,
        "oldest_age_seconds": oldest_age_seconds,
        "reconcile_stale_recurrences_total": int(stale_recurrences),
    }


def legacy_transition_review_snapshot(db, *, now: int | None = None) -> dict:
    """Identifier-free aggregate over the P0 `mgboost_legacy_commercial_transitions`
    table: how many transitions are stuck in MANUAL_REVIEW right now, how old
    the oldest one is, and how often a transition has been re-flagged for
    review (`MANUAL_REVIEW_RETRY`) -- same shape and same read-only
    discipline as `error_reconcile_snapshot()`, over the P0 worker's own
    existing durable state/events tables. No new table, no new write. No
    alert threshold is set here: whether a MANUAL_REVIEW backlog age is
    actionable is an explicit open question for the owner, same as
    `error_reconcile_snapshot()`'s.

    This is deliberately *not* the same thing as "worker health" for
    `mgboost-legacy-commercial-transition.service/.timer`: unlike WL
    reconciliation, that worker has no durable cycle-heartbeat table (no
    `mgboost_wl_reconciliation_cycles` equivalent), so "did the worker run
    recently" cannot be answered from existing storage without a new table
    -- out of PH8-04 Step 2 scope, left as a follow-up."""
    timestamp = int(time.time()) if now is None else int(now)
    conn = db._conn
    count_row = conn.execute(
        "SELECT COUNT(*) AS n, MIN(updated_at) AS oldest_updated_at "
        "FROM mgboost_legacy_commercial_transitions WHERE state='MANUAL_REVIEW'"
    ).fetchone()
    count = int(count_row["n"])
    oldest_updated_at = count_row["oldest_updated_at"]
    oldest_age_seconds = (
        max(0, timestamp - int(oldest_updated_at)) if oldest_updated_at is not None else None
    )
    retries = conn.execute(
        "SELECT COUNT(*) AS n FROM mgboost_legacy_commercial_transition_events "
        "WHERE event_type='MANUAL_REVIEW_RETRY'"
    ).fetchone()["n"]
    return {
        "count_in_state": count,
        "oldest_updated_at": int(oldest_updated_at) if oldest_updated_at is not None else None,
        "oldest_age_seconds": oldest_age_seconds,
        "manual_review_retries_total": int(retries),
    }


def health_snapshot(db, *, now: int | None = None) -> dict:
    """Composed operator health snapshot for `GET /admin/ops/health`:
    aggregate counts/timestamps/enums only, no per-account/per-device
    identifiers anywhere -- same privacy discipline as
    `backlog_snapshot()` and `legacy_grace_observability.py`.

    Fail-open: each independent signal is computed through
    `_safe_source()`. If a signal's source is missing/broken, that signal
    alone becomes `{"status": "UNKNOWN", "error_class": ...}` and
    `sources[name]` records `"UNKNOWN"` -- the rest of the snapshot, and
    the HTTP response itself, are unaffected. `status` is `"OK"` only if
    every signal resolved; otherwise `"DEGRADED"`. This endpoint never
    raises past this function on a broken monitoring source."""
    timestamp = int(time.time()) if now is None else int(now)
    sources: dict = {}

    backlog = _safe_source(sources, "wl_reconciliation_backlog",
                            lambda: backlog_snapshot(db, now=timestamp))
    monotonicity = _safe_source(sources, "monotonicity",
                                 lambda: monotonicity_snapshot(db, now=timestamp))
    error_reconcile = _safe_source(sources, "error_reconcile",
                                    lambda: error_reconcile_snapshot(db, now=timestamp))
    legacy_transition_review = _safe_source(
        sources, "legacy_transition_review",
        lambda: legacy_transition_review_snapshot(db, now=timestamp))

    if sources["wl_reconciliation_backlog"] == "OK":
        # Collector lag, outbox age, desired/observed drift, and worker
        # health are already surfaced by the existing PH6-07 read model --
        # reused verbatim, not recomputed.
        collector_freshness = backlog["collector_freshness"]
        outbox = {
            "op_counts": backlog["op_counts"],
            "oldest_backlog_age_seconds": backlog["oldest_backlog_age_seconds"],
        }
        drift = backlog["drift"]
        worker_health = backlog["worker_health"]
        last_reconciliation_cycle = backlog["last_cycle"]
        last_successful_reconciliation_cycle = backlog["last_successful_cycle"]
    else:
        collector_freshness = backlog
        outbox = backlog
        drift = backlog
        worker_health = backlog
        last_reconciliation_cycle = backlog
        last_successful_reconciliation_cycle = backlog

    return {
        "generated_at": timestamp,
        "status": "OK" if all(v == "OK" for v in sources.values()) else "DEGRADED",
        "sources": sources,
        "collector_freshness": collector_freshness,
        "outbox": outbox,
        "drift": drift,
        "worker_health": worker_health,
        "last_reconciliation_cycle": last_reconciliation_cycle,
        "last_successful_reconciliation_cycle": last_successful_reconciliation_cycle,
        "monotonicity": monotonicity,
        "error_reconcile": error_reconcile,
        "legacy_transition_review": legacy_transition_review,
    }
