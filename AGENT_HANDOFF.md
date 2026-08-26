# AGENT_HANDOFF — PH6-03 closed and production-deployed (real observe-only collection verified); PH6-04 next / Wave A authenticated walkthrough still owner-only / PH4-05 live / PH4-06 not started

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
