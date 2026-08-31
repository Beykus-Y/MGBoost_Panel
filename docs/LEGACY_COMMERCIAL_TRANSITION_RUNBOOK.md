# Legacy -> commercial transition

Only a primary admin may create a transition from a pending canonical
`MANUAL_RUB` payment. Confirming it freezes the source facts and creates one
audited `LEGACY_COMMERCIAL_ALIGNMENT_GRACE`: legacy terms remain effective
only until `activation_at = ceil_to_utc_hour(max(original_source_expiry,
payment_confirmed_at))`. It is never renewed after a failure.

For a lower device limit, select exact `slot_generation_id` values before the
boundary. The worker never chooses devices. At the boundary it re-reads each
selection and performs `REVOKE -> verify -> FREE`; stale/rebound lineage is
`MANUAL_REVIEW`.

Before the first revoke the worker fences the exact transition lease/revision
and proves that the selected set still equals the current capacity excess.
Every later destructive step repeats that fence. Durable `REVOKE/APPLIED` and
a release performed by the same durable `FREE` operation are recoverable crash
points; a foreign/rebound generation is never accepted as such a replay.

For `WL`, `EXTENDED`, and `FAMILY`, the first observation for each surviving
child/node after activation is a durable `TRANSITION_BASELINE`: the complete
crossing collector interval is forgiven, and only later deltas count. This
is intentionally limited to legacy UNLIMITED -> commercial LIMITED and does
not change ordinary renewal accounting.

Baseline consumption is period-independent: a collector outage across one or
more commercial periods still consumes the baseline on the first relevant
post-boundary observation, then charges later deltas normally. Before LIMITED
apply every surviving current generation must have one authoritative ACTIVE
child lineage; missing or ambiguous lineage fails closed.

Confirmation freezes the exact source subscription id and its post-grace
`row_version`. Plan/expiry/status ABA changes or replacement of the latest
subscription row therefore require `MANUAL_REVIEW` even if visible values were
later restored.

Do not use `ADMIN_GRANT`, direct Marzban expiry changes, raw SQL, or a new
parent account. A failed/manual-review transition needs an explicit audited
operator action; do not create another alignment grace.

## Operator flow

1. Open account → Payments → **Перевести с архивного тарифа**.
2. Select one catalog-pinned 30/60-day commercial SKU and enter the real
   payment method/reference/evidence. Create the pending payment/transition.
3. Verify the frozen preview, then press **Подтвердить реальную оплату**.
   From this point cancellation and generic payment operations are forbidden.
4. If state is `SELECTION_REQUIRED`, select exactly the displayed excess
   generations. Selection records intent only; devices remain live until the
   boundary.
5. Observe the 30-second timer. Expected terminal state is `APPLIED`; any
   ambiguous source/device/remote state becomes `MANUAL_REVIEW` without a
   second grace.

Operational checks:

```sh
systemctl status mgboost-legacy-commercial-transition.timer
journalctl -u mgboost-legacy-commercial-transition.service --since today
```

The worker output contains only bounded counters and exception class names.
Never paste raw UUID, HWID, opaque token or credentials into transition
reasons. For `MANUAL_REVIEW`, preserve all rows and escalate for an explicit
audited resolution; do not edit SQLite or Marzban manually.
