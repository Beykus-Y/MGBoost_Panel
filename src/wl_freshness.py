"""PH6-09 -- usage-telemetry freshness contract.

One question, one honest answer: is the PH6-03 ledger's picture of the
fleet's WL traffic fresh enough to act on right now?

`usage_freshness()` reads the collector's own single-row lease (the exact
row `run_collection_cycle` stamps on every release: `last_run_completed_at`
+ `last_run_outcome`) and derives:

    fresh = (last_run_outcome == 'OK')
            AND (now - last_run_completed_at) <= USAGE_FRESHNESS_MAX_AGE_SECONDS

- A collector run that never happened is UNKNOWN -- never fresh.
- A PARTIAL/ERROR run is never trusted either: a partially-collected
  fleet is not a trustworthy whole-fleet observation (ZERO from a missing
  child is UNKNOWN, not zero traffic).
- The default bound is a TECHNICAL value (3x the 10-minute collector
  cadence the PH6-09 systemd units run at), not a product SLA; it bounds
  how long access-increasing WL decisions keep being permitted after the
  last trusted observation.

Policy wiring (the PH6-09 fail-safe):
  - access-INCREASING decisions (DISABLED -> ACTIVE restore, newly-approved
    WL auto-add) require `fresh == True`; otherwise they are refused with
    zero remote mutation (fail closed).
  - access-DECREASING quota decisions are NOT gated: a stale ledger can
    only under-count, and an under-count can never manufacture a fresh
    `exceeded` proof -- so telemetry loss can never mass-disable
    already-active users (the second PH6-09 invariant).
"""

from __future__ import annotations

import sqlite3


# Technical bound, not an SLA: 3x the 10-minute collector timer cadence
# (`mgboost-wl-usage-collector.timer`). Covers normal jitter plus one
# missed run while keeping the demonstrated overshoot window meaningful.
USAGE_FRESHNESS_MAX_AGE_SECONDS = 1800


def usage_freshness(db, *, now: int, max_age_seconds: int = USAGE_FRESHNESS_MAX_AGE_SECONDS) -> dict:
    """Identifier-free freshness snapshot of the PH6-03 usage ledger."""
    conn: sqlite3.Connection = db._conn
    row = conn.execute(
        "SELECT last_run_completed_at, last_run_outcome, last_run_error_class "
        "FROM mgboost_wl_usage_collector_lease WHERE id=1"
    ).fetchone()
    if row is None or row["last_run_completed_at"] is None:
        return {
            "fresh": False,
            "last_ok_run_at": None,
            "last_run_outcome": row["last_run_outcome"] if row else None,
            "last_run_error_class": row["last_run_error_class"] if row else None,
            "age_seconds": None,
            "max_age_seconds": int(max_age_seconds),
        }
    completed_at = int(row["last_run_completed_at"])
    outcome = row["last_run_outcome"]
    age = int(now) - completed_at
    return {
        # A negative age (completed_at in the future -- clock skew, or a
        # corrupted row) is never treated as "freshest possible": clamping
        # it to 0 would make an untrustworthy timestamp look maximally
        # trusted. Fail closed instead -- age is reported as observed.
        "fresh": bool(outcome == "OK" and 0 <= age <= int(max_age_seconds)),
        "last_ok_run_at": completed_at,
        "last_run_outcome": outcome,
        "last_run_error_class": row["last_run_error_class"],
        "age_seconds": age,
        "max_age_seconds": int(max_age_seconds),
    }
