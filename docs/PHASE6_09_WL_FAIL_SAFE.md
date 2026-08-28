# PH6-09 — WL overshoot/outage fail-safe: cadence, freshness, outage matrix

Status: implemented and fully tested locally; **local checkpoint only — NO
push, NO deploy, NO production mutation.** Baseline: `d6afae1`. Builds
directly on the production-deployed PH6-07 runtime (0f0795f deploy, 3
verified scheduled cycles).

## What PH6-09 is (and is not)

No second enforcement engine. The chain stays:

    PH6-03 collector -> ledger -> PH6-04 parent pool -> PH6-06 machine
    -> PH6-07 reconciliation/scheduler

PH6-09 adds the runtime policy that makes it safe to switch commercial
LIMITED WL on: a real collector cadence, an explicit freshness contract,
asymmetric fail-safe semantics (access-increase strict / no blind
mass-disable), the DL-059 approved-expansion auto-add, and overshoot/
outage observability.

## Runtime chain (the closed blocker)

Before PH6-09 the PH6-03 collector had NO scheduler (production fact,
2026-08-28: last real collector run 2026-08-26, while enforcement fired
every 15 minutes against that 2-day-old ledger). New units:

    mgboost-wl-usage-collector.timer   # OnBootSec=5min, OnUnitActiveSec=10min
    mgboost-wl-usage-collector.service # hardened oneshot, EnvironmentFile, no secrets in argv

run the EXISTING canonical `run_collection_cycle` (PH6-03's own CAS lease
makes overlap a no-op; no parallel collector was created). The enforcement
timer stays 15 min (technical default).

## Freshness contract

`src/wl_freshness.py::usage_freshness()` — one signal, derived from the
collector lease row (`last_run_completed_at`, `last_run_outcome`):

    fresh = (outcome == 'OK') AND (now - completed_at <= 1800s)

- never-ran / ERROR / PARTIAL are UNKNOWN → **not fresh** (ZERO is never
  inferred from a missing observation);
- 1800 s is a TECHNICAL bound (3x collector cadence), not a product SLA;
- topology: already fresh per enforcement cycle (PH6-01 fail-closed);
- entitlement: already re-derived per cycle and immediately before every
  repair epoch (PH6-07 TOCTOU fix).

## The two governing invariants

1. **Uncertainty cannot increase WL access.** Restore (DISABLED→ACTIVE)
   and DL-059 auto-add require fresh usage + topology + entitlement;
   otherwise refused with 0 mutation and a counter
   (`accounts_skipped_stale_usage` / `access_increase_blocked`).
2. **Uncertainty cannot mass-disable active users.** The ledger is
   monotonic: stale telemetry can only under-count, and an under-count can
   never manufacture `exceeded`. EXCLUDED decisions are deliberately NOT
   freshness-gated; a collector/node outage must never become an outage of
   all WL clients.

## Overshoot model (demonstrated bound, not an SLA)

    observed overshoot = traffic between the last trustworthy usage
                         observation and successful disable convergence
    demonstrated window = 10 min (collector) + 15 min (enforcement)
                          + bounded retry (cap 8, 60 s backoff, per op)
    byte overshoot      = link rate x window   (temporal bound ONLY)

No byte-level guarantee is claimed or implementable. **Headroom: none** —
the exact quota threshold is kept; headroom would reduce purchased GB and
is a product decision deferred to the owner.

## DL-059 — ACTIVE + newly-approved exact WL inbound

Operator-approved versioned PH0-05 update (new exact tag + version bump):

- ACTIVE LIMITED child below quota **gains** the tag automatically on the
  next scheduled cycle (drift class `WL_MISSING_WHILE_INCLUDED`, existing
  repair machinery — no second mutation path);
- DISABLED child **loses** it (unchanged `WL_PRESENT_WHILE_EXCLUDED`);
- scoping is provable, not heuristic: append-only
  `mgboost_wl_topology_versions` records the exact tag set of every
  positively-asserted config_version; a child gains ONLY
  `tags_added_since(<its frozen manifest's topology_version>)`; unknown
  version → nothing; unknown/wl-like tags still block the whole cycle;
- UUID/proxies/expire/data_limit/status never touched; fresh entitlement
  re-checked immediately before the repair epoch; replay cycles write 0.

## Outage matrix

| Outage | Behavior |
|---|---|
| DB unavailable/locked | cycle records bounded `ERROR`, exits; durable op rows resume idempotently next cycle (epoch/lease machinery unchanged) |
| Broker unavailable | desired state stays durable; RETRY with `next_attempt_at` backoff, cap 8 → account `ERROR_RECONCILE`; no retry storm (cadence-bounded) |
| Broker outage after remote success, before ACK | frozen manifest replay settles `ALREADY_IN_SYNC`, exactly-once by observation |
| Marzban unavailable | same as broker: no blind mutation, recover by reread |
| WL node / usage unavailable for one child | that child's observation is UNKNOWN (never 0); other children unaffected; no global disable |
| Collector stale | access-increases freeze (fail closed); already-ACTIVE users stay ACTIVE; access-decreasing actions still need their own fresh proof, which stale data cannot fabricate |
| Topology mismatch/unreachable | whole cycle blocked before any judgment (PH6-01, unchanged) |
| Concurrent scheduler/manual | refused via cycle lock (`SKIPPED_BUSY`), never queued |

## Observability (identifier-free)

`backlog_snapshot()` now exposes `collector_freshness` (fresh, age,
outcome, error class, bound) and `overshoot_bounds` (the cadence-derived
demonstrated window). Each cycle's `summary_json` gains a `ph6_09` block:
usage freshness snapshot, `accounts_skipped_stale_usage`,
`access_increase_blocked`. No UUID/HWID/token/username anywhere.

## Deploy plan (when the owner authorizes)

1. Fresh encrypted backup + restore PASS.
2. Push checkpoint; `git pull --ff-only` on production.
3. Install the two new units (`cp mgboost-wl-usage-collector.{service,timer}
   /etc/systemd/system/ && systemctl daemon-reload`); `systemd-analyze
   verify` (expected clean except the pre-existing unrelated warning).
4. Restart `mgboost-panel` (additive self-applying migration
   `ph6_09_wl_topology_versions_v1`), then
   `systemctl enable --now mgboost-wl-usage-collector.timer`.
5. Acceptance expectation on the current shape (0 LIMITED accounts):
   collector timer fires every ~10 min, `last_run_outcome='OK'`, freshness
   age stays < 1800 s, enforcement cycles stay `OK` with 0 mutations,
   drift rows 0.
6. Rollback: `systemctl disable --now mgboost-wl-usage-collector.timer` +
   code revert — fail-safe direction only (freshness gate freezes
   access-increases; it can never cause a mutation).

Ops quick reference:

    systemctl list-timers mgboost-wl-usage-collector.timer
    journalctl -u mgboost-wl-usage-collector.service -n 20   # aggregate JSON only
    sqlite3 data/db.sqlite3 "SELECT last_run_outcome, datetime(last_run_completed_at,'unixepoch') FROM mgboost_wl_usage_collector_lease;"
    sqlite3 data/db.sqlite3 "SELECT summary_json FROM mgboost_wl_reconciliation_cycles ORDER BY id DESC LIMIT 1;" | python3 -m json.tool | grep -A6 ph6_09

## Deliberately NOT decided here (owner STOP items)

Commercial overshoot budget; headroom size (reduces purchased GB); outage
SLA numbers; maximum stale-telemetry window as a product guarantee. The
1800 s freshness bound is a technical fail-safe default, not any of these.
