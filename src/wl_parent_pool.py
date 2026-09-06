"""PH6-04 -- default shared parent WL pool (accounting/read model only).

Scope discipline (roadmap `Rollback`: "derive desired from ledger, no
consumed edit"): this module writes nothing new. It is a pure SUM read
model over the already-durable, already-deduplicated PH6-03 ledger
(`mgboost_wl_usage_samples`), grouped by the exact canonical period
boundaries PH6-02/PH5-02 already own (`mgboost_wl_periods`). No new table,
no new accounting path, no enforcement (PH6-06/09 own that), no purchase-
flow wiring, no config/inbound/Marzban mutation of any kind.

Parent-pool semantics: WL quota belongs to the parent account (`mgboost_
accounts`/`mgboost_wl_periods.account_id`), not to any individual child --
"family" is not a separate entity anywhere in this schema, it is simply an
account with more than one device-slot generation. Every `mgboost_child_
user_intents` row for that account_id that ever recorded a usage sample
inside a given period's id contributes to that period's pool sum,
regardless of the child's *current* `observed_state`: a revoked/rebound
generation's already-ledgered samples for a period it was live during stay
counted forever (`mgboost_wl_usage_samples`/`_sample_events` are fully
immutable/append-only by PH6-03's own schema -- there is nothing to lose).
A genesis/bootstrap placeholder child never issues a real HTTP request
(PH6-03's own attribution note), so it can only ever contribute a real,
non-fabricated zero -- never a fictitious nonzero.

Idempotent/deterministic by construction: summing an immutable, already-
deduplicated ledger is naturally safe to recompute any number of times, from
any process, at any point after a restart, with zero risk of double-
counting a duplicate/racing collector observation -- PH6-03's own
`UNIQUE(child_intent_id, node_id, reset_generation, cursor_before)` event key
(BUG-004 fix: `reset_generation` durably disambiguates a post-reset epoch from
an earlier one that happened to pass through the same raw cursor value) and
monotonic-non-decreasing `bytes_delta` trigger already own that guarantee;
this module never re-derives it.

Period resolution reuses -- never duplicates -- `WLUsageLedgerStore.
resolve_active_wl_period` (the exact same resolver PH6-03's own collector
uses to attribute `wl_period_id` at collection time), via `WLUsageLedgerStore
.sync_wl_period_statuses` first advancing that account's own periods through
the time-only `PLANNED -> ACTIVE -> CLOSED` machine `wl_period_lifecycle_
schema.py` reserved but never built. `sample_hour` remains UTC-hour bucketed
for diagnostics, while the PH6 period-aware ledger key also includes
`wl_period_id`; an arbitrary-second boundary therefore has distinct durable
rows and this SUM remains unambiguous.

Node scope is the exact PH0-05 topology allowlist (`WL_NODE_IDS`), applied
defensively at aggregation time even though PH6-03's own collector already
only ever writes samples for those node ids -- "considering only
authoritative WL usage from PH6-03 by exact topology PH6-01" holds at this
read model's own query, not only by upstream construction.
"""

from __future__ import annotations

import sqlite3
import time

from .wl_topology import WL_NODE_IDS


class WLParentPoolError(RuntimeError):
    pass


class WLPeriodNotFound(WLParentPoolError):
    pass


def compute_parent_wl_pool(connection: sqlite3.Connection, *, account_id: int, wl_period_id: int) -> dict:
    """Pure read: the shared-pool sum for one specific WL period belonging
    to one parent account, across every child (ACTIVE or historical/
    revoked) and every node in the exact WL topology allowlist. Raises if
    the period does not exist or belongs to a different account -- callers
    that don't already know a valid period id should go through
    `resolve_current_parent_wl_pool` instead."""
    period = connection.execute(
        "SELECT id, account_id, subscription_id, sequence_no, starts_at, ends_at, "
        "quota_mode, base_quota_bytes, status FROM mgboost_wl_periods "
        "WHERE id=? AND account_id=?",
        (int(wl_period_id), int(account_id)),
    ).fetchone()
    if period is None:
        raise WLPeriodNotFound(f"WL period {wl_period_id} not found for account {account_id}")

    node_ids = sorted(WL_NODE_IDS)
    placeholders = ",".join("?" for _ in node_ids)
    totals = connection.execute(
        f"SELECT COALESCE(SUM(bytes_delta),0) AS consumed_bytes, "
        f"COUNT(DISTINCT child_intent_id) AS contributing_children "
        f"FROM mgboost_wl_usage_samples "
        f"WHERE account_id=? AND wl_period_id=? AND node_id IN ({placeholders})",
        (int(account_id), int(wl_period_id), *node_ids),
    ).fetchone()

    consumed_bytes = int(totals["consumed_bytes"])
    base_quota_bytes = period["base_quota_bytes"]
    if period["quota_mode"] == "UNLIMITED":
        remaining_bytes = None
        exceeded = False
    else:
        base_quota_bytes = int(base_quota_bytes)
        remaining_bytes = max(0, base_quota_bytes - consumed_bytes)
        exceeded = consumed_bytes >= base_quota_bytes

    return {
        "account_id": int(account_id),
        "wl_period_id": int(wl_period_id),
        "sequence_no": int(period["sequence_no"]),
        "starts_at": int(period["starts_at"]),
        "ends_at": int(period["ends_at"]),
        "status": period["status"],
        "quota_mode": period["quota_mode"],
        "base_quota_bytes": base_quota_bytes,
        "consumed_bytes": consumed_bytes,
        "remaining_bytes": remaining_bytes,
        "exceeded": exceeded,
        "contributing_children": int(totals["contributing_children"]),
    }


def resolve_current_parent_wl_pool(db, *, account_id: int, now: int | None = None) -> dict | None:
    """Read-model entrypoint: advances this account's own WL periods
    through the time-only status machine, resolves whichever period is
    ACTIVE for `account_id` at `now` (the exact same resolver PH6-03's own
    collector uses), and returns its shared-pool sum. Returns `None` -- not
    an error -- when the account is not currently inside any WL period
    window: a Non-WL/UNLIMITED-WL account (zero periods ever scheduled) and
    an account between two periods both look identical here, by design,
    since neither has any quota to report against."""
    ledger = db.wl_usage_ledger
    timestamp = int(time.time()) if now is None else int(now)
    ledger.sync_wl_period_statuses(account_id=int(account_id), now=timestamp)
    wl_period_id = ledger.resolve_active_wl_period(int(account_id), timestamp)
    if wl_period_id is None:
        return None
    return compute_parent_wl_pool(db._conn, account_id=int(account_id), wl_period_id=wl_period_id)
