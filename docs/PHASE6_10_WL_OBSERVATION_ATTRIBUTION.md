# PH6-10 — WL usage observation-time attribution

## Owner decision

Marzban supplies a cumulative per-node counter.  Between two successful
polls it does not provide byte timestamps, so if a polling interval crosses
a WL-period boundary, the physical time of each byte inside that interval is
not recoverable.  The ledger must not manufacture a proportional split.

The deterministic operational quota policy is therefore **right-edge /
observation-time attribution**:

```text
delta = cumulative(current observation) - cursor_before
attribution period = WL period ACTIVE at current observation timestamp
```

The full non-negative `delta` is placed into that one period.  For example,
if poll A observes period A and poll B observes period B, then
`counter(B) - counter(A)` belongs wholly to period B.  This is quota
accounting policy; it is not a statement that every byte physically moved in
period B.

## Durable invariants

- Ledger aggregation key remains
  `(child_intent_id, node_id, sample_hour, COALESCE(wl_period_id, 0))`.
  `sample_hour` is diagnostic grouping only, not an attribution rule.
- The immutable event/retry boundary remains
  `(child_intent_id, node_id, cursor_before)`.  A replay of the same cursor
  transition is a no-op and cannot add bytes twice.
- No delta is proportionally split between periods.
- No delta is double-counted.  Parent-pool totals, exhaustion and
  enforcement consume only the period-attributed ledger rows.
- If no period is active at observation time, the established nullable
  `wl_period_id` / `COALESCE(..., 0)` bucket is used.  It is intentionally
  not guessed, backfilled, or assigned to a neighbouring period.

## Product communication boundary

This must not be described to customers as exact real-time or byte-level
billing inside a polling interval.  It is deterministic quota accounting
from successful cumulative observations, with the existing collector
cadence/freshness and enforcement guarantees documented separately in
`PHASE6_09_WL_FAIL_SAFE.md`.

## Regression evidence

`tests/test_wl_usage_ledger.py` proves arbitrary-second pre/post-boundary
polling, full right-edge attribution of a boundary-crossing delta, separate
periods inside one `sample_hour`, same-period aggregation, replay safety and
the NULL bucket.  `tests/test_wl_parent_pool.py`,
`tests/test_wl_enforcement.py` and `tests/test_wl_reconciliation.py` retain
the downstream parent-pool, exhaustion/enforcement and reconciliation
contracts.  `tests/test_wl_usage_ledger_period_bucket_schema.py` covers
fresh, production-shaped and repeated migration execution.
