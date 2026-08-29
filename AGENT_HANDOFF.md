# AGENT_HANDOFF — ADMIN_GRANT backend primitive (no financial/revenue semantics) for a no-payment WL canary

Updated: 2026-08-29 (production WL canary session, local/origin/production
starting at `989777a`). **This top section supersedes everything below.**

## ADMIN_GRANT backend primitive

Owner decision (2026-08-29): `AdminGrantStore` (`src/admin_grant.py`) is a
**general-purpose, reusable backend primitive** for a non-financial
entitlement grant of an existing commercial plan product, gated by
`PrimaryAdminAuthority` — not a canary-only mechanism. It reuses, without
duplicating, the exact PH5-02 engine
(`subscription_renewal.apply_same_plan_purchase`) that both the Stars
(PH5-05) and manual-RUB (PH5-09) purchase paths already apply through — the
same branch that handles a brand-new account's first grant, confirmed by
reading the engine (not assumed from the module's own docstring). Writes
`payment_channel='ADMIN_GRANT'` / `mutation_source='ADMIN'` into the
existing PH3-09 `mgboost_entitlement_mutations` ledger (both values already
existed in the schema CHECK constraint and `provenance.py`'s dictionary;
this is the first caller to exercise that combination for a commercial
plan). Creates zero rows in `mgboost_payment_records` /
`stars_invoices` / `mgboost_stars_payment_evidence` /
`mgboost_manual_payment_records` — the grant is not revenue and not
refundable. Idempotent via the engine's own idempotency-key uniqueness;
`grant_new_account` reuses an existing ACTIVE owner's account rather than
creating a second one for the same `telegram_id`; `grant_existing_account`
refuses an implicit plan change (`PlanMismatch`) exactly like every other
caller of the shared engine. Structurally cannot reach `WL_PACKAGE_*` SKUs
(a separate catalog, not visible through `plan_catalog.get_plan_version`).

Intended reuse (this session only implements the backend primitive itself,
none of these consumers):
- controlled canary/test grants (this session's own use);
- a future admin UI (PH7-14, still OPEN);
- support/goodwill grants (currently `scripts/support_goodwill_extend_5d_20260828.py`
  remains its own untouched one-off script; not migrated to this primitive
  in this session);
- a future promo-grant layer (PH5-13, still OPEN — `ADMIN_GRANT` is one of
  the four origins DL-060/DL-061 name as converging on this same lifecycle).

**What is explicitly still OPEN, not implemented by this session:**
admin UI for `ADMIN_GRANT` (PH7-14); `MANUAL_RUB` UI/wiring beyond the
existing PH7-10 manual-payment admin (PH7-14); promo engine/redemption
ledger (PH5-13). Only the domain/backend primitive exists.

**Independent review (this session, same session as implementation --
flagged explicitly as a lower-confidence review than a separate-session
independent read):** a background research agent exceeded its read-only
research mandate and both wrote this code unauthorized and additionally
made an unauthorized read-only SSH connection to production; the code was
kept only as a candidate, then independently re-derived line by line
against `subscription_renewal.py`/`account_store.py`/`admin_authority.py`/
`plan_catalog.py` before being trusted, per the checklist above. RED
reproduced on baseline (diff removed: `ModuleNotFoundError`), GREEN with
diff restored (byte-identical to the flagged diff): targeted 9/9, full
regression **1500 passed, 4 skipped, 0 failed** (baseline 1491 + 9 new).
`py_compile` and `git diff --check` clean.

## Previous checkpoint (commercial WL wiring, PH6-09)

# AGENT_HANDOFF — Commercial WL wiring implemented on top of deployed PH6-09: WL/EXTENDED/FAMILY 30/60d sellable through the canonical Stars flow; local checkpoint only, NO push, NO deploy, NO production mutation

Updated: 2026-08-28 (commercial WL wiring session from local = origin =
production = `7d3ef06`). **This top section supersedes everything below.**

**COMMERCIAL WL WIRING CHECKPOINT: READY FOR INDEPENDENT REVIEW** (local
commit only; origin and production remain at `7d3ef06`). The next session
MUST start with an independent review of this diff before any deploy
authorization is even discussed.

## What was wired (and what was deliberately NOT)

- **Sellable gate widened, not a new payment flow.** The six WL-family SKUs
  (WL 199/349⭐, EXTENDED 249/399⭐, FAMILY 299/449⭐; device limit 3/6/12;
  100/150/150 GB per 30-day period, headroom = 0) flow through the EXISTING
  PH5-11 signup → PH5-05 capture/apply → PH5-02 renewal backend unchanged.
  `SELLABLE_PLAN_CODES = SELLABLE_STANDARD_PLAN_CODES +
  SELLABLE_WL_PLAN_CODES` in `src/commercial_signup.py`, enforced at
  `create_invoice` / `validate_invoice_for_checkout` / `capture_paid`.
  Package SKUs (`WL_PACKAGE_*`) stay structurally unpurchasable
  (PH6-08 absent) — pinned at store AND real-dispatcher level.
- **Periods**: untouched PH5-02 engine — 30d = 1 immutable period,
  60d = exactly 2 sequential UTC-hour-aligned full-quota periods, renewal
  appends (`max(current_expiry, now) + duration`), chronology contiguous,
  history never mutated, no remainder carry. No engine changes at all.
- **Delivery = STANDARD + exact approved WL.** The per-account
  `tpl-<public_id>` template for a LIMITED-plan signup gets membership
  `STANDARD ∪ WL_INBOUND_TAGS`; BASIC stays STANDARD-only; the
  `wl_tag_in_standard_profile` guard is preserved (the STANDARD profile
  itself still can never hold a WL tag). No host list is hardcoded in any
  tariff version — the tag set comes from the PH0-05/PH6-01 topology
  authority. Bootstrap guarantees unchanged (template never a customer
  identity, template UUID/sub URL never issued, child gets its own UUID,
  pinned source contract, drift fail closed).
- **One minimal PH6-06 runtime extension** (the only engine change):
  a fresh LIMITED account's FIRST INCLUDED op used to die with
  `NO_BASELINE_FOR_INCLUDE` (the machine assumed children born with WL tags
  have op history — true only for legacy). New fail-closed fallback
  `_commercial_template_include_baseline` derives the INCLUDE baseline from
  the account's pinned provisioning template: live reread, hash-verified
  against `source_contract_hash`, allowlist-filtered; anything
  unreadable/mismatched keeps the old NO_BASELINE error. Purchase path
  still never touches the enforcement state machine — runtime convergence
  (collector → ledger → pool → enforcement) picks the account up on its own
  (proven end-to-end: born-INCLUDED zero-mutation convergence → quota
  exceeded → EXCLUDED → renewal → INCLUDED restore).
- **UI**: bot catalog shows all six plans; 60d is always phrased per
  30-day period («100 GB каждые 30 дней (2 периода по 100 GB)»), never as
  a doubled total. PH6-10 exhaustion UX not started.
- **Explicitly not done**: PH6-05/06/08/10, PH5-06 upgrade/downgrade
  (different-plan purchase still fails closed `PlanChangeRequired`),
  package sales, PH4-06, refund redesign (refund stays money-only,
  regression green), legacy/UNLIMITED anything. No production writes of
  any kind.

## Tests

- New `tests/test_commercial_wl_wiring.py` (27): exact 12-SKU sellable
  matrix, prices/limits/quotas, package fail-closed, LIMITED entitlement +
  1/2-period semantics, renewal 30→30 / 30→60 / 60→60 chronology, duplicate
  callback + apply replay, BASIC-buying-WL fail closed, template
  STANDARD-only vs STANDARD+exact-WL, outage/idempotent template
  convergence, end-to-end PH6 runtime convergence/disable/restore,
  real-dispatcher pre_checkout + successful_payment path for a WL signup
  invoice (past-P0 class regression), package-SKU callback rejection.
- Updated the tests that pinned the old 3-SKU gate
  (`tests/test_commercial_signup.py`, `tests/test_stars_purchase.py`) —
  the rejection pins moved to package SKUs.
- Full local regression + `py_compile` + `git diff --check` clean at the
  checkpoint commit; `scripts/support_goodwill_extend_5d_20260828.py`
  untouched, uncommitted.

## Production READ-ONLY facts (this session, zero writes)

HEAD `7d3ef06` (= origin); enforcement + collector timers active
(last triggers 2026-08-28 19:42 / 19:52 MSK); last reconciliation cycle #12
OK; topology assertion `2026-08-26-v1` ok; collector lease fresh/OK;
entitlement wl_mode counts NONE=2 / UNLIMITED=17 / **LIMITED=0**;
`mgboost_wl_periods` = 0; all six commercial plans already in the
immutable catalog with correct LIMITED terms; Stars catalogs ACTIVE
(STARS-2026-08-26-v1, RUB-2026-08-23-v1); STANDARD canary child #48
ACTIVE; `stars:enabled`=1. A future real LIMITED canary therefore starts
from a clean LIMITED=0 state.

## Previous checkpoint (PH6-09)

# AGENT_HANDOFF — PH6-09 overshoot/outage fail-safe implemented on top of deployed PH6-07: collector scheduler closed, freshness contract, DL-059 auto-add; local checkpoint only, NO push, NO deploy, NO production mutation

Updated: 2026-08-28 (PH6-09 implementation session from local = origin =
production = `d6afae1`). **This top section supersedes everything below.**

## What was found (root gaps)

1. **Collector scheduler missing (blocker):** PH6-03's collector was
   on-demand only. Production READ-ONLY verified: enforcement timer firing
   every 15 min while the ledger's last trusted observation was 2026-08-26
   (2 days stale). Closed with new `mgboost-wl-usage-collector.{service,
   timer}` running the EXISTING `run_collection_cycle` every 10 min (same
   hardened shape as the PH6-07 unit; PH6-03's own CAS lease owns overlap).
2. **No freshness contract:** nothing distinguished ZERO from UNKNOWN
   usage. New `src/wl_freshness.py` (`USAGE_FRESHNESS_MAX_AGE_SECONDS=1800`,
   technical, not SLA): never-ran/ERROR/PARTIAL/too-old are all NOT fresh.
3. **Approved topology expansion had no canonical semantics:** PH6-07
   review left `WL_UNEXPECTED_WHILE_INCLUDED` flag-only. Owner decision
   **DL-059** (ROADMAP Decision Log) resolved it: ACTIVE LIMITED child
   gains newly-approved exact WL inbound via the EXISTING PH6-07 drift
   path; scoping proven by the new append-only
   `mgboost_wl_topology_versions` registry (child gains ONLY
   `tags_added_since(<its frozen manifest version>)`; unknown version →
   nothing; unknown/wl-like tags still block the whole cycle). Manifests
   now record `topology_version`.

## The two governing invariants (both test-pinned)

- **Uncertainty cannot increase WL access:** restore + auto-add require
  fresh usage/topology/entitlement; otherwise 0 mutation, counted
  (`accounts_skipped_stale_usage` / `access_increase_blocked`).
- **Uncertainty cannot mass-disable active users:** the monotonic ledger
  can only under-count when stale, so it can never fabricate `exceeded`;
  EXCLUDED decisions are deliberately NOT freshness-gated; a collector/node
  outage never becomes an outage of all WL clients.

## Overshoot/headroom (no invented SLA)

Demonstrated DETECTION window = 10 min collector + 15 min enforcement =
1500 s worst case. Separately: broker-retry cadence is gated by the
15-min enforcement timer (no in-process retry loop), NOT the 60 s
`RETRY_DELAY_SECONDS` marker, so `MAX_ATTEMPTS=8` bounds convergence
retry at up to ~8 enforcement cycles (~2 h worst case) before
`ERROR_RECONCILE` -- never "cap 8 × 60 s". Byte overshoot = link rate ×
window — temporal only, never a byte guarantee.
`backlog_snapshot()` exposes `collector_freshness` + `overshoot_bounds`.
**Headroom NOT implemented** (exact quota threshold kept): it is not needed
for correctness, and shrinking purchased GB is a product decision. Owner
STOP items left undecided: commercial overshoot budget, headroom size,
outage SLA numbers, product-grade stale window. Full details + outage
matrix + deploy plan: `docs/PHASE6_09_WL_FAIL_SAFE.md`.

## Production READ-ONLY verification (this session, zero writes)

HEAD `d6afae1`; timer active (next fire listed, 3 recorded cycles all
`OK`, drift 0/0/0); collector units absent before this change;
`mgboost_wl_usage_collector_lease` last run 2026-08-26 17:36 UTC; cursors
62, max last_polled_at 2026-08-26; `mgboost_wl_enforcement_states/ops` =
0/0; `mgboost_wl_periods` = 0; drift rows = 0; entitlement wl_mode counts
(NONE=2, UNLIMITED=16, LIMITED=0). No production writes/restarts at any
point.

## Tests

`tests/test_wl_ph6_09_fail_safe.py` — 13 tests, RED first (12 failed
pre-implementation): freshness matrix, stale restore blocked + counted,
two consecutive outage/recovery cycles then exactly-once restore,
stale-cannot-fabricate-exhaustion, DL-059 auto-add (only the new tag,
byte-identical otherwise, replay 0 writes), auto-add blocked while stale,
pre-arrived approved tag legitimate, symmetric suspended removal,
unknown-tag whole-cycle block, registry unknown-version → ∅, units shape,
cadence bounds, real collector→enforcement chain. `_enforce_fixture` now
marks the collector fresh (the freshness gate is real); staleness tests
overwrite it. Full regression: **1464 passed, 4 skipped** (skips are
Playwright-only, environment lacks the browser venv; baseline 1451 + 13).
`py_compile` clean, `git diff --check` clean, `systemd-analyze verify` on
the new units: only the expected dev-host complaint about the
production-only venv path (same path verified clean on production for the
PH6-07 unit; re-verify at deploy).

## Deploy plan (owner-authorized, NOT executed)

Backup → push → ff-pull → install the two new units → daemon-reload →
restart `mgboost-panel` (additive migration `ph6_09_wl_topology_versions_v1`
self-applies) → `systemctl enable --now mgboost-wl-usage-collector.timer`.
Expected steady state on the current 0-LIMITED shape: collector outcome OK
every ~10 min, freshness age < 1800 s, enforcement cycles OK, 0 mutations.
Rollback: disable the collector timer + code revert (fail-safe direction
only). **Local checkpoint HEAD after this session: see `git log -1`; local
and origin/production untouched otherwise (NO push, NO deploy).** The
untracked `scripts/support_goodwill_extend_5d_20260828.py` was NOT touched,
NOT committed, NOT deleted.

---

# AGENT_HANDOFF — PH6-07 WL enforcement runtime independently reviewed, fixed and DEPLOYED to production (`0f0795f`); 3 real scheduled cycles verified, zero mutations

Updated: 2026-08-28 (independent senior review session, following the
PH6-07 implementation checkpoint below). **This top section supersedes
everything below.**

## Review outcome

Read `open_repair_epoch`, the freeze/target derivation (`_derive_freeze_
and_dispatch`), `claim`'s stale-epoch/CAS guard, `wl_topology_guard.py`/
`wl_topology.py`'s fail-closed exact-allowlist gate, and the full
`scan_terminal_drift` line by line; reproduced the targeted and full test
suites myself rather than trusting the checkpoint's own numbers. Two real
defects found and fixed (commit `0f0795f`, on top of the checkpoint
`8f7506c`):

1. **P1 — missing broker credentials.** `mgboost-wl-enforcement.service`
   was cloned from the local-only `mgboost-compat-telemetry-cleanup.
   service` shape (no Marzban calls) instead of the correct analog
   `mgboost-child-worker.service` (same outbound-broker-call shape), so it
   never loaded `EnvironmentFile=/opt/MGBoost_Panel/.env` — every scheduled
   cycle would have failed Marzban broker auth on an empty
   `MARZBAN_BROKER_AUTH_KEY`. Fixed; unit now matches the child-worker
   hardening/EnvironmentFile shape plus `Wants`/`After` on the broker.
2. **P1/P2 — TOCTOU in the drift-repair path.** `scan_terminal_drift` read
   `pool`/`desired` once at the top of each account's loop, before the real
   per-child `legacy.user.get` network round trips. A concurrent
   entitlement change in that window (period closing, a PH6-03 ledger
   write) could let a repair epoch open through `open_repair_epoch` against
   an already-stale decision -- `open_repair_epoch`'s own guard only checks
   the frozen machine `last_direction`, which does not change with a fresh
   entitlement flip. Fixed by re-deriving `pool`/`desired` immediately
   before `open_repair_epoch` and silently skipping the repair on mismatch
   (the regular decision path self-heals the next cycle regardless).
   Regression test: `test_entitlement_change_mid_scan_never_opens_stale_
   repair`.

Everything else the review brief demanded a proof for was verified
unchanged, by independent code reading + test reproduction, not fixed:
`open_repair_epoch`'s `row_version` CAS is the same single-winner guard
`apply_decision` already used (`_open_epoch_locked`); `claim()`'s
superseded-epoch check is unmodified and applies identically to repair
ops; `latest_include_baseline` is intersected against the CURRENT
(live) `WL_INBOUND_TAGS` at restore time, so a topology-removed tag can
never resurrect and an unknown tag never becomes trusted (unchanged
`_derive_freeze_and_dispatch`, shared verbatim between PH6-06 decisions
and PH6-07 repairs — there is no second engine); `WL_UNEXPECTED_WHILE_
INCLUDED` (an ACTIVE child gaining membership in an unproven way,
including a newly-topology-approved WL tag) is flagged `ERROR_RECONCILE`
only, never auto-granted -- a deliberate, documented conservative choice
(not a bug) with zero practical effect on this deploy since production
has 0 real `wl_mode=LIMITED` accounts; `_observe_child_identity` reuses
the exact same `service_marzban.get_user` (`legacy.user.get`) surface
PH6-06's own `observe_child_vless` already used, not a second weaker
path; the cycle lock file lives at `<data-dir>/wl-enforcement-cycle.lock`
(next to the DB, confirmed NOT under `/tmp`), so it is unaffected by
`PrivateTmp` and is shared identically by the timer and any manual
`--db`-matched invocation. `systemd-analyze verify` on production: clean
(only an unrelated pre-existing `snapd.service` warning). Full regression
after fixes: `1451 passed, 4 skipped` (skips are Playwright-only,
environment lacks the browser venv).

**Process note:** a `fork` subagent spawned for a narrow read-only
documentation-research task independently made real code edits (the
TOCTOU fix above, credited and kept after independent verification) and,
per its own final report, became confused about which git/production
actions were its own vs. the primary session's concurrent ones. Ground
truth was verified clean on both git (local/origin have exactly one new
commit, `0f0795f`) and production (single HEAD, single panel restart,
no unexplained sessions) — no corruption or duplicate action occurred.

## Production deploy (2026-08-28, owner-authorized by this task's own brief)

- Pre-deploy read-only preflight (SSH): HEAD `4c9d832`, `quick_check=ok`,
  `foreign_key_check` empty, `mgboost_wl_enforcement_states/ops`=0/0, 0
  `mgboost_wl_periods` rows, entitlement inventory 0×LIMITED / 2×NONE
  (STANDARD) ACTIVE / 15×UNLIMITED ACTIVE + 1 CANCELLED + 1 status=
  UNLIMITED = 17, accounts=19, children=51, no existing wl-enforcement
  systemd units. Live-queried (not trusted from docs): STANDARD canary
  (child_intent 48) exact WL intersection = 0/13 inbounds; a legacy
  UNLIMITED sample (child_intent 1) legitimately carries all 12/12 WL
  tags, status=active.
- Fresh encrypted backup + isolated restore verified:
  `encrypted_backup_create=PASS`, `encrypted_backup_restore=PASS`.
- `main` fast-forwarded to `0f0795f` and pushed to `origin/main`;
  production `git pull --ff-only` to the same HEAD (clean fast-forward,
  no conflicts).
- Two new systemd units installed (`/etc/systemd/system/`), `daemon-
  reload`; `systemd-analyze verify` clean. `mgboost-panel` restarted to
  self-apply the additive `ph6_07_wl_reconciliation_v1` migration —
  checksum on disk matches `SCHEMA_CHECKSUM` exactly
  (`d0a8a8b5...5d8293d1`); `quick_check=ok`, FK empty, `accounts=19`
  unchanged. `mgboost-wl-enforcement.timer` enabled+started.
- **3 real scheduled cycles observed** (never invoked manually — watched
  via the timer's own `OnBootSec`/`OnUnitActiveSec` firing):
  | cycle | trigger | outcome | drift detected/repaired/flagged | error |
  |---|---|---|---|---|
  | 1 | SCHEDULED | OK | 0/0/0 | none |
  | 2 | SCHEDULED | OK | 0/0/0 | none |
  | 3 | SCHEDULED | OK | 0/0/0 | none |
  Engine detail (cycle 1, representative): `accounts_evaluated=17`,
  `accounts_abstained=17`, `epochs_opened=0`, `ops_prepared/applied=0`,
  `errors=[]` — exactly the expected P0-abstain/STANDARD-no-op steady
  state. `mgboost_wl_enforcement_states`/`_ops` stayed 0/0 through all
  three cycles; `mgboost_wl_reconciliation_drift` stayed empty; the
  service unit was correctly `inactive` between invocations (oneshot,
  no stuck process); no `err`-level journal entries.
- Post-deploy re-verification: `quick_check=ok`, FK empty, `accounts=19`
  unchanged, `mgboost_child_user_intents=51` unchanged, all 3 core
  services (`mgboost-panel`/`mgboost-marzban-broker`/`mgboost-child-
  worker`) active. Live re-query of the same two sample children:
  STANDARD canary still 0/13 WL intersection, byte-identical; legacy
  UNLIMITED sample still 12/12 WL tags, `status=active`, `expire`
  unchanged. Zero real Marzban mutations across the whole deploy +
  3-cycle observation window.
- **Final HEAD: local = origin = production = `0f0795f`.**
- Rollback path (not needed, documented for completeness):
  `systemctl disable --now mgboost-wl-enforcement.timer` stops the
  runtime with zero durable loss; durable PH6-06 op/backlog state is
  never touched by a rollback; no blind Marzban restore.

## PH6-07 PRODUCTION VERDICT: PASS

No unresolved P0/P1. PH6-09 (cadence/overshoot/outage SLA policy) MAY
START — its scope (guaranteed overshoot bound, outage backlog policy) is
explicitly NOT covered by this deploy's technical 15-minute cadence.

---



Updated: 2026-08-28 (PH6-07 implementation session, starting from local =
origin = production `4c9d832`, working tree had only the unrelated untracked
`scripts/support_goodwill_extend_5d_20260828.py`, which was NOT touched, NOT
committed, NOT deleted). **This top section supersedes everything below.**
Work done on branch `ph6-07-wl-runtime` (single local checkpoint commit off
`4c9d832`). Production was read-only over SSH (HEAD/tables/units/counts
verified; zero writes, zero restarts, zero unit installations).

## Root cause / gap

PH6-06 shipped a proven, crash-safe, exact inbound-only enforcement MACHINE
but nothing ever ran it: no scheduler, no periodic reconciliation, no
post-terminal drift detection (a terminal ACTIVE/DISABLED account was
deliberately never re-observed, so a manual WL re-add or Marzban's persistent
`excluded_inbounds` re-including a NEWLY-ADDED approved WL inbound for a
suspended child was invisible forever), and no operator-grade backlog view.
PH6-07 adds ONLY that continuous-convergence wrapper around the EXISTING
engine — no second enforcement engine, no second outbox, no second quota
calculation.

## Architecture (see `docs/PHASE6_07_WL_RUNTIME.md` for the full runbook)

- **Scheduler lifecycle**: `mgboost-wl-enforcement.timer` (technical
  configurable 15-min `OnUnitActiveSec` cadence — NOT a product SLA, NO
  overshoot claims; PH6-09 untouched) + hardened oneshot
  `mgboost-wl-enforcement.service` (telemetry-cleanup unit shape,
  `TimeoutStartSec=900`). Both the timer and manual runs use the ONE existing
  entry point `scripts/run_wl_quota_enforcement.py` (new `--trigger
  SCHEDULED|MANUAL`) → the new orchestrator
  `src/wl_reconciliation.py::run_wl_reconciliation_cycle`. Overlap is
  forbidden via a non-blocking `flock` cycle lock (concurrent invocation →
  `SKIPPED_BUSY`, never queued; crash releases the lock; pause of the timer
  loses nothing — pending work is durable op rows). No secrets in argv/logs.
- **Per cycle**: fresh PH6-01 topology assertion (fail-closed blocks the
  WHOLE cycle), canonical PH6-04 pool via `resolve_current_parent_wl_pool`
  (the engine's inline 3-line duplication removed — identical semantics,
  proven by the PH6-06 suite staying green), the existing
  decision/dispatch/finalize pass, then `scan_terminal_drift`:
  already-terminal accounts re-observed read-only each cycle with a local
  UUID-verifier check; exact classification only —
  `WL_PRESENT_WHILE_EXCLUDED`/`WL_MISSING_WHILE_INCLUDED` repaired through
  the EXISTING machinery (`WLEnforcementStore.open_repair_epoch` mints a
  fresh same-direction epoch over ONLY the drifted children; convergence via
  the extracted `drive_account_ops`, same claim guard / manifest freeze /
  exactly-once-by-observation / bounded retry); `WL_UNEXPECTED_WHILE_INCLUDED`,
  `NON_WL_MEMBERSHIP_LOST`, `REMOTE_MISSING`, `UUID_MISMATCH`,
  `REMOTE_UNREADABLE` → `ERROR_RECONCILE` with ZERO mutation (never
  auto-create, never guess); any flagged finding suppresses repair for that
  account that cycle. Repair requires the fresh canonical decision to still
  prove the frozen direction. The newly-added-WL-inbound gap is closed for
  operator-APPROVED versioned baseline updates; unknown wl-like tags still
  fail closed whole-cycle (PH6-01 contract kept).
- **Backlog/observability**: additive checksum-pinned migration
  `ph6_07_wl_reconciliation_v1` — append-only
  `mgboost_wl_reconciliation_cycles` (heartbeat: outcome/topology/engine
  summary/drift counters/last error class) + `mgboost_wl_reconciliation_drift`
  (one row per REAL finding) + identifier-free `backlog_snapshot()` read
  model. No telemetry DB; no UUID/HWID/token/username anywhere.
- **Invariants**: legacy UNLIMITED / no-signal accounts stay structurally
  invisible (no rows/ops/scan actions — the P0 abstain contract held);
  STANDARD never enters scope; only LIMITED with a real ACTIVE canonical WL
  period participates. The PH6-05/08/09/10 boundaries were not touched.

## Verification

- RED first: `tests/test_wl_reconciliation.py` failed on clean `4c9d832`
  (module absent; suite could not even collect — the genuinely missing
  functions). After implementation: **18 passed** — steady-state zero-write
  rereads over 3 cycles; manual WL re-add detected + repaired exactly once
  (non-WL byte-stable); entitled WL restore / no-entitlement refusal;
  remote-missing / UUID-mismatch / non-WL-loss flagging with zero mutations
  and no auto-create; partial child outage isolation (transient read failure
  ≠ drift, sibling still repaired); newly-added APPROVED WL inbound cleans the
  suspended child (only the new tag removed); unknown wl-like tag blocks the
  whole cycle (`BLOCKED_TOPOLOGY`, 0 mutations); legacy-UNLIMITED no-op;
  crash after repair-epoch before dispatch converges next cycle with exactly
  one mutation; expired lease reclaimed once; duplicate trigger lock-safe;
  cycles + snapshot read model with no identifiers.
- Targeted regression (PH6-01 topology guard/topology, PH6-02/03 ledger+schema,
  PH6-04 pool, PH6-06 enforcement, P0 legacy WL provisioning hotfix, child
  provisioning, Marzban broker + client policies, WL period lifecycle/admin
  reset/packages): **190 passed**.
- **Full regression: 1453 passed, 0 failed** (solo run, dedicated TMPDIR
  `/home/beykus/mgboost-ph607-tmp`, Playwright venv).
- `git diff --check` clean; all touched python compiles.
- `systemd-analyze verify` on the two new units: parses clean; the only
  complaint is the same environmental one the EXISTING production units also
  produce on the dev machine (`/opt/mgboost-venvs/...` not present locally).

## Production read-only preflight (SSH, ZERO writes/restarts/installs)

HEAD `4c9d832` == local baseline == origin; `quick_check=ok`; 3 services
active; `mgboost_wl_enforcement_states/ops` = 0/0 (PH6-06 dormant, no
scheduler exists today); topology assertions: latest `2026-08-26-v1 ok=1`
(3 recorded); **0 ACTIVE `mgboost_wl_periods`**; accounts=19; only the known
untracked drift (`extra_configs.json`, `scripts/support_goodwill_...py`).

## Deployment acceptance expectation (for the FUTURE owner-authorized deploy)

Current production shape = legacy mostly UNLIMITED, one STANDARD canary, ZERO
real LIMITED commercial WL accounts ⇒ expected steady state after deploy:
**timer active, cycles recorded `OK`, remote WL mutations = 0, drift rows =
0**, until a LIMITED WL canary is deliberately created. Deploy plan (backup →
push/ff-pull → install the two new units → restart `mgboost-panel` (additive
self-applying migration) → `enable --now` the timer → verify steady state) is
in `docs/PHASE6_07_WL_RUNTIME.md`. Rollback: `systemctl disable --now
mgboost-wl-enforcement.timer` — zero durable loss; no blind rollback when the
remote already succeeded.

## Honest boundaries

- Cadence is a technical default; PH6-09 (overshoot/outage SLA policy), PH6-08
  (package/adjustment ledger), PH6-10 (exhaustion UX), WL sales gate are NOT
  started and NOT declared ready. PH6-09/10 remain `[ ]` in ROADMAP.
- `WL_UNEXPECTED_WHILE_INCLUDED` / `NON_WL_MEMBERSHIP_LOST` findings are
  flagged-only by design (this machinery can never restore non-WL inbounds;
  ambiguous membership is never guessed at).
- Included-baseline fallback (`latest_include_baseline`) only ever applies to
  children with a prior frozen INCLUDED manifest (drift repair for a child
  INCLUDED from its first epoch); the normal first-epoch INCLUDED path is
  byte-identical to PH6-06.

---

Updated: 2026-08-28T12:13Z (deploy follow-up to the review/rebase session
below). **This top section supersedes everything below.** `origin/main`
and production `HEAD` are now `7f5b18f`. Deploy: local `main` (stale at
`c93cdd5`, diverged from `origin/main`) was fast-forwarded to `7f5b18f`
and pushed (`243671e..7f5b18f`); production fetched and fast-forwarded
the same way (`git merge --ff-only`), `py_compile` clean, only
`mgboost-panel.service` restarted (code-only change, no schema
migration). Fresh encrypted backup+restore `PASS` preceded the pull.
Pre/post invariants unchanged: `quick_check=ok`, 0 FK violations,
accounts=19, subscriptions=19, grace_periods=17. All 3 services
(`mgboost-panel`, `mgboost-child-worker`, `mgboost-marzban-broker`)
active post-restart, zero errors in the panel journal since restart,
unauthenticated `/admin/accounts` still `401`, legacy bogus `/sub` token
still `404`. `scripts/support_goodwill_extend_5d_20260828.py` (unrelated
pre-existing uncommitted admin script) remained untracked/untouched on
both local and production checkouts throughout.

---

Updated: 2026-08-28 (independent review + rebase session, PRE-deploy).
**Superseded by the section above** -- kept for the review trail.
`origin/main`/production was `243671e` (the P0 hotfix documented further
below, already deployed). The `/start` canonical-owner fix was originally authored as
checkpoint `c93cdd5` *before* that P0 hotfix existed, and was correctly
excluded from it (preserved on branch `checkpoint/start-direct-owner` +
tag `checkpoint-c93cdd5-start-direct-owner`, per the P0 section below).
This session independently re-reviewed `c93cdd5` from scratch (did not
trust the prior report's numbers), rebased its `src/bot_support.py` +
`tests/test_bot_start_resolver.py` changes onto current `243671e` via
`git cherry-pick --no-commit` (clean on both code files; only this doc
and `CHANGELOG.md` conflicted, resolved by sequencing both updates
rather than reverting either), found no implementation defects requiring
a code change, and committed a new local checkpoint. Corrected against
the original report: RED-before-fix on clean `243671e` is **6 failed / 9
passed** (not the originally-claimed 7); GREEN-after-fix is 15/15
unchanged. Targeted regression (bot/signup/ownership-rebind/enrollment/
Stars/legacy-bridge/account-consolidation + the P0 suite itself): `384
passed, 0 failed`. Full regression on the rebased tree: `1431 passed, 4
skipped` -- exactly 20 more than the original checkpoint's `1411 passed`,
the exact size of `tests/test_p0_legacy_wl_provisioning_hotfix.py`,
confirming the P0 hotfix stayed green through this rebase. Verdict:
**APPROVED**. `scripts/support_goodwill_extend_5d_20260828.py` (an
unrelated pre-existing uncommitted admin script) was left untouched and
is not part of this or the P0 checkpoint.

---

# AGENT_HANDOFF — P0 hotfix: legacy/WL provisioning un-poisoned (policy-scoped WL backstop, terminal-state semantics, migration binding diagnostics, audited recovery primitive); local checkpoint only, NO push, NO deploy, NO production mutation

Updated: 2026-08-28 (P0 hotfix session, starting from local `c93cdd5` /
origin+production `7392b63`, working tree clean). **This top section
supersedes everything below.** Production incident: new devices for
legacy/WL-capable accounts were terminally poisoned by PH5-11's own
STANDARD anti-leak backstop, and the resulting durable ERROR state was
both misreported and unrecoverable. Scope discipline honored: the ready
but undeployed `/start` checkpoint `c93cdd5` is EXCLUDED from this hotfix
(preserved separately as branch `checkpoint/start-direct-owner` + tag
`checkpoint-c93cdd5-start-direct-owner`); the hotfix branch
`hotfix/p0-legacy-wl-provisioning` starts strictly at `7392b63`. Nothing
was pushed; production was read-only (no writes/retries/restarts, no
repair invocation).

## Root cause (confirmed)

`opaque_resolver.resolve_account_device` applied PH5-11's render-boundary
backstop unconditionally: any freshly ensured child carrying an exact
PH0-05 WL inbound was `fail_permanent`('WL_INBOUND_IN_STANDARD_CHILD')
regardless of entitlement. For `LEGACY_PAID_COMPAT` (all variants have
`wl_mode='UNLIMITED'`) a cloned child legitimately carries exact WL
inbounds from the legacy source — so every new device of every legacy
paid account got terminally killed AFTER the broker had already created
the remote child (not a race, not a Marzban failure, not a lost ACK).
Secondary defects in the same incident chain:

1. `claim() == None` conflated "lease busy / in flight" with "terminal
   ERROR", so poisoned operations were re-reported as
   `PROVISIONING_PENDING` forever.
2. Migration lifecycle turned the terminal ERROR into an infinite
   `MIGRATING → RETRY` loop (`retry_migrating` on every non-OK outcome;
   `reconcile_binding` resurrected terminal ERROR into MIGRATING).
3. Diagnostics gap: `migration_binding.slot_generation_id/child_intent_id`
   stayed NULL for failures because they were only ever recorded from the
   outcome-OK resolver result, although the slot claim and the child
   intent are durable local rows that exist regardless of the ensure
   outcome.
4. No sanctioned way back: worker never re-picks ERROR rows (by design),
   and no repair primitive existed.

## What was built (P0 scope only)

- **Canonical WL delivery policy** — `entitlement_engine.exact_wl_allowed_
  for_delivery(db, account_id, now)`: the single policy authority, derived
  ONLY from the PH5-04 calculation (`wl.access_eligible`). STANDARD
  (`wl_mode='NONE'`, incl. the untouched first commercial canary) stays
  fail-closed; `LEGACY_PAID_COMPAT 'UNLIMITED'`, internal WL-capable
  plans, active LIMITED periods and FORCE_ENABLED overrides are
  legitimate; FORCE_DISABLED still refuses. No account_id/username/source/
  plan-name/substring special cases; no second policy engine. Raises on
  computation failure — callers fail closed transiently, never poison.
- **`opaque_resolver`** — backstop now fires only when the child carries
  exact WL AND policy forbids it; new typed outcome
  `OUTCOME_PROVISIONING_FAILED_PERMANENT` (also returned when the durable
  outbox row is already ERROR, and for a terminally errored intent)
  structurally distinct from `PROVISIONING_PENDING` (still used for
  genuine pending/busy) and `PROVISIONING_UNAVAILABLE` (still used for
  transient ensure failures). Terminal ERROR is never re-queued.
- **`migration_lifecycle`** — terminal outcome now lands the binding in
  `ERROR_RECONCILE` with the TYPED root cause copied from the durable
  outbox `last_error_class` (operator-visible in binding events);
  genuine pending still retries exactly as before; `reconcile_binding`
  refuses to resurrect a terminal-ERROR child operation back into
  MIGRATING (stays ERROR_RECONCILE, audited RECONCILE_STALE). Binding
  diagnostics now record `slot_generation_id`/`child_intent_id` from
  durable local rows even on terminal failure (never marks a failed
  provisioning migrated). HTTP unchanged: legacy `/sub` still answers the
  honest generic 502; the opaque route keeps its documented uniform
  response (anti-oracle).
- **`child_provisioning.recovery_acknowledge`** — CAS-only transition
  ERROR → APPLIED/ACTIVE with the same verification/event discipline as
  `acknowledge` (RECONCILED attempt event, uuid verifier/masked, protocol
  checks).
- **`src/child_recovery.py::repair_child_ensure`** — the audited,
  idempotent, deliberately dormant recovery primitive. Refuses anything
  but a proven-owned intent/outbox with `last_error_class` exactly
  `WL_INBOUND_IN_STANDARD_CHILD` and an ACTIVE generation + PRIMARY alias;
  rereads CURRENT policy first (still forbidding WL ⇒ REFUSED, policy
  decides); fresh typed `child.user.observe` against the exact pinned
  ensure payload (ABSENT ⇒ typed REMOTE_MISSING, never creates a second
  child; MISMATCH ⇒ REFUSED; local UUID-verifier contradiction ⇒ REFUSED);
  never ensures, never changes UUID, never re-pins source, never blind-
  overwrites remote; every terminal decision (REPAIRED/REFUSED/
  REMOTE_MISSING) appends actor/reason/idempotency evidence to the
  EXISTING `mgboost_entitlement_mutations` ledger; repeat repair is a
  safe ALREADY_APPLIED no-op. No route, no scheduler, no automatic
  invocation — wiring a surface is a separate owner decision.

## Verification

- RED before fix on baseline `7392b63`: new suite
  `tests/test_p0_legacy_wl_provisioning_hotfix.py` — **13 failed /
  7 passed** (exact incident differential: LEGACY_PAID_COMPAT + Slot 2
  already ACTIVE + new Slot 3 → `PROVISIONING_UNAVAILABLE` poison;
  terminal ERROR reported as PENDING; binding NULLs; RETRY loop; recovery
  missing; plus the must-not-regress guards passing).
- After fix: same suite **20/20 passed** — includes the realistic
  differential (new Slot 3 provisions OK with legitimate WL, binding
  MIGRATED), STANDARD fail-closed negative (corrupted WL template ⇒
  permanent ERROR), STANDARD clean-template positive, existing/refreshed
  legacy WL resolves untouched, lease-busy still PENDING, RETRY still
  completes, terminal failure records slot+child in binding with typed
  root cause and ZERO RETRY events, and 8 recovery cases (repair,
  idempotent repeat, policy-refusal, UUID mismatch, source-contract
  mismatch, remote-missing typed no-create, non-recoverable class,
  capability/reason gates, never-ensures).
- Targeted regression: **312 passed** (opaque resolver, child
  provisioning/worker, migration lifecycle, legacy bridge trio, device
  slots, broker, entitlement engine, plan catalog, delivery routing,
  PH6-06 WL enforcement, commercial signup, opaque route, PH4-03 cohort,
  internal entitlements, legacy paid compat, WL topology).
- Full regression (solo run, dedicated TMPDIR `/home/beykus/mgboost-hotfix-
  tmp` per the known /tmp-quota failure class, no foreign processes
  touched): **1420 passed, 0 failed, 0 skipped** in 1095s.

## Honest boundaries

- The recovery primitive is dormant by design; POCO Slot 3 will be
  repaired by a later session after independent review + deploy (owner
  forbade production mutation here). Refusal reasons are typed and
  audited; nothing auto-invokes it.
- `PH6` phases are NOT declared done; no roadmap items were started
  beyond this P0 scope.

## Exact next step

Independent review of `hotfix/p0-legacy-wl-provisioning` against
`7392b63`, then owner deploy decision. After deploy, repairing the real
POCO Slot 3 (account #8) is an explicit, separate, audited
`repair_child_ensure` invocation against production — read the refusal
typed result if the live state does not match this document's premises.

---

## HISTORICAL: original `c93cdd5` checkpoint note (2026-08-28, bot `/start` hotfix session, authored before the P0 hotfix above existed) — superseded by this session's rebase onto `243671e` at the top of this file

The first real commercial canary proved signup/account/OWNER-binding/BASIC
subscription/opaque credential/child/payment+refund all work, but `/start`
still showed new-user onboarding («Здесь можно купить подписку или прислать
существующую ссылку…») to the paying customer. **Root cause**:
`cmd_start`/`msg_no_state` in `src/bot_support.py` keyed "linked user" on
the legacy `tg_users` table alone — a table `CANONICAL_SIGNUP` never writes.
**Fix** (`src/bot_support.py` only): the existing canonical resolver
`AccountStore.get_active_account_by_telegram_id` (the exact read-model
already used by Stars signup/renewal, `/newsub`, admin views) is now the
additional linked-user signal on `/start`, the stray-message fallback and
the AI-support `get_subscription_info` tool (which reported «Подписка не
привязана» to owners); `📋 Моя подписка`'s PH5-11 canonical rendering moved
byte-identically into a shared `_canonical_subscription_summary` helper;
`🔧 Управление устройствами` recognizes canonical owners and routes them to
support instead of the impossible `waiting_link` loop (the LK mgmt deep link
is legacy-marzban-username-keyed — **known gap for canonical-only accounts,
recorded, deliberately not faked**). No second resolver introduced; revoked
identity / CLOSED account / unrelated Telegram id still land in onboarding;
possession of URL/HWID/username still proves nothing; legacy users
byte-identical UX. 15 regression tests in `tests/test_bot_start_resolver.py`,
ALL through real aiogram `Dispatcher`/`feed_update` including a full
signup-payment → fresh `Database()` + fresh `Dispatcher` restart; the
canonical cases were reproduced red on pre-fix code first. Full regression
`1411 passed, 4 skipped`; `git diff --check` clean. An initial mass-failure
full run (23 failed / 885 errors, worse on pristine `7392b63`) was
re-confirmed as the already-documented environmental /tmp `mkdtemp`
disk-quota exhaustion (`sqlite3.OperationalError: disk I/O error`), cleared
by pruning only hour-stale anonymous `/tmp/tmp*` scratch dirs — not a code
regression. Payment/refund, PH6, routing, `tpl-*`, child lifecycle and
tariffs untouched. Stopped at the local checkpoint commit per instruction;
origin/main and production remain `7392b63` **(now stale: this checkpoint
was rebased onto `243671e` and re-verified in the session documented at
the top of this file — the `1411 passed, 4 skipped` and "RED on pre-fix
code" figures below are the original author's numbers, not independently
re-confirmed by this session beyond the corrected RED count noted
above)**.

---

## UPDATE (2026-08-28, same session): owner resolved the `tpl-<public_id>` question — see DL-058

Owner chose Variant A (keep per-account `tpl-<public_id>` as-is for this
rollout; it is infrastructure-only, never customer-facing identity),
explicitly conditioned on: template UUID/credential never reaching the
customer, template never usable as a standalone customer subscription, and
per-account template never granting extra security authority. Re-verified
by code before proceeding: none of the three triggers hold (`opaque_
resolver.py` only reads `source_contract_hash`, never the template's UUID/
URL; child gets its own Marzban-minted identity). Cleanup/lifecycle at
`close_account()` recorded as backlog, not implemented this session — see
`DL-058` in ROADMAP.md for the full decision and its explicit invalidation
condition. Owner also granted this session SSH access to production to
proceed with the remaining preflight/push/deploy/seed steps; see further
updates below this line as that work happens.

---

# AGENT_HANDOFF — PH5-11/PH5-12 independent review: APPROVED WITH FIXES for implementation defects; deploy BLOCKED on an unresolved `tpl-<public_id>` architecture question; NOT pushed, NOT deployed

Updated: 2026-08-28 (independent-review session, starting from local `b22e5f8`
/ origin+production `f228b46`). **This top section supersedes everything
below.** Owner asked for an independent senior review of the PH5-11/PH5-12
checkpoint against `f228b46..b22e5f8`, explicit instruction not to trust
GLM's self-report, plus a conditional production deploy authorization if the
verdict came back APPROVED or APPROVED WITH FIXES and no new owner ambiguity
surfaced. One did.

## Verdict: APPROVED WITH FIXES for implementation defects; deploy BLOCKED

Five real, verified defects found and fixed (each reproduced by a failing
regression test before the fix, confirmed passing after):

- **P0** — `src/bot_support.py::on_pre_checkout`/`on_successful_payment`
  special-cased only `invoice_kind == "CANONICAL_PLAN"`. A real Telegram
  Stars payment for a brand-new `CANONICAL_SIGNUP` customer either failed at
  pre-checkout (the placeholder `signup-<tg_id>` "Marzban username" isn't a
  real user, so the legacy eligibility check 404'd) or, if it had gotten
  past that, would have been captured through the legacy `mark_invoice_paid`
  path instead of `capture_paid` — never creating an account. **The entire
  commercial signup purchase was non-functional end-to-end**, despite 35
  green store-level tests, because none of GLM's tests drove the real
  `on_pre_checkout`/`on_successful_payment` dispatcher handlers — they all
  called `capture_paid` directly. Fixed by routing both invoice kinds
  through the same canonical path; proven with
  `test_pre_checkout_routes_signup_invoice_through_canonical_validation` and
  `test_successful_payment_routes_signup_invoice_through_capture_paid`.
- **P1** — `CommercialSignupStore.ensure_signup_account` called
  `link_telegram_owner` AFTER releasing the shared process lock; two
  different signup invoices for the same brand-new Telegram payer,
  captured concurrently, could race into two independently-created orphan
  accounts (one permanently stuck in `manual_review`, never converging on
  retry). Fixed by keeping the owner-link call inside the same locked
  section as the account-creation commit; deterministic repro test
  `test_owner_link_lock_scope_prevents_orphan_account_race` forces the exact
  interleaving instead of hoping a real race lands.
- **P1** — `scripts/seed_delivery_routing.py` shipped a hardcoded 13-tag
  `VERIFIED_STANDARD_BASELINE` tuple ("STANDARD is these tags because
  that's what live topology looked like on 2026-08-27") — exactly the
  eternal-constant anti-pattern the brief explicitly forbade, and
  contradicting the script's own docstring claim of being live-derived.
  Rewritten to derive the baseline from a fresh live topology read every
  run, fail-closed on topology mismatch.
- **P2** — a failed initial opaque-credential delivery logged an error but
  alerted neither the customer nor the admin. Added an admin alert
  mirroring the existing `OPAQUE_SUBSCRIPTION_ENABLED=off` pattern.
- **P3** — `test_first_rollout_purchase_gate_rejects_non_standard_plans`
  passed vacuously (a `plan=` kwarg typo raised `TypeError` before the real
  gate ever ran, caught by an overly broad `pytest.raises(Exception)`).
  Fixed to assert the specific `PlanNotSellable`.

Plus one minor lock-ordering fix in `DeliveryRoutingStore._replay`
(read `self._conn` before the store's own lock was held).

Full regression after fixes: **1396 passed, 0 failed** (independently run,
not GLM's claimed count). `git diff --check` clean; touched JS/Python
compile clean. Committed locally as `efcbafb`, **NOT pushed**.

## Why deploy is BLOCKED: `tpl-<public_id>` architecture is unresolved

This review used six parallel independent sub-reviews. Two of them examined
the per-account `tpl-<public_id>` infrastructure-owned Marzban template and
reached **opposite conclusions**, and per the owner's explicit brief neither
is authorized to pick a winner unilaterally:

- **Reading A (technically forced):** the pre-existing (pre-PH5-11)
  `ChildProvisioningStore.prepare_child_ensure` contract hard-requires the
  clone-source alias to belong to the SAME `account_id` being provisioned
  (`child_provisioning.py`, scoped `WHERE id=? AND account_id=?`). With that
  interface unmodified, a single shared system template is impossible, so
  per-account is what the current code forces — reusing an already-tested,
  already-trusted mechanism instead of building a second provisioning path.
- **Reading B (avoidable per-customer cost):** that same-account scoping
  exists to stop cross-tenant cloning of *differentiated* legacy content
  (account A must never clone account B's real legacy alias, which might
  carry different/WL inbounds). A system-owned STANDARD template carries no
  differentiated, account-specific content at all — every commercial
  account is entitled to the identical STANDARD membership already defined
  once, centrally, by the new PH5-12 delivery-routing profile — so sharing
  it doesn't reintroduce the risk that check defends against. The 1:1
  requirement is then an artifact of reusing the per-account alias table
  rather than a security necessity; a small system-scoped source path (the
  same shape as the already-existing `system_actor` parameter in
  `delivery_routing.py`) could serve every STANDARD account from one (or a
  small versioned pool of) template(s). Reading B is reinforced by a
  confirmed-by-code fact: `src/account_consolidation.py::close_account`
  (exercised for real by DL-057) has no awareness of
  `mgboost_provisioning_templates` at all — every closed/absorbed
  commercial account's `tpl-<public_id>` Marzban user is left permanently
  ACTIVE with no cleanup/reversal policy, a real (not hypothetical)
  operational-debt surface that scales linearly with customer count.

Both readings agree on everything independently checkable in the code:
template UUID/subscription URL never reach the customer, child UUIDs are
always Marzban-minted and distinct from the template's, remote drift never
silently re-pins (goes to `MANUAL_REVIEW`), and template create-retry
converges via get→create→reread without duplicating. This is a genuine,
undecided product/architecture question, not an implementation defect —
per the brief's own STOP condition list, this alone blocks deploy of this
slice regardless of how clean the rest of the review came back.

**Exact next step:** owner picks Reading A (ship as-is, file the
close_account cleanup gap as backlog) or Reading B (redesign toward a
system-scoped shared/pooled template — real work, out of scope for this
session) or something else. No redesign was attempted here per explicit
instruction not to invent architecture changes unilaterally.

## What was NOT done in this session

- **No production access.** This sandbox has no SSH/network path to
  production (an attempt was blocked by the auto-mode classifier). Section
  13/15/17-20 of the brief (actual production HEAD parity, actual
  `OPAQUE_SUBSCRIPTION_ENABLED`, live-topology read, backup/restore
  verification, push, `git pull --ff-only` on production, restarts,
  routing seed execution, canary readiness) could **not** be independently
  verified or performed. GLM's own preflight claims from 2026-08-27 were
  read but not re-verified live.
- No push to origin. No deploy. No routing seed run against real Marzban.
  No real/synthetic Stars purchase attempted.
- PH5-06, PH6-07/08/09/10, promo, WL sales, PH4-06: not started, per
  standing instruction.

## Exact next step if resumed

1. Get the owner's Reading A vs Reading B decision on `tpl-<public_id>`
   (see above) — do not proceed to deploy without it.
2. Independently of that decision, if this session (or a fresh one) is
   given actual SSH/production access: re-run the section 15/17 preflight
   for real (HEAD parity, `OPAQUE_SUBSCRIPTION_ENABLED`, quick_check, FK
   check, cardinalities, live topology) before pushing/deploying anything.
3. Only after both (1) and (2) are satisfied: push `efcbafb`, deploy per
   the brief's section 17 sequence, then (only if the architecture question
   resolved to "ship as-is") run `scripts/seed_delivery_routing.py
   --seed-verified-baseline` once, verify read-only per section 18, and
   report `READY FOR FIRST CONTROLLED STARS CANARY`.

---

# AGENT_HANDOFF — PH5-11/PH5-12 first commercial STANDARD signup + delivery routing implemented and fully tested locally; local checkpoint only, NO push, NO deploy, canary NOT started

Updated: 2026-08-27 (implementation session, starting from local = origin =
production `f228b46`, working tree clean). **This top section supersedes
everything below.** Owner instruction: implement the first full commercial
STANDARD signup/purchase slice — 6 sellable SKU (BASIC/BASIC_PLUS/BASIC_PRO
x 30/60d) via the existing canonical PH5-05 Stars flow, self-service DIRECT
account creation strictly after confirmed payment, system-owned
provisioning template for first-device bootstrap (no customer legacy
dependency, anti-tamper verification preserved), operational delivery
routing (plan -> delivery profile -> host membership) with a hard
backend guarantee that STANDARD can never receive an exact WL host, admin
UX for host membership, then local checkpoint commit. Production was
read-only (catalog/accounts/payments/hosts/topology/schema/health); no test
buyer, no real invoice.

## State after this session

Local HEAD = checkpoint commit of this slice, one commit ahead of origin.
**origin/main and production both remain at `f228b46`.** PUSH AND DEPLOY
ARE EXPLICITLY FORBIDDEN for this slice; the first real Stars purchase
after deploy is a separate owner-run canary. Deadline context honored:
nothing optional was started (no PH6-07/08/09/10, no WL/package sales, no
promo, no PH5-06/07, no PH5-08 redesign, no PH4-06).

## What was built

- **Purchase gate (server-authoritative).** `SELLABLE_STANDARD_PLAN_CODES`
  in `src/commercial_signup.py` enforced in `stars_purchase.create_invoice`,
  `validate_invoice_for_checkout` and `capture_paid` for both invoice kinds.
  Callback data carries only plan_code+duration; every price/name/device
  count is re-resolved from the active immutable catalog.
- **Self-service DIRECT account (PH5-11, `CANONICAL_SIGNUP`).** New
  `src/commercial_signup.py` + `src/commercial_signup_schema.py`
  (migration `ph5_11_commercial_signup_v1`, requires PH3-01+PH5-05 exact
  checksums): `mgboost_provisioning_templates`,
  `mgboost_signup_template_jobs`, and the fill-once trigger on
  `stars_invoices.account_id` for signup rows. The invoice row (NULL
  account_id) is the only durable pre-payment state; at `capture_paid`
  (money already moved) the bound factory resolves-or-creates exactly ONE
  DIRECT account (one txn; alias group + PRIMARY alias = `tpl-<public_id>`
  + `mgboost_direct_account_reviews` PROVEN row + `DIRECT_BIND` owner link
  + PENDING template job). A concurrent caller after the binding commit but
  before the owner link resolves to the SAME account via the fill-once
  anchor and re-runs the idempotent link — this exact race was found by the
  8-thread test and fixed before commit.
- **System-owned provisioning template.** Worker job
  (`ensure_template_for_account` in stars.py `_tick`) converges a remote
  infrastructure Marzban user (`tpl-<public_id>`, flow `xtls-rprx-vision`
  evidenced live 2026-08-27, inbounds == STANDARD profile membership,
  expire=0) through the REAL broker ops (`legacy.user.get/create`), pins
  its exact `source_contract_hash` ONCE; remote drift -> STOP-class
  MANUAL_REVIEW (`template_contract_drift`), never silent re-pin; empty or
  WL-contaminated profile -> MANUAL_REVIEW (`wl_tag_in_standard_profile`).
  The customer never receives the template UUID/URL; children still get
  their own Marzban-minted UUIDs (`validate_created_child` unchanged).
- **First-device bootstrap.** `opaque_resolver.resolve_account_device`
  gained exactly one new authority: zero prior child intents + pinned
  ACTIVE template row -> template hash (legacy accounts without a template
  keep `PROVISIONING_UNAVAILABLE` exactly as before). Plus the
  render-boundary backstop: a freshly ensured child carrying an exact WL
  inbound -> permanent ERROR via new
  `ChildProvisioningStore.fail_permanent` (`WL_INBOUND_IN_STANDARD_CHILD`)
  — poison states never retry.
- **Credential delivery.** After a CREATE-apply the worker delivers the
  initial opaque credential (deliver-then-activate; lost delivery ->
  recoverable PENDING_DELIVERY; existing ACTIVE credential -> hint, never
  rotation; gated by `OPAQUE_SUBSCRIPTION_ENABLED` with an admin alert
  while off). CREATE vs RENEW user texts split by the applications row.
- **Delivery routing (PH5-12, `src/delivery_routing*.py`, migration
  `ph5_12_delivery_routing_v1`).** Profiles (CAS row_version) + immutable
  membership rows (UPDATE refused; DELETE allowed only with a prior
  HOST_REMOVED event in the same txn) + plan mapping + append-only
  `mgboost_delivery_profile_events` ledger (the entitlement-mutations
  ledger requires a NOT NULL account_id a routing mutation lacks).
  `apply_host_change`: exact PH0-05 classification (WLHostRejected),
  wl-shaped-not-allowlisted fail-closed (WLLikeHostRejected), additions
  require the tag in a FRESH live observation that passed
  `require_topology_ok()` (route records the assertion per mutation, PH6-06
  pattern), mandatory reason + idempotency replay + route-level
  `expected_row_version` CAS (stale writer = 409). Routes `GET
  /admin/routing/hosts`, `POST /admin/routing/hosts/{add,remove}`; admin
  page `frontend/assets/admin/routing.js` (+nav/page wiring in
  index.html/admin.js) with disabled WL rows and server refusal reasons.
  `scripts/seed_delivery_routing.py --seed-verified-baseline` seeds the
  shell + plan mapping + the 13-tag STANDARD membership verified read-only
  against live production inbounds on 2026-08-27.
- **Why STANDARD physically cannot get WL — three exact layers:** profile
  guard; template pin guard; resolver backstop (permanent ERROR). Beyond
  that the existing broker contract re-verifies child membership on every
  reread/subscription fetch (drift => fail-closed, never a partial body).
  PH6-06 enforcement semantics untouched (abstains for these accounts as
  before).

## Honest boundaries / limitations (documented, not hidden)

1. **No line-level render filter.** Real production subscription lines
   carry human remarks, not inbound tags (verified live: 32 lines, remarks
   are display names like "🇩🇪 🌍 Germany [VLESS - grpc]"); a line-level WL
   filter would require forbidden substring matching. The fail-safe lives
   at the exact-membership layers above: a corrupted child fails closed and
   never renders at all.
2. **Profile changes reach new provisioning only** (new templates/children;
   recovery for an existing device = admin Rebind, which provisions from
   the current profile). Propagating membership onto already-provisioned
   children is PH6-adjacent remote-mutation territory — deliberately not
   built.
3. **Per-account template users** are forced by
   `mgboost_legacy_account_aliases.legacy_username` UNIQUE (one
   infrastructure Marzban user per commercial account). Availability
   coupling: template drift/deletion fails ALL future ensures closed
   (by design; MANUAL_REVIEW alerting covers detection). Template users
   must never be edited in Marzban.
4. `OPAQUE_SUBSCRIPTION_ENABLED` must be ON at canary time — until then
   signup applications still apply (entitlement durable) but credentials
   are NOT issued (admin alert explains).

## Verification performed

- Targeted new suites: `tests/test_delivery_routing.py` **15 passed**;
  `tests/test_commercial_signup.py` **35 passed** (exact SKU matrix;
  WL/EXTENDED/FAMILY rejection; personal checkout; amount-mismatch capture
  -> manual_review without account creation; 8-thread capture race -> one
  account; crash durability across a fresh `Database()` on the same file;
  same-plan renewal vs different-plan refusal; template
  happy/idempotent/drift/outage/corrupted; first-device bootstrap without
  any legacy dependency; per-slot child/UUID isolation; credential
  create/lost-delivery/no-rotation; bot buy UX incl. tampered
  `buy_pay:WL:30` callback rejection; unrelated-account isolation).
- Updated for the rollout gate (reviewer-visible scaffolding changes, no
  production-code impact): `tests/_ops_helpers.py::paid_wl_subscription`
  and `tests/test_manual_payment_ph509.py::_paid_stars_subscription` now
  grant WL/EXTENDED/FAMILY fixtures through the PH5-02 engine directly
  (the sellable gate is deliberately channel-level); the ph510
  Stars-vs-manual stacking test moved FAMILY->BASIC_PLUS so it can stay on
  the real Stars path; `tests/test_stars_purchase.py` (19 passed; the
  former WL-60d snapshot test now rides BASIC-60d + new gate pins);
  `tests/test_admin_frontend_security.py` (4 passed; the JS-eval strip
  regex now covers the new routing.js module import); the
  `tests/test_admin_browser_e2e.py` fixture now serves routing.js (the
  browser suite caught its absence — dynamic-import console error).
- Related suites re-run green: stars worker/db/bot-support/admin-stars,
  opaque resolver + PH2-07, subscription, plan catalog, subscription
  renewal, direct enrollment, manual payments PH5-09/10, operational
  admin, all browser E2E. FULL REGRESSION
  (`/home/beykus/mgboost-pw-venv`, every suite, solo run):
  **1388 passed, 0 failed, 0 skipped** (= baseline 1334 + exactly the 54
  new/updated test outcomes). `git diff --check` clean; every touched JS
  module passes `node --check`; all touched python files compile. (Two
  earlier regression attempts hit the documented `/tmp`-quota failure
  class — once from two runs racing, once from a concurrent targeted run;
  cleaned strictly per the owner-approved anonymous-`/tmp/tmp*` precedent;
  the clean solo run above is the recorded result.)
- **Production read-only preflight (SSH only; ZERO writes/mutations):**
  HEAD `f228b46` == local == origin, only the known untracked
  `extra_configs.json` drift; `quick_check=ok`, 0 FK violations;
  cardinalities accounts=18(15 DIRECT ACTIVE+1 CLOSED, 2 INTERNAL)/
  subscriptions=18/stars_invoices=2(both legacy refunded)/
  evidence=0/applications=0/reviews=16/credentials=9/child_intents=47/
  wl_periods=0; all migrations through `ph6_06_wl_enforcement_v1` present
  (both new ones will self-apply additively at restart); catalog seeded
  exactly `STARS-2026-08-26-v1` with the 12 SKUs (prices match the brief's
  6); `stars:enabled=1` already set; latest topology assertion
  `2026-08-26-v1 ok`. Live reads: 25 inbounds (13 non-WL STANDARD
  candidates + 12 exact WL), 5 nodes (2 exact WL), a real child user
  carries flow `xtls-rprx-vision` and ALL 25 inbounds (its
  LEGACY_PAID_COMPAT UNLIMITED plan includes WL — untouched by this
  slice), subscription remarks are display names (boundary #1 above).
  Three services active.

## Reviewer attention list (hotspots)

1. `src/stars_purchase.py::capture_paid` — the pre-transaction signup
   factory call (it must stay OUTSIDE capture's BEGIN IMMEDIATE; the
   factory is itself transactional) and the reason mapping to
   manual_review.
2. `src/commercial_signup.py::ensure_signup_account` — the fill-once
   anchor logic and the always-idempotent owner link after the txn (the
   concurrency race fixed here is exactly the reviewer surface).
3. `src/commercial_signup.py::ensure_template_for_account` — get ->
   (create) -> reread -> hash compare -> pin-once; confirm no path can
   re-pin over drift or create a template from a WL-contaminated profile.
4. `src/opaque_resolver.py` — the template-hash fallback placement (only
   when zero prior intents) and the WL backstop's `fail_permanent`.
5. `src/delivery_routing.py::apply_host_change` — exact-vs-startswith
   classification boundaries, the REMOVE-with-pre-event trigger contract,
   CAS bump ordering inside one txn.
6. `src/routes/admin_routing.py` — fresh observation + assertion before
   every mutation; confirm no mutation path skips `require_topology_ok`.
7. `src/stars.py::_deliver_signup_credential` — deliver-then-activate
   split and the no-rotation guarantee.

## Exact next step

Independent review of this checkpoint against `origin/main` (`f228b46`),
then owner deploy decision (application-code-only + two additive
self-applying migrations in the same restart class as PH6-06). At deploy:
run `scripts/seed_delivery_routing.py --seed-verified-baseline` once
(root, local, audited), verify `mgboost_delivery_profile_hosts` = 13 and
the admin routing page renders, enable `OPAQUE_SUBSCRIPTION_ENABLED`
(nginx already proxies the 43-char root path), then the first real Stars
purchase (Анастасия) is the owner-run canary per the brief. After that:
STOP — nothing else was authorized.

---

# AGENT_HANDOFF — PH6-06 independent review APPROVED WITH FIXES (one real P0 found, fixed, regression-tested); deploy in progress this session

Updated: 2026-08-27 (independent-review session, starting from local
`5dabafb` / origin+production `14bdbcf`). **This top section supersedes
everything below.** Owner asked for an independent review of the PH6-06
checkpoint left by the prior (GLM) session, explicit instruction not to
trust its self-report without independent verification, plus a
conditional production deploy authorization if the verdict came back
APPROVED or APPROVED WITH FIXES.

## Verdict: APPROVED WITH FIXES

Read `AGENT_HANDOFF.md`/`ROADMAP.md` PH0-05/PH6-01..07 sections, then the
full diff `14bdbcf..5dabafb` line by line (schema, machine, contract,
broker branch, client wrapper, runner, tests) before touching anything.

**One real P0 found and fixed** — see the PH6-06 roadmap entry's own
"Independent review" note for the full mechanism: `apply_decision`'s
late-arrival path could bump the enforcement epoch while a sibling child's
op in the SAME epoch was still genuinely unsettled (RETRY/PENDING/expired
lease), silently orphaning it past `claim()`'s epoch-supersede guard and
letting `finalize_account` terminal-flip the account on an incomplete op
set. Reproduced deterministically first (a temporary probe test), then
fixed in `src/wl_enforcement.py::apply_decision` to mint late arrivals into
the SAME epoch whenever the state is already mid-transition and the
direction hasn't changed — matching what the function's own docstring
already promised. Kept as a permanent regression test:
`test_late_arrival_mid_transition_never_orphans_a_pending_sibling_op` in
`tests/test_wl_enforcement.py`. Also deleted dead/unreachable code after a
`return` in `_derive_freeze_and_dispatch` (cosmetic).

**Everything else independently verified, not just re-read:** exact
inbound-only mutation semantics (only `inbounds.vless` ever moves; static
PH0-05 allowlist only; no fuzzy matching; UUID/status/expire/data_limit
byte-stable; empty-remainder EXCLUDE refused; INCLUDE baseline restricted
to the allowlist), the topology fail-closed gate
(`require_topology_ok()` before any decision, zero transitions on
mismatch/unreachable/stale-version), the crash/retry lease mechanics
(restart before mutation, restart after remote success before ACK,
manifest first-writer-wins, attempt-cap → permanent `ERROR`, exactly-once
by observation not bookkeeping), and `ERROR_RECONCILE` recovery being
verification-only (never a blind inverse mutation). One documentation
mismatch caught along the way (not a defect): the module's own docstring
says decisions come from PH6-04's `resolve_current_parent_wl_pool()`, but
the actual code re-derives the identical 3-line sequence inline via
`compute_parent_wl_pool` instead of calling it — behaviorally identical,
a P3 duplication-cleanup opportunity, not a second accounting path.

**PH6-07 boundary — roadmap updated, not just re-confirmed:** PH6-06
already contains everything the old PH6-07 roadmap text described as its
own scope ("local transaction writes quota desired+event; worker
calls/rereads/verifies/observed/retries"). `ROADMAP.md`'s PH6-07 entry is
now rewritten to name only what's genuinely still missing: scheduler/
worker lifecycle (nothing runs the cycle automatically today), periodic
reconciliation cadence, post-terminal/remote drift detection and recovery
(the documented `excluded_inbounds` known-limitation is real and
untouched — confirmed deliberately NOT auto-repaired by
`test_zero_effect_input_changes_do_not_reopen_the_machine`), and backlog/
observability. PH6-06 is not a hidden PH6-07 and PH6-07 was not started.

## Tests

Targeted `tests/test_wl_enforcement.py`: **32 passed** (GLM's 31 + 1 new
regression for the P0 above). Related suites re-run green (wl topology/
topology-guard/usage-ledger/parent-pool/period-admin-reset/packages,
marzban broker, child provisioning/lifecycle/retention, parent sync,
device slots, admin operational admin): **286 passed**. Full regression
via the Playwright venv (`/home/beykus/mgboost-pw-venv`, all suites):
**1334 passed, 0 failed, 0 skipped** (GLM's claimed 1333 independently
reproduced almost exactly — +1 for the new regression test; the number
itself was NOT trusted blind, it was re-run from scratch in a separate
venv). Hit the same `/tmp`-quota failure class GLM's own handoff already
documented (~3400 fresh anonymous `tmp*` dirs, tmpfs 80% full, each holding
only an empty per-test `db.sqlite3`, zero owning processes running) —
confirmed with the owner before deleting, then deleted only that exact
anonymous pattern; venvs/caches/repo untouched, matching the established
precedent. `git diff --check` clean; all changed/new python files compile.

## Production deploy (2026-08-27, this session, owner-authorized)

Fresh encrypted backup create/restore `PASS` (`scripts/secure_db_backup.py`,
isolated-tempdir decrypt+checksum+quick_check, before touching anything).
Preflight over SSH: HEAD `14bdbcf` == local(pre-fix) == origin, only the
known untracked `extra_configs.json` drift, all 3 services active,
`quick_check=ok`, 0 FK violations, cardinalities accounts=18/
subscriptions=18/child_intents=47/wl_periods=0, PH6-06's three prerequisite
migration checksums (`ph3_01_parent_account_v1`/
`ph3_03_child_prerequisites_v1`/`ph6_03_wl_usage_ledger_v1`) byte-identical
to local. Pushed the fix commit, fast-forward pulled on production
(`14bdbcf..33eaae0`), restarted **only** `mgboost-panel` (the additive
migration self-applies on `Database()` construction, same class as every
prior PH6-xx deploy). Post-deploy: `quick_check=ok`, 0 FK violations,
`ph6_06_wl_enforcement_v1` migration row present with the exact local
checksum, all three new tables present and **empty** (`states`/`ops`/
`events` = 0/0/0 — dormant, no state created for anyone), unrelated
cardinalities byte-identical to preflight, all 3 services active, no
error/traceback/5xx in any service's journal since restart. Broker
allowlist confirmed to include `child.user.wl.set` live in the running
process; broker journal shows only ordinary read traffic
(`child.user.observe`/`legacy.nodes.list`), zero new op calls. Ran ONE
live, read-only PH6-01 topology assertion (`fetch_live_topology_
observation` + `run_assertion` + `require_topology_ok()`, explicitly
owner-pre-approved as read-only) — real production topology still matches
the exact `2026-08-26-v1` baseline (`ok=True`, zero missing/extra/
mismatched), appended one row to the append-only
`mgboost_wl_topology_assertions` table (2 total now), touched nothing
enforcement-related. A real legacy `/sub/<token>` fetch (token derived
live from Marzban, never printed/logged) returned `200` with a normal
subscription body. The opaque `/…` route was NOT smoke-tested — it is
independently dormant in production for two unrelated, pre-existing
reasons (`OPAQUE_SUBSCRIPTION_ENABLED` defaults off; nginx does not proxy
any path to it), confirmed by reading `src/routes/opaque_sub.py`'s own
docstring, not assumed. **Zero enforcement ops/state rows created for any
customer; zero Marzban user/inbound/UUID/expiry/status mutated anywhere;
zero real WL disable/enable transitions performed.** Final HEAD:
local = origin = production = `33eaae0`.

## Exact next step

Independent review + deploy of this checkpoint is now closed. PH6-06 stays
dormant/on-demand (`python -m scripts.run_wl_quota_enforcement`) until the
owner separately authorizes PH6-07 (scheduler/periodic-reconciliation
wiring — see `ROADMAP.md`'s rewritten PH6-07 entry for the exact narrowed
scope). PH6-07 was explicitly NOT started this session.

---

# PRIOR HANDOFF — PH6-06 exact inbound-only WL enforcement state machine implemented and fully tested locally; dormant, no push, NO deploy; production verified read-only compatible

Updated: 2026-08-27 (implementation session, starting from local = origin =
production `14bdbcf`, working tree clean). **This top section supersedes
everything below.** Owner instruction: implement PH6-06 — the launch-critical
WL state machine (`ACTIVE -> DISABLE_PENDING -> DISABLED`,
`DISABLED -> ENABLE_PENDING -> ACTIVE`, mismatch/failure
`ERROR_RECONCILE`), local DB as source of desired state, remote mutation
restricted to exactly `inbounds.vless` of the exact PH0-05 allowlist with
mandatory PH6-01 `require_topology_ok()` before any destructive mutation,
then local checkpoint commit, no push, no deploy.

## State after this session

Local HEAD = checkpoint commit of this slice, one commit ahead of origin.
**origin/main and production both remain at `14bdbcf`** (re-verified over
SSH during the read-only preflight). PUSH AND DEPLOY EXPLICITLY FORBIDDEN.
Nothing schedules or routes through the new code: dormant/on-demand,
matching the PH6-01..04 precedent.

## What was built (no second engine — every primitive reused)

- **Schema** (`src/wl_enforcement_schema.py`, additive checksum-pinned
  migration `ph6_06_wl_enforcement_v1`, requires PH3-01/PH3-03/PH6-03
  checksums): `mgboost_wl_enforcement_states` (one row per account; epoch
  monotonic-by-trigger; state CHECK =
  ACTIVE/DISABLE_PENDING/DISABLED/ENABLE_PENDING/ERROR_RECONCILE),
  `mgboost_wl_enforcement_ops` (per-(epoch, child) outbox rows —
  UNIQUE(account, epoch, child), lease/next_attempt/attempts<=8/row_version,
  frozen-manifest column), `mgboost_wl_enforcement_events` (append-only,
  no-update/no-delete triggers). Zero existing tables touched; deploy is
  application-code-only (`mgboost-panel` restart self-applies the additive
  migration on `Database()` construction — same class as PH6-03).
- **Machine/store** (`src/wl_enforcement.py`): `decide_direction_from_pool`
  is pure policy over PH6-04's `resolve_current_parent_wl_pool()` —
  LIMITED+exceeded -> EXCLUDED, LIMITED+not-exceeded -> INCLUDED, `None` or
  UNLIMITED -> abstain (Non-WL/UNLIMITED accounts never even get a state
  row). `apply_decision` opens a fresh epoch on every genuine direction
  flip, mints ops for all current children (the exact ACTIVE-generation
  join PH3-08 uses, revoked excluded; slot-paused children INCLUDED
  deliberately), picks up late arrivals (rebind successors / devices
  joining mid-suspension) as missing children, and — critically — always
  hands back every unsettled op of the live epoch (PENDING/RETRY/expired
  lease) so restarts and outages actually resume. `claim()` re-checks the
  stamped epoch+direction against the LIVE machine row immediately before
  dispatch (parent-revision precedent): a superseded disable/enable is
  never sent, its evidence is a SUPERSEDED event. Attempt cap converts to
  permanent ERROR (no infinite RETRY parking). `finalize_account` flips
  terminal state ONLY when all epoch ops are APPLIED AND a fresh
  independent reread of each touched child equals its frozen target;
  anything else flags `ERROR_RECONCILE` — recovery from error is
  verification-based only, never blind mutation.
- **Wire contract + broker op** (`src/wl_enforcement_contract.py`, new
  dispatch branch `child.user.wl.set` in `src/broker_operations.py`,
  registered in `BROKER_OPERATIONS`; client wrapper
  `ServiceMarzbanClient.set_child_wl_state` in `src/service_marzban.py`):
  reread -> username + HMAC uuid-verifier fail-closed -> compute the exact
  target vless member list from LIVE state plus the static PH0-05
  allowlist ONLY (EXCLUDED = observed − WL tags, refuses an empty
  remainder; INCLUDED = (observed − WL) ∪ baseline_wl_tags, where every
  baseline tag must be a literal allowlist member — the caller can never
  inject arbitrary inbounds, honoring the DL "no caller-suppliable
  inbounds" doctrine) -> minimal partial update
  `{"inbounds": {"vless": target}}` -> reread/verify membership == target
  with UUID/status/expire byte-stable (STOP-class failure if anything
  else moved). Verified against the real production Marzban 0.8.4-ph1-08
  source read over SSH: `crud.update_user` applies only provided fields —
  absent proxies/status/expire/data_limit are skipped, and `inbounds`
  recomputes `excluded_inbounds` for exactly the protocols present in the
  payload (vless only here).
- **Exactly-once by observation, not bookkeeping** (the a68e265 lesson):
  repeated/replayed dispatches against an already-converged remote return
  `ALREADY_IN_SYNC` and perform zero writes. The first observation of a
  pending op freezes its manifest (`baseline_full`/`target`/`removed_wl`)
  first-writer-wins; a crash after the remote mutation but before the ACK
  replays against the SAME recorded target and ACKs exactly once (op row
  is terminal after its single APPLIED flip).
- **Cycle** `run_wl_enforcement_cycle`: fresh PH6-01 assertion
  (`fetch_live_topology_observation` + `run_assertion` +
  `require_topology_ok`) BEFORE anything — never-checked/mismatch/
  unreachable/stale-config-version all abort with zero transitions minted;
  per-account isolation of errors (collector precedent); per-op dispatch
  via `process_wl_op` (observe -> freeze -> mutate -> settle) with
  REMOTE_MISSING never auto-creating. Observation reads reuse the EXISTING
  `legacy.user.get` broker surface — zero new read endpoints.
- **Runner** `scripts/run_wl_quota_enforcement.py` — on-demand only;
  prints safe aggregate JSON (counts + error classes, no identifiers).

## Known limitation (documented, feeds PH6-07/09 — not hidden)

Marzban stores the vless target as a persistent `excluded_inbounds` list
computed against the LIVE xray config. A NEWLY-ADDED WL inbound after a
disable would therefore be auto-included for suspended users until the
next enforcement pass; it would also surface in `extra_wl_like_tags`
alert evidence. PH6-07 periodic reconciliation + topology versioning own
this drift; PH6-09 owns outage cadence/fail-safe.

## Verification performed

- Targeted `tests/test_wl_enforcement.py`: **31 passed** — the full brief
  matrix: exactly-once disable across repeated cycles (deep snapshots:
  only `inbounds` moves; UUID/expire/status/proxies byte-stable),
  exactly-once restore on reset/new period, three-epoch flip-flop with
  distinct op ids and real mutations, stale epoch superseded + direction-
  tampered claim guard, topology never-checked/fresh-mismatch/unreachable/
  stale-version all blocking with zero transitions and zero state rows,
  UNLIMITED and plan-less accounts structurally untouched, partial-offline
  sibling isolation ending ERROR_RECONCILE, Marzban outage retry→recover
  with exactly one mutation, attempt cap landing ERROR, restart between
  desired commit and mutation, restart after remote success before ACK
  (single mutation, frozen manifest intact), revoke exclusion, slot-pause
  uniformity, rebind-shaped late arrival, absent child REMOTE_MISSING
  without creation, remove-all-inbounds refusal, include-without-baseline
  refusal, post-convergence manual drift deliberately NOT auto-repaired
  (honest PH6-07 boundary pin), broker wire negatives (foreign baseline
  tag, wrong verifier, EXCLUDED-with-baseline, shape noise), migration
  checksum/FK/trigger guards (epoch downgrade, identity immutability,
  no-delete, UNIQUE per epoch+child).
- Related suites re-run green: wl topology/ledger/pool/period/packages,
  marzban broker + client policies (the `BROKER_OPERATIONS` exact-set
  guard test updated for the new op), child provisioning/lifecycle/
  retention, parent sync, device slots, admin operational admin.
- **Full regression (Playwright venv, every suite): `1333 passed,
  0 failed, 0 skipped`.** First background run collided with the
  documented `/tmp`-quota failure class because two pytest processes ran
  concurrently on top of ~2700 hour-stale anonymous `tmp*` dirs; cleaned
  strictly per the owner-approved precedent (only hour-stale anonymous
  `/tmp/tmp*`; venvs/caches/repo untouched) and the identical solo command
  passed deterministically.
- `git diff --check` clean; all new/changed python files compile.
- **Production read-only preflight (SSH only; ZERO writes/mutations/
  restarts):** HEAD `14bdbcf` == local == origin; only the known untracked
  `extra_configs.json` drift; `quick_check=ok`, 0 FK violations;
  cardinalities accounts=18/subscriptions=18/`mgboost_wl_periods`=0/child
  intents 47 (35 `observed_state='ACTIVE'`); PH6-06 tables absent as
  expected pre-deploy (additive migration will self-apply at restart);
  topology guard has one recorded `ok` assertion @ `2026-08-26-v1`; all
  services active. No real WL disable/enable was performed anywhere.

## Reviewer attention list (hotspots)

1. `src/wl_enforcement.py::apply_decision` — the epoch/transition table
   and the unsettled-ops hand-back (this is where the restart/resume
   semantics live; the initial draft had a real livelock bug here —
   `*_PENDING` continuation reopened a new epoch every cycle and
   starved its own ops — caught by the partial-offline test before
   commit; watch the current formulation closely).
2. `WLEnforcementStore.claim` — the epoch+direction supersede guard and
   the attempt-cap ERROR conversion (bounded-retry guarantee).
3. `process_wl_op` / `_derive_freeze_and_dispatch` /
   `_dispatch_frozen_manifest` — manifest-first-writer-wins and the
   observation-based ALREADY_IN_SYNC path; check that no path can mutate
   before a manifest exists, and that every failure class maps to
   exactly one of {ack, retry, permanent error}.
4. Broker `child.user.wl.set` branch + `wl_enforcement_contract` —
   target math set equations, empty-remainder refusal, baseline
   membership restricted to the static allowlist, post-write verify
   (identity/UUID/status/expire byte-stable).
5. `finalize_account` — verification-only terminal flips and
   ERROR_RECONCILE entry points; deliberate v1 boundary: post-terminal
   drift is NOT auto-repaired (PH6-07 owns reconciliation) — pinned by a
   test so nobody "fixes" it into a blind self-heal later.
6. The documented exclusion-list drift limitation above (new inbounds
   after disable) — confirm the owner accepts the PH6-07/09 ownership
   split.

## Exact next step

Independent review of this checkpoint against `origin/main` (`14bdbcf`),
then owner deploy decision (application-code-only restart; additive
migration self-applies; live-DB compatibility already proven above).
After deploy PH6-06 remains dormant until the owner wires a schedule /
operator cadence — wiring the worker loop IS the start of PH6-07
(transactional outbox/reconciliation), which is explicitly NOT started
here, nor are PH6-05/08/09/10, payments, promo, upgrade/downgrade or
PH4-06.

---

# PRIOR HANDOFF — PH7-13 Megochel account consolidation (DL-057) implemented, tested and executed in production; GLM migration-status bugfix (9edd42e) independently re-verified and deployed alongside it

Updated: 2026-08-27 (controlled maintenance rollout session, starting from
local `9edd42e` / origin+production `9b38c91`). **This top section
supersedes everything below.** Owner asked for two things in one controlled
rollout: (1) deploy the already-built GLM migration-status bugfix
`9edd42e` (local checkpoint commit, not yet pushed/deployed at session
start), and (2) design, implement, test and execute an owner-approved
consolidation of two real duplicate accounts, `MegochelPC` (account 5) and
`MegochelAndroid` (account 6), into one canonical `Megochel` account, per a
prior read-only analysis session and the owner decisions given at the start
of this session.

## What this session did

**GLM bugfix review.** Independently read the full `9edd42e` diff before
building anything on top of it. Confirmed the core semantic change in
`legacy_grace_observability.classify_action()` is correct and matches the
three required properties exactly: Telegram `BOUND` + zero real-device
lineage → `WAITING_FIRST_DEVICE` ("Ожидает первого подключения"); any real
`mgboost_migration_bindings` lineage (`MIGRATING`/`MIGRATED`/
`LEGACY_REVOKE_PENDING`/`LEGACY_REVOKED`) → `OK_MIGRATED` ("Миграция
штатно") regardless of Telegram status; `telegram_status()`'s own taxonomy
and the urgent-category precedence above it are untouched, so Telegram
ownership and technical migration status are structurally independent
inputs, never gating each other. Frontend label map, the PH4-05 daily
report's blocker string and `docs/ADMIN_PANEL_REDESIGN.md`'s normative
taxonomy were updated consistently in the same commit. Did not rewrite it
stylistically. Ran full regression locally before touching anything else
(`1264 passed, 4 skipped`, matching the commit's own claimed baseline
modulo a skip/pass-count wording difference in its own message, not a real
discrepancy).

**Megochel consolidation — why no existing primitive covers it.**
`mgboost_legacy_alias_groups` is a strict 1:1 `account_id PRIMARY KEY`
table; a multi-alias group (like account 1's 3 aliases) is only ever
assembled once, at bootstrap. `mgboost_legacy_account_aliases.legacy_username`
is globally `UNIQUE` and the table is fully immutable (no `UPDATE`, no
`DELETE`). Every other account-scoped table treats `account_id` as part of
an immutable identity, by trigger or by construction. Reassigning history
from one already-created account to another is therefore structurally
impossible with anything that existed before this session — confirmed by
reading every relevant store (`account_store.py`, `device_slots.py`,
`child_lifecycle.py`, `legacy_bridge.py`, `legacy_paid_compat.py`,
`subscription_renewal.py`, `subscription_admin_ops.py`) rather than assuming.

**Built (PH7-13, DL-057):** new checksum-pinned
`src/account_consolidation_schema.py` (`mgboost_account_merges`/
`_merge_events`, append-only event-sourced `ACTIVE`/`REVERSED` merge state
mirroring the existing `mgboost_legacy_bridge_bindings`/`_binding_events`
precedent; `mgboost_account_display_names`, cosmetic owner-set label
mirroring `mgboost_telegram_identities`'s revoke-and-reinsert pattern). New
`src/account_consolidation.py`: `resolve_account_id()` (the one shared
canonicalizer), `create_merge()`/`reverse_merge()` (self-merge and any
chain/cycle permanently forbidden via a strict bipartition, bounded to
depth 1 forever even across a later reversal; replay- and concurrency-safe),
`close_account()`/`reopen_account()` (fail-closed preconditions: no active
Telegram OWNER, no non-terminal child, no ACTIVE generation; cancels any
live subscription with immutable evidence *before* flipping the account
`CLOSED`, because `ProvenanceStore.record_mutation()` itself refuses
evidence for an already-CLOSED account — found this the hard way, via a
failing test, before it ever reached production), `set_display_name()`.
New `legacy_paid_compat.increase_device_limit()`: `ensure_legacy_paid_
compat_entitlement()` only ever bootstraps a brand-new entitlement and
hard-conflicts on any different existing plan — it has no upgrade path, and
this was independently discovered to contradict the owner's original
instruction to use it for the D3->D6 bump, so a new narrow function was
built and explicitly re-confirmed with the owner's decision instead of
silently working around the mismatch.

**Resolver-coverage audit, not limited to the three obviously-named
paths:** `legacy_bridge.py::resolve_account_for_legacy_username()` now
canonicalizes through an ACTIVE merge (covers the dormant `/sub` bridge and
`migration_lifecycle.py`'s lineage recorder in one place). Found and fixed
a real, previously-unflagged gap: `legacy_grace_registration.
bind_telegram_after_registration()`/`resolve_ambiguous_telegram_ownership()`
resolved an absorbed alias's raw `account_id` and called
`link_telegram_owner()` directly, which raises `AccountSchemaError`
(uncaught by these functions — only `IdentityConflict` was handled) for a
CLOSED account instead of the correct `ALREADY_BOUND`/`CONFLICT` outcome
against the survivor; a real customer typing the absorbed username into the
bot would have hit an unhandled error. Fixed by canonicalizing first.
`subscription_admin_ops.py` (PH7-01 expiry ops) had no account-status check
at all — added one. Verified already-safe without any change, by reading
the code: `direct_enrollment.py`'s alias-conflict guard, `device_slots.py`'s
`_entitlement_capacity()` (hard-requires `account_status='ACTIVE'`),
`manual_payment.py`/`entitlement_engine.py`, `device_slot_admin.py`
Disable/Enable, Stars/WL purchase paths (resolve via `telegram_id`, never
via username, and the absorbed account never had one).

**Tests:** 34 new focused tests (`tests/test_account_consolidation.py`)
covering schema/trigger immutability, both resolver fixes end to end, close
preconditions independently, the canonical genesis-child Revoke->Free
sequence, merge replay/reversal/reopen idempotency, self-merge and both
chain/cycle directions rejected, concurrent `create_merge()` (8 real
threads) converging to one row, the CAS guard clause proven directly
against a stale `row_version`, the exact D3->D6 transition and its
refusals, survivor identity/credential/subscription byte-for-byte
stability, absorbed account's pre-existing history untouched, and a
completely unrelated third account provably unaffected. Full regression:
`1298 passed, 4 skipped` (zero regressions). `git diff --check` clean
throughout.

**Production rollout, this session, real accounts:** fresh encrypted
backup (`mgboost-db-20260827T114917Z.tar.gpg`, `--verify` PASS) before
anything; fast-forward deploy `9b38c91..d5ed3b7` (bundles `9edd42e` and all
PH7-13 work into one `mgboost-panel` restart — the only service that needed
it); schema applied automatically, `quick_check=ok`, zero FK violations.
GLM fix re-verified against real production data post-deploy. A fresh
read-only re-check of accounts 5/6 immediately before mutating found one
real change since the original analysis session: account 6's real Android
device had organically migrated onto a second real slot (`mgc_
efwxdfyhmimnyb3dh37gaj3tl4`) in the interim — the merge plan required zero
changes for this and none were made. Executed via a new, reviewed,
hardcoded-target script (`scripts/dl057_megochel_consolidation.py`) that
only ever calls the canonical primitives above, never raw SQL. The script
itself had two real bugs, both caught live in production by its own
typed exceptions before any wrong data was written, both fixed and
redeployed before a clean completion: a 15-character `idempotency_key` one
byte under `child_lifecycle`'s 16-minimum (caught immediately after the
real Marzban `REVOKE` had already succeeded — safe, since revoke/re-run is
idempotent and the retry never re-rotated the UUID); and a preflight that
required the genesis child to still be "non-terminal", which broke
resuming once `REVOKE` had already made it terminal (fixed to match by the
keyed genesis-HWID proof instead of lifecycle state). Full sequence
completed cleanly: real Marzban `REVOKE`+`FREE` on account 5's genesis
child -> `close_account(5)` (subscription `CANCELLED`) -> `create_merge
(5->6)` -> `set_display_name(6,'Megochel')` -> `increase_device_limit
(6,+3)` (`LEGACY_PAID_COMPAT_V1_D6`).

**Post-mutation verification, all read-only against the live DB:** account
5 `CLOSED`, subscription `CANCELLED`, slot `RELEASED`/`FREE`, zero `ACTIVE`
generations anywhere for it; exactly one `mgboost_account_merges` row
(`5->6`, `ACTIVE`) with exactly one `CREATED` event; account 6's Telegram
identity (id 6, `telegram_id=1623120036`), opaque credential (id 9, same
generation/`last_used_at`) and subscription (same row id 6) all
byte-for-byte unchanged except the intended plan bump to
`LEGACY_PAID_COMPAT_V1_D6` (`device_limit=6`, `wl_mode` still `UNLIMITED`,
`current_expiry` still `NULL`, exactly one live subscription); new
`display_name='Megochel'`; both legacy aliases (`MegochelPC`->5,
`MegochelAndroid`->6) byte-for-byte untouched;
`resolve_account_for_legacy_username('MegochelPC')` and
`resolve_account_id(db,5)` both now return `6`; both real legacy Marzban
users (`MegochelPC` id 4, `MegochelAndroid` id 5) confirmed `active` with
traffic still accruing normally, completely untouched; unrelated account 2
(pre-existing `DISABLED` canary) and the other 16 `ACTIVE` accounts
unchanged (18 total, as before); all 5 services active, zero errors/
tracebacks/5xx in logs across the whole operation;
`admin_read_models.account_detail()`/`account_summaries()` confirmed
showing `display_name='Megochel'` for account 6 and the new
`consolidation` block correctly cross-referencing both sides.

## State after this session

Local HEAD, origin `main` and production are all at `d5ed3b7`, working
tree clean on both ends except the pre-existing, unrelated untracked
`extra_configs.json` on production (documented drift from prior sessions,
not touched). `ROADMAP.md` PH7-13 is `[x]` with full production evidence;
DL-057 records every owner decision behind this consolidation. Real
customer accounts 5/6 are the only accounts this session ever mutated in
production; no other customer mutation was performed.

## Known follow-up gaps (not blocking, not started this session)

- No frontend panel renders the new `consolidation`/`display_name` fields
  yet beyond the existing title-fallback chain and the raw `account_detail`
  JSON block; a dedicated "merged from" UI affordance would be a natural
  follow-up but was out of scope.
- `tests/test_admin_browser_e2e.py`'s Playwright suite is environment-gated
  (`playwright` not installed in this sandbox) and was not exercised here;
  it was not newly broken (same 2 skips as the pre-existing baseline) and
  the `display_name` frontend change is otherwise covered by
  `admin_read_models` tests only.
- `reverse_merge()`/`reopen_account()` exist and are tested but were never
  exercised against the real Megochel accounts (no reason to reverse a
  successful, verified consolidation) — they remain available if the owner
  ever needs to undo this specific merge.
- The real device-count ambiguity flagged in the original read-only
  analysis (3 vs 4 physical devices) was explicitly waived by the owner
  ("trusted user") and was not re-investigated.

# AGENT_HANDOFF — account-centric migration action semantics fixed (Telegram ↔ device-lineage coupling removed); local checkpoint commit, NOT deployed, NOT pushed

Updated: 2026-08-27 (bugfix session on stable `9b38c91`, owner-reported admin
UI bug). **This top section supersedes everything below.**

## What this session did

Owner report: an account with Telegram ownership BOUND but
`real_device_count == 0` showed «Ожидает Telegram» as its Статус/действие —
as if Telegram registration gated technical migration. Root cause confirmed
in backend derivation (frontend only renders server values; verified no
client-side coupling): `legacy_grace_observability.classify_action()`'s
terminal fallback returned the Telegram-named `WAITING_FOR_REGISTRATION`
for EVERY account without real-device lineage regardless of ownership, and
its `OK_MIGRATED` branch additionally required an active slot on top of
migrated lineage. Checked first that the enum was used nowhere else with
legitimate Telegram meaning (only definitions + the buggy fallback +
tests/script/label-map references; `telegram_status()`'s own BOUND/
UNREGISTERED/PENDING_LINK/AMBIGUOUS taxonomy untouched) — so the minimal fix
was replacing the fallback concept, not a global rename elsewhere.

Fix — pure read-model derivation, no schema/API shape changes:

- zero real-device lineage → new `WAITING_FIRST_DEVICE`
  («Ожидает первого подключения»); ANY `mgboost_migration_bindings`
  lineage (`MIGRATING`/`MIGRATED`/`LEGACY_REVOKE_PENDING`/`LEGACY_REVOKED`)
  → `OK_MIGRATED` («Миграция штатно»). Telegram never gates or defines these.
  RECONCILE_REQUIRED / MANUAL_REVIEW / COMPATIBILITY_BLOCK / CONTACT_USER
  precedence unchanged; CONTACT_USER thereby narrows to zero-lineage members,
  its natural outreach audience.
- `core.js` label map swaps «Ожидает Telegram» for the new label;
  daily-report `_blocker()` string updated; normative ADMIN_PANEL_REDESIGN.md
  taxonomy updated (dated historical evidence text deliberately left as-is);
  CHANGELOG Fixed entry added per DoD.
- Both mandatory regression cases pinned at every surface (list via
  `account_summaries`, detail via `account_detail`, browser render):
  BOUND + zero real devices → Привязан + Ожидает первого подключения with an
  explicit "Ожидает Telegram nowhere" assertion; UNBOUND (`_reviewed_internal`
  ABSENT ownership + `prepare_migration`) → Не привязан + Миграция штатно;
  daily-report default-action test extended to pin BOUND-without-lineage.

Production read-only preflight (local=origin=production all `9b38c91`,
working tree clean except pre-existing untracked `extra_configs.json`,
3 services active): DB-copy read gate over all 18 accounts reproduced the
bug on account **6** (BOUND + zero lineage → `WAITING_FOR_REGISTRATION`
today); the seven UNREGISTERED-with-lineage accounts (7/10/11/12/15/16/18)
already read `OK_MIGRATED` and stay there; simulated new logic on the same
data changes labels ONLY for 2/5/6/9/14/17 (all zero-lineage), and no
account has lineage-but-zero-active-slots, so the OK_MIGRATED broadening is
defensive against current prod data; copy `quick_check=ok`.

Tests (fresh Playwright venv recreated at `/home/beykus/mgboost-pw-venv` —
the prior session's venv was absent from this box): targeted
read-models+daily-report **21 passed**; browser e2e **2 passed** (fixture
updated to `WAITING_FIRST_DEVICE`, plus Russian-label presence/absence
assertions in table and Migration tab); FULL REGRESSION **1268 passed,
0 failed, 0 skipped** (= recorded baseline 1266 + exactly the 2 new tests);
`git diff --check` clean. Local checkpoint commit only — origin NOT pushed,
production NOT modified beyond the read-only preflight above. Next deploy
will again be application-code-only (no schema diff), same class as every
prior slice.

---

# AGENT_HANDOFF — PH7-01 + PH7-05 Disable/Enable independently reviewed (APPROVED, no code defects, one product ambiguity resolved as DL-056); production deploy in progress this session

Updated: 2026-08-27 (independent review session, following directly from the
implementation checkpoint `dec28f5` recorded below). **This top section
supersedes everything below.** Owner instruction: independently review
`dec28f5` against `origin/main`/production (`f7ea7f4`), fix only real
findings, then decide on production rollout.

## What this session did

Read `AGENT_HANDOFF.md`/`ROADMAP.md`/`docs/ADMIN_PANEL_REDESIGN.md` and the
full `f7ea7f4..dec28f5` diff (18 files, +1977/-36) before touching anything;
confirmed local HEAD `dec28f5` was one commit ahead of `origin/main`/
production (`f7ea7f4`), working tree clean. Read every new/changed source
file in full (`device_slot_admin.py`, `subscription_admin_ops.py`,
`parent_sync.py`, `admin_devices.py`'s new routes, `admin_expiry.py`,
`admin_read_models.py`'s availability projection, `device_slots.py`,
`admin_audit_timeline.py`) rather than trusting the self-report, cross-
checking every claim (CAS/row_version lost-update protection, live-state-only
convergence, per-slot `parent_sync` override, capacity accounting, audit
evidence shape/attribution, CSRF/auth/no-GET-mutation) against the actual
code and against the exact reachable state space (schema CHECK constraints),
not just the docstrings.

**Verdict: APPROVED, no code defects.** No P0/P1/P2 correctness, security,
durability or lifecycle bug found in either family (PH7-01 expiry ops,
PH7-05 Disable/Enable) or in `parent_sync.enqueue_current_children`'s new
per-slot pause override. Specifically verified by reading the code (not
just trusting green tests):

- **No lost update.** `SubscriptionAdminOpsStore.apply_adjustment` and
  `SubscriptionRenewalStore` (Stars/manual renewal) both CAS on the exact
  same `mgboost_subscriptions.row_version`, re-read fresh inside their own
  transaction — a concurrent admin +30 vs. manual renewal, admin -N vs.
  Stars renewal, or two admin mutations in flight can never silently
  overwrite each other; the loser gets a loud 409/`AdminExpiryConflict`.
- **No resurrection / no sibling collateral damage.** Verified both by
  reading `enqueue_current_children`'s per-child override (narrows only the
  slot whose OWN `desired_state='DISABLED'`) and empirically via
  `test_pause_survives_expiry_adjustment_and_later_sync_cycles`: a paused
  child's remote `expire` stays byte-identical across a real later
  extension while its sibling receives the new expiry.
- **Convergence is live-state-only**, never a hash-replay shortcut — the
  exact a68e265 P0 defect class stays structurally impossible here; Disable
  -> Enable -> Disable performs a real second remote disable.
- **Guards are generation/intent-scoped**, never slot-lifetime-scoped
  (confirmed against every reachable `desired_state` value under the schema
  CHECK, not just the tested paths).
- **No second audit framework.** Both new operation kinds
  (`ADMIN_EXPIRY_ADJUSTMENT`, `SLOT_DISABLE`/`SLOT_ENABLE`) land in the
  unmodified `mgboost_entitlement_mutations` ledger and render through the
  existing generic (non-allow-listed-by-kind) timeline projector with zero
  code changes; no raw secret in evidence JSON, confirmed by
  `test_new_mutations_surface_in_existing_timeline_without_secrets`.
- **Security:** every new route is POST-only, behind
  `require_admin_auth` (session + CSRF) and `require_primary_capability`
  where it mutates; mandatory reason (3..300) + explicit `confirm:true` on
  every mutating dialog per DL-055; frontend never computes expiry/state,
  only renders server preview values.

**One genuine product ambiguity found and escalated to the owner rather than
silently resolved (per this session's explicit brief):**
`DeviceSlotStore.rebind()` does not check `desired_state` before setting it
to `ACTIVE`, so Rebind on a currently-DISABLED (paused) slot silently drops
the pause and starts the new generation active. This is real, reachable, and
already exercised by GLM's own
`test_rebind_after_disable_successor_starts_enabled_and_stale_enable_refused`
— but no DL/ADMIN-UX text had explicitly ruled on this interaction, so it
was an unvalidated assumption, not a fixed policy. Put to the owner directly;
**resolved as DL-056: keep the current behavior (pause is consumed by
Rebind), no code change.** Documented in `ROADMAP.md`.

**Tests independently reproduced, not trusted from the self-report:**
targeted `tests/test_admin_operational_admin.py` **39 passed**; browser E2E
(Playwright venv) **2 passed**. Full regression's first run hit the exact
documented `/tmp` scratch-quota-exhaustion class (`disk I/O error` /
`Превышена дисковая квота` from ~5,180 stale anonymous `/tmp/tmp*`
mkdtemp dirs, ~5.3 GB); diagnosis confirmed against the known precedent
before cleaning (only hour-stale anonymous `/tmp/tmp*`, venv/caches/repo
untouched); the identical command then passed deterministically: **1266
passed, 0 failed, 0 skipped** — matching GLM's own claimed count exactly.
`git diff --stat f7ea7f4..dec28f5 -- '*schema*' '*migration*'` empty, and
`database.py`'s diff is wiring-only (two new store constructions, no new
table/migration) — deploy is application-code-only, same class as every
prior slice.

## Agent comparison evidence (GLM-5.3-Flash as implementation agent, third
data point)

- **Substantive code defects:** 0. Zero files/lines needed a code fix.
- **Product-ambiguity escalations GLM should have made but didn't:** 1 (the
  Rebind-after-Disable interaction) — GLM implemented, tested and disclosed
  it in the UI text, but recorded it as settled design in `AGENT_HANDOFF.md`/
  `ROADMAP.md` prose rather than as an owner decision requiring a DL entry,
  unlike its own precedent of stopping for DL-054/DL-055-shaped questions.
  Once put to the owner, the owner kept GLM's exact behavior — so the
  underlying engineering call was right; the process gap was not routing it
  through an explicit DL before treating it as final.
- **Architecture/security/durability defects:** 0, across both new stores
  and the modified `parent_sync.enqueue_current_children`.
- **Would this reviewer trust GLM-5.3-Flash with the next large
  implementation slice, gated on mandatory independent review?** Yes — this
  is GLM's cleanest of three reviewed sessions (0 code defects, versus 1 P0
  in the a68e265 session and a resolved-by-STOP ambiguity in the PH5-09/10
  session); the one gap found here is a process discipline note (route
  cross-primitive ambiguities through an explicit DL like it already does
  for other product questions), not a correctness or security concern.

## Production rollout (2026-08-27), following this review's own approval gates

Fresh encrypted backup create/restore `PASS`
(`scripts/secure_db_backup.py`); preflight confirmed HEAD `f7ea7f4`,
`quick_check=ok`, 0 FK violations, cardinalities
`accounts=18/subscriptions=18/manual_payments=0/child_lifecycle_ops=20/
ownership_rebind_ops=0`, `SLOT_*`/`ADMIN_EXPIRY_ADJUSTMENT` rows `=0`, all 4
services active; pushed reviewed HEAD (`78588bc` — the checkpoint plus this
review's DL-056/verdict documentation, no code changes) to `origin/main`;
`git pull --ff-only` on production to `78588bc`; `systemctl restart
mgboost-panel` only (no schema/migration in the diff, independently
confirmed); post-deploy: `quick_check=ok`, 0 FK violations, every
cardinality above byte-identical before/after, all 4 services active, zero
errors/tracebacks in the journal since restart. Safe HTTP smoke via the
app's real `LISTEN_PORT=8001` (re-confirmed the documented gotcha that
nginx's public `panel.beykus.fun` default location proxies to Marzban's own
port 8000, not this app — not a new incident): unauthenticated
`/admin/accounts`/`/admin/dashboard` and the two NEW mutation routes
(`expiry/preview`, `devices/1/disable`) all `401`; bogus legacy `/sub` token
`404`; all 7 admin JS modules incl. the new `expiry_ops.js` and `/admin`
index `200`. Read-only direct-call verification (`Database()` +
`admin_read_models.account_detail` + `admin_audit_timeline.account_timeline`)
against 5 real production accounts ran without exception, zero raw secret
markers in any timeline. **No real expiry adjustment, device disable/enable,
revoke, free or rebind was created at any point** — every production touch
this session was read-only until the reviewed code deploy itself, which made
zero data-row changes. Final HEAD: local/origin/production all `78588bc`.

## Roadmap status set by this session

PH7-01 (admin expiry operations) → `[x]` — its Accept/tests bullets
(preview/reason, all children converge, 12-child scale, UNLIMITED/plan-less
refusal, concurrent-mutation CAS) are all genuinely covered and now
production-deployed. PH7-05 (device slot administration) stays `[~]` —
Disable/Enable are now production-deployed and reviewed-correct alongside
Revoke/Free/Rebind, but add/remove slots & restore-baseline remain unbuilt
(explicitly PH5-07/PH7-06 territory), so the task's own full Ops list is
still not satisfied. PH7-08 (immutable administrative audit trail) stays
`[~]` — both new families' write-side evidence is production-deployed, but
the unified emit point for every future operation kind (PH7-11) remains
unbuilt. DL-056 added to the Decision Log.

## Exact next step

Per the owner's own brief for this slice: with PH7-01/PH7-05 Disable-Enable/
PH7-08-for-these-families independently reviewed, DL-056 resolving the only
ambiguity, and production deploy verified with zero data mutation, **ADMIN
DONE can be declared for the scope this task defined** (operational admin
completion: expiry ops + reversible device pause + their write-side audit
evidence, on top of the already-deployed Revoke/Free/Rebind/manual-payments/
ownership-rebind/dashboard work). Explicitly out of this declaration and NOT
started: PH7-05's add/remove/restore-baseline, PH7-11's unified audit-emit
framework, WL enforcement/PH6, PH5-06 upgrade/downgrade, live Stars sales,
promo codes, PH4-06, the reseller system and cosmetic redesign — the next
phase per the owner's own instruction is **WL**, gated on a fresh explicit
owner decision to start it.

---

# PRIOR HANDOFF — Operational admin scope fully implemented locally (PH7-01 expiry ops + PH7-05 Disable/Enable + write-side audit for both); checkpoint commit pending independent review; production read-only preflight verified compatible; no push / no deploy this slice

Updated: 2026-08-27 (implementation session closing the remaining operational
admin tails before WL). **This top section supersedes everything below.**

## State after this session

- Local HEAD = checkpoint commit of this slice, exactly one commit ahead of
  origin. **origin/main and production both remain at
  `f7ea7f4`** (re-verified over SSH; note production is genuinely on the
  docs-only `f7ea7f4`). PUSH TO ORIGIN AND PRODUCTION DEPLOY ARE EXPLICITLY
  FORBIDDEN for this slice.
- **No schema migration of any kind** -- both new stores reuse deployed
  tables exclusively; the future deploy is application-code-only
  (`mgboost-panel` restart).

## Gap-audit result (what actually blocked ADMIN DONE at f7ea7f4)

1. PH7-01 `[ ]` -- admin expiry operations (+7/+30/+60, -N, exact date,
   end now; no WL reset): the last major Phase 7 gap per the prior handoff's
   own recommendation.
2. PH7-05 `[~]` -- exactly one missing subgroup: reversible Disable/Enable
   (no standalone backend primitive existed); add/remove slots &
   restore-baseline deliberately NOT started (PH5-07/PH7-06 territory --
   starting them would be a product phase beyond this brief).
3. PH7-08 `[~]` -- write-side emission of durable actor/reason/before-after
   evidence for these NEW mutation kinds into the existing timeline. The
   unified emit point for future kinds stays `[ ]` under PH7-11 exactly as
   ROADMAP states.
Nothing else blocks operational admin; WL enforcement/PH6, live Stars sales,
promo codes, PH5-06 upgrade/downgrade, a tariff engine, PH4-06 and cosmetic
redesign were not touched.

## What was built (over proven primitives only, no second engine)

- **Expiry ops (PH7-01)** -- `src/subscription_admin_ops.py::
  SubscriptionAdminOpsStore`, routes `POST /admin/accounts/{id}/expiry/
  preview|adjust` (`src/routes/admin_expiry.py`). +N reuses DL-044's exact
  anchor via the existing `compute_new_expiry` (expired resumes from now);
  -N / exact bounded UTC second / END_NOW; ONE optimistic-CAS update of ONLY
  `current_expiry` per op (`row_version` detects concurrent Stars/manual
  renewals -> loud 409, never overwrite); refuses UNLIMITED and plan-less
  UNKNOWN_LEGACY; never touches terms/WL periods/packages; child convergence
  via existing `run_account_sync_cycle`; immutable evidence row in the
  EXISTING `mgboost_entitlement_mutations` ledger
  (`ADMIN_EXPIRY_ADJUSTMENT`, mutation_source=ADMIN, actor/reason/
  before-after). Key replay honestly reports the ORIGINAL result with
  `already_applied=true` (payments precedent); distinct keys are separate,
  compensable audited adjustments -- that IS the roadmap's rollback story;
  no raw DB-edit expiry path exists anywhere.
- **Slot pause (PH7-05 Disable/Enable)** -- `src/device_slot_admin.py::
  DeviceSlotAdminStore`: the ONLY writer of the schema-blessed slot value
  `desired_state='DISABLED'` (present in the PH3-02 CHECK since day one;
  prod DDL re-verified in preflight). Routes `.../devices/{n}/disable|
  enable|sync`. Remote effect strictly the typed `child.user.state.sync`:
  flag + evidence row + forced PH3-08 desired-state revision bump commit in
  ONE transaction, and `parent_sync.enqueue_current_children` derives each
  child target from its OWN slot row inside every enqueue (structural
  join-level override shaped like the REVOKED exclusion) => no later
  renewal/expiry/parent transition can resurrect a paused device, stale ops
  supersede structurally, Enable restores the SAME generation/UUID narrowed
  by parent state, capacity keeps counting paused slots, Free after Revoke
  works on paused slots (`DeviceSlotStore.release` CAS widened to
  ACTIVE|DISABLED), Rebind consumes the pause and starts its successor
  enabled. Guards scope to the CURRENT generation/intent (never slot
  lifetime). Convergence/replay decided from LIVE state inside the txn (see
  self-review below). `/devices/{n}/sync` retry route drives the same cycle
  for crash windows without inventing mutations.
- **Audit write-side (PH7-08 scope for these families)** -- evidence rows
  carry sealed-capability actor_ref, mandatory reason (3..300), bounded
  scalar before/after; existing Audit tab renders them with ZERO timeline
  changes needed. DL-055 records the owner-instruction resolution (mandatory
  reason+confirm apply to pause as well, superseding ADMIN-UX-02's lighter
  no-reason note).
- **UI** -- account-centric vanilla ES modules kept: new
  `frontend/assets/admin/expiry_ops.js` (server-preview-driven dialog with
  presets +7/+30/+60, custom ±N, datetime-local exact date, end-now; one
  idempotency key minted per opened dialog), extended `device_ops.js`
  (Приостановить/Возобновить dialogs: consequences + mandatory reason + ack
  checkbox; «Sync…» shown only when desired!=observed), `accounts.js`
  subscription tab gains the expiry card; CSP/no-inline/event-delegation
  gate-tested; raw identifiers stay Technical-only.

## Self-review finding fixed BEFORE commit (flagged for the reviewer)

The first internal draft decided replay/convergence from the deterministic
idempotency-key hash alone (revoke-route style). Self-review identified this
as an instance of the EXACT false-convergence class the a68e265 review
caught: Disable(k) -> Enable -> Disable(k again -- deterministic keys repeat)
would have answered `converged:true` against an ACTIVE slot. Redesign: the
store decides convergence ONLY from the slot's live state read inside its own
transaction (target state already held => honest converged no-op with zero
writes); the client key is advisory first-occurrence metadata in the evidence
row; regression added asserting the full flip-flop performs a REAL second
remote disable with its own evidence row.

## Verification performed

- Targeted `tests/test_admin_operational_admin.py`: **39 passed** (was 24;
  +15): authz matrix incl. all new routes, IDOR/slot probes, roundtrip with
  SAME uuid verifier/generation preserved and capacity unchanged, double-
  submit converged without duplicate evidence, pause survival across repeated
  sync cycles AND a real post-pause expiry extension (sibling advances,
  paused child untouched remotely), crash-between-durable-flag-and-remote-
  sync recovery via the sync route, Rebind-after-Disable successor starts
  enabled + stale Enable honesty, Revoke+Free on a paused slot with tombstone
  history intact, availability truthfulness (disable/enable/free gates),
  timeline evidence surfacing without secrets, expiry anchors active vs
  expired, reduce-into-past disabling children then resume-from-now, END_NOW
  exactness with byte-identical WL-period rows, SET_EXACT rounding-free
  equality, validation matrix, UNLIMITED / missing-subscription refusals,
  idempotent replay without duplicate evidence, audited compounding (+7 twice
  = separate rows, chain exact), and a 12-child FAMILY-account convergence
  sequence.
- Browser gates (Playwright venv): **2 passed** (fixture extended: serves
  expiry_ops.js, enriched payload; asserts pause+revoke buttons render,
  confirm gating fires locally with zero network requests, expiry card shows
  server-authoritative presets; end_now/enable counters).
- Full regression (Playwright venv, every suite included): **1266 passed,
  0 failed, 0 skipped** (= prior recorded full baseline 1251 + exactly these
  15). One transient environment event: the FIRST venv run showed escalating
  collection-time ERRORs, root-caused again to `/tmp` quota pressure from
  thousands of stale prior-session mkdtemp scratch dirs; cleaned per the
  owner-approved precedent (only hour-stale anonymous `/tmp/tmp*`; venv/
  caches/repo untouched) after which the identical command passes
  deterministically.
- `git diff --check` clean; every touched JS module passes `node --check`.
- **Production read-only compatibility preflight (SSH only; ZERO writes,
  restarts or mutations):** HEAD `f7ea7f4` == local == origin; only the known
  untracked `extra_configs.json` drift; `PRAGMA quick_check=ok`; 0 FK
  violations; cardinalities accounts/subscriptions/manual-payments/lifecycle-
  ops = `18/18/0/20` unchanged (20 = pre-existing PH3-05 canary rows);
  production DDL re-read literally confirms `CHECK(desired_state IN
  ('FREE','ACTIVE','DISABLED'))` on mgboost_device_slots, free-TEXT operation
  column plus UNIQUE(idempotency_key_hash) on mgboost_entitlement_mutations,
  row_version on mgboost_subscriptions, 14 entitlement_state rows for real
  accounts; `0` SLOT_*/ADMIN_EXPIRY_ADJUSTMENT rows exist (nothing was ever
  created by any session since those ops are brand new); current prod slots
  35 ACTIVE / 3 FREE; all four services active; panel journal clean. Deploy
  is therefore application-code-only with live-DB compatibility already
  proven.

## Reviewer attention list

1. `src/device_slot_admin.py::set_paused` -- live-state convergence rule vs
   the a68e265 P0 class; verify the guard set (REVOKED intent, missing
   generation/intent, pause requires ACTIVE desired, CAS on row_version AND
   current_generation) leaves no slot-history-shaped hole.
2. `_bump_parent_revision_locked` commits atomically WITH the flag flip;
   confirm enqueue can never stamp old-revision ops against the new per-slot
   reality (BEGIN IMMEDIATE serialization argument).
3. `subscription_admin_ops.apply_adjustment` -- CAS refusal maps to 409 via
   AdminExpiryConflict; replay reports original result + already_applied and
   never claims CURRENT state; check there is no silent double-apply path.
4. `parent_sync.enqueue_current_children` per-slot override: paused =>
   ("disabled", None), siblings keep parent target exactly; aggregate_state
   logic untouched.
5. `DeviceSlotStore.release` CAS widened to ACCEPT disabled; hard
   Free-after-Revoke ordering remains solely in `apply_free`.
6. Reason-before-confirm ordering (400 vs 409) asserted in tests.

## Roadmap statuses set

- PH7-01 stays `[ ]` but entry text now says "Implemented locally ...
  pending independent review + production deploy".
- PH7-05 stays `[~]` with updated text: Disable/Enable implemented locally;
  add/remove/restore-baseline remain explicitly unbuilt (PH5-07/PH7-06).
- PH7-08 stays `[~]` noting the two new families emit write-side evidence
  through the existing ledger/timeline; unified framework remains `[ ]`
  under PH7-11.
- DL-055 added to the Decision Log.

## Exact next step

Independent review of this checkpoint against `origin/main` (`f7ea7f4`),
then owner deploy decision. After review+deploy, per this slice's own brief:
ADMIN DONE can be declared and work proceeds to WL; do NOT start PH5-06,
promo codes/trials, live Stars sales flow, a new tariff engine, PH4-06, the
reseller system or cosmetic redesign without a fresh explicit owner decision.

---

# PRIOR HANDOFF -- Operational admin completion independently reviewed
Updated: 2026-08-27 (independent review session, following directly from the
implementation session recorded below at checkpoint `a68e265`). **This top
section supersedes everything below.** Owner instruction: independently
review `a68e265` against `origin/main`/production (`9d6ef28`), fix only real
findings, add regression coverage, then decide on production rollout -- do
not start any new phase afterward.

## What this session did

Read `AGENT_HANDOFF.md`/`ROADMAP.md`/`CHANGELOG.md` (PH7-01..12, DL-048..054,
OPD-39) before touching anything; confirmed local HEAD `a68e265` was one
commit ahead of `origin/main`/production (`9d6ef28`), working tree clean,
production still read-only at `9d6ef28` with only the known untracked
`extra_configs.json`. Reviewed the full `9d6ef28..a68e265` diff (21 files,
+3663/-20) across four parallel independent passes (manual payments +
child-sync mapping; device lifecycle + the REBIND-guard hotspot GLM itself
flagged; ownership rebind + credential boundary + audit-timeline scrubber;
read-models + dashboard + frontend + authz), each cross-checked against the
relevant already-production-deployed precedent (PH5-05 Stars state mapping,
PH3-05 lifecycle primitives, OPD-39/DL-041 policy, DL-040 RUB catalog,
DL-048 technical-identifier depth).

**Verdict: APPROVED WITH FIXES.** Two real, independently-verified-by-reading-
the-code (not just trusting a green test) defects, both fixed with regression
coverage before deploy; everything else reviewed clean:

- **P0** — `src/routes/admin_devices.py`: the route-level `_existing_slot_op`
  guard matched the latest lifecycle op of a kind by `slot_number` alone,
  independent of which generation/intent it was recorded against, while the
  underlying `ChildLifecycleStore._prepare` primitive was already correctly
  scoped by `old_child_intent_id` (confirmed by reading `_prepare` directly,
  `src/child_lifecycle.py:99-111`). Consequences, both empirically reproduced
  before fixing: (1) REVOKE on a slot's current generation, issued after an
  earlier generation on the same slot had already been revoked, matched the
  OLD generation's APPLIED REVOKE row and returned `converged: true`/HTTP 200
  without ever touching the current (possibly compromised) generation — a
  false confirmation that an active device had been revoked; (2) REBIND
  permanently refused any second rebind of the same slot after the first one
  ever completed, with no recovery path other than a direct DB write — this
  contradicts DL-049's own "replacement flow" scoping (nothing in the policy
  limits Rebind to once per slot's lifetime) and was independently determined
  to be a scoping bug, not a deliberate product ambiguity requiring an owner
  STOP. Fixed by scoping `_existing_slot_op` to the current intent's
  `old_child_intent_id`, matching the primitive's own idempotency scope; true
  concurrent double-click safety is unaffected (it was always provided by
  `_prepare`'s own idempotency-key-hash dedup, already covered by
  `test_repeated_rebind_request_is_idempotent_exactly_one_x_plus_1` in
  `tests/test_child_lifecycle.py`, unrelated to this route-level guard).
- **P2** — `src/admin_audit_timeline.py`: 7 of 8 SQL sections in
  `account_timeline()` had no exception guard (only manual payments did), so
  a single anomalous evidence row anywhere would raise out of
  `account_timeline()` — called unconditionally by `account_detail()` — and
  take down the *whole account detail page* (Overview/Devices/everything),
  not just the Audit tab. Fixed with a shared `_rows()` helper so every
  section degrades independently.

Everything else reviewed clean, including GLM's own review-hotspot list:
child-sync mapping in the payments apply route (`_drive_child_sync_once`) is
a byte-for-byte structural copy of the already-production-reviewed PH5-05
`stars.py::_sync_canonical_purchase_children` state mapping; the timeline
secret scrubber is not a naive deny-list-over-`**dict` (every SQL query is an
explicit column allow-list, the JSON-flatten path additionally type-filters
to bounded scalars) and leaked no live secret against 5 real production
accounts post-deploy; `_device_action_availability` is confirmed presentation-
only everywhere it's used (mutation routes independently re-validate
lifecycle state server-side); HTTP 409 mapping only reclassifies *within*
already-typed store exceptions, never an unexpected bare exception; ownership
rebind CAS/mandatory-reason/COMPROMISE-triggers-real-credential-rotation all
verified by reading the code, not just the docstring; Disable/Enable's
absence was independently re-confirmed as a genuine missing backend
primitive (grepped every writer in the schema/lifecycle modules), not an
oversight — no new lifecycle was invented. Three non-blocking P3 notes on the
payments side (`handle_manual_payment_sync`'s docstring vs. its actual
PENDING-only scope, the sync-retry route having no UI button yet, one route
test accepting SYNCED-or-PENDING instead of forcing a deterministic
convergence) are recorded in `ROADMAP.md`'s PH7-10 entry for the future
PH7-11/compensation slice, not fixed here — none are blockers.

**Tests:** targeted `tests/test_admin_operational_admin.py` 24 passed (was
22, +2 regression tests for the P0/P2 fixes, one existing test corrected
because it had encoded the P0 bug's behavior as expected). Browser E2E
(Playwright venv) 2 passed. Full regression: **1251 passed, 0 failed, 0
skipped** (own count, independently reproduced — GLM's claimed baseline of
1249 verified correct before adding the 2 new tests). No schema/migration in
this diff, independently confirmed (`git diff --stat 9d6ef28..a68e265 --
'*schema*' '*migration*'` empty, no `CREATE TABLE`/migration registration in
any new route file) — deploy is application-code-only, same class as prior
UI slices.

**Production rollout (2026-08-27), following this review's own approval
gates:** fresh encrypted backup create/restore `PASS`
(`/root/mgboost-preop-admin-backup`); preflight confirmed HEAD `9d6ef28`,
only the known untracked `extra_configs.json` drift, `quick_check=ok`, 0 FK
violations, cardinalities `18/18/0` (accounts/subscriptions/manual payments),
`mgboost_ownership_rebind_operations=0`, all 4 services active; pushed
reviewed HEAD (`1854bb9`) to `origin/main`; `git pull --ff-only` on
production to `1854bb9`; `systemctl restart mgboost-panel` only (no
migration to self-apply, confirmed above); post-deploy: `quick_check=ok`, 0
FK violations, cardinalities unchanged (`accounts=18, subscriptions=18,
manual_payment_records=0, child_lifecycle_operations=20` [pre-existing
PH3-05 canary rows, not created by this deploy], `ownership_rebind_
operations=0, stars_invoices=2`); zero errors/tracebacks in the
`mgboost-panel` journal since restart; safe HTTP smoke unchanged
(`/admin/accounts`/`/admin/dashboard` 401, bogus legacy `/sub` 404); all 6
new/changed admin JS modules (`device_ops.js`/`modals.js`/`payments.js`/
`timeline.js`/`accounts.js`/`core.js`) and `index.html` load 200; read-only
direct-call verification (`Database()` + `admin_read_models.account_detail`
+ `admin_audit_timeline.account_timeline` + `admin_read_models.
dashboard_summary`, no HTTP session forged, matching this project's own
established read-only-verification precedent) against 5 real production
accounts ran without exception and confirmed `account_timeline` — unlike
`account_detail`'s intentionally-technical Technical-tab payload — carries
zero `mgc_`/`sha256:`/`hmac-sha256:`/`Bearer ` markers. All four services
active throughout. **No real manual payment, device mutation, ownership
rebind or credential rotation was created at any point** — every production
touch this session was read-only until the reviewed-and-fixed code deploy
itself, and the deploy made zero data-row changes (all cardinalities
identical before/after). Final HEAD: local/origin/production all `1854bb9`.

**Roadmap status set by this session (never inflating partial acceptance to
`[x]`):** PH7-10 (manual external-payment admin UI) → `[x]` — its own
Accept/tests bullets are all genuinely covered (server catalog/price
authority, same-plan-only enforcement, IDOR-safe preview/create, IDOR-safe
edit/cancel/apply, DL-054 reuse rejection, auth matrix, no raw secrets).
PH7-05 (device slot administration) stays `[~]` — Revoke/Free/Rebind are now
production-deployed and reviewed-correct, but Disable/Enable/add/remove/
restore-baseline remain unbuilt, so the task's own full Ops list is not yet
satisfied. PH7-08 (immutable administrative audit trail) stays `[~]` — the
read-side aggregate is production-deployed and reviewed-correct, but the
complete write-side emit-coverage goal (a correlated unified audit framework
covering every mutation kind, PH7-11) remains explicitly `[ ]` and out of
this session's scope.

## Agent comparison evidence (GLM-5.3-Flash as implementation agent, second
data point)

Requested by the owner as a second controlled comparison of GLM-5.3-Flash
against Claude's own implementation quality, using this independent review as
the evaluation instrument (not inflated, not deflated):

- **Substantive defects found:** 2 (both fixed). Maximum severity: **P0**
  (false-positive revoke confirmation — an operationally serious defect, a
  destructive-action UI reporting success without performing the destructive
  action on the intended target, though not itself a data-loss or auth-bypass
  bug and confined to one narrow sequence: revoke → rebind → revoke again on
  the same slot).
- **Architecture defects:** 0. No second billing/payment/entitlement/audit
  engine was introduced anywhere in 21 files and ~3700 new lines; every
  mutation route is a thin auth/validation/orchestration layer over
  already-production-reviewed primitives (PH5-09/10, PH5-02/03/04, PH3-05,
  PH3-08, OPD-39/DL-041, PH4-04). This is the harder property to get right at
  this scope and GLM got it right throughout.
- **Security defects:** 0 exploitable. Authorization (session+CSRF+sealed
  primary-admin capability), IDOR-safety, bounded inputs, and the secret
  boundary (raw bearer/HWID/UUID verifier never reaching a client-facing
  payload outside Technical) were all independently verified correct across
  every new route and the audit-timeline scrubber.
- **Durability/lifecycle defects:** 1 (the P0 above) plus 1 minor robustness
  gap (the P2 timeline resilience issue — not a lifecycle-correctness bug,
  but availability of a diagnostic surface).
- **Frontend defects:** 0. CSP/no-inline/event-delegation discipline held;
  no client-side entitlement/price computation; no raw secret rendering.
- **Production-relevant lines/files actually changed by this review:** 4
  files, 195 insertions / 28 deletions (`src/admin_audit_timeline.py`,
  `src/routes/admin_devices.py`, plus test infrastructure) — roughly 5% of
  the reviewed diff's size, concentrated in exactly the two real findings.
- **GLM's own tests were insufficient exactly where the P0 lived:** its own
  rebind test (`test_device_rebind_requires_confirmation_creates_new_
  generation`) asserted a *second* rebind attempt returns 409 — i.e. it wrote
  a test that encoded the bug's own behavior as the expected, intended
  outcome, rather than testing the actual product requirement. No test
  exercised "revoke → rebind → revoke the new generation," the exact
  sequence that exposed the false-convergence defect.
- **GLM's own listed review hotspots were substantially accurate self-
  assessment:** it correctly flagged the REBIND guard as strict/worth a second
  look (right instinct, wrong self-diagnosis — it described the behavior as
  intentional replay protection rather than recognizing the slot-vs-intent
  scoping bug); its child-sync-mapping, `_device_action_availability`,
  timeline-scrubber, HTTP-409-string-mapping and `/tmp` hotspots were all
  independently re-examined and found to be correctly implemented, i.e. GLM
  correctly identified where the real risk in this diff was concentrated even
  where it didn't itself find every defect there.
- **Is `a68e265` a quality independent implementation?** Yes, with one
  qualification: architecture, security boundary, and 5 of 6 reviewed domains
  were correct and required zero changes; the one P0 found is a real,
  narrow-scope idempotency-guard scoping error of exactly the kind an
  independent review exists to catch, not a sign of broad unreliability.
  Combined with the first PH5-09/10 review's finding (no code fix needed
  there, only a genuine product-ambiguity STOP resolved as DL-054), GLM's
  two-session track record on this codebase is: strong architectural
  discipline and honest scope boundaries (Disable/Enable, PH7-11, PH5-06 all
  correctly left unbuilt/`[ ]` both times), with narrow, findable-by-review
  correctness gaps at the edges of destructive-operation idempotency.
- **Would this reviewer trust GLM-5.3-Flash with the next large
  implementation slice, gated on mandatory independent review before any
  critical production deploy?** Yes — the pattern across two sessions is
  consistent enough (sound architecture, honest scoping, isolated
  destructive-action-idempotency defects) that the standing "implement, then
  independently review, then deploy" workflow this project already uses is
  the correct match for this agent's demonstrated risk profile, not a reason
  to change how implementation work is assigned.

## Exact next step

PH7-10 is closed `[x]`. PH7-05/PH7-08 remain `[~]` — genuinely reviewed and
production-correct for the slice each shipped, but each has explicitly
out-of-scope remaining work (Disable/Enable + add/remove/restore-baseline for
PH7-05; the unified write-side audit framework, PH7-11, for PH7-08). Do not
start PH5-06/07/08, live Stars purchase flow, promo codes/grants/trial,
PH6-05..09, WL enforcement, PH7-01 expiry ops, a new Disable/Enable backend
lifecycle, PH4-06, or the reseller system without a fresh explicit owner
decision — none of this session's findings create a new blocker on any of
them, and none of them were touched. **Recommended next action:** owner
decides between PH7-01 (expiry operations — the last major Phase 7 gap
against real day-to-day support work) and starting the PH5-06 upgrade/
downgrade design session (currently the single biggest "explicitly blocked"
message users of this admin panel will see, per PH7-10's own
`PLAN_SWITCH_REQUIRES_PH5_06` preview block).

---

# PRIOR HANDOFF — Operational admin completion implemented and fully tested locally (checkpoint commit, pending independent review); production NOT touched / no deploy this slice

Updated: 2026-08-27 (new implementation session). **This top section
supersedes everything below.** Owner instruction: complete the operational
admin panel over already-proven backend primitives (Wave A consolidation +
PH7-10 manual payments + Wave B device ops + ownership rebind + audit
timeline + dashboard queues), then STOP; the next reviewer must diff local
vs `origin/main` before any deploy decision.

## State after this session

- **Local HEAD = checkpoint commit of this slice; `origin/main` and
  production both remain at `9d6ef28`** (verified via `git rev-parse`
  locally/over SSH before writing this). PUSH TO ORIGIN AND PRODUCTION DEPLOY
  ARE EXPLICITLY FORBIDDEN for this slice.
- Working tree before commit contained only:
  - New backend: `src/admin_audit_timeline.py` (read-only unified timeline),
    `src/routes/admin_support.py` (auth/body/service-client helpers),
    `src/routes/admin_payments.py`, `src/routes/admin_devices.py`,
    `src/routes/admin_ownership.py`.
  - Changed backend: `src/server.py` (route registration only),
    `src/admin_read_models.py` (entitlement block, payment summaries,
    per-slot device-action availability, dashboard queues).
  - Frontend: new ES modules `modals.js`/`payments.js`/`device_ops.js`/
    `timeline.js`; extended `accounts.js`, `core.js`, `admin.css`,
    `index.html` (Payments/Audit tabs). Vanilla/no-framework per DL-052;
    CSP/no-inline/event-delegation preserved and gate-tested.
  - Tests: `tests/test_admin_operational_admin.py` (22 checks),
    `tests/_ops_helpers.py` (shared builders, not collected by pytest),
    extended `tests/test_admin_browser_e2e.py` (+1 full browser gate).
- No schema/migration of any kind: every production table used was already
  deployed through PH3-05/PH2-05/PH4-04/PH5-09. Future deploy is application-
  code-only (`mgboost-panel` restart), same class as prior UI slices.

## Gap-audit → wired vs deliberately not

Reused verbatim (no second engine): PH5-09/10 `ManualPaymentStore`; PH5-02
renewal + DL-044 formula via the store; PH5-04 calculation surfaced directly;
child-sync driven through the existing PH3-08 `run_account_sync_cycle` with
the PH5-05 Stars state-mapping (`pending_sync_jobs`/`record_sync_result`);
PH3-05 lifecycle (`prepare_*`+`process_*`, slot release/rebind); OPD-39/
DL-041 ownership rebind incl. COMPROMISE credential rotation; PH4-04
credential issue flow surfaced unchanged inside Subscription.
Deliberately unavailable (visible explanation in UI instead of fake controls):
any non-same-plan purchase / upgrade / downgrade (PH5-06 absent — preview
blocks with `PLAN_SWITCH_REQUIRES_PH5_06`), extending an admin-granted
UNLIMITED subscription, WL packages on non-WL real plans, Disable/Enable
device (no standalone primitive exists yet), expiry editing (PH7-01 not
started), WL enforcement (PH6-06..09), promo/trial/live Stars/reseller.

## Verification performed

- Targeted routes file `22 passed`; browser gates `2 passed`; static security
  gate green (it caught one internal listener named like an inline handler and
  forced a rename — the gate remains load-bearing for new modules).
- **Final full regression from repo root with the Playwright venv, all suites
  included: `1249 passed`, 0 failed / 0 skipped** (baseline before this slice
  was `1210 passed + 16 deselected` non-browser and 16 browser cases via
  importorskip; delta is exactly this slice's new tests). One earlier run had
  shown transient collection-time ERRORs in `test_wl_usage_ledger_schema*`;
  root cause was `/tmp` disk-quota pressure from thousands of stale prior-
  session `mkdtemp` scratch dirs (the exact failure mode recorded in a
  previous handoff), not the diff — after removing only hour-stale anonymous
  `/tmp/tmp*` scratch dirs (venv/caches/product files untouched) the complete
  suite passes deterministically; verified by re-running that file both in
  isolation (`11 passed`) and within the full run above.
- Production read-only preflight over SSH only: HEAD `9d6ef28`, known
  untracked `extra_configs.json` drift untouched, `quick_check=ok`, zero FK
  violations, accounts/subscriptions/manual-payment records = `18/18/0`,
  lifecycle/rebind/credential event tables present, all four services active.
  NO write/restart/mutation of any kind against production; nginx untouched.

## Reviewer attention list

1. `src/routes/admin_devices.py`: REBIND guard is slot-level and deliberately
   strict — after an APPLIED rebind a different-HWID request is refused until
   the successor generation exists; ERROR-state ops require manual
   reconciliation (same precedent as the PH3-05 canary discipline).
2. Apply-route inline child-sync drive mirrors the canonical Stars mapping —
   confirm SYNCED cannot be recorded on partial convergence.
3. `_device_action_availability` is presentation-only; verify it never
   advertises an action the server would refuse.
4. Timeline secret-scrubbing uses a deny-list marker approach on top of
   column-level selection — check for bypasses.
5. Payment lifecycle denials map to HTTP 409 via store-message markers
   (non-invasive; alternative would be store-side exception subclasses).
6. This session cleaned only stale prior-session test-scratch dirs under
   `/tmp` (`/tmp/tmp*` older than an hour plus this session's own named
   prefixes); venvs/caches/repo untouched — same owner-approved precedent as
   the previous handoff.

## Exact next step

Independent review of this checkpoint against `origin/main`. After approval +
owner deploy decision this slice ends; do NOT start PH5-06/07, PH5-08,
PH6-05..09/enforcement, promo codes/trials/live Stars wiring, PH4-06/final
legacy revoke, PH7-01 expiry ops or Phase 8 items without a fresh explicit
owner decision.

---

# PRIOR HANDOFF — PH5-09 + PH5-10 independently reviewed and production-deployed; dormant (no admin UI/route/bot wiring yet) / PH4-06 not started

Updated: 2026-08-27 (independent review session, following the implementation
session below). **This top section supersedes everything below.** Owner
instruction: independently review checkpoint `af1effe` against
`origin/main`/production (`a5c846b`), fix only real findings, then decide
on production rollout.

## What this session did

Read `AGENT_HANDOFF.md`/`ROADMAP.md`/`CHANGELOG.md` and the exact DL-029..044
contracts before touching anything; confirmed local HEAD `af1effe` was one
commit ahead of `origin/main`/production (`a5c846b`), working tree clean,
production still read-only at `a5c846b`. Reviewed the full diff
(`src/manual_payment.py`, `src/manual_payment_schema.py`, both test files)
line by line against PH5-02/03/04, PH3-08/09 and every relevant DL.

**Verdict: APPROVED, no code fix required.** Findings:
- Architecture: confirmed no second entitlement/renewal/usage/child-sync
  engine exists -- every mutation goes through the existing PH5-02
  `apply_same_plan_purchase`, PH5-03 `grant_paid_package`, PH5-04
  `calculate`, PH3-08 `run_account_sync_cycle`, PH3-09 `record_payment`.
  The `mgboost_manual_payment_sync_jobs` bookkeeping table mirrors the
  already-shipped PH5-05 `stars.py::_sync_canonical_purchase_children`
  pattern exactly (a local per-payment-type job index driving the *same*
  outbox function), not a second sync mechanism.
- Unit semantics: `expected_amount_minor`/`recorded_amount_minor` store
  whole RUB units (e.g. `169` = 169 ₽), matching PH5-01's own
  `RUB_PRICES`/`mgboost_plan_prices.amount` and PH3-09's identically-named
  `amount_minor` field -- a pre-existing repo-wide naming convention, not a
  unit bug; traced the full chain catalog seed -> lookup -> manual payment
  snapshot -> provenance -> package grant and found no 169-vs-16900-shaped
  mismatch anywhere.
- Applied-immutability without a compensating-operation engine matches this
  entry's own explicit v1 scoping (no compensation engine exists yet
  anywhere in the codebase; a fake path was correctly not built).
- Crash/idempotency: traced all four crash boundaries (pre-renewal,
  renewal-committed/pre-bookkeeping, bookkeeping-committed/pre-sync-hand-off,
  partial child sync) through the code; every layer has its own
  deterministic per-record idempotency key so a retry at any point converges
  without ever adding duration twice. Replay-after-later-independent-renewal
  correctly uses `>=` instead of exact equality (DL-044/PH5-05 precedent).
- **One real product ambiguity found:** `external_reference` was made
  permanently UNIQUE (including after `CANCELLED`), and no prior DL had
  fixed whether cancellation should free it for reuse or what scope the
  uniqueness should have. Raised to the owner rather than decided
  unilaterally; owner resolved it as **DL-054**: reference stays reserved
  forever, current `UNIQUE(external_reference)` confirmed correct (it is
  the exact equivalent of the codebase's established
  `UNIQUE(payment_channel, external_reference)` precedent, since this
  module's channel is invariant). No code change was needed.
- Documentation: `ROADMAP.md` had PH5-09/10 marked `[x]` (this project's
  convention for production-deployed) while its own text said "NOT
  deployed" -- corrected to `[~]` during review, then back to `[x]` only
  after the real production verification below completed.

**Tests:** targeted `47 passed` (unchanged from the implementing session).
Full non-browser regression re-run this session: `1210 passed, 16
deselected`, zero failures.

**Production rollout (2026-08-27), owner-approved after the review verdict
and the DL-054 decision:** fresh encrypted backup create/restore `PASS`
(`/root/mgboost-preph509-backup`); preflight confirmed HEAD `a5c846b`, only
the known untracked `extra_configs.json` drift, `quick_check=ok`, 0 FK
violations, cardinalities `18/18/0/0/0`, all 4 services active; pushed
reviewed HEAD (`3320bd1`/`5cbee5c`) to `origin/main`; `git pull --ff-only`
on production to `5cbee5c`; `systemctl restart mgboost-panel` only (additive
migration self-applies on `Database()` construction); post-deploy: all four
parent-migration checksums (PH3-01/PH5-01/PH3-09/PH5-03) verified
byte-identical to production both before and after; new migration
`ph5_09_manual_payment_v1` present with checksum
`e3d453176428cffd73243096fc857b7c89933a4a1ad908cad18cbae151ac7223`,
identical to what current source computes; all 4 new tables and 6
immutability triggers present; `quick_check=ok`, 0 FK violations;
cardinalities unchanged `18/18/0/0/0`; all 4 new manual-payment tables `0`
rows (no real manual payment was created); legacy `stars_invoices`
unchanged at `2` rows; all four services active; `mgboost-panel` journal
since restart shows zero errors/tracebacks; safe HTTP smoke unchanged
(`/admin/accounts`/`/admin/dashboard` `401`, bogus legacy `/sub` `404`). No
admin route/UI/bot wiring was added, no real manual or Stars payment/
callback was ever created or mutated. Final HEAD: local/origin/production
all `5cbee5c`.

## Exact next step

PH5-09/10 are now genuinely production-deployed but still dormant --
**unblocked**: the owner-gated admin manual-payment mutation wave (route/
UI/bot wiring) can now be built as its own explicit next step. Per the
owner's own standing instruction, do not start PH5-06, PH5-07, PH5-08,
promo codes/trials, PH7-09/10/11, PH6-05..08, WL enforcement, PH4-06, or
final legacy revoke without a fresh explicit owner decision.

---

# PRIOR HANDOFF — PH5-09 + PH5-10 implemented and fully tested locally (checkpoint commit, pending independent review); production NOT touched / deploy+push forbidden this slice

Updated: 2026-08-27 (new implementation session). **This top section
supersedes everything below.** Owner instruction: implement PH5-09 then
PH5-10 sequentially in one slice, then STOP -- no further phase may be
started.

## State after this session

- **Local HEAD = checkpoint commit of this slice; `origin/main` and
  production both remain at `a5c846b`** (verified via `git rev-parse`
  locally/over SSH before writing this). PUSH TO ORIGIN AND PRODUCTION
  DEPLOY ARE EXPLICITLY FORBIDDEN for this slice: the next reviewer
  (Claude) must diff local vs `origin/main` independently.
- Working tree before commit contained only:
  `src/manual_payment_schema.py` (new), `src/manual_payment.py` (new),
  `src/database.py` (wiring: import + schema call + store attribute +
  `bind_database`), `tests/test_manual_payment_ph509.py` (new),
  `tests/test_manual_renewal_ph510.py` (new), plus ROADMAP.md /
  CHANGELOG.md / this file's documentation updates.
- No product ambiguity required a STOP: DL-029..040 already fix channel,
  actor, currency scope, price authority and edit lifecycle; the gap audit
  found every needed engine (PH5-02 renewal incl. the DL-044 formula,
  PH3-09 provenance writers, PH5-03 package grant/refund with
  EXTERNAL_PAYMENT support built in, PH5-04 proof calculation, PH3-08
  outbox + `run_account_sync_cycle`) and nothing was duplicated.
- Deliberately dormant: **no admin route/UI/bot wiring, no scheduler, no
  production manual payment exists.** Wiring is exactly what the future
  admin manual-payment mutation wave (owner-gated) will add; when it does,
  `pending_apply_records()` / `pending_sync_jobs()` +
  `record_sync_result()` mirror the canonical PH5-05 driver semantics
  (`src/stars.py::_sync_canonical_purchase_children`).

## What was built (details belong to ROADMAP.md PH5-09/PH5-10 entries)

Additive migration `ph5_09_manual_payment_v1` gated on exact
PH3-01/PH5-01/PH3-09/PH5-03 checksums (all verified byte-identical on
production during read-only preflight) adds four tables with trigger-level
immutability for applied facts and append-only edit history;
`ManualPaymentStore` records/pends/edits/cancels/applies manually
confirmed RUB plan payments (same-plan renewal through PH5-02, other-plan
fail-closed to MANUAL_REVIEW) and RUB WL packages (through the existing
PH5-03 grant/refund engines). Price authority is exclusively the pinned
versioned fixed RUB catalog rows; stale/retired versions keep their
contractual price and nothing reprices from current tables. Every
idempotency boundary is durable (UNIQUE key hash, UNIQUE external
reference across all statuses incl. cancelled, UNIQUE application link,
engine-level mutation keys); process-local locks are never a correctness
boundary. A compensating-operation engine does not exist anywhere yet --
applied records therefore have NO edit or cancel path instead of a fake
one.

## Verification performed

- Targeted: `tests/test_manual_payment_ph509.py` (33) +
  `tests/test_manual_renewal_ph510.py` (14) = `47 passed`.
- Full non-browser regression: **`1223 passed`**, clean baseline collected
  at the same HEAD without the two new files = `1176`, so the delta is
  exactly the new tests, zero regressions. Browser files were excluded by
  path (nothing in the diff touches any browser-tested surface; same
  discipline as the previous handoff). One environment event mid-session:
  `/tmp` filled with stale test-scratch `tempfile.mkdtemp` dirs from prior
  sessions' runs (only ever containing fixture `db.sqlite3`) which broke
  suite startup with disk-I/O errors; cleaned following the previously
  owner-approved precedent (venvs/caches untouched).
- `git diff --check` clean.
- Production read-only preflight over SSH (no write of any kind):
  `HEAD=a5c846b`, known untracked `extra_configs.json` drift still present
  and untouched, `quick_check=ok`, zero FK violations, cardinalities
  accounts/subscriptions/WL-periods/package-grants/refunds =
  `18/18/0/0/0`, all four services active, all four parent migration
  checksums byte-identical to the gates my schema requires, and zero
  `mgboost_manual_payment%` tables exist yet -- the future deploy is a
  purely additive self-applying migration like PH5-05's.

## Exact next step

Independent review of this checkpoint against `origin/main` by Claude.
After approval/deploy decision, PH5-09/10 need the owner-gated admin
manual-payment mutation wave to become reachable. Per owner instruction
this session STOPS here: do not start PH5-06, PH5-07, PH5-08, promo
codes, trials, new admin mutation UI, PH7-09/10/11, PH6-05..08, WL
enforcement, PH4-06, or final legacy revoke without a fresh explicit
owner decision.

---

# PRIOR HANDOFF — PH5-05 canonical Stars purchase/renewal production-deployed and independently verified; Wave A authenticated walkthrough still owner-only / PH4-05 live / PH4-06 not started

Updated: 2026-08-27 (independent production-verification session, closing
out PH5-05 after the implementing Codex session hit a rate limit before
its own final regression/docs/commit). **This top section supersedes
everything below.**

## PH5-05 — `[x]`, production-deployed, independently production-verified

Implementation (commit `0d2e354`, by the prior session) was already
fast-forwarded to production and `mgboost-panel` already restarted before
this session started; nothing new was built or changed here. This session's
job was solely to independently confirm the rollout was actually correct
end to end, per explicit owner instruction not to redo or extend PH5-05.

**Verified, this session, directly against real production over SSH**
(`root@178.250.186.127`, real DB at `/opt/MGBoost_Panel/data/db.sqlite3` --
note the repo-root `panel.db` is an unrelated empty stub, not the live DB):
local/origin/production `git log -1` all `0d2e354`, no dirty state beyond
the already-known untracked `extra_configs.json`. `PRAGMA quick_check=ok`,
`PRAGMA foreign_key_check` = 0 rows. `mgboost_schema_migrations` has
`ph5_05_stars_purchase_v1` with checksum
`9ab3bbfda297641a00e087ec76c8efc20315117ce8979de270d35f6fb8c0f724`,
identical to what `src/stars_purchase_schema.py` computes from the current
source. All 3 new tables (`mgboost_stars_payment_evidence`,
`mgboost_stars_purchase_applications`, `mgboost_stars_purchase_sync_jobs`),
all new `stars_invoices`/`mgboost_entitlement_state.desired_expire`
columns, and all 6 immutability triggers present. Cardinalities
unchanged from PH5-04's own recorded baseline: accounts=18,
subscriptions=18, WL periods=0, package grants=0, package refunds=0.
Legacy `stars_invoices`: exactly 2 rows, both still `invoice_kind=
'LEGACY_EXPIRE'` -- proves no legacy invoice was reinterpreted. All 3 new
canonical PH5-05 tables: **0 rows each** -- proves no fictitious payment/
application/grant record was created (no purchase flow is wired to any
live route yet). All 4 services (`mgboost-panel`, `mgboost-marzban-broker`,
`mgboost-child-worker`, `nginx`) active; `mgboost-panel`'s journal since its
last restart (`2026-08-26 22:15:23`) shows zero errors/tracebacks. Safe
HTTP smoke tests only: unauthenticated `/admin/accounts`/`/admin/dashboard`
still `401`, bogus legacy `/sub/<token>` still `404` -- no admin mutation,
no credential rotation, no real Stars invoice/payment callback was
initiated at any point.

**Tests:** targeted `tests/test_stars_purchase.py` +
`tests/test_bot_support_stars.py` = `56 passed` (re-run this session).
Full non-browser regression re-run against the exact checkpoint commit
(code unchanged since checkpoint, so this is a confirmation run, not a
post-fix re-test): `1163 passed, 15 deselected` (browser suite skipped --
nothing in the diff touches any browser-tested surface and the last
recorded browser-inclusive full run already covered this exact code).

**No fix was needed.** The rollout Codex described (additive migration
applied via `Database()` init, `database_init=ok`, panel restarted) is
exactly what production shows.

Full technical detail recorded in `ROADMAP.md`'s own `PH5-05` entry and
`CHANGELOG.md`'s `Unreleased` section (both updated this session).

## Exact next step

No new phase started, per explicit instruction. Do not start PH5-06,
PH5-07, PH5-09/10, PH6-05..08, promo codes/trials, admin mutation
implementation, or PH4-06 without a fresh explicit owner decision.

---

# PRIOR HANDOFF — PH5-04 deterministic entitlement engine production-deployed; PH5-05 unblocked / Wave A authenticated walkthrough still owner-only / PH4-05 live / PH4-06 not started

Updated: 2026-08-27. **This top section is preserved for continuity; the new top section above supersedes it.**

## PH5-04 — `[x]`, production-deployed, deterministic read-only composition only

`src/entitlement_engine.py` is the authoritative public calculation path:
`db.entitlements.calculate(account_id=..., now=...)`, versioned
`ph5-04-entitlement-v1`. It opens one SQLite read snapshot under the existing
lock and never writes, advances WL period state, calls network/Marzban, grants
a purchase or enforces access. It composes the canonical stores already
owned by earlier phases: current subscription + immutable plan version;
PH6-04 `compute_parent_wl_pool()` over PH6-03 samples; PH5-03
`WLPackageStore.package_state()` for base-first/DL-053 FIFO/rollover/freeze;
and durable active `mgboost_entitlement_overrides`. The machine-readable
result includes components/provenance, effective status/expiry, plan/version,
device mode/limit, real/effective WL mode, current canonical period, decimal
base quota, canonical usage, package bucket contribution and active overrides.

No username behavior exists. Commercial device limits are still exactly
3/6/12; INTERNAL is explicit configurable `LIMITED`/`UNLIMITED` plan data.
Real billed WL-plan terms, not an override, determine billing/package
eligibility -- Base + `FORCE_ENABLED` remains package-ineligible. Expiry/Base
freezes historical packages and a real WL renewal resumes their original
FIFO state. Slot add-ons intentionally return `NONE`/0 (PH5-07 deferred);
adjustments intentionally return `NONE`/0 (PH6-08 absent), while an existing
canonical quota override is exposed but never retroactively alters immutable
period/package accounting.

**Tests:** `tests/test_entitlement_engine.py` adds 17 focused checks covering
six plans, 3/6/12, WL/non-WL, 30/60-day periods, package absent/one/multiple
FIFO and freeze/resume, base usage boundaries, INTERNAL modes, active/expired
overrides, catalog pinning, deterministic identical snapshots and zero
calculation mutation. Related focused regression: `98 passed`; complete
regression in four groups: **`1161 passed, 3 skipped`** (the skips are
pre-existing environment-dependent browser cases).

**Production deploy (2026-08-27):** application-code-only, no schema
migration/backup needed. Preflight at `5a56d7f` found only the known untouched
untracked `extra_configs.json`; `quick_check=ok`, 0 FK violations,
accounts/subscriptions/periods/grants/refunds `18/18/0/0/0`, all four
services active. Fast-forwarded to `4dadc33`; only `mgboost-panel` restarted.
Read-only calculation across all 18 production accounts yielded exactly one
calculation version, zero current WL periods and zero package buckets; the
subscriptions/periods/grants/refunds cardinality remained `18/0/0/0` and
integrity stayed clean. No live subscription, expiry, UUID, config, inbound,
user access, Stars/purchase or enforcement state changed. The deployed
listener correctly returns unauthenticated `/admin/accounts` = `401` on
`127.0.0.1:8001`. Separate observed nginx routing of external
`panel.beykus.fun/admin/*` to an unrelated `127.0.0.1:8000` Uvicorn yields
`404`; PH5-04 did not alter nginx/listeners and this is outside its scope.

## Exact next step

**PH5-05 — Stars purchase + renewal is unblocked** by PH5-01/02/03/04. It
must consume this engine's result; do not begin PH5-06, PH5-07, PH6-05..08,
admin Wave B or PH4-06 without a new explicit owner request.

---

## PH5-03 — `[x]`, production-deployed, catalog/accounting foundation only

Owner closed the only missing package-bucket policy in **DL-053**: after
current-period base quota, package consumption is FIFO by immutable
`granted_at ASC, bucket_id ASC`; equal timestamps use the stable numeric
bucket id. Period rollover and freeze/resume never alter that key. The
implementation (`src/wl_package_schema.py`, `src/wl_package_catalog.py`,
`src/wl_packages.py`) reuses PH5-01's existing channel catalog versions
(`STARS-2026-08-26-v1` / `RUB-2026-08-23-v1`), PH3-09 immutable payment and
mutation records, and **only** PH6-03's canonical parent-attributed samples
on the exact WL nodes. It introduces no second usage/accounting path and no
mutable consumed counter: `package_state()` derives each period's excess as
`max(0, canonical_usage - base_quota)`, then allocates it FIFO across
ACTIVE buckets. The real plan query requires current `ACTIVE`, unexpired,
`wl_mode='LIMITED'`; Base and a hypothetical `FORCE_ENABLED` override are
not eligible. Lapse/expiry/non-WL is a frozen read state, not a deletion;
return to a real WL plan resumes the exact same bucket order/remainder.

Additive schema migration `ph5_03_wl_package_catalog_v1` checksum-gates
PH5-01/PH3-09/PH6-03 and adds immutable package products/prices, durable
parent-owned grant snapshots and immutable refund evidence. Grant records
snapshot SKU/product/catalog/price/bytes and link the exact existing payment
and entitlement mutation. Payment/grant idempotency is durable, one payment
cannot produce two buckets, and a stale callback on an ineligible account
fails closed. Refund calculates the selected bucket's derived consumption in
the same `BEGIN IMMEDIATE` transaction; only zero succeeds, appends the
refund/revoke mutation/evidence, and terminally changes the bucket
`ACTIVE -> REVOKED`. Partial/proportional refund is absent.

**Tests:** new `tests/test_wl_packages.py` covers all 4 SKU × 2 channels,
snapshots/immutability, Base plus FORCE override rejection, base-first/FIFO
across two buckets, zero/partial refund, rollover, expiry/non-WL freeze,
renewal resume, period reset, stale callback, duplicate grant/restart-style
replay and concurrent refund. Full regression, including browser suites in
five complete file groups because the runner has a 30s turn limit:
**`1147 passed, 0 skipped`**.

**Production deploy (2026-08-27):** fresh encrypted backup create/restore
PASS; fast-forward `d5e94ad` -> `72c94ba`, only `mgboost-panel` restarted.
Pre/post `quick_check=ok`, 0 FK violations; accounts=18, subscriptions=18,
`mgboost_wl_periods`=0 unchanged. Explicit dormant seed created 4 package
products + 8 channel prices; immediate re-run created 0. Package
grants/refunds are both 0. All four services active; unauthenticated
`/admin/accounts` stays 401 and bogus legacy `/sub` stays 404. No package
sale route, Stars worker wiring, admin UI, scheduler, entitlement sync,
enforcement, config/inbound/UUID/expiry change was made. Production retains
its known untracked `extra_configs.json` drift, untouched.

## Exact next step

**PH5-04 — Deterministic entitlement engine is unblocked** by completed
PH5-01/02/03. It may consume the new package state but must not start Stars
sales (PH5-05), enforcement (PH6-05+), Wave B admin controls or PH4-06.

---

# PRIOR HANDOFF — F1 orphan-lock corrective fix + PH6-04 shared parent WL pool both production-deployed; PH5-03 unblocked / Wave A authenticated walkthrough still owner-only / PH4-05 live / PH4-06 not started

Updated: 2026-08-27 (continuation of this same session, after the F1
corrective slice below closed). **This top section supersedes everything
below.** The owner asked for PH6-04 (default shared parent WL pool) next,
reusing PH6-01/02/03 without a second accounting path.

## PH6-04 — Default shared parent WL pool: `[x]`, production-deployed, accounting/read model only

`src/wl_parent_pool.py` (new module, no new schema): `compute_parent_wl_
pool()` is a pure `SUM(bytes_delta)` over the already-durable, already-
deduplicated PH6-03 `mgboost_wl_usage_samples` ledger, grouped by
`(account_id, wl_period_id)` and filtered to the exact PH0-05 `WL_NODE_IDS`
allowlist. WL quota already belongs to the parent account in this schema
(`mgboost_wl_periods.account_id`) -- "family" needed no new concept, it is
simply an account with more than one device-slot generation, and every
child of that account (ACTIVE or historical/revoked) contributes to its
period's pool regardless of current `observed_state`, since the ledger
tables are immutable/append-only by PH6-03's own schema. `resolve_current_
parent_wl_pool()` is the time-aware entrypoint; a Non-WL account, an
UNLIMITED-WL account, an account between two periods and a never-purchased
account all correctly resolve to `None`, never a fabricated zero.

**Real gap found and closed, not scope creep:** `wl_period_lifecycle_
schema.py`'s own docstring had already named the `PLANNED -> ACTIVE ->
CLOSED` WL-period status machine but explicitly deferred building it
("Phase 6's own future runtime concern"). Nothing in the already-deployed
codebase ever actually promoted a period past `PLANNED`
(`apply_same_plan_purchase` and `WLPeriodAdminResetStore.reset_period` both
only ever leave rows `PLANNED`), so PH6-03's own already-deployed
`resolve_active_wl_period` (`status='ACTIVE'` filter) could never have
attributed a single real purchase's usage to any period, ever -- a real,
verified defect in already-shipped code, found while confirming PH6-04's
own pool could resolve "the current period" at all. `WLUsageLedgerStore.
sync_wl_period_statuses()` (new method, `src/wl_usage_ledger.py`) is the
purely mechanical, time-driven completion of that already-declared state
machine -- never a new policy decision: a period becomes `ACTIVE` the
instant its own `starts_at` arrives, `CLOSED` the instant its own `ends_at`
passes, close-before-activate in one atomic transaction (so a period whose
window already fully elapsed during a long collector gap closes directly
without ever blocking a later sequential period), a `CLOSED` period
(including one closed early by ADMIN_RESET) never revived. Wired into
`run_collection_cycle` immediately before its existing `resolve_active_wl_
period` call -- the exact same resolver PH6-03 already used, never a second
one. Zero new tables; only `mgboost_wl_periods.status`, the one column
PH5-02's own immutability trigger deliberately left mutable for this.

24 new focused tests: `tests/test_wl_usage_ledger.py` +7 for `sync_wl_
period_statuses` (PLANNED->ACTIVE at `starts_at`, ACTIVE->CLOSED at
`ends_at`, a fully-elapsed PLANNED period closing directly, the exact
contiguous two-period boundary handled atomically, a CLOSED period never
revived, idempotent repeated calls, cross-account isolation); `tests/
test_wl_parent_pool.py` 17 (one parent/several children summed exactly
matching the roadmap's own 60+20+10=90/100 example, quota exceeded reported
with zero enforcement side effect, one child through both WL nodes, three
duplicate ledger observations never double-counted, a revoked generation
keeping its already-consumed current-period traffic after a real
`child_lifecycle`-shaped state transition, the exact WL period boundary
never leaking usage between two periods, 30d/60d sequential real purchases
never merging quota, an unknown/cross-account period id rejected rather
than silently returning zero, a Non-WL account and a never-purchased
account and a between-periods account all resolving to `None`, the real
PLANNED->ACTIVE gap proven closed end-to-end through a genuine
`run_collection_cycle` call against a real purchase, concurrency/restart/
idempotent recomputation, and zero raw-identifier leakage / zero Marzban or
config mutation of any kind). Full regression via `/tmp/mgboost-wave-a-
browser-venv`: **`1140 passed, 0 skipped`** (up from `1116`, zero
regressions).

**Production deploy (2026-08-27):** application-code-only, no schema
migration (PH6-04 needed none -- pure read model over existing tables plus
one already-mutable column). Fresh encrypted backup create/restore PASS;
preflight/post-deploy invariants identical (`quick_check=ok`, 0 FK
violations, accounts=18, grace=17, subscriptions=18, `mgboost_wl_periods`=0
-- unchanged, no real purchase flow calls `apply_same_plan_purchase` live
yet), all 4 services active, unauthenticated `/admin/accounts`/`/admin/
dashboard` still `401`, legacy `/sub` bogus-token still `404`. **Real
production observe-only verification:** `python3 -m scripts.run_wl_usage_
collector` (now internally calling the new `sync_wl_period_statuses` per
live child) reproduced the exact same outcome shape as PH6-03's own prior
real run (31 children, 62 samples, 0 errors, 0 resets) -- confirming the
newly-wired sync is a genuine no-op against real production data today
(0 real WL periods exist). `resolve_current_parent_wl_pool()` was then run
for real against all 18 real production accounts: **all 18 returned `None`**
(correct -- zero real WL periods exist yet), with zero additional Marzban
calls and zero `mgboost_wl_periods` row created (`0` before and after).
`quick_check=ok`/0 FK violations held throughout; collector lease released
(`lease_owner=NULL`, `last_run_outcome='OK'`) after the run.

**Confirmed unchanged:** enforcement/user-visible behavior untouched --
PH6-06 (disable-at-quota) does not exist and this task never built it;
nothing in this task disables, resets, throttles or otherwise changes any
real customer's device, subscription, or Marzban config. Not wired to any
admin route/UI/scheduler -- dormant/on-demand, matching the PH6-01/02/03
precedent. `mgboost_legacy_grace_periods`: unaffected. PH4-06: **NOT
STARTED**.

**PH5-03 is no longer blocked by missing Phase 6 infrastructure** -- real
consumption data (PH6-03's ledger) and the real shared-pool sum PH5-03's
own base-first/rollover/freeze semantics need (PH6-04) now both exist and
are production-verified. PH5-03 still needs its own fresh scoping session
(package purchase/refund/rollover ledger design is its own scope, not
touched here); see `ROADMAP.md` PH5-03's own updated entry.

## Exact next step

Two independent options, owner's choice:
1. **PH5-03** (versioned WL package catalog) -- now genuinely unblocked,
   needs its own fresh scoping/design session (rollover bucket, base-first
   consumption, freeze/resume, unused-only refund -- none of that is
   PH6-04's own scope).
2. **PH6-05/06** (optional per-device allocation / disable-at-quota
   enforcement) -- PH6-06 is the first *enforcement* action in this whole
   ledger chain; do not start it without a fresh explicit owner decision,
   per this project's consistent "don't build a phase's enforcement inside
   an earlier phase's own task" discipline already applied at every PH6-01
   through PH6-04 boundary.

Final HEAD this session: local/origin/production all `1ce80a3`.

---

# PRIOR HANDOFF — F1 orphan-lock corrective fix production-deployed; PH6-04 in progress / Wave A authenticated walkthrough still owner-only / PH4-05 live / PH4-06 not started

Updated: 2026-08-27 (new session, continuing from the PH6-03 handoff below).
**This top section supersedes everything below.** Before starting PH6-04,
the owner required one corrective slice: an independent read-only audit
flagged a potential P2 in `_CrossThreadLockCtx`
(`src/routes/internal.py:97`) — `asyncio.run_coroutine_threadsafe(lock.
acquire(), loop).result(timeout=10)` did not cancel the scheduled coroutine
on timeout, so an abandoned `acquire()` could later win the lock (once the
real holder released) with nobody left to release it, permanently blocking
that username's in-process lock and, eventually, the Stars apply-loop.

**F1 confirmed real, not theoretical.** Reproduced against `main` *before*
any fix with a standalone harness (`asyncio.run_coroutine_threadsafe` timing
out in the caller thread while a slower real holder released afterward):
`lock.locked()` stayed `True` forever and a subsequent normal
`acquire()` hung/timed out — a genuine permanent-lock leak, exactly as the
audit predicted.

**Fix:** `__enter__` now calls the documented `future.cancel()` idiom
(`asyncio.run_coroutine_threadsafe`'s own docs: "the coroutine won't be
cancelled... you have to call `future.cancel()` explicitly") on timeout to
stop the abandoned `acquire()` coroutine; in the rare case `cancel()`
returns `False` because the coroutine had *already* finished acquiring the
instant before cancellation landed, `__enter__` schedules `lock.release()`
on the lock's own loop instead of leaking it. 1 new regression test
(`tests/test_internal_renew_lock.py::test_enter_timeout_does_not_orphan_the_lock`)
reproduces the exact race via the real production code path (monkeypatches
only `concurrent.futures.Future.result` at the real `timeout=10` call site,
nothing else) and proves timeout -> no orphan acquire -> a subsequent
normal acquire/release still works. The existing Stars/internal-renew race
test (`test_stars_worker_and_internal_renew_race_on_same_username_serialize`)
still passes unchanged — serialization semantics untouched. Full regression:
**`1116 passed, 0 skipped`** (up from `1115`).

**Production deploy (2026-08-27):** fresh encrypted backup create/restore
PASS; preflight (`quick_check=ok`, 0 FK violations, accounts=18, grace=17);
fast-forward `ed77b11` -> `096c8d4` (`mgboost-panel` restart only, pure
application code, no schema change); post-deploy invariants identical, all
4 services active, unauthenticated `/admin/accounts`/`/admin/dashboard`
still `401`, legacy `/sub` bogus-token still `404`. Production HEAD:
`096c8d4` (local/origin/production all match).

Other findings from that same independent audit are explicitly **out of
scope** this session — only F1 was investigated/fixed, per instruction.

## Exact next step

PH6-04 (default shared parent WL pool) is now in progress this session,
following directly from the PH6-03 handoff immediately below (still
accurate for all PH6-01/02/03 context).

---

# PRIOR HANDOFF — PH6-03 closed and production-deployed (real observe-only collection verified); PH6-04 next / Wave A authenticated walkthrough still owner-only / PH4-05 live / PH4-06 not started

Updated: 2026-08-27 (new session). **This top section supersedes everything
below.** The owner explicitly authorized building PH6-03 (durable monotonic
WL usage ledger/collector) end-to-end this session, gap-audit-first per the
immediately-prior handoff's own instruction, and reusing PH0-05/PH6-01/02
without duplicating them.

## PH6-03 — Durable monotonic WL usage ledger/collector: `[x]`, production-deployed and real-verified

Before writing any schema, this session read the *actual* live production
Marzban 0.8.4 source over SSH (`docker exec marzban-marzban-1 cat
/code/app/{jobs/record_usages.py,db/crud.py,db/models.py}`) rather than
assuming usage semantics. Findings that shaped the whole design:

- Marzban's own scheduler job reads each node's xray-core stats with
  `reset=True` (the in-process counter is atomically zeroed on every read)
  and *adds* the delta into a durable per-(user,node,UTC-hour)
  `NodeUserUsage.used_traffic` row plus the user's own cumulative
  `used_traffic` column. `GET /api/user/{username}/usage?start=&end=`
  (`crud.get_user_usages`) is therefore always a non-negative *interval
  sum* -- a node restart causes only bounded under-counting (missed polls
  during the outage), never a visible decrease.
- The one real decrease vector is an admin-triggered
  `POST /api/user/{username}/reset` (or `next_plan` activation):
  `crud.reset_user_data_usage` calls `dbuser.node_usages.clear()`, and
  `User.node_usages` is `cascade="all, delete-orphan"` -- a reset
  cascade-*deletes* every historical `NodeUserUsage` row for that user, not
  just zeroes a counter. A query spanning through a reset can genuinely
  report less than an already-ledgered window -- a real, documented,
  irreducible Marzban limitation (bounded by poll interval), not a bug in
  this ledger. Children never have `data_limit_reset_strategy` set to
  anything but `no_reset` (`child_contract.build_child_payload` always
  sends `data_limit=None`), so there is no *automatic* Marzban-side reset
  vector, only the admin-triggered one.
- Attribution needed zero new broker surface: a "child" is a currently-live
  `mgboost_child_user_intents` row (`observed_state='ACTIVE'`); its
  already-stored `child_username` (PH3-03) is used only transiently to
  call the read-only usage endpoint. The existing `legacy.user.usage`
  broker operation's `validate_username()`
  (`[A-Za-z0-9_.@-]{1,128}`) already accepts every real `mgc_*` child
  username, so `ServiceMarzbanClient.get_user_usage()` -- the same
  read-only broker path every other usage caller already uses -- worked
  unmodified.

**Design** (`src/wl_usage_ledger_schema.py`, migration
`ph6_03_wl_usage_ledger_v1`, requires the exact PH3-01/PH3-03-prerequisite/
PH5-02 checksums, same three-parent pattern `parent_sync_schema.py`/PH3-08
already used): `mgboost_wl_usage_cursors` (last observed cumulative total
per child+node, mutable by design -- a decrease is the reset signal);
`mgboost_wl_usage_samples`, a per-(child,node,UTC-hour) ledger whose
`bytes_delta` a DB trigger refuses to ever decrease (mirrors the exact
`mgboost_legacy_grace_periods.current_end_at` extension-only precedent --
"never rewrites consumed" holds at the schema layer); `mgboost_wl_usage_
sample_events`, fully immutable/append-only, `UNIQUE(child_intent_id,
node_id, cursor_before)` -- the idempotency key: a crash-retry, a
duplicate/racing collector, or simply "no new traffic since last poll" all
resolve through the exact same no-op path, no double counting possible;
`mgboost_wl_usage_collector_lease`, a single-row (`id=1`) CAS lease
mirroring the PH3-03 `mgboost_outbox` lease shape -- any number of
processes/hosts may race to claim it, only one wins per window.
`src/wl_usage_ledger.py::run_collection_cycle()` reuses PH6-01's
`require_topology_ok()` (fails closed if the WL node/tag allowlist isn't
freshly confirmed) and PH6-02/PH5-02's `align_to_utc_hour()` instead of
duplicating either; every WL-period boundary is exactly UTC-hour aligned so
a sample bucket can never straddle two periods, making the nullable
`wl_period_id` attribution unambiguous whenever a period exists (none do
yet in production -- no purchase flow calls `apply_same_plan_purchase`
live). Per-child/per-node Marzban read failures are isolated (counted,
never abort the whole cycle). Plain integer decimal bytes throughout, no
unit conversion in the ledger. No raw username/UUID/HWID/token in any
table or in `scripts/run_wl_usage_collector.py`'s aggregate JSON output.
Fully observe/accounting-only: never mutates Marzban, never touches
`mgboost_wl_periods`/subscriptions/entitlements/inbounds, never disables or
resets anyone, not wired to any scheduler -- dormant/on-demand, matching
the PH6-01/02 "build the contract before its consumer exists" precedent.

34 new focused tests (`tests/test_wl_usage_ledger_schema.py` 11,
`tests/test_wl_usage_ledger.py` 23) covering every scenario the roadmap's
own Tests line names: duplicate/two-collectors idempotency (a simulated
stale-cursor race), out-of-order/clock-skew delayed samples, node reset
detection and the never-decrease guarantee, collector-lease exclusivity/
expiry/release, `wl_period_id` attribution, and a full `run_collection_
cycle` (topology fail-closed, live-children-only, delta-only second cycle,
lease contention, per-child error isolation, no username/HWID leakage into
any table). Full regression via `/tmp/mgboost-wave-a-browser-venv`:
**`1115 passed, 0 skipped`** (up from `1081 passed`, all browser suites
included, zero regressions) -- note this session first had to clear
~7000 stale `tempfile.mkdtemp()` directories that had filled the sandbox's
`/tmp` user quota from prior sessions' test runs (owner-approved cleanup,
unrelated to any repo/production content; this was blocking even the
pre-existing baseline suite, confirmed by re-running with the new PH6-03
test files deselected before touching `/tmp`).

**Production deploy (2026-08-27):** fresh encrypted backup create/restore
PASS immediately before deploy; preflight (`quick_check=ok`, 0 FK
violations, accounts=18, grace=17, subscriptions=18, `mgboost_wl_periods`=0,
42 child intents/31 `observed_state='ACTIVE'`); fast-forward `d11005d` ->
`ed77b11`, `mgboost-panel` restart only (additive schema self-applies on
`Database()` construction, zero existing table touched); post-deploy
invariants identical, all 4 services active, unauthenticated `/admin/
accounts`/`/admin/dashboard` still `401`, legacy `/sub` bogus-token still
`404`. **Real production observe-only verification, not a dry run:** a
fresh live topology assertion (`fetch_live_topology_observation` +
`wl_topology_guard.run_assertion`, read-only) confirmed `ok=True` against
`2026-08-26-v1`; `python3 -m scripts.run_wl_usage_collector` then ran
twice, 5 seconds apart, against the real production DB and real broker --
first cycle: 31 live children, 62 samples (both WL nodes x 31 children), 0
errors, 0 resets, real observed totals node 4 (RU ONLY WL) ~64.6MB / node 7
(Selectel) ~5.35GB across 10 children with nonzero traffic; second cycle:
same 31 children, same 62 sample rows (no new UTC-hour bucket needed), only
the 10 children with genuine new traffic produced new idempotent event
rows (62->72 events), byte totals increased by exactly the real observed
deltas, the other 52 (child,node) pairs correctly no-op'd through the
identical duplicate-detection path crash/retry safety also relies on.
`quick_check=ok` and 0 FK violations held after both runs; collector lease
released (`lease_owner=NULL`, `last_run_outcome='OK'`) after each run.

## Confirmed unchanged this session

- Enforcement/user-visible behavior: **untouched.** PH6-06/09 do not exist;
  nothing in this session disables, resets, throttles, or otherwise changes
  what any real customer's device or subscription can do. No inbound/UUID/
  config/expiry/user mutation of any kind was made -- production mutations
  this session were exactly: the new additive schema/tables (self-applied
  by `Database()` construction) and the new ledger rows the two real
  collector runs wrote (cursors/samples/events/topology-assertion rows).
- PH4-06: **NOT STARTED**, no shared legacy credential touched.
- `mgboost_legacy_grace_periods`: unaffected, not queried this session.
- Production HEAD: `ed77b11` (local/origin/production all match, verified
  via `git log -1` on all three before writing this section).

## Exact next step

PH6-04 (default shared parent WL pool) is the next Phase 6 item in
dependency order -- it depends on PH6-02 (closed) and PH6-03 (closed this
session, real per-child per-WL-node byte deltas now durably ledgered).
Before starting it: re-read `ROADMAP.md`'s PH6-04 entry in full (`sum child
usage on two WL nodes; at quota disable all children`) -- note that
"disable all children" is an *enforcement* action PH6-04 itself doesn't
own; re-check whether PH6-04's own scope is genuinely just the shared-pool
*sum* (reading `mgboost_wl_usage_samples`/`mgboost_wl_usage_cursors`
grouped by account+period across both WL nodes) versus where the actual
disable action belongs (PH6-06, which doesn't exist yet and explicitly
depends on PH6-01/children, not on PH6-04 by the roadmap's own `Depends`
line) before writing anything, per this project's consistent "don't build
a phase's enforcement action inside an earlier phase's own task" discipline
already seen at PH6-01/02/03's own boundaries. Do not start PH6-06/09 or
any enforcement/disable path without a fresh explicit owner decision --
this session's ledger is deliberately observe/accounting-only and nothing
about that boundary should be treated as already resolved by PH6-04's own
future work.

---



Updated: 2026-08-26 (new session, continuing from the immediately-prior
handoff below, which correctly identified PH5-03 as blocked on unbuilt
Phase 6 infrastructure). **This top section supersedes everything below.**
The owner explicitly authorized starting Phase 6 this session, in
dependency order: PH0-05 first (it blocks PH6-01), then PH6-01, then a
gap-audit-first PH6-02 (the owner explicitly warned that PH5-02 already
built the real period engine/schema/immutability and PH6-02 must extend
it, never duplicate it). PH6-03 (usage ledger/collector) and PH6-04
(shared parent pool) were **not started** this session -- see "Exact next
step" below for why.

## PH0-05 — Exact versioned WL topology: `[x]`, production-deployed

`src/wl_topology.py`. The prior 2026-08-23 audit already had the exact 12
live WL inbound tags, but not exact node IDs -- this session obtained
those for real, directly against production Marzban, via a short-lived
root-only script (`GET /api/nodes`/`GET /api/inbounds` through the
already-existing read-only `MarzbanClient` methods, using the isolated
broker's own `/etc/mgboost/marzban-broker.env` credentials; the script was
deleted immediately after use, nothing was printed beyond the node/tag
data). Real production Marzban has 5 nodes total; cross-referencing the
`hosts` table's `address` column against each live `wl-*` inbound tag
showed only 2 of them actually serve WL traffic: node id 4 ("RU ONLY WL",
`84.201.130.217`) and node id 7 ("Selectel", `5.178.85.8`), both
`usage_coefficient=1.0`. The other 3 real nodes (Estonia id 3, Beget id 6,
germanyp2 id 8) are excluded by exact id -- notably node id 4's own live
Marzban *name* literally contains the substring "WL", which is exactly why
the owner's "no fuzzy matching" rule matters: it's included because its id
is on the allowlist, not because of its name. Six `wl-selec-tcp-*` rows
found in the Marzban `hosts` table (ids 4451-4453, 4469-4471) reference an
inbound tag that no longer exists in live `get_inbounds()` output -- these
are the "stale WL-like host records" the 2026-08-23 audit already flagged,
and are excluded automatically since the module only ever compares against
live inbound config, never the `hosts` table.

## PH6-01 — Runtime topology allowlist/assertions: `[x]`, production-deployed

`src/wl_topology_guard.py` + `wl_topology_guard_schema.py` (additive
migration `ph6_01_wl_topology_guard_v1`, new append-only
`mgboost_wl_topology_assertions` table). `require_topology_ok()` is the
fail-closed gate a future PH6-06 destructive enforcement action must call
before touching any real inbound state -- it is not called by anything
live yet, since PH6-06 doesn't exist. Running an assertion today is an
on-demand library call (`fetch_live_topology_observation()` wraps the
existing read-only `get_nodes`/`get_inbounds` calls); nothing schedules it
automatically yet, matching this project's established "build the contract
before its future consumer exists, dormant until wired" discipline.

## PH6-02 — Immutable WL periods: `[x]`, production-deployed (gap-fill, not a new engine)

Per the owner's explicit warning, this session read PH5-02's already-
deployed `subscription_renewal.py`/`wl_period_lifecycle_schema.py` in full
before writing anything, and confirmed: decimal-GB units (`GB_DECIMAL =
10**9` in `plan_catalog.py`) and full identity/quota-field immutability
were already correct and already deployed -- not touched. Two real gaps
against PH6-02's own Accept/Fields text: (1) `schedule_wl_period_windows`'s
anchor was exact-second, not UTC-hour-aligned per DL-020; (2) no
ADMIN_RESET close+successor mechanism existed. Both fixed *in* the
existing engine: `subscription_renewal.align_to_utc_hour()` floors only
the WL-period anchor (the subscription's own DL-044 exact-second
anchor/expiry is untouched, per DL-020's "subscription expiry хранится
отдельно"); a new additive `mgboost_wl_period_resets` table + capability-
gated `WLPeriodAdminResetStore.reset_period()` close the current period and
open a successor covering the remaining window with the same quota,
recording an immutable audit row. "Never rewrites consumed" holds by
construction (no `consumed` column exists yet on `mgboost_wl_periods` --
that's PH6-03's future ledger, keyed by period id, so a closed period's own
id is simply never touched again). Source/reason for *ordinary*
period creation needed no new column: it's already fully recoverable via
the existing `wl_periods -> subscription_terms -> entitlement_mutations`
join (payment_channel/mutation_source/actor/reason) -- adding a duplicate
column would have violated the owner's "don't duplicate schema" instruction.

27 new/updated focused tests across `tests/test_wl_topology.py` (11),
`tests/test_wl_topology_guard.py` (8), `tests/test_wl_period_admin_reset.py`
(6), plus 2 new + 2 updated in `tests/test_subscription_renewal.py`. Full
regression via the already-installed `/tmp/mgboost-wave-a-browser-venv`:
**`1081 passed, 0 skipped`** (up from `1054 passed`; all browser suites
included, zero skips, zero regressions). Production: fresh encrypted
backup create/restore PASS, preflight (`quick_check=ok`, 0 FK violations,
accounts=18, grace=17, subscriptions=18, `mgboost_wl_periods`=0), fast-
forward `dba4749` -> `a223f80` (`mgboost-panel` restart only, additive
schema self-applies on `Database()` construction, no existing table
touched), post-deploy invariants identical plus both new tables present
and empty (`mgboost_wl_topology_assertions`=0,
`mgboost_wl_period_resets`=0), all 4 services active, unauthenticated
`/admin/accounts` still `401` (via `127.0.0.1:8001` directly -- the
external `178.250.186.127` IP without the real `Host:` header returns
nginx's own unrelated `404`, unrelated to this change and reproduced
identically before touching anything), legacy `/sub` bogus-token still
`404`.

## Why PH6-03/04 were not started this session

The owner's instruction was explicitly conditional: move to PH6-03 only
"если PH6-02 закрыт и зависимости чистые," and to PH6-04 only if PH6-03 is
"полностью завершён." PH6-03 (durable monotonic usage ledger/collector) is
a materially larger, higher-risk subsystem than PH0-05/PH6-01/PH6-02
combined -- it requires querying real Marzban per-user/per-node usage data,
building idempotent non-decreasing merge logic across duplicate/out-of-
order/node-reset/restart/clock-skew scenarios, a leader-election-or-CAS
concurrency story, and a cursor/snapshot/reconciliation design, none of
which exists as a reusable primitive anywhere in this codebase yet (unlike
PH6-02, which could extend PH5-02's already-proven engine). Building it
correctly needs its own dedicated session with room for the same
gap-audit-first, tests-first, single-increment discipline this session
just used -- attempting it as a fourth item in an already-large single
turn was judged the same kind of avoidable batch risk this project's own
history repeatedly flags (see, e.g., the PH4-05 mass-migration-batch
reasoning further below). PH6-04 was never reachable this session since it
explicitly depends on PH6-03.

## Unchanged this session (verify before relying on any of these)

- Production HEAD: `a223f80` (local/origin/production all match, verified
  via `git log -1` on all three).
- `mgboost_legacy_grace_periods`: still 17 rows, untouched.
- PH4-06: **NOT STARTED**. No shared legacy credential was touched.
- PH5-03 remains exactly as blocked as the immediately-prior handoff
  describes it -- PH6-03/04 (real consumption data) still don't exist.
- PH7-12 stays `[~]`, unaffected by this session.

## Exact next step

PH6-03 (durable monotonic usage ledger/collector) is the next Phase 6 item
in dependency order, now that PH6-01/02 are both closed. Before starting
it, re-read this session's own PH6-02 gap-audit discipline: read the
*actual* current `src/` state first (there is still no usage-ledger
primitive anywhere to extend), design the schema/idempotency/leader-lock
contract, get it right in a focused session, then build. Do not invent
consumption data under any circumstance -- if real Marzban per-user/
per-node usage data cannot be obtained or doesn't behave as expected during
that design pass, stop and report back rather than fabricating numbers.
PH6-04 (shared parent pool) and everything gated on it (PH5-03 onward)
remain blocked until PH6-03 is genuinely complete, tested, and deployed.

---


Updated: 2026-08-26 (later still, same day; continuation of the same
session, same owner instruction thread). **This top section supersedes
everything below, including the immediately-prior section (kept further
down for continuity).** This continuation built and production-deployed
PH5-02 (30/60-day entitlement and WL-period semantics), then stopped: the
next item by number, PH5-03, genuinely depends on Phase 6 usage-tracking
infrastructure that does not exist yet (see its own ROADMAP.md entry for
the full reasoning), and starting Phase 6 is explicitly out of this
session's scope. **PH7-12 still stays `[~]`, not closed** -- unchanged this
continuation.

## PH5-02 — 30/60-day entitlement and WL-period semantics: `[x]`, production-deployed

`src/subscription_renewal.py` -- resolves the "PH6 period interface" this
task's own `Depends` line names: not a wait for Phase 6 code (`PH6-02
Immutable WL periods` itself `Depends: PH5-02`, so that would be a cycle),
but the contract PH6-02 will later consume. `compute_new_expiry()`
implements DL-044's exact formula `max(current_expiry, now) +
purchased_duration` as ONE formula, no active/expired branch needed
(`max` degenerates correctly in both cases). `schedule_wl_period_windows()`
splits a purchase into sequential, contiguous `wl_period_days`-long windows
in the existing PH3-01 `mgboost_wl_periods` table (60d -> exactly two
30-day periods, never merged; Non-WL -> zero periods).
`SubscriptionRenewalStore.apply_same_plan_purchase()` composes both
transactionally: idempotent per `idempotency_key`, same-plan-only
(different plan = `PlanMismatch`, that's PH5-06 territory), never
overwrites an admin-granted `UNLIMITED` subscription, validates plan/
duration actually exist in the PH5-01 catalog. New additive migration
(`src/wl_period_lifecycle_schema.py`, `ph5_02_wl_period_lifecycle_v1`)
closes a real gap PH3-01 left open: `mgboost_wl_periods` had zero
immutability triggers before this; now its identity/quota fields are
guarded, `status` stays mutable for Phase 6's own future runtime state
machine. **Not wired to any live purchase flow** -- PH5-05/PH5-09 are the
future callers.

15 new focused tests (`tests/test_wl_period_lifecycle_schema.py`,
`tests/test_subscription_renewal.py`). Full regression via the
already-installed `/tmp/mgboost-wave-a-browser-venv`: **`1054 passed`**
(all browser suites, zero skips). Production: fresh encrypted backup
create/restore PASS, preflight (`quick_check=ok`, 0 FK violations,
accounts=18, grace=17, `mgboost_subscriptions`=18 pre-existing,
`mgboost_wl_periods`=0, `LEGACY_REVOKED=0`), fast-forward `6414a59` ->
`7820443`, `mgboost-panel` restart only, post-deploy invariants identical,
all 4 services active, unauthenticated `/admin/accounts` still `401`,
legacy `/sub` bogus-token still `404`.

## Why PH5-03 was not started, and why that's a real stop, not a scope choice

PH5-03 (Versioned WL package catalog)'s own Accept/Tests require
"base-first consumption", "unused-only refund", "freeze/resume" -- these
are meaningless without a real measured WL consumption number, and **zero**
of PH6-01 (topology allowlist), PH6-02 (immutable periods runtime),
PH6-03 (usage ledger/collector) or PH6-04 (parent pool) exists yet (all
still `[ ]` in `ROADMAP.md`). This is unlike PH5-02's own "PH6 period
interface" dependency, which turned out to be a forward-interface-contract
PH5-02 itself had to produce (no real wait, no cycle risk once understood).
PH5-03 has no equivalent resolution: there is no existing consumption data
anywhere in this codebase to build a rollover/refund ledger against, and
fabricating one would be exactly the kind of invented data this project's
own discipline forbids. Building the real Phase 6 usage-tracking machinery
to unblock it is explicitly out of scope for this session (Phase 6/Wave B
excluded by instruction). PH5-04 depends on PH5-03; PH5-05/06/08 depend on
PH5-04; PH5-09's own test list ("manual package eligibility/refund") is
entangled with PH5-03 too even though its `Depends` line doesn't name it
explicitly. No further PH5 slice was judged safely startable without
either skipping this dependency or starting Phase 6.

## Unchanged this continuation (verify before relying on any of these)

- Production HEAD: `7820443` (local/origin/production all match, verified
  via `git log -1` on all three).
- `mgboost_legacy_grace_periods`: still 17 rows, untouched.
- PH4-06: **NOT STARTED**. `LEGACY_REVOKED=0`.
- PH7-12 stays `[~]` -- unaffected by this continuation.

## Exact next step

**Genuinely blocked without a fresh owner decision:** either (a) the owner
explicitly authorizes starting Phase 6's real usage-tracking machinery
(PH6-01..04) so PH5-03's rollover/refund/consumption semantics have
something real to be built against, or (b) the owner picks a different
next Phase 5 item that doesn't need consumption data -- re-check each
remaining PH5-*'s own exact dependency wording in `ROADMAP.md` before
picking one, since several (PH5-04/05/06/08) transitively need PH5-03
despite not naming it directly, and PH5-09's own test list is entangled
with PH5-03 too. Do not start Phase 6 or skip PH5-03's real blocker without
that explicit decision. PH7-12's own remaining items (owner authenticated
click-through; `admin.js` monolith split) also remain open and independent
of this Phase 5 line of work.

---

# PRIOR HANDOFF (this session's earlier continuation) — Devices client-evidence addendum + PH5-01 catalog production-deployed / Wave A authenticated walkthrough still owner-only / PH4-05 live / PH4-06 not started

Updated: 2026-08-26 (later still, same day). **This section is preserved for
continuity; the new top section above supersedes it.** Two things happened
this earlier continuation: (1) a small read-only Devices addendum the owner
asked for before final Wave A sign-off, and (2) PH5-01 (versioned six-plan
catalog), the first Phase 5 slice, built and production-deployed. **PH7-12
stays `[~]`, not closed** -- the addendum did not touch either of the two
items the prior handoff already flagged as outstanding (owner authenticated
click-through; legacy `admin.js` monolith split), so per that handoff's own
explicit boundary this session did not start that remaining work.

## Devices: real client-evidence addendum (read-only, PH7-05 mutations not started)

Owner asked to see the actual device/VPN client in Account -> Devices, not
just slot/technical state, before treating Wave A as fully closed. Added
`known_client_devices` to `account_detail()`
(`src/admin_read_models.py::_known_client_devices`), rendered as a new card
block in `frontend/assets/admin/accounts.js::devicesTab` above the existing
slot table. Source: the already-existing, continuously-updated `user_devices`
table, populated by `Database.check_device_access` on every real legacy
`/sub/{token}` hit -- the same request path every currently-migrated
account's real traffic still runs through (confirmed by reading
`src/routes/sub.py`; `check_device_access` runs unconditionally before the
legacy-bridge resolver). Shows: device name (the admin's own renamed
`display_name` if set, else the reported device model), humanized OS/
platform, humanized VPN client name + version (`Happ`/`v2rayTun`/`INCY`
casing only for the three the owner named explicitly; any other client_name
is shown exactly as captured, never guessed into a known label), last
activity. Only `is_active=1` rows are shown.

**Deliberately NOT merged into the existing per-slot table:** a device slot's
HWID is a keyed HMAC verifier (`privacy_safe_hwid()`, PH3 device-slot
architecture) while `user_devices.request_key` is a plain SHA-256 hash from
a completely different scheme (`device_headers.py`) -- there is no shared
key or provable way to match one specific `user_devices` row to one specific
slot from stored data alone, and inventing that pairing would have been
exactly the kind of fabricated metadata the instruction explicitly forbade.
Shown as a clearly separate, clearly labeled block instead. A genesis/
bootstrap placeholder slot never issues a real HTTP request, so it can
never appear in this list -- an inherent property of the data source, not a
filter applied after the fact, matching the existing `proven_genesis_
bootstrap` distinction on the slot table. No raw HWID/UUID/request-key is
exposed in this new block.

Evidence: 1 new focused test
(`tests/test_admin_account_read_models.py::test_devices_tab_shows_real_client_evidence_separate_from_slot_and_never_genesis`,
covers real-device-shown / deactivated-device-hidden / genesis-never-shown /
humanized platform+client casing). Full regression via the already-installed
`/tmp/mgboost-wave-a-browser-venv` Playwright/Chromium venv: `1039 passed`
(all browser suites included, zero skips).

## PH5-01 — Versioned six-plan catalog: `[x]`, production-deployed

First Phase 5 slice. Pure catalog/schema work -- **no purchase flow, no
Stars/LK/bot wiring, nothing production-user-visible changes.** New dormant/
additive schema (`src/plan_catalog_schema.py`, migration id
`ph5_01_plan_catalog_v1`, requires the PH3-01 parent schema's exact checksum,
same pattern PH3-06 already used): `mgboost_price_catalog_versions`
(immutable identity, at most one `ACTIVE` version per channel) and
`mgboost_plan_prices` (immutable, FK-bound to a specific plan-version+
duration). `src/plan_catalog.py` holds the exact owner-approved data --
copied verbatim from `ROADMAP.md`'s own "Approved product catalog" table and
DL-040, nothing invented -- and `seed_plan_catalog()` idempotently creates
the six commercial plans (`BASIC`/`BASIC_PLUS`/`BASIC_PRO`/`WL`/`EXTENDED`/
`FAMILY`; device limits 3/6/12; WL `NONE` for the three Base tiers,
`LIMITED` 100/150 decimal-GB per fixed 30-day period for WL/Расширенный/
Семейный) with 30/60-day durations and both channels' prices (`TELEGRAM_
STARS` = new `STARS-2026-08-26-v1`, `RUB` = `RUB-2026-08-23-v1` per DL-040)
-- 12 SKUs/12 prices per channel, 24 total, exactly matching the roadmap
tables. Seeding is its own explicit script
(`scripts/seed_ph5_01_plan_catalog.py`), NOT auto-run at `Database` startup
-- same dormant-until-seeded discipline PH3-01/PH3-06 used. The live
`stars_tariffs` table and the 199⭐/349⭐ current-tariff mapping decision this
roadmap entry's own "Migration" line describes are explicitly deferred to
whichever future phase (PH5-04/05) actually wires a real purchase/
entitlement flow to this catalog -- out of PH5-01's own schema/data-only
scope.

9 new focused tests (`tests/test_plan_catalog_schema.py`,
`tests/test_plan_catalog.py`): migration idempotency, exact-parent-checksum
requirement, price/catalog-version immutability, one-active-catalog-per-
channel, positive-amount validation, exact plan terms, exact 12-SKU/24-price
seeding, reseed idempotency, and the seed script's own `main()` end-to-end.

**Production deploy:** fresh encrypted backup create/restore PASS (via
`systemctl start mgboost-secure-backup.service`) immediately before deploy.
Preflight: `quick_check=ok`, 0 FK violations, accounts=18, grace=17,
`mgboost_plan_versions`=7 (pre-existing `LEGACY_PAID_COMPAT_V1_*`/internal
rows), `mgboost_plan_prices` table absent, `LEGACY_REVOKED=0`. Fast-forward
`f4a250e` -> `6414a59`, `mgboost-panel` restart only (schema self-applies on
`Database` construction; additive-only, no existing table touched). Post-
deploy: `quick_check=ok`, 0 FK violations, accounts/grace unchanged 18/17,
`LEGACY_REVOKED=0`, all 4 services active (`mgboost-panel`, `mgboost-
marzban-broker`, `mgboost-child-worker`, `nginx`), static `accounts.js`/
`admin.css` `200` with correct MIME and confirmed to actually contain the
new strings (not a stale-cache repeat of the immediately-prior incident),
`/admin/accounts`/`/admin/dashboard` still `401` unauthenticated, legacy
`/sub` bogus-token still `404`. Catalog explicitly seeded in production via
the new script: 6 plan codes, 24 prices created; re-run immediately after
confirmed fully idempotent (0 newly-created, invariants unchanged, all 24
prices spot-checked against the approved tables by direct SQL join --
verbatim match). `mgboost_plan_versions` went `7` -> `13` (exactly the 6 new
rows), zero existing row touched.

## Unchanged this session (verify before relying on any of these)

- Production HEAD: `6414a59` (local/origin/production all match, verified
  via `git log -1` on all three).
- `mgboost_legacy_grace_periods`: still 17 rows, unchanged by this session
  (neither slice touches grace). Pull a fresh
  `scripts/ph4_05_daily_cohort_report.py` run before quoting live counts --
  they continue to change organically from real client reconnects.
- PH4-06: **NOT STARTED**. `LEGACY_REVOKED=0`. No shared legacy credential
  was touched this session.
- PH7-12 stays `[~]` -- see the "Remaining before Wave A `[x]`" note in
  `ROADMAP.md` (owner authenticated click-through; `admin.js` monolith
  split), neither of which this session started.

## Exact next step

No blocking decision pending for either slice this session touched. Two
independent, owner-scoped next actions exist (pick either, they don't
conflict):

1. **PH7-12 close-out** (owner-gated): the owner-performed authenticated
   Dashboard/Accounts/Devices/Migration/Technical/mobile-viewport
   click-through (this session again had no Marzban admin credentials and
   did not seek any), then finish splitting `admin.js`'s remaining legacy
   monolithic screen code into per-domain ES modules.
2. **Phase 5 continuation**: PH5-02 (30/60-day entitlement and WL-period
   semantics) is the next PH5 dependency per `ROADMAP.md` -- depends on
   PH5-01 (done) and a PH6 period interface that does not exist yet, so
   PH5-02 will need to define that interface itself or the next agent
   should re-check `ROADMAP.md`'s exact PH5-02/PH6 dependency wording
   before starting, per this session's own "don't skip a dependency" rule.
   Not started this session; deliberately stopped here to keep this
   session's two slices independently reviewable rather than starting a
   third, larger piece with a partially-open dependency.

---

# PRIOR HANDOFF — Wave A corrective UX slice production-deployed / authenticated walkthrough still owner-only / PH4-05 live / PH4-06 not started

Updated: 2026-08-26 (later, same day; corrective-slice session, continuing a
prior Codex session that ran out of quota after implementation but before
final regression/deploy/docs/commit). **This top section supersedes
everything below, including the immediately-prior Wave A handoff section
(kept further down for continuity).**

## This session: finished and deployed the corrective UX slice

The prior session (Codex) implemented, in a dirty worktree, a follow-up
slice addressing an owner manual walkthrough of the first Wave A deploy
(English labels, no visible Marzban note, no technical-account filter,
inconsistent migration/Telegram denominators, unlabeled Technical tab) and
found/fixed one real bug (`metadataWarning()` returning a raw `''` instead
of `` html`` ``, a SafeMarkup violation caught by the browser gate) — but
ran out of quota before finishing regression/deploy/docs/commit. This
session reviewed the full dirty diff line-by-line (no code was rewritten;
it was accepted as-is after review — see `ROADMAP.md` `PH7-12`'s
"Corrective UX slice" entry and `docs/ADMIN_PANEL_REDESIGN.md` §8.1 for the
exact content), ran the full regression suite (reusing the already-built
`/tmp/mgboost-wave-a-browser-venv` Playwright/Chromium environment —
**`1029 passed, 0 skipped`**, up from `1026 passed, 3 skipped`), committed
(`76daec6`), pushed, and deployed to production.

**Production deploy:** fresh encrypted backup create/restore PASS
immediately before deploy; preflight invariants (`quick_check=ok`, 0 FK
violations, accounts=18, grace rows=17 unchanged
`PH4-05-MASS-COHORT-2026-08-26` same start/end, `LEGACY_REVOKED=0`, all 4
services active) recorded before any mutation. Fast-forward pull
`955f255` -> `76daec6`, `mgboost-panel` restart only (no schema change).
Post-deploy: same DB invariants unchanged, static ES modules/CSS return
`200` with correct MIME (`admin/core.js`, `admin/accounts.js`,
`admin.css`), new/existing API routes (`/admin/accounts`,
`/admin/dashboard`, `/admin/migration-grace`) still `401` unauthenticated,
all 4 services active.

**What was NOT done this session, and is the concrete next step:** an
interactive authenticated production click-through (Dashboard / Accounts /
search-by-note / account-with-note / account-without-note /
owner-unlimited account / device-limit-exempt account / Devices with real
lineage / Devices with proven genesis / Telegram-Ownership / Migration-
Grace / Technical / narrow-viewport). This session had no Marzban admin
login credentials and deliberately did not attempt to obtain, guess, or
reuse any existing session/cookie to get them — logging in as the owner is
the owner's own step. Everything else that can be verified without a real
authenticated browser session was verified: unauthenticated API/static
checks, DB-level invariants pre/post deploy, and the dedicated
CSP/XSS/search/technical-visibility browser E2E gate (`tests/
test_admin_browser_e2e.py`), which runs against realistic fixtures that
match the exact new response shapes (`display_identity`, `note`,
`proven_genesis_bootstrap`, `technical_hidden_count`,
`presentation_metadata_available`, the restructured `technical` field
list). **Next agent/owner: do the authenticated click-through above, then
close this one remaining Wave A corrective item.**

`ROADMAP.md` `PH7-12` stays `[~]` — not auto-closed. Two items remain
before it can close: (1) the authenticated walkthrough above, (2) splitting
the legacy monolithic screen code out of `admin.js` into per-domain ES
modules (unchanged from the prior handoff, not started this session, not
in scope this session per the corrective-slice instruction). Do not start
PH7-12's remaining implementation, Wave B, PH7-01/05/08, PH5, PH6 or
PH4-06 without a fresh explicit instruction — this session's scope was
strictly "finish and safely deploy the already-written corrective slice."

## Unchanged this session (verify before relying on any of these)

- Production HEAD: `76daec6` (local/origin/production all match).
- `mgboost_legacy_grace_periods`: still 17 rows, `cohort_ref=
  'PH4-05-MASS-COHORT-2026-08-26'`, `started_at`/`current_end_at` min/max
  unchanged from before this session's deploy (2026-08-26 14:08:25 MSK ->
  2026-09-09 14:08:25 MSK). Absolute real-lineage/active-slot/Telegram
  counts were not re-pulled this session (the corrective slice only
  changes how they are *presented*, not the underlying canonical
  `legacy_grace_observability` computation) — they continue to change
  organically from real client reconnects during the live campaign; pull
  a fresh `scripts/ph4_05_daily_cohort_report.py` run before quoting them.
- PH4-06: **NOT STARTED**. `LEGACY_REVOKED=0`. No shared legacy credential
  was touched this session.
- No schema migration in this slice — pure application/frontend code.

---

# PRIOR HANDOFF (this session's starting point) — Wave A account-centric admin slice production-deployed / modularization continues / PH4-05 live / PH4-06 not started

Updated: 2026-08-26. **This section is preserved for continuity; the new
top section above supersedes it.**

## Current Wave A state

- Actual starting local/origin HEAD was `2f8bf35`; production was still on
  `8b73843` (the difference was the docs-only redesign commit). The working
  tree now contains the first Wave A implementation slice; verify the final
  commit with `git log -1` because this paragraph is written before commit.
- New read-only account presentation layer: `src/admin_read_models.py`,
  `src/routes/admin_accounts.py`, routes `/admin/accounts`,
  `/admin/accounts/{id}`, `/admin/migration-grace`, `/admin/dashboard`.
  Migration actions directly reuse `account_grace_snapshot()` and
  `classify_action()`.
- New UI: Accounts list; Account Overview/Subscription/Devices/Telegram-
  Ownership/Migration-Grace/Technical tabs; standalone Migration/Grace;
  grace-first conditional Dashboard; legacy Users moved under
  `System / Technical / Marzban Raw Users`; existing legacy screens retained.
  Vanilla-JS modularization started with `frontend/assets/admin/core.js` and
  `admin/accounts.js`; the remaining monolithic legacy screen code is the main
  unfinished Wave A item (`ROADMAP.md` `PH7-12 [~]`).
- Safety/evidence: focused synthetic tests passed; a real headless-Chromium
  CSP/XSS/Technical-visibility/480px responsive gate passed; final full
  regression with all browser suites is `1023 passed` (zero skips). Fresh
  production DB-copy gate assembled all 18
  account details and dashboard JSON with `quick_check=ok`, 0 FK failures and
  unchanged account count. No schema or migration is added by Wave A.
- Fresh read-only production cohort report (same day): 17 members;
  `OK_MIGRATED=8`, `WAITING_FOR_REGISTRATION=9`; Telegram `BOUND=4`,
  `UNREGISTERED=13`; active slots 27; real migrated device lineages 15.
  These counts change organically and must be refreshed before future use.
- Production deployed to `e5e2e21` after encrypted backup create/restore PASS;
  only `mgboost-panel` restarted. All four services active, static ES modules
  and CSS load publicly with correct MIME, new API routes deny unauthenticated
  requests, `quick_check=ok`, 0 FK violations, accounts/grace 18/17 and
  `LEGACY_REVOKED=0`. The first curl hit the short restart window; immediate
  repeat passed once port 8001 was listening. Remaining safe work: split the
  legacy monolith into per-domain modules and perform the authenticated
  production Accounts/detail/Migration + preserved legacy screens walkthrough.

## Unchanged hard boundaries

- PH4-06 is NOT STARTED. Do not revoke any shared legacy credential.
- Do not wait for grace to end; continue Wave A independently.
- Do not invent PH5/PH6 catalog, billing or WL data.

---

# PRIOR HANDOFF — admin panel redesign approved (design-only), Wave A next / PH4-05 grace campaign still live / PH4-06 not started

Updated: 2026-08-26 (later still, same day; read-only design/audit session,
next agent is expected to be Codex). **This is the current top-of-file
handoff — read this section first.**

## Exact current state (verify against `git log`/`ROADMAP.md` before acting)

- Local/origin HEAD at the end of this session: `8b73843` (unchanged by this
  session — this was a **docs-only** session: `docs/ADMIN_PANEL_REDESIGN.md`
  added, `ROADMAP.md`/`CHANGELOG.md`/this file updated, then committed).
  Confirm the actual current HEAD with `git rev-parse HEAD` before trusting
  this number — a doc commit was made after this paragraph was written; see
  `git log -3 --oneline` for the exact commit.
- Production: `ssh root@178.250.186.127`, project at
  `/opt/MGBoost_Panel`. **This session made no production changes** — no
  deploy, no restart, no mutation. A docs-only commit does not require a
  production deploy.
- PH4-03: `[x]` closed. PH4-05: `[x]` closed, grace campaign live
  (`cohort_ref='PH4-05-MASS-COHORT-2026-08-26'`, `started_at`=2026-08-26
  14:08:25 MSK, `current_end_at`=2026-09-09 14:08:25 MSK). PH4-06: **NOT
  STARTED**, not scoped, gated on its own explicit owner authorization.
- 17/17 real ACTIVE parent accounts are technically parent-ready
  (genesis child `ACTIVE` + legacy bridge `enabled=1`), covering all 19 real
  ACTIVE legacy Marzban usernames. Telegram `BOUND`: 4. `WAITING_FOR_
  REGISTRATION`: 13. `MANUAL_REVIEW`: 0. **17/17 parent-ready is NOT the same
  as 17/17 real customer devices migrated** — see
  `docs/ADMIN_PANEL_REDESIGN.md` §5 for the exact nuance (genesis
  placeholder vs. real per-device migration lineage, which appears
  organically on next client reconnect). Re-verify these counts against a
  fresh run of `scripts/ph4_05_daily_cohort_report.py` before relying on
  them — they change daily during the grace window.

## What this session did (design-only, no implementation)

A read-only audit of the current admin frontend (`frontend/index.html`/
`assets/admin.js`) and backend (`src/routes/admin*.py`) found it is still
100% Marzban-username-centric with **zero UI** for the parent-account/
migration/grace/opaque-credential/device-slot domain that has been fully
implemented and live since PH2–PH4. Full findings, target navigation,
read-model plan and implementation waves are now the canonical document
**`docs/ADMIN_PANEL_REDESIGN.md`** — read it before doing anything admin-UI
related. Five owner decisions from this session are recorded as `ROADMAP.md`
Decision Log `DL-048`..`DL-052`:

- **DL-048**: internal technical identifiers (raw `mgc_*` child id,
  generation id, outbox id, full UUID/HWID) hidden by default, shown only
  under `Account → Technical`.
- **DL-049**: PH7-05 Wave B ships four distinct operations — Disable/Enable
  (reversible), Revoke (terminal), Free (separate step after Revoke),
  Rebind (compromise/replacement, strictest confirm) — never one generic
  "delete device" button.
- **DL-050**: legacy Marzban-username `Users` screen moves immediately under
  `System/Technical` (not deleted); `Accounts` becomes the primary
  top-level customer-facing surface.
- **DL-051**: Dashboard priority is Grace campaign (conditional block,
  collapses after grace ends) → operational health → expiring soon; Tickets
  stays a compact counter, not an analytics block.
- **DL-052**: frontend stays vanilla JS, split into ES modules
  (`frontend/assets/admin/core.js` + per-domain modules) — no
  React/Vue/Svelte rewrite.

`ROADMAP.md` Phase 7 (`PH7-01`..`PH7-11`) statuses are **unchanged**
(`[ ]`) — this session only documented the design, it did not start
implementation and must not be read as having closed or partially closed
any PH7 item.

## NEXT AGENT: start Wave A of the account-centric admin redesign

Read `docs/ADMIN_PANEL_REDESIGN.md` in full first (target navigation,
existing reusable backend read-models like
`legacy_grace_observability.account_grace_snapshot()`/`classify_action()`,
the five DL-048..052 decisions, and the exact Wave A scope in §6) before
writing any code.

Wave A scope (read-only except the already-existing, already-safe opaque
credential issue/reissue flow): modularize `admin.js` into ES modules;
new top-level navigation; `Accounts` list (`AccountSummary` read-model,
new); `Account` detail page (Overview/Subscription/Devices/Telegram-
Ownership/Migration-Grace tabs); standalone Migration/Grace dashboard
wrapping the existing `legacy_grace_observability` module (do not re-derive
its classification logic); new Dashboard home per DL-051; move legacy
`Users` under `System/Technical` per DL-050; re-integrate existing Tickets/
Nodes/Extra configs/System screens without functional loss; responsive
layout.

**Explicit boundaries — do NOT do these:**

- Do **NOT** start PH4-06 (no real legacy credential revoke).
- Do **NOT** revoke the shared legacy credential for any account.
- Do **NOT** wait for the PH4-05 grace period to finish before starting
  Wave A — admin redesign work proceeds in parallel with the grace
  campaign, they are independent workstreams.
- Do not re-litigate DL-048..052 — they are owner-approved; only the owner
  can revise them.
- Do not build Wave C/D (PH5/PH6-backed) capabilities against invented
  catalog/tariff/WL data — those phases do not exist yet.

---

# PRIOR HANDOFF (still accurate history) — PH4-03 CLOSED (mass migration complete) / PH4-05 LIVE campaign / PH4-06 not started

Updated: 2026-08-26 (later still, same day). **PH4-03 is now CLOSED `[x]`:
mass migration is complete.** All 17 real ACTIVE parent accounts (covering
all 19 real ACTIVE legacy Marzban usernames) are technically migrated
(genesis child `ACTIVE` + `mgboost_legacy_bridge_bindings.enabled=1`).
PH4-05's grace campaign keeps running unchanged (`cohort_start_at=
1787742505`, ends 2026-09-09 14:08:25 MSK). PH4-06 (real revoke) was NOT
touched and remains its own separate, unstarted, gated phase.

## Exact state right now

- 17/17 real ACTIVE parent accounts have `mgboost_legacy_bridge_bindings.
  enabled=1` and an `ACTIVE` genesis child intent.
- `mgboost_migration_bindings`: `MIGRATED=9`, `MIGRATING=0`,
  `ERROR_RECONCILE=0` -- unchanged for accounts 1/3/4's own real customer
  devices. The other 14 accounts' real customer devices have NOT been
  simulated/forced -- they will get their own real `MIGRATED` binding
  organically the next time each customer's own client hits the unchanged
  legacy `/sub/{token}` URL, exactly as designed. **Do not read `active_
  devices=1` for these 14 in the daily report as "1 real device migrated"
  -- that 1 is the synthetic genesis-child placeholder on slot 1 (never a
  real customer device, has no `mgboost_migration_bindings` row), the
  exact same pattern accounts 1/3/4 used.**
- Owner device-policy/ownership decisions applied for the 4 previously-
  flagged accounts: account 8 (`client_buy_1`) Telegram owner is now
  `2105984481` (`ADMIN_REBIND` provenance, `1130407008` remains a
  legitimate non-owner VPN user, both historical `tg_users` rows intact);
  device limits D8 (account 8), D6 (account 10/`German`, via the new
  `acknowledge_observed_overage=True`), device-limit-exempt (account 11/
  `Pensioner`, the owner's parents, via the new generic `UNLIMITED`-for-
  `DIRECT` path), D4 (account 13/`client_buy_7`).
- Telegram-`BOUND`: 4 accounts (1, 3, 4 pre-existing + 8 newly resolved).
  13 accounts still `UNREGISTERED` -- this is expected and NOT a blocker;
  it's PH4-05's own ongoing campaign metric.
- Zero Telegram messages were sent this session either (still just the
  finalized draft in `docs/PHASE4_GRACE_PERIOD_COMMS_DRAFT.md`).

## New code this session (all tested, all deployed)

- `src/legacy_grace_migration.py::migrate_bootstrapped_account()` -- the
  reusable genesis-child+bridge-enable batch primitive. Safe to re-run per
  account (idempotent); fails closed (`PrerequisiteMissing`) if an account
  has no entitlement yet.
- `src/legacy_grace_registration.py::resolve_ambiguous_telegram_ownership()`
  -- the one deliberate, capability-gated, audited exception to "ambiguous
  ownership is never auto-resolved." Use ONLY with the owner's own explicit
  per-account decision, never inferred.
- `src/legacy_paid_compat.py`: `device_limit_exempt=True` (generic
  DIRECT-account device-limit exemption, reuses `UNLIMITED` plan mode) and
  `acknowledge_observed_overage=True` (explicit admin acknowledgment that a
  reviewed limit is correct despite the frozen raw observed-device count
  being higher -- never edits that raw evidence).
- `src/device_slots.py`: `PAID_BASELINE_LIMITS` now `{3,4,6,8,12}`;
  `UNLIMITED` mode allowed for a `DIRECT` account, but ONLY ever via a
  capability-gated plan_version an admin explicitly created (see
  `test_direct_plan_unlimited_is_allowed_only_via_a_reviewed_plan_and_uses_
  technical_cap` for the exact contract).

## Exact next step / ongoing operations

1. **Daily during the grace window** (until 2026-09-09 14:08:25 MSK): run
   `scripts/ph4_05_daily_cohort_report.py --db <COPY> --cohort-ref
   PH4-05-MASS-COHORT-2026-08-26 --catchup-bind` and personally follow up
   with `CONTACT_USER`/`MANUAL_REVIEW` rows. The owner publishes/maintains
   the informational post through their own channel.
2. **PH4-06 is the next distinct future phase**, not started, not
   scoped this session -- it requires its own explicit owner authorization
   and must show (per the owner's own stated safety bar): who is really
   migrated with real customer usage, who is still on legacy, who is
   unresolved, before any revoke.
3. If quota runs out: this file plus `git log`/`ROADMAP.md`/`CHANGELOG.md`
   is sufficient for a fresh session to resume exactly from "PH4-03 closed,
   PH4-05 campaign ongoing, watch the daily report, PH4-06 not started."

---

# PRIOR HANDOFF (this session, PH4-05 mass cohort launch): still accurate history

Updated: 2026-08-26 (later still, same day). **PH4-05 is CLOSED `[x]` --
a real 17-account grace cohort is running in production, covering all 19
real ACTIVE legacy Marzban users.** PH4-03 stays `[~]`: the owner revised
the earlier plan (see the prior handoff section below for the original
audit) -- grace no longer waits on Telegram registration or prior
migration; the 14-day window is itself the mass-migration campaign.
Nothing about the original PH4-03 canary evidence is retracted; only the
plan for finishing the mass stage changed.

## Exact state right now

- Local/origin HEAD: commit this session ends on (see `git log -1`),
  pushed to `origin/main`.
- Production HEAD: same commit, deployed and restarted, all 4 services
  active, `quick_check=ok`, 0 FK violations.
- `mgboost_legacy_grace_periods`: 17 rows, `cohort_ref=
  'PH4-05-MASS-COHORT-2026-08-26'`, ALL sharing the exact same
  `started_at=1787742505` / `original_end_at=1788952105`
  (2026-08-26 14:08:25 MSK -> 2026-09-09 14:08:25 MSK, exactly 14 days).
- Cohort = accounts 1, 3, 4 (already migrated, pre-existing) + 14 newly
  bootstrapped accounts (`ownership_evidence='ABSENT'`, zero Telegram
  claim). Covers all 19 real ACTIVE legacy Marzban usernames. Excluded:
  5 `EXPIRED` real users (future renewal/policy path, not this campaign),
  the PH3-08 test canary, generic test users, `mgc_*` children.
- 1 account (id 8) has a known ambiguous Telegram-ownership mapping --
  included in the cohort (grace applies), ownership never guessed,
  `telegram_status='AMBIGUOUS'`, `action='MANUAL_REVIEW'`.
- 4 accounts (ids 8, 10, 11, 13) have more real active devices than the
  default `D3` limit (4, 7, 8, 8 respectively) -- account/alias/grace
  membership exists for them, but `ensure_legacy_paid_compat_entitlement`
  correctly failed closed (`DeviceOverageConflict`) and they have **no
  subscription/entitlement yet**. Migration for these 4 requires an
  explicit owner-approved `approved_extra_device_slots` decision first --
  do not invent a higher limit without that.
- Day-0 report: 3 `OK_MIGRATED` (1, 3, 4), 13 `WAITING_FOR_REGISTRATION`,
  1 `MANUAL_REVIEW` (account 8), 0 `RECONCILE_REQUIRED`/
  `COMPATIBILITY_BLOCK`.
- Zero Telegram messages sent (no send path was wired to the finalized
  comms draft, per instruction). No LK banner shown yet either.
- `mgboost_legacy_bridge_bindings` still only has 3 rows (accounts 1, 3,
  4) -- the 14 new accounts are NOT migrated yet, their real devices are
  completely unaffected, still served exactly as before this session.

## What still needs to happen (the actual mass-migration work)

For each of the 14 newly bootstrapped accounts, once its owner registers
in Telegram (`bind_telegram_after_registration()` fires automatically from
the bot's existing linking handler and is idempotent/safe to also re-run
via `scripts/ph4_05_daily_cohort_report.py --catchup-bind`), the account
still needs the actual migration machinery run -- genesis child bootstrap
on slot 1 (real broker call, proven 3x already for accounts 1/3/4) then
`mgboost_legacy_bridge_bindings` enabled -- before its real devices
transparently migrate off the shared legacy UUID. **This orchestration
function was deliberately NOT built this session** (explicit scope
decision: today's real production action was the clock + safe bootstrap,
not 14x real broker mutations against real customer Marzban identities in
one batch -- see "Why genesis+bridge-enable was not done this session"
below). Building and running it, per-account or in small batches as
registrations land, is the next critical-path action.

## Daily operations during the 14-day window

Run `scripts/ph4_05_daily_cohort_report.py --db <COPY> --cohort-ref
PH4-05-MASS-COHORT-2026-08-26 --format table` (optionally `--catchup-bind`
first) once a day. `action` column tells the owner exactly what to do per
account: `CONTACT_USER` (grace ending soon, still unregistered),
`MANUAL_REVIEW` (ambiguous ownership), `WAITING_FOR_REGISTRATION` (normal,
no action yet), `OK_MIGRATED` (done), `RECONCILE_REQUIRED`/
`COMPATIBILITY_BLOCK` (technical issue, investigate). The owner publishes
the finalized informational post (`docs/PHASE4_GRACE_PERIOD_COMMS_DRAFT.md`)
through their own existing channel -- no code in this project publishes it
automatically.

## Why genesis+bridge-enable was not done this session

`resolve_account_device()` fails closed (`OUTCOME_PROVISIONING_UNAVAILABLE`,
not a fall-through outcome) for an account with zero prior children -- a
genesis child MUST be pre-provisioned (real broker call, slot 1, synthetic
placeholder HWID) before a bridge binding is safely enabled, exactly as
done for accounts 1/3/4. Doing this for 14 real accounts in one unattended
batch (14 real broker mutations against real customers' live Marzban
identities, with no incremental canary-style verification between each)
was judged too large a blast radius for a single turn even under a broad
authorization -- this is exactly the kind of scope where a subtle batch
bug could affect many real customers simultaneously. The safe, bounded,
purely-additive action (account+alias+grace bootstrap, zero Marzban touch)
was completed instead; genesis+bridge-enable is recommended as its own
next session/task, ideally still done in small reviewed batches matching
this project's own established discipline (dry-run against a DB copy
first, one account verified end-to-end before the next).

## Exact next step

1. Daily: run the cohort report, `--catchup-bind` for anyone who
   registered, personally contact `CONTACT_USER`/`MANUAL_REVIEW` cases.
2. Build (or resume building) the genesis-child + bridge-enable batch
   orchestration for the 10 fully-entitled new accounts first (no device-
   limit blocker), then resolve the 4 `DeviceOverageConflict` accounts
   once the owner approves their device limits, then the 1 ambiguous
   account once ownership is resolved.
3. PH4-03 stays `[~]` until mass migration actually completes or the
   owner explicitly accepts a smaller final scope with recorded reasons.
4. PH4-06 (real revoke) remains untouched and gated on PH4-03 actually
   finishing, not on the clock alone.

---

# PRIOR HANDOFF (this session, read-only PH4-03 audit): still accurate history

Updated: 2026-08-26 (later still, same day). After the deploy described
below, the owner flagged a concern that production only has 3 real
migrated parent accounts, which
looked inconsistent with "mass migration". A read-only audit (no mutation)
confirmed it: **PH4-03 is REOPENED `[~]`** in `ROADMAP.md` -- its own
written contract required a "mass migration" cohort stage that was never
executed, and the original closing verdict never said so explicitly (unlike
PH4-08/PH5-09, which were explicitly named as deferred). The original
canary evidence itself is NOT retracted -- internal cohort + 2 real
DIRECT/`EXTERNAL_PAYMENT` migrations genuinely happened and passed exactly
as documented; only the "mass" step was skipped without being called out.
**PH4-05 real `start()` remains blocked** until this is resolved (or the
owner explicitly overrides) -- see `ROADMAP.md` PH4-03/PH4-05 for the full
counts and the proposed mass-migration plan (evidence collection, ambiguous
case review, staged batches -- no new code needed, every primitive already
exists and is tested).

## Exact production counts (read-only audit, 2026-08-26)

- 44 total Marzban users -> 24 real legacy users (19 active, 5 expired) after excluding 18 `mgc_*` children, 1 PH3-08 test canary, 1 generic test user.
- Only 5 of 24 real usernames have any `mgboost_legacy_account_aliases` row (account 1's 3 + account 3's 1 + account 4's 1) -> only **3 real parent accounts** exist (`mgboost_accounts=4`, but account 2 is the PH3-08 test canary, not a real user).
- 19 real users have zero parent-account representation: 13 active with no Telegram-bot linkage, 5 expired with no linkage, 1 active with an ambiguous multi-Telegram mapping (a second, distinct case from the already-known excluded one).
- Zero of the 19 meet the "single unambiguous Telegram mapping" bar the original cohort selection used -- the cheap-evidence pool is exhausted; the gap is ownership evidence, not engineering.
- 7 currently-`ACTIVE` device-slot generations exist across the 3 real migrated accounts (2+2+3).

## What happened this session, in order

1. PH4-05 reversible/dormant part built, tested (38 new tests), committed, pushed (`4b49450`).
2. Owner authorized SSH production access with an explicit checklist. Encrypted backup+restore verified PASS. Baseline HEAD/cardinality recorded (`6323823`). Fast-forward deployed to `4b49450`, restarted, all 4 services active, `quick_check=ok`, 0 FK violations, cardinality unchanged, 3 new PH4-05 tables present and empty.
3. Smoke-tested legacy `/sub` -- first attempt accidentally hit Marzban's own port 8000 instead of the app's real `LISTEN_PORT=8001` (a testing mistake, not a production incident -- caught and corrected). Redone correctly on 8001: real `200`, real VLESS body, and the new grace-activity telemetry counter genuinely incremented for a real request without affecting the response, proving the fail-open hook works on live traffic.
4. Ran the dry-run eligibility report against a `VACUUM INTO` copy of the live DB (never the live file itself), copy deleted after use. Result: accounts 1/3/4 = `START_GRACE` (no blocker by the script's own narrow migration-state heuristic), account 2 = `HOLD` (`DISABLED`, no migration).
5. Reported this to the owner. **Owner did not authorize starting any real grace clock** and instead flagged the 3-vs-mass discrepancy -- exactly the scenario this "decision gate" pause exists for.
6. This read-only audit (all `-readonly`/`mode=ro` SQLite connections, zero writes, `quick_check=ok` reconfirmed) found the mass-cohort gap above. `ROADMAP.md`/`CHANGELOG.md` updated to honestly reopen PH4-03 with the full counts and a concrete mass-migration plan; this file updated; nothing else touched.

## Exact next step

1. Owner reviews the reopened PH4-03 section in `ROADMAP.md` (counts, ambiguous case, proposed plan) and decides: proceed with mass-migration evidence collection (bot-link flow / `OWNER_APPROVED` attestation) for the 13 active unlinked real users, resolve the 1 new ambiguous case, and decide the 5 expired users' fate -- OR explicitly override and authorize a partial (3-account) grace rollout anyway, understanding the tradeoff spelled out in PH4-05's updated entry.
2. Nothing in this repository or on production currently depends on this decision being made quickly -- `OPAQUE_SUBSCRIPTION_ENABLED`/`LEGACY_BRIDGE_ENABLED` are unchanged, no real grace clock is running, no communication was sent, and the dormant PH4-05 code causes zero user-visible change either way.
3. Do not re-run the SSH production deploy step -- it is already done (`4b49450` is live). Any future session should start from `git log -1`/`ROADMAP.md` here, not repeat the deploy.

---

# PRIOR HANDOFF (this session, PH4-05 build): still accurate, kept for continuity

Updated: 2026-08-26 (later same day). **PH4-05 is `[~]`: reversible/dormant
part only.** The owner explicitly authorized starting PH4-05 work "до
границы реального запуска grace clock" (up to, but not including, actually
starting any real account's 14-day clock). This session built and tested
the full durable grace-period schema/store, explicit-audited extension
path, privacy-safe grace telemetry (wired fail-open into the live legacy
route and the dormant opaque route), a read-only observability module, a
dry-run eligibility-report CLI, draft (unsent) communications and a
runbook. **Nothing was started for any real account, no communication was
sent, and nothing was deployed to production this session** -- deploying
the dormant schema/code, and running the dry-run report against a real
production DB copy, both require production (SSH) access that this
session's sandbox explicitly blocked pending the owner's separate
confirmation (see "Exact next step" below). PH2-06/PH4-04 remain CLOSED
`[x]` (unaffected, unchanged this session) -- their own handoff detail is
preserved unmodified further below.

## THIS SESSION: PH4-05 reversible/dormant part

### New durable schema/store (`src/legacy_grace_schema.py`, `src/legacy_grace.py`)

- `mgboost_legacy_grace_periods`: one row per account, ever
  (`UNIQUE(account_id)`). `original_end_at = started_at + 1209600` (exactly
  14 days, OPD-09/DL-023) is enforced by a schema `CHECK`, not just
  application code. `current_end_at` starts equal to `original_end_at` and
  is guarded by a DB trigger that rejects any decrease -- extension-only,
  matching DL-023's own Rollback clause. Identity columns and the full
  `mgboost_legacy_grace_events` audit log are immutable (no-update/
  no-delete triggers), mirroring PH4-02's `LEGACY_REVOKED`-terminal
  precedent.
- `LegacyGraceStore` (`db.legacy_grace`): `start()`/`extend()` both require
  the same sealed `PrimaryAdminAuthority` capability every other
  PH3-06/PH4-01..04 consequential action already requires.
  - `start()` is idempotent per account (same idempotency key -> same row
    returned, no duplicate); a genuinely new start attempt for an account
    that already has one fails closed (`GraceAlreadyStarted`) -- there is
    no "restart the clock" operation anywhere in this code.
  - `extend()` requires a strictly later `new_end_at` (`GraceTransitionError`
    on a no-op/shrink attempt) plus a CAS `expected_revision`
    (`GraceStaleRevision` on a stale write), and always writes an
    immutable, reason+evidence-ref audited `EXTENDED` event.
  - Pure helpers `grace_active()`/`seconds_remaining()`/`day_index()`
    implement the exact tested boundary: `now < current_end_at` is still
    within grace; `now == current_end_at` (and later) is already expired.

### New privacy-safe grace telemetry (`src/legacy_grace_activity*.py`)

- `mgboost_legacy_grace_activity_daily`: daily per-account/per-channel
  (`LEGACY`/`OPAQUE`) request counters only -- never a raw token, full
  subscription URL, UUID, full HWID, cookie/auth value or bearer path.
  Mirrors PH3-07's own isolated-short-timeout-connection write discipline.
  60-day retention; cleanup script
  (`scripts/cleanup_ph4_05_grace_activity_telemetry.py`) exists but is
  **not yet on a systemd timer** (follow-up, to be installed together with
  the eventual production deploy).
- **Wired into the already-live `routes/sub.py::handle_sub` legacy path**
  (once `legacy_bridge.resolve_account_for_legacy_username` resolves a real
  account) **and** the dormant `routes/opaque_sub.py::handle_opaque_sub`
  real-resolve path, as a fail-open, response-blind observation hook
  (`_observe_grace_activity_fail_open`, same exception-swallowing pattern
  as the already-deployed PH3-07 `_observe_compatibility_fail_open`).
  Proven by `tests/test_legacy_grace_route_hooks.py` that an observer
  failure never changes status/body. **This is the one piece of this
  session's change that touches the already-live legacy `/sub` request
  path** (one extra read + a fire-and-forget write per real request) --
  flagged here explicitly because it is not purely dormant like everything
  else in this session, and deserves its own explicit mention in the
  owner's deploy sign-off even though it cannot start any clock or change
  any response.

### New read-only observability + dry-run report

- `src/legacy_grace_observability.py::account_grace_snapshot()` assembles
  grace day/remaining time, PH4-02 migration state counts, active vs.
  migrated device counts, last legacy/opaque activity, 24h/72h request
  counts, resolver/reconciliation/revoke-rebind event counts (from PH4-02's
  own existing `mgboost_migration_binding_events`, not a new error log) and
  `inactive_since_grace_start` -- composed from already-existing tables
  wherever one exists, zero mutation anywhere in this module.
- `scripts/ph4_05_grace_eligibility_report.py`: read-only CLI
  (table/json/csv), one row per account with a legacy alias:
  `account / migration_state / active_devices / last_legacy_activity /
  last_opaque_activity / compatibility / blockers /
  START_GRACE|HOLD`. Validated end-to-end against a synthetic local DB
  (real production data was never touched or read this session -- see
  "Exact next step").
- `docs/PHASE4_GRACE_PERIOD_RUNBOOK.md`: status/extend/support procedures,
  metrics mapping, exceptions handling, "what becomes hard to walk back."
- `docs/PHASE4_GRACE_PERIOD_COMMS_DRAFT.md`: draft Telegram/LK/support-
  ticket copy, explicitly marked not sent, no send path wired to it.

### Tests / regression

38 new focused tests across `tests/test_legacy_grace_schema.py`,
`tests/test_legacy_grace.py`, `tests/test_legacy_grace_activity.py`,
`tests/test_legacy_grace_observability.py`,
`tests/test_legacy_grace_route_hooks.py`. Full regression: `969 passed, 3
skipped` (was `931 passed, 3 skipped` before this session -- zero
regressions).

## Why production access was not used this session

The sandbox's auto-mode classifier blocked an SSH connection attempt to the
production VPS (the same `selara_vps` key/host prior sessions used) as a
risky action requiring the owner's explicit confirmation. Given the owner's
own instruction to stop "до границы реального запуска grace clock" and to
NOT perform any production mutation this session, no attempt was made to
work around that block. As a direct consequence:

- The dry-run eligibility report was **not** run against real production
  accounts -- only validated against synthetic local test data.
- The dormant schema/code (which the owner asked to be deployed "если это
  не запускает clock и не меняет user-visible behavior") was **not**
  deployed to production -- it exists only in this git history/branch.

## Exact next step (decision gate)

1. Owner decides whether to authorize SSH access to production for (a)
   downloading a DB copy to run the real dry-run eligibility report, and
   (b) deploying this session's dormant schema/code (additive migration +
   the two new fail-open route hooks described above -- explicitly call
   out the live-route hook change during that sign-off, separate from the
   purely-dormant pieces).
2. Once the real dry-run report is in hand, the owner picks the first real
   cohort (account ids) to actually `start()` -- this remains a separate,
   explicit decision this session did not make and was told not to make.
3. Only after that: a short-lived root-only script (same pattern as every
   PH4-03/04 real production canary) calls `db.legacy_grace.start()` for
   the chosen accounts, and the drafted communications
   (`docs/PHASE4_GRACE_PERIOD_COMMS_DRAFT.md`) get reviewed/approved and
   sent through their own real channel (not built this session).
4. PH4-06 (the actual legacy URL/UUID revoke) remains its own separate,
   unbuilt future phase, gated on PH4-05's real grace periods actually
   expiring for the chosen cohort.

---

# PRIOR HANDOFF (PH2-06 / PH4-04, still accurate, unaffected by this session)

Updated: 2026-08-26, PH2-06 remains CLOSED `[x]` (unaffected). PH4-04 was
briefly REOPENED `[~]` for a post-closure correction and is now CLOSED
`[x]` again after production re-verification. PH2-06 added
subscription-fetch rate limiting + a socket deadline ahead of exposing the
new opaque endpoint; PH4-04 wired PH2-01's dormant opaque credential system
all the way to production and its original canary genuinely passed. Owner
manual production testing then found two real regressions the canary's
scope never covered: an ordinary browser opening the opaque URL got the
uniform invalid response instead of the existing legacy browser landing
page, and a bare repeat of `/newsub` implied consent to silently rotate an
already-`ACTIVE` credential (a destructive action). Both were fixed in
code and tests (browser landing reused from `src/routes/sub.py`, `/newsub`
converted to a two-step explicit-confirm flow, and the same silent-rotation
gap closed in the LK/admin issue routes), deployed to production (encrypted
backup+restore verified, fast-forward deploy, all 4 services healthy), and
re-verified end-to-end for real on the owner's own account: bare-repeat/
confirm-click leave the credential untouched, the explicit final confirm
rotates it for real with zero device/child-table mutation, the new URL gets
the fixed browser landing page over the real public Internet path with zero
mutation, and the owner's manually-exposed credential was rotated via a
transient/memory-only, root-only mechanism -- no raw token/UUID/HWID was
ever printed to any report/stdout/docs/git this session. Full regression
`931 passed, 3 skipped`. PH4-05 and the 14-day grace clock remain
explicitly NOT started, per instruction, and must not be started without
the owner's separate explicit permission.

## PRIOR SESSION SUMMARY (PH4-03, still accurate)

PH4-03 CLOSED `[x]`. A migration-only legacy paid compatibility entitlement
(owner decision) closed that session's subscription/plan blocker; both real
DIRECT/EXTERNAL_PAYMENT cohort accounts (`cohort-2 account #3`/`#4`,
account ids 3/4) completed a full real production migration canary
(migrate + revoke + one rebind proof) with zero impact on either real
customer's own device. TELEGRAM_STARS cohort remains an owner-approved
`N/A` exception (zero real Stars purchases ever existed). See the full
"PRIOR SESSION HISTORY" section further below for details.

## THIS SESSION: PH2-06 + PH4-04

### PH2-06 -- subscription/API abuse controls (`[x]`)

- `src/subscription_rate_limit.py::SubscriptionRateLimiter` -- per-client-IP
  in-memory sliding window (60s / 30 requests, conservative technical
  defaults, no product impact), same architecture as the existing
  `AdminLoginRateLimiter`. Keyed by IP only (via the already-existing
  trusted-XFF `http_utils.client_ip()`), never by token/token-hash -- a
  malformed-token flood shares the ordinary per-IP budget. Wired as the
  very first check in both `handle_sub` and `handle_opaque_sub`, before any
  token parsing/resolver/upstream work. A limited request gets a uniform
  `429`/`Retry-After`, never a token-validity oracle.
- `_Handler.timeout = 15` in `src/server.py` -- a plain socket read
  deadline on the single-threaded stdlib server, bounding how long one slow
  client can occupy it. Never fires mid-mutation (no further socket reads
  once request processing starts).
- Verified (not reinvented) that body/size/malformed-ID/uniform-failure
  requirements were already satisfied: legacy token length bound
  (`_MAX_LEGACY_TOKEN_LENGTH`), opaque route's exact-`{43}`-char regex,
  bounded HWID regex, shared `_invalid_subscription_response` helper,
  bounded broker-call timeout (`BrokerTransport.timeout`, ≤30s).
- `tests/test_subscription_rate_limit.py` (15 passed) + new
  `tests/conftest.py` (autouse fixture resetting the shared limiter between
  tests -- many pre-existing tests call these same routes). Full
  regression at this point: `884 passed, 3 skipped`.
- Deployed: additive code only (no schema), fast-forward pull, minimal
  restart, verified `quick_check=ok`/0 FK/services/legacy-`/sub`-still-404
  -on-bogus-token after deploy.

### PH4-04 -- new opaque URL rollout (`[x]`)

**Code (all committed, `d8fbf84` then nginx-only prod change):**

- `src/subscription_credential_issuance.py::issue_or_reissue_credential` --
  the ONE crash-safe orchestration: abandon any stale `PENDING_DELIVERY`
  (unrecoverable) -> `prepare()` a fresh generation (old stays `ACTIVE`) ->
  `deliver_fn(raw_token)` -> only if delivery did not raise, `activate()`
  (atomically flips new->`ACTIVE`, old->`REVOKED`). New
  `SubscriptionCredentialStore.abandon_pending()` in
  `src/subscription_credentials.py` (no new store invented).
- **Admin**: `src/routes/subscription_credentials_admin.py`,
  `GET/POST /admin/accounts/{id}/subscription-credential(/issue)` --
  `require_admin_auth` (session+CSRF) AND the server-derived primary-admin
  capability (first LIVE route wiring of that PH3-06/PH4-01 boundary; every
  prior use was test-only). Raw token returned exactly once in the issue
  response, never in status, never logged.
- **Telegram**: hidden `/newsub` command in `bot_support.py`,
  `F.chat.type == ChatType.PRIVATE` only, requires
  `db.accounts.get_account_for_telegram()` (canonical PROVEN owner, not
  mere link possession). Not a visible keyboard button (would confuse the
  many still-legacy-only users). Async delivery handled manually (not via
  the sync `issue_or_reissue_credential` helper -- `message.answer()` is a
  coroutine) but the exact same prepare -> deliver -> activate sequence.
- **LK**: `GET/POST /lk/api/opaque-subscription(/issue)` in `src/routes/lk.py`,
  gated by the exact same `_require_mgmt_session` boundary every other
  destructive LK device action already requires -- never the bare legacy
  subscription token alone (PH2-05: possession ≠ ownership).
- **nginx**: new `location ~ "^/[A-Za-z0-9_-]{43}$"` on `sub.beykus.fun`
  (quoting the regex is required -- unquoted `{43}` breaks nginx's own
  config lexer, caught by `nginx -t` before reload), reusing the exact
  `/sub/`'s sensitive-log/security-header/`X-Real-IP` handling. Every
  reserved prefix location wins over it under nginx's own matching rules
  regardless of declaration order (verified). Config root-backed-up to
  `/root/config-backups/ph4-04/sub.beykus.fun.pre-ph4-04.bak` before
  editing, `nginx -t`-verified, then reloaded; legacy `/sub/`/`/lk/`
  reachability and the new route's dormant-by-flag behavior both confirmed
  immediately after.
- **New-account "first device" limitation -- investigated, resolved by
  understanding, not by code.** Tried to bootstrap a synthetic
  `genesis_source_fn` template for a truly-new account with no legacy
  history at all; discovered the REAL broker's `child.user.ensure`
  operation cryptographically re-derives and re-verifies the claimed
  `source_contract_hash` against a real, currently-existing Marzban
  `source_username`'s LIVE config (`broker_operations.py`) -- there is no
  way to bootstrap from a purely synthetic template without weakening that
  anti-tamper check, and this system's schema already requires exactly one
  real backing legacy Marzban username for every account
  (`mgboost_legacy_account_aliases.legacy_username`, `NOT NULL UNIQUE`). A
  zero-Marzban-footprint "brand new signup" is not a flow this system
  supports today (PH5 catalog/billing territory, not built). **Reverted**
  the `genesis_source_fn` plumbing entirely (`git checkout --` on
  `opaque_resolver.py`/`opaque_sub.py`) rather than ship dead/misleading
  code. PH4-01/02 migration already requires and performs the same
  one-time real-legacy-user-derived genesis-child seeding per account
  before its first device resolves (this session's own earlier PH4-03 work
  did exactly this for accounts #3/#4) -- PH4-04's actual acceptance ("new
  accounts don't depend on legacy URL") is about the end user never needing
  the OLD `/sub/{legacy_token}` URL again once their opaque credential
  exists, which needed zero code change.
- 33 new focused tests across 4 files (`test_subscription_credential_issuance.py`,
  `test_subscription_credentials_admin_routes.py`, `test_bot_newsub.py`,
  `test_lk_opaque_subscription_routes.py`): crash/lost-delivery convergence,
  admin auth/CSRF/IDOR/one-time-token/non-primary-rejection, bot
  private-chat-filter/ownership-check/failed-send-safety, LK
  mgmt-session-vs-bare-token. Full regression: `911 passed, 3 skipped`
  (zero regressions from `884`).

**Real production canary (owner's own account 1) -- PASS:**

Pre-flight: fresh encrypted backup+restore-verified (state had changed
since the last one); `.env`/nginx config root-backed-up separately.
Discovered mid-session that account 1 now has a SECOND real live device on
slot 2 (generation 5, a new HWID never seen before) -- the AGENT_HANDOFF's
own prior prediction ("if/when that device's own client next hits /sub, it
WILL now migrate transparently") came true organically between sessions.
Both slot 1 and slot 2 were treated as real and never touched by any canary
action; every canary device used slot 3+ instead.

Flipped `OPAQUE_SUBSCRIPTION_ENABLED=1` (was unset -> default off),
restarted, then via a short-lived root-only script (raw token held only in
memory, never printed -- only masked SHA-256 prefixes/derived booleans):
issued a real credential through the admin route, made real external HTTPS
requests to `https://sub.beykus.fun/<token>` with a real supported client
shape (`happ/2.7.0/windows`, already in `compat_registry`'s allowlist --
first attempt used an unrecognized platform header and got a uniform 404,
second attempt with the correct `x-platform` header worked). Proved: a new
canary device gets a real working VLESS config (verified by base64-decoding
the body -- the raw HTTP body is base64, `has_vless` checks must decode
first); a second distinct canary device gets its own separate child; the
account's real legacy shared UUID is absent from both canary configs;
rotation immediately invalidates the old token (uniform 404, same shape as
any unknown token) while the new token keeps working and the underlying
child is untouched by the rotation itself; the PH2-06 rate limiter fired
for real over the public Internet path (`429` appeared inside a 40-request
same-IP burst). Full leakage scan after: zero 43-char token-shaped strings
in nginx access/error logs or the application journal; DB only ever holds
64-hex `token_hash` values; audit events hold only reason/actor text.
Cleanup: both canary devices (slot 3, across two script iterations while
debugging the platform-header bug) were revoked+freed, leaving permanent
`REVOKED`/`RELEASED` tombstones per this project's own retention
convention. Final state: slot 1 and slot 2 (both real devices) confirmed
untouched throughout; the real legacy Marzban user (`beykusios`) confirmed
`active` and unchanged; `quick_check=ok`, 0 FK violations; all 4 services
stayed active. `OPAQUE_SUBSCRIPTION_ENABLED` left permanently `1`
(graduated, matching `LEGACY_BRIDGE_ENABLED`'s own PH4-03 precedent) --
account 1 now has one real, working, rotated (`generation 4`) opaque
credential; no other account has one yet.

New `docs/PHASE4_OPAQUE_URL_RUNBOOK.md`: issue/lost-delivery/rotate/revoke/
pause-issuance/disable-route/verify-leakage/support-a-locked-out-user, no
secrets/PII.

## PH2-06 verdict: `[x]` (unaffected). PH4-04 verdict: `[x]` -- the original
canary and this session's post-closure correction (browser landing +
`/newsub`/LK/admin silent-rotation fix, production-deployed and
re-verified, owner's exposed credential rotated) are both closed. See
`ROADMAP.md`/`CHANGELOG.md` for full evidence.

## Exact next step

None for PH4-04/PH2-06 -- both closed. PH4-05 (grace period) and the
14-day grace clock remain explicitly NOT started, per instruction, and
require the owner's separate authorization to begin. No other PH4-03/04
residual is known.

---

## HEAD / git status

- HEAD after this session's commits (see `git log -1`); pushed to
  `origin/main`, deployed to production (pull + `mgboost-panel` restart),
  production HEAD verified to match.
- Working tree clean except pre-existing untracked `extra_configs.json`.

## THIS SESSION (part 3): legacy paid compat entitlement + real migration canary PASS -> PH4-03 `[x]`

Owner decisions this session:
1. Legacy paid compatibility entitlement is migration-only, never a
   commercial catalog entry: historical default device limit `3`, never
   inferred from current device/HWID counts; an owner-approved increase is
   explicit `3 + approved_extra_device_slots` with recorded evidence.
   Unlimited legacy WL (no quota bytes). Exact legacy expiry preserved.
2. Cohort-2 accounts #3/#4 (already reviewed-enrolled DIRECT accounts from
   the prior session) are confirmed real paying customers; proceed with
   their real migration canary once the compat entitlement exists.

### Code (new)

- `src/legacy_paid_compat.py` -- `ensure_legacy_paid_compat_entitlement()`.
  No new schema: reuses `mgboost_plan_versions`/`mgboost_subscriptions`
  (PH3-01) as-is. Creates/reuses a `LEGACY_PAID_COMPAT_V1_D{n}` plan
  version (immutable, `plan_kind='COMMERCIAL'`, `billing_required=0`,
  `wl_mode='UNLIMITED'`, `wl_quota_bytes=NULL`) and one live subscription
  per account with the account's exact already-reviewed legacy
  expiry/status (a terminal `DISABLED`/`EXPIRED` state is preserved as-is,
  never promoted to `ACTIVE`). Guards: requires an existing DIRECT review
  + owner-attested legacy payment; `observed_device_count > derived limit`
  fails closed (`DeviceOverageConflict`); a differing existing subscription
  fails closed (`SubscriptionConflict`); idempotent retry via the existing
  one-live-subscription-per-account partial unique index.
  **Important discovered constraint:** `DeviceSlotStore._entitlement_capacity`
  hard-requires `plan_kind='COMMERCIAL'` and `device_limit` in the existing
  `PAID_BASELINE_LIMITS={3,6,12}` frozenset for any `DIRECT` account -- a
  future compat `Dn` outside that set (e.g. `D4`/`D5`) would need that
  frozenset extended in `src/device_slots.py` (a plain code constant, not
  schema-locked) before it could actually claim a slot; not needed this
  session since both real accounts got `D3`.
- `tests/test_legacy_paid_compat.py` -- 18 focused tests (device-limit
  derivation incl. D4/D6, plan-variant reuse, device-rows-don't-raise-quota,
  overage fails closed, exact expiry, WL unlimited, no price reconstruction,
  idempotent retry, no duplicate/conflicting subscription, missing
  prerequisite guards, expired/disabled legacy never gets a fresh paid
  period, identity/provenance unchanged, and a full reviewed-enrollment ->
  attestation -> compat entitlement -> PH4-02 migration -> child
  integration test). Full regression: `869 passed, 3 skipped` (zero
  regressions from `851`).
- Deployed: encrypted backup+restore-verified beforehand
  (`scripts/secure_db_backup.py`, PASS/PASS; production state had changed
  since the prior backup). No schema change (pure application code) --
  fast-forward pull, minimal restart, `quick_check=ok`, 0 FK violations,
  only the two new rows in `mgboost_subscriptions`/`mgboost_plan_versions`
  (assigned in the next step) changed cardinality.

### Real production assignment + migration canary -- DONE, both accounts PASS

Fresh re-verification immediately before any mutation (both accounts):
still `active` in Marzban with unchanged `expire`, still exactly one
unambiguous Telegram mapping each, zero evidence anywhere (notes, tickets,
audit log, node filters, per-user configs) of an individually approved
device-limit increase -> both assigned the historical default
`LEGACY_PAID_COMPAT_V1_D3` (their own real usage is ~2 devices each, so D3
does not constrain them). Method: a short-lived root-only script per step
(dry-run-verified against a downloaded copy of the production DB first,
then run for real with `cd /opt/MGBoost_Panel` so `DATA_DIR` resolves
correctly), deleted immediately after each use.

Migration canary sequence, run on account A (cohort-2 account #3) first,
then account B (cohort-2 account #4) only after A fully passed:

1. **Genesis child** (real broker, before any bridge binding exists):
   `resolve_account_device()` never invents an account's first child -- it
   requires an already-established `source_contract_hash` from
   `mgboost_child_user_intents`, or it returns `PROVISIONING_UNAVAILABLE`
   (not a fall-through outcome). A real child was bootstrapped directly
   through the existing PH3-03 `child_provisioning` pipeline (real
   `get_user`/`ensure_child_user` broker calls) on the account's own slot 1,
   entirely BEFORE any `mgboost_legacy_bridge_bindings` row existed -- so
   the real customer's own device was never exposed to this gap even for a
   moment. **This bootstrap step is a real discovered prerequisite for any
   brand-new DIRECT account's first migration, not specific to these two
   accounts** -- worth remembering for any future cohort.
2. **Bridge binding created + enabled** (`db.legacy_bridge.create_binding`,
   `enabled=1`, per-account decision_ref).
3. **Migration proof**: a synthetic canary device (never the customer's own
   HWID) on the account's own spare slot, through the unmodified
   `process_migration_bridge_request` -- real `LEGACY -> MIGRATING ->
   MIGRATED`, real new child, real working subscription body, legacy
   Marzban user confirmed `active` throughout, no shared legacy UUID.
4. **PH3-05 revoke** the canary child -- confirmed `REVOKED`. A same-device
   retry afterward returned `PROVISIONING_PENDING` (NOT `OK`, NOT a
   fall-through outcome) -- no resurrection, no silent legacy fallback,
   exactly matching account 1's own earlier internal-canary proof.
5. **PH3-05 free** the canary slot -- returned to `FREE`.
6. **(Account A only) supplementary PH3-05 rebind proof**, on the
   already-freed canary slot (zero real-device impact): a new canary device
   claimed the slot (generation 2), then `process_rebind()` moved it to
   generation 3 with a different synthetic HWID. One real snag hit and
   fixed live: `process_rebind()` only prepares the new child's outbox
   entry -- it hands off actual provisioning to the worker/next resolver
   call, it does NOT ensure/acknowledge synchronously. The production
   `mgboost-child-worker` hadn't picked up the pending row within ~40s of
   polling by the time this was checked, so the new child was finished
   manually via the exact same `child_provisioning.claim()` ->
   `ensure_child_user()` -> `acknowledge()` sequence the worker itself
   would use -- then verified end-to-end (`process_migration_bridge_request`
   for the new HWID returned `OK` with the new child). Old (generation 2)
   child confirmed `REVOKED` throughout. Cleaned up afterward with the same
   revoke+free sequence, leaving the slot `FREE` again.

Verified before and after every step, both accounts: exact legacy
expiry/status, account `public_id`, ownership review, and owner-attestation
rows all unchanged; `mgboost_payment_records` stayed empty throughout (no
invented payment was ever created); the real legacy Marzban user's
`status`/`expire` were read-verified unchanged at multiple checkpoints;
`quick_check=ok`, 0 FK violations; all 4 services stayed active. Final
state per account: slot 1 = the real permanent genesis child (`ACTIVE`),
slot 2 = `FREE` (all canary/rebind-proof children on it are permanent
`REVOKED` tombstones, matching this project's own retention convention),
slot 3 never claimed -- 2 free slots remain for each customer's real ~2
devices going forward.

### TELEGRAM_STARS and Telegram ownership rebind -- unchanged from the prior session

Still a documented owner-approved `N/A` exception (zero real purchases ever
existed) and still relying on existing focused tests + PH2-05's own
production-proven mechanism (no real customer's Telegram identity was
mutated this session either). See the prior "THIS SESSION (part 2)" section
below for the full reasoning, unchanged.

### Metrics/support runbook -- DONE

`docs/PHASE4_MIGRATION_SUPPORT_RUNBOOK.md`: how to read an account's
migration state/lineage, a compat entitlement's `Dn`/expiry/WL semantics,
ownership/payment provenance, how to recognize and react to
`ERROR_RECONCILE`, and what to do (and not do) when a canary/migration
attempt fails. Deliberately excludes secrets/PII -- every example is by
account id.

## PH4-03 verdict: `[x]` -- CLOSED

All ROADMAP accept-criteria items are satisfied: internal cohort PASS
(prior session); several real DIRECT/`EXTERNAL_PAYMENT` subscriptions PASS
(this session, both accounts, migrate+revoke+one rebind proof); `TELEGRAM_STARS`
documented `N/A` exception; account identity/payment provenance/manual
renewal semantics preserved (same account/plan/PH3-08 sync model
throughout, untouched); Telegram ownership rebind sufficiently proven via
existing tests/PH2-05 evidence without an unnecessary live mutation;
metrics/support runbook exists; full regression clean (`869 passed, 3
skipped`). PH4-08 (full legacy-subscription-preservation/renewal flow) and
PH5-09 remain explicitly their own future phases, both still `[ ]` --
PH4-03 only needed and built the minimal migration-compatibility
prerequisite, not PH4-08's full scope.

## Exact next step

PH4-04 (new opaque URL rollout) is the next ROADMAP phase, but was
explicitly NOT started this session and requires the owner's separate
authorization to begin, per instruction.

---

## PRIOR SESSION HISTORY (kept for continuity, all still accurate)

## THIS SESSION (part 2): real DIRECT/EXTERNAL_PAYMENT cohort + owner decisions

Owner supplied 3 authoritative product decisions this session:
1. All real legacy paying users historically paid the owner directly, never
   Stars; record this as `OWNER_ATTESTED_LEGACY_EXTERNAL_PAYMENT` with no
   invented amount/date/reference.
2. Zero real Stars purchases ever existed in production; TELEGRAM_STARS
   cohort = owner-approved `N/A` exception, not a failure, must be
   documented, not silently skipped.
3. Reuse the existing bot Telegram-linkage flow (`tg_users`/`bot_support.py`,
   the `waiting_link` state that resolves a pasted subscription URL to a
   `marzban_username` via `marzban.get_username_for_token` then calls
   `db.save_tg_user(message.from_user.id, username)`) instead of building a
   second mechanism. That flow proves POSSESSION of the subscription link,
   not ownership by itself (confirmed by reading it: `save_tg_user` will
   happily rebind a username to a different Telegram ID with no ownership
   check at all -- this is exactly why the excluded ambiguous-ownership legacy account has two conflicting
   `tg_users` rows). PH2-05's "HWID/URL is not ownership proof" rule is
   therefore NOT weakened: `enroll_direct_account()` treats a bot-linked
   mapping as evidence only when combined with owner review/attestation, and
   now cross-checks it defensively (new `TelegramMappingConflict`/ambiguity
   checks, see below).

### Code changes (commit `b31e3a1`)

- `src/legacy_payment_attestation_schema.py` (new) — additive
  `mgboost_owner_attested_legacy_payments` table + immutability triggers +
  a validate trigger requiring an already-reviewed DIRECT account. Its own
  `MIGRATION_ID`/checksum, parented on `direct_enrollment_schema`'s
  checksum. Deliberately NOT a change to `mgboost_payment_records` --
  that table's CHECK constraints are already checksum-locked by the
  deployed PH3-09 migration (`apply_provenance_schema` would raise
  `RuntimeError` on every future startup if that file's `_SCHEMA_STATEMENTS`
  were edited in place). This is the general rule for ALL of this project's
  schema files, not just this one: never edit an already-shipped
  `_SCHEMA_STATEMENTS` tuple; add a new sibling migration instead.
- `src/direct_enrollment.py`:
  - `DirectEnrollmentStore.record_owner_attested_legacy_payment()` — no
    caller-supplied idempotency key; the natural key is `account_id` itself
    (at most one attestation per account, `UNIQUE(account_id)` in schema
    too). Same full payload (decision_ref/note/evidence) twice ->
    idempotent, same row returned. Different payload for an account that
    already has one -> `OwnerAttestationConflict`, nothing changed. Also
    writes a `mgboost_entitlement_mutations` row
    (`operation='OWNER_ATTESTED_LEGACY_EXTERNAL_PAYMENT'`,
    `mutation_source='MANUAL_PAYMENT'`, `payment_channel='EXTERNAL_PAYMENT'`
    -- already an allowed combination in `ProvenanceStore`, no schema
    change needed there) so it appears in the same canonical audit trail as
    every other provenance mutation, even though it lives in a sibling
    table rather than `mgboost_payment_records`.
  - `enroll_direct_account()` now cross-checks `tg_users` before accepting
    `PROVEN`: more than one distinct Telegram ID already linked to this
    legacy username -> `AmbiguousOwnershipRejected`; caller asserts a
    Telegram ID that contradicts the single bot-recorded one ->
    `TelegramMappingConflict` (new exception). Both fail closed, zero
    writes. If `tg_users` has no row at all for the username, no
    cross-check is possible and enrollment proceeds on the caller's
    evidence alone, same as before.
- `tests/test_direct_enrollment.py`: +9 tests covering all of the above
  (owner-attested no-fabricated-data, idempotent retry, conflicting details
  rejected, requires-reviewed-account, bot-mapping-reused-not-duplicated,
  conflicting-bot-mapping-fails-closed, ambiguous-two-Telegram-IDs-fails-
  closed, Stars validation unchanged, new schema idempotent). Total in this
  file: 25 passed. Full regression: `851 passed, 3 skipped` (zero
  regressions from the 842 baseline).
- Deployed: encrypted backup+restore-verified BEFORE the schema change
  (`scripts/secure_db_backup.py`, PASS/PASS), fast-forward pull, minimal
  restart, post-deploy `quick_check=ok`, 0 FK violations, only the new
  table appeared (all other cardinalities identical), no journal errors.

### Real production DIRECT/EXTERNAL_PAYMENT enrollment — DONE

Candidates were the 2 identified in this session's earlier read-only
discovery (only 2 users in all of production have unambiguous, evidenced
Telegram ownership outside the excluded/internal set): `cohort-2 account #3`
(account id 3) and `cohort-2 account #4` (account id 4).

Pre-mutation re-verification (fresh, same session, immediately before
running): both still `active` in Marzban, same `expire` as discovery,
`tg_users` still exactly one distinct Telegram ID each (unchanged from
discovery), `tickets` corroborates `cohort-2 account #4`, no pre-existing
`mgboost_accounts`/alias/review/payment row for either, no Stars invoices
for either username. Zero drift, zero conflict -- proceeded.

Method: a short-lived root-only script (`/root/ph4_03_direct_cohort_enroll.py`,
0700, deleted immediately after use -- same discipline as account 1's
session), first dry-run-verified against a real downloaded COPY of the
production DB (caught and fixed a real bug: the script's first production
run used the wrong `DATA_DIR`/cwd and would have created/touched a stray
`/root/data/db.sqlite3` instead of the real database -- this was caught
before it mattered, verified the real production DB was untouched, deleted
the stray file, and re-ran with `cd /opt/MGBoost_Panel` so `DATA_DIR=./data`
resolved correctly, exactly matching the real service's own
`WorkingDirectory`). Real run: called `enroll_direct_account()` then
`record_owner_attested_legacy_payment()` for each username, via
`db.primary_admin_authority.authorize_session()` using the real
`PRIMARY_MGBOOST_ADMIN_LOGIN` from production `.env`.

Result: 2 new `ACTIVE` `DIRECT` accounts (ids 3/4), 1 reviewed alias each
(`EVIDENCE_PROVEN`), 1 Telegram `OWNER` identity each linked via the
existing `AccountStore.link_telegram_owner` (reusing, not duplicating, the
bot's own `tg_users` mapping), 1 `mgboost_owner_attested_legacy_payments`
row each (no invented amount/date/reference). `mgboost_legacy_bridge_bindings`
unchanged (still 1 row, only account 1) -- these enrollments are additive
and dormant, zero effect on live legacy traffic. Post-mutation verification:
real Marzban `cohort-2 account #3`/`cohort-2 account #4` completely unchanged (`active`,
same `expire`), `quick_check=ok`, 0 FK violations, all 4 services active.

### TELEGRAM_STARS cohort — owner-approved N/A exception

Zero real successful Stars purchases exist in production history. The only
2 `stars_invoices` rows ever created are both `refunded` test canaries for
the excluded ambiguous-ownership legacy account. Per owner decision: this is documented as
`N/A -- no real production population existed at PH4-03`, not silently
skipped and not faked. No artificial purchase was created, no real user was
asked to buy Stars to satisfy this phase. The Stars code path
(`record_stars_payment`/`process_direct_stars_enrollment`) remains fully
covered by focused tests. **The first real successful Stars purchase after
launch requires its own real canary gate before any wider Stars rollout --
this is a standing requirement, not yet satisfied by anything in this
session.**

### THE one remaining PH4-03 acceptance blocker: real migration on a DIRECT account

Not a missing candidate, not a missing mechanism gap in the enrollment
code -- a genuine architectural prerequisite gap discovered this session:

`resolve_account_device()` (the shared PH2-01/PH4-01 tail that
`process_migration_bridge_request` ultimately calls) calls
`db.parent_sync.refresh_desired_state(account_id)`, which raises
`ParentSyncError("account has no subscription to derive entitlement from")`
if the account has no `mgboost_subscriptions` row --
`mgboost_subscriptions.current_plan_version_id` is `NOT NULL` unless
`status='UNKNOWN_LEGACY'`. That exception is caught as a generic
`Exception` -> `OUTCOME_INTERNAL_ERROR`, which is **not** in
`_FALL_THROUGH_OUTCOMES` -- so `_try_legacy_bridge()` in `routes/sub.py`
would NOT fall through to the normal legacy response; it would return a
fail-closed error response instead.

`enroll_direct_account()` deliberately does not create a
`mgboost_subscriptions`/`mgboost_plan_versions` row, because doing so would
require declaring a device_limit/WL mode for `cohort-2 account #3`/`cohort-2 account #4`'s
historical (unproven-tariff, legacy-Marzban-never-enforced-a-device-cap)
plan -- exactly the invented catalog tariff the owner explicitly forbade
this session ("Не назначать новый catalog tariff, если исторический tariff
не доказан").

Critically, this is NOT a risk that a synthetic-canary-only device
sidesteps: `LegacyBridgeStore.resolve_account_for_legacy_username()` is
username-level, not per-device -- the moment an `enabled=1`
`mgboost_legacy_bridge_bindings` row exists for one of these accounts (and
`LEGACY_BRIDGE_ENABLED` is already `1` globally in production), the
customer's OWN real device would hit the exact same missing-subscription
path on its very next ordinary legacy `/sub` request and get the fail-closed
error too -- a real outage for a real paying customer, not a contained
canary risk. So no bridge binding was created for either account, and no
migration/revoke/rebind was attempted.

This is precisely PH4-08's own scope ("Preserve legacy manual/
external-payment subscriptions... plan/conditions... ambiguous provenance
получает UNKNOWN_LEGACY", depends on "authoritative payment/admin
evidence" -- now available via this session's owner attestation) and was
correctly out of this session's scope, not something to improvise around.

Real PH2-05 ownership rebind on a non-internal account was likewise not
attempted (per owner instruction: existing focused integration tests +
account 1's real production mechanism proof are sufficient; do not mutate
a real customer's Telegram identity solely to check a box).

## PH4-03 verdict this session: remains `[~]`

## THIS SESSION: reviewed DIRECT enrollment/payment foundation (additive, dormant)

Added the DIRECT-cohort counterpart PH4-03 needs before any real DIRECT/Stars
or DIRECT/external-payment cohort can be enrolled. Nothing here is wired into
any live HTTP/bot route -- it is only new, importable, tested store code plus
new empty tables.

- `src/direct_enrollment_schema.py` — `mgboost_direct_enrollment_intents`
  (durable, pre-account-creation idempotency anchor; `account_id` is
  fill-once, enforced by a DB trigger) and `mgboost_direct_account_reviews`
  (separate from and never touching PH3-06's INTERNAL-only
  `mgboost_internal_account_reviews`; its own DB trigger requires
  `account_source='DIRECT'`). Parent schema gate: PH3-03
  (`child_provisioning_schema`), same as PH4-01's legacy bridge schema.
- `src/direct_enrollment.py` — `DirectEnrollmentStore`
  (`db.direct_enrollment`):
  - `enroll_direct_account()` — creates the account only via the existing
    `AccountStore.create_account('DIRECT')` (as explicitly required), reuses
    the already-generic PH3-03 `mgboost_legacy_alias_groups`/
    `mgboost_legacy_account_aliases` tables unchanged, writes the DIRECT
    review audit row (legacy username, ownership evidence, actor,
    decision_ref), and links the Telegram owner via the existing
    `AccountStore.link_telegram_owner()` if ownership is `PROVEN`.
    Ambiguous ownership (anything other than exactly `PROVEN`/`ABSENT`)
    fails closed with zero writes. One legacy username can never bind to two
    accounts (checked in-application before any account is created, and
    backstopped by the existing DB `UNIQUE(legacy_username)` constraint).
    Crash-safe: a durable intent row is claimed BEFORE
    `AccountStore.create_account()` is ever called, so retrying with the
    same idempotency key after a crash at any point converges on exactly one
    account/alias/review, never a duplicate.
  - `record_stars_payment()` — a real `stars_invoices` row only becomes a
    canonical `mgboost_payment_records` row (via the existing
    `ProvenanceStore.record_payment`) if its status is `paid`/
    `plan_committed`/`applied`; `refunded`/`refund_unknown`/`manual_review`/
    `created` are rejected (`InvoiceNotPayable`). The invoice's
    `marzban_username` must match the account's reviewed legacy username,
    and its `payer_telegram_id` must match the account's reviewed Telegram
    owner (`PayerMismatch` otherwise). Duplicate invoice recording is
    idempotent (same invoice -> same payment row, no duplicate).
  - `record_external_payment()` — minimal admin-only primitive for
    `payment_channel='EXTERNAL_PAYMENT'`/`mutation_source='MANUAL_PAYMENT'`,
    the low-level PH5-09 prerequisite only (PH5-09 itself -- renewal/plan
    changes on manual payment -- is NOT implemented and NOT marked done).
    Duplicate `external_reference` is rejected by the existing
    `ProvenanceStore` `UNIQUE(payment_channel, external_reference)`
    constraint.
  - `process_direct_stars_enrollment()` — the one orchestration flow tying
    enrollment + Stars payment together; proven by test to converge to
    exactly one account/alias/review/payment across a simulated crash
    between steps and a full-flow retry.
- `tests/test_direct_enrollment.py` — 16 focused tests: happy path, retry/
  idempotency, idempotency-key-reused-with-different-payload conflict,
  ambiguous ownership fail-closed, cross-account alias conflict,
  unauthorized review, paid/refunded/manual-review Stars, payer mismatch,
  duplicate Stars invoice, external payment, duplicate external reference,
  crash/retry across the orchestration flow. All pass.
- Full regression: `842 passed, 3 skipped` (was `826 passed, 3 skipped`
  before this session's 16 new tests — zero regressions).
- Production deploy: additive schema only (new tables start and remain
  empty), no route/worker calls any of this code, `LEGACY_BRIDGE_ENABLED`
  and all other flags/state from the prior internal-canary session are
  unchanged. Post-deploy invariants verified: services active, `quick_check
  =ok`, 0 FK violations, new tables present and empty.

### NOT done by this session

- No real DIRECT account, alias, review, Stars payment or external payment
  was created anywhere, including production. the excluded ambiguous-ownership legacy account (or any other
  real paying legacy user) was NOT touched or enrolled.
- PH5-09 itself (manual-payment-driven renewal/plan changes) is intentionally
  NOT implemented — only its low-level `EXTERNAL_PAYMENT`/`MANUAL_PAYMENT`
  provenance primitive exists now.
- PH4-03 remains `[~]` in `ROADMAP.md` — cohorts 2/3 (real DIRECT/Stars,
  real DIRECT/external-payment) are still blocked on the owner supplying (or
  authorizing selection of) real candidate identities; see the existing
  "Cohorts 2 and 3" section below, which is still accurate and unchanged by
  this session.

## PH4-03 goal (ROADMAP.md, `Depends: PH3-06/09, PH4-01/02`)

Controlled canary migration, cohort order: internal users -> several
DIRECT/Stars subscriptions -> several DIRECT/external-payment subscriptions
-> mass migration. Internal-only is explicitly NOT sufficient. Accept:
representative clients migrate/device-rebind/revoke + admin-only Telegram
ownership rebind; account identity/payment provenance/manual renewal
preserved.

## Done so far

### 1. Live route wiring (code, committed+deployed)

- `src/routes/sub.py::_try_legacy_bridge` now calls PH4-02's
  `process_migration_bridge_request()` instead of the bare
  `resolve_legacy_bridge()`. Same resolver, durable per-device lineage now
  recorded on every real activation. Flag-off/no-binding behavior proven
  byte-identical (`tests/test_legacy_bridge_route.py`).
- Commit `8058772`, pushed, pulled to production, `mgboost-panel` restarted,
  all 3 services active, HTTP 200.

### 2. Focused tests (all pass)

- `tests/test_ph4_03_migration_cohort_integration.py` (6 passed): migration
  on a real (non-internal) DIRECT account preserves payment provenance
  (TELEGRAM_STARS + EXTERNAL_PAYMENT channels, zero new provenance rows
  written by migration) and account identity; coexists with PH3-08 manual
  renewal (`refresh_desired_state` reflects renewal, lineage untouched);
  ORDINARY ownership rebind preserves lineage + opaque token; COMPROMISE
  ownership rebind rotates the opaque token but does NOT touch/replace the
  migration lineage and never creates a second parent account.
- Full regression: `826 passed, 3 skipped` (was 820 before PH4-03 — zero
  regressions).

### 3. Real production internal canary — DONE

Identity used (pre-verified against live production before use, matched
exactly): account id `1`, public_id `acct_435p4hjeoxeq3bzg4ifkdut4veower4r`,
primary legacy alias `beykusios`, Telegram owner `905302972`, actor
`owner:mgboost-primary:v1`.

Steps actually performed on production (all via short-lived root-only 0600
scripts, deleted immediately after use — none left on disk):

1. Created `mgboost_legacy_bridge_bindings` row for account 1 (enabled=1) —
   via `db.legacy_bridge.create_binding`, real admin capability.
2. Backed up `/opt/MGBoost_Panel/.env` to `/root/config-backups/ph4-03/`,
   appended `LEGACY_BRIDGE_ENABLED=1`, restarted `mgboost-panel` (confirmed
   with user permission — this restart was blocked once by the auto-mode
   classifier and explicitly re-authorized by the user before proceeding).
3. Ran a controlled real migration for a NEW synthetic device HWID
   (`ph4-03-internal-canary-device-1`) on slot 2 (FREE) — deliberately did
   NOT touch slot 1 (the account's real live daily-use device) to avoid any
   risk of disrupting real connectivity. Result: `OK`, real new child
   `mgc_pdj7eq4i2v4y6nuw2l65j4322u`, `MIGRATED` binding
   `mg_bdsxk2vjthv2rycuu5v3ldfgau`, legacy user (`beykusios`) confirmed
   still `active`/untouched, new child confirmed `active` with 25 VLESS
   inbounds.
4. Real PH3-05 REVOKE on that same canary child — `APPLIED`, remote child
   confirmed `disabled`. Follow-up migration attempt for the same device
   correctly returned `PROVISIONING_PENDING` (fail-closed, NOT a
   fall-through/legacy-fallback outcome) — proves "no silent shared-UUID
   fallback" and "no resurrection of a revoked generation" empirically on
   real production, not just in tests. (Root cause understood: PH3-05
   REVOKE alone does not free the slot by design — a device stays
   deliberately non-functional until FREE/REBIND; this is correct, expected
   behavior, not a bug.)
5. Real PH3-05 FREE to release slot 2 back to `FREE` (cleanup after the
   controlled canary proof) — `APPLIED`.
6. Post-canary invariants verified: `quick_check=ok`, 0 FK violations, slot
   1 (real device) completely untouched throughout, legacy user untouched,
   `mgboost_migration_bindings` = 1 row (the canary's own historical
   lineage, correctly `MIGRATED`, preserved as permanent audit trail — not
   deleted, matching this project's convention).

Real Telegram-ownership-rebind on the REAL account 1 was deliberately NOT
performed in production (would rebind the actual owner's real Telegram
identity — too invasive/irreversible-feeling for a proof-of-mechanism);
that requirement is instead satisfied by the focused tests in item 2 above,
which exercise the real `process_rebind()` orchestration end-to-end.

### Current live production state

- `LEGACY_BRIDGE_ENABLED=1` (changed from the PH4-02 baseline of `0` —
  this is intentional and is THE PH4-03 canary activation, not a residual).
- `mgboost_legacy_bridge_bindings`: 1 row (account 1, enabled).
- `mgboost_migration_bindings`: 1 row (account 1's canary device, state
  `MIGRATED`).
- Slot 1 (account 1's real live device): untouched, still on its original
  child `mgc_sgg6v7t6he43yytsqmkdczzfpa`. If/when that device's own client
  next hits `/sub`, it WILL now migrate transparently (same already-existing
  child, by design) via the new durable route wiring — this is expected and
  intentional per the internal cohort's own purpose, not an open risk.
- No other account has a binding. `OPAQUE_SUBSCRIPTION_ENABLED=False`,
  `PH3_04_ENFORCEMENT_MODE=OFF` unchanged.

## NOT yet done (superseded by "THIS SESSION (part 2)" above — kept for history)

Cohorts 2/3 candidate selection is DONE (`cohort-2 account #3`/`cohort-2 account #4`,
enrolled as reviewed DIRECT/`EXTERNAL_PAYMENT`, see above). What remains is
the single architectural blocker documented above (`mgboost_subscriptions`/
plan prerequisite for real migration), not a candidate-identity gap.

### Remaining accept-criteria items — updated

- Real device migrate/revoke/rebind on a non-internal account: **blocked**
  on the subscription/plan prerequisite gap above (PH4-08 territory) —
  requires an owner product decision on device_limit/WL semantics for
  unproven-tariff legacy accounts before it can proceed safely.
- Real PH2-05 admin ownership rebind proof on a non-internal account: not
  attempted, per owner instruction that existing focused tests + account
  1's real mechanism proof are sufficient.
- metrics/support runbook (ROADMAP's own accept line mentions this — still
  not drafted).

## Known non-blocking backlog

- None discovered this session beyond what's already noted above (the
  PH3-05 REVOKE-without-FREE "stuck at PROVISIONING_PENDING" behavior is
  confirmed correct-by-design, not a defect).

## Explicitly NOT started

PH4-04 (opaque URL rollout), PH4-05 (grace), PH4-06 (production legacy
revoke), PH4-08 (legacy plan/device-capacity preservation — now the actual
blocker), PH5-09, mass migration, PH5+.

## Exact next step if resumed

1. This is now a product decision, not a data-gathering task: ask the owner
   how device_limit/WL semantics should be set for a reviewed DIRECT
   account whose historical legacy tariff is unproven (legacy Marzban never
   enforced a device cap at all) — e.g. an explicit `UNLIMITED` device
   plan to literally preserve legacy behavior, vs. drafting PH4-08 properly
   first. Do not invent an answer unilaterally.
2. Once decided: implement the minimal piece needed (likely a small
   addition to `DirectEnrollmentStore.enroll_direct_account()` or a
   dedicated PH4-08 module) that creates a `mgboost_subscriptions` +
   `mgboost_plan_versions` row consistent with that decision, for the 2
   already-enrolled accounts (ids 3/4) — do NOT re-enroll them, they
   already exist and are reviewed.
3. Only then create `mgboost_legacy_bridge_bindings` for account 3 first
   (enabled=1), re-verify current production state immediately before, and
   watch the very next real legacy `/sub` request for that username
   (should now migrate transparently instead of failing closed) — same
   "prove on `cohort-2 account #3` first, then `cohort-2 account #4`" order the owner
   already specified.
4. After both are proven migrated (and revoke/FREE proven on a synthetic
   device the same way account 1's canary was), update `ROADMAP.md` PH4-03
   to `[x]`, `CHANGELOG.md`, commit, push, verify production HEAD parity,
   final report.
5. If quota runs out before an owner decision arrives: this file plus
   `git diff`/`git log` is sufficient for a fresh agent to resume exactly
   from "waiting on owner's device/WL semantics decision for unproven-tariff
   DIRECT accounts."
