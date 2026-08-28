# PH6-07 — WL enforcement runtime: scheduler / reconciliation / drift / backlog

Status: implemented and fully tested locally; **local checkpoint only — NO push,
NO deploy, NO production mutation.** Baseline: `4c9d832`.

## What PH6-07 is (and is not)

PH6-07 adds NO second enforcement engine and NO second outbox. It wraps the
production-proven PH6-06 machine (`run_wl_enforcement_cycle`,
`WLEnforcementStore`, existing epoch/op/lease/manifest mechanics) with exactly
three things the on-demand slice deliberately lacked:

1. **Scheduler/worker lifecycle** — `mgboost-wl-enforcement.timer` +
   `mgboost-wl-enforcement.service` (oneshot, same hardened shape as the
   telemetry-cleanup unit). One cycle = one bounded invocation of the
   orchestrator `run_wl_reconciliation_cycle`:
   - non-blocking `flock` cycle lock (`<data-dir>/wl-enforcement-cycle.lock`):
     overlap is REFUSED (`SKIPPED_BUSY`), never queued; concurrent
     timer/manual invocations are safe; a crashed holder releases the lock
     with its process; `TimeoutStartSec=900` bounds execution; clean exit.
   - no secrets in argv/logs: the Marzban credentials come from the service
     environment; journald gets only the aggregate identifier-free JSON.
   - pausing/disabling the timer loses nothing: all pending/repair work lives
     in the durable `mgboost_wl_enforcement_ops` rows and is resumed on the
     next run (timer or manual).
   - **Cadence is a TECHNICAL, unit-configurable default (15 min,
     `OnUnitActiveSec`) — NOT a product SLA and NO maximum-overshoot claim.**
     PH6-09 separately owns guaranteed overshoot/outage policy.
2. **Periodic reconciliation** — every cycle: fresh PH6-01 topology assertion
   (fail-closed on unknown/mismatch/unreachable — the whole cycle is blocked
   before any observation), canonical PH6-04 pool recompute (the cycle now
   calls `resolve_current_parent_wl_pool` — the former inline 3-line
   duplication was removed with identical semantics, proven by the PH6-06
   suite), the existing per-account decision/dispatch/finalize pass, then the
   post-terminal drift scan.
3. **Post-terminal drift detection + safe repair** (`scan_terminal_drift`) —
   an account already ACTIVE/DISABLED with all live-epoch ops APPLIED is
   re-observed each cycle (read-only `legacy.user.get`). The desired direction
   is re-derived from the canonical read model; a terminal account without a
   current entitlement signal (period over) is skipped — repair never invents
   an entitlement. Classification is exact (static PH0-05 allowlist only):

   | drift class | action |
   |---|---|
   | `WL_PRESENT_WHILE_EXCLUDED` (manual WL re-add, or Marzban's persistent `excluded_inbounds` silently including a NEWLY-ADDED approved WL inbound) | REPAIR_QUEUED — fresh same-direction epoch over ONLY the drifted children via `WLEnforcementStore.open_repair_epoch`, converged through `drive_account_ops` (the exact engine path: claim guard, manifest freeze, observe→mutate→verify, bounded retry, exactly-once by observation) |
   | `WL_MISSING_WHILE_INCLUDED` (entitled tag gone) | REPAIR_QUEUED (same path; target proven by the child's own frozen APPLIED manifest; a child INCLUDED from its first epoch may fall back to its own frozen INCLUDED baseline — still allowlist-filtered) |
   | `WL_UNEXPECTED_WHILE_INCLUDED`, `NON_WL_MEMBERSHIP_LOST`, `REMOTE_MISSING`, `UUID_MISMATCH`, `REMOTE_UNREADABLE` | FLAGGED — account lands `ERROR_RECONCILE`, ZERO mutation; remote-missing is never auto-created; any flagged finding suppresses repair for that account this cycle (never a mixed repair-plus-guess) |
   | transient observation failure | not drift; counted, retried next cycle |

   Converged accounts produce zero writes: no new epochs, events or rows —
   the steady state stays silent (`test_converged_disabled_account_...`).

4. **Backlog/observability** — two additive tables (`ph6_07_wl_reconciliation_v1`):
   append-only `mgboost_wl_reconciliation_cycles` (the scheduler heartbeat:
   trigger/outcome/topology/engine summary/drift counters/last error class)
   and `mgboost_wl_reconciliation_drift` (evidence only, one row per REAL
   finding). `backlog_snapshot()` is the operator read model: last/last-ok
   cycle, topology assertion status/version, account state counts, op counts,
   oldest backlog age, drift detected/repaired/flagged, last error class,
   worker health. No UUID/HWID/token/username anywhere.

## Safety invariants (proven by `tests/test_wl_reconciliation.py`, 18 tests)

- **Legacy UNLIMITED / no-signal**: structurally invisible — no decisions, no
  state rows, no ops, no scan actions (the P0 abstain contract held).
- **STANDARD (`wl_mode='NONE'`)**: never in scope (no WL period can exist);
  PH5 anti-leak untouched.
- **LIMITED**: only accounts with a real ACTIVE canonical WL period participate.
- **Crash/restart**: crash after repair-epoch commit before dispatch → durable
  PENDING ops converge next cycle with exactly one mutation; expired lease from
  a dead worker is reclaimed once; repeated cycles after convergence mutate
  nothing; topology fail-closed blocks the WHOLE cycle (engine + scan).
- **Newly-added WL inbound**: with an operator-APPROVED versioned baseline
  update (new exact tag added to `WL_INBOUND_TAGS` + version bump — the PH6-01
  contract; unknown tags are never auto-trusted), the suspended child is
  cleaned on the next cycle, removing ONLY the new tag, non-WL membership
  byte-stable, no fuzzy decisions.

## Deploy plan (when the owner authorizes it)

1. Fresh encrypted backup + restore PASS (`scripts/secure_db_backup.py`).
2. Push the checkpoint; `git merge --ff-only` on production; **install the two
   new units** (`cp mgboost-wl-enforcement.{service,timer} /etc/systemd/system/
   && daemon-reload`) — the only deploy step beyond the usual restart.
3. Restart `mgboost-panel` (additive self-applying migration
   `ph6_07_wl_reconciliation_v1`), then `systemctl enable --now
   mgboost-wl-enforcement.timer`.
4. **Deployment acceptance expectation (current production shape):** legacy
   accounts are UNLIMITED, the STANDARD canary exists, and there are ZERO
   real LIMITED commercial WL accounts (verified read-only 2026-08-28:
   0 ACTIVE `mgboost_wl_periods`, `mgboost_wl_enforcement_states/ops` = 0/0).
   Therefore the expected steady state after deploy is: **timer runs, cycles
   recorded `OK`, remote WL mutations = 0, drift rows = 0** — until the owner
   deliberately creates a LIMITED WL canary.
5. Rollback: `systemctl disable --now mgboost-wl-enforcement.timer` stops the
   runtime with zero durable loss; code rollback is the usual ff-reset.

Ops quick reference:

    systemctl list-timers mgboost-wl-enforcement.timer
    journalctl -u mgboost-wl-enforcement.service -n 50     # aggregate JSON only
    sqlite3 data/db.sqlite3 "SELECT id,outcome,trigger,drift_detected,drift_flagged,last_error_class FROM mgboost_wl_reconciliation_cycles ORDER BY id DESC LIMIT 5;"
    sqlite3 data/db.sqlite3 "SELECT state,COUNT(*) FROM mgboost_wl_enforcement_ops GROUP BY state;"
