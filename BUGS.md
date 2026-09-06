# BUGS — independent static audit, 2026-09-06

Baseline: `main` / cached `origin/main` = `98e27fe9e4ea72727dab36436e573d701c83c3b7`.
Only defects reproduced against this checkout are listed. Production data, deployment,
frequency and customer impact were not inspected: **UNVERIFIED_PRODUCTION**.
`Production reachability: YES` means an existing production entrypoint can execute the
path when configured; it does **not** assert a live incident. No runtime fix was made.
Severity is conditional impact, not measured incident prevalence. No confirmed P0.

Five bounded, synthetic cases ran in one local harness; final exit 0. No pytest suite,
HTTP, Telegram, Marzban, staging or load execution. Network is explicitly forbidden
inside the harness. Full reproducible source is retained below; test helpers only
build synthetic accounts/children and FakeMarzban. See [RISKS.md](RISKS.md) for hypotheses.

# BUG-001 — Manual payment can be cancelled after its entitlement has committed

Severity: P1
Confidence: CONFIRMED
Status: OPEN
Production reachability: YES
Related roadmap: PH5-09, PH5-10, PH7-10, PH7-11, PH8-09

## Симптом

The payment remains PENDING after an apply crash, so admin can successfully cancel a
payment whose 30-day entitlement is already ACTIVE. No application/sync-job row is
written. A cancelled payment then cannot be recovered by ordinary apply.

## Evidence

- `src/manual_payment.py:482` `apply_record` reads the record before dispatch.
- `src/manual_payment.py:507` `_apply_plan_locked` calls the independently committing renewal.
- `src/manual_payment.py:555` starts the later payment-bookkeeping transaction.
- `src/manual_payment.py:442` `cancel_record` checks record status and a confirmed legacy
  transition, but not an already committed renewal mutation; edit at line 271 has the same gap.
- `src/routes/admin_payments.py` `handle_manual_payment_apply`, `handle_manual_payment_cancel`,
  `handle_manual_payment_edit` are registered in `src/server.py` behind primary-admin auth.
- `tests/test_manual_payment_ph509.py:605` already constructs this PENDING-after-commit
  state, but retries apply directly; it never attempts cancel/edit in between.
  The race test at line 408 changes only `comment`, not plan/duration/reference.

## Reproduction

Harness case `manual_crash_cancel`: create BASIC/30d record; inject an exception
immediately after real `apply_same_plan_purchase` commits; close and reopen Database;
call real `cancel_record`. Result: PENDING → CANCELLED while subscription is
ACTIVE, expiry=2592200 (200 + 30*86400). This needs no simultaneous HTTP requests.

## Root cause

The logical payment operation spans independent transactions without a durable
apply-in-progress/frozen-contract fence shared by apply, edit and cancel. The renewal
idempotency key prevents double duration, but does not freeze the editable payment.

## Impact

Financial evidence contradicts delivered entitlement. An operator sees a successful
cancellation with access still granted. Editing a pending contract in the same crash
window can also desynchronise its immutable renewal evidence; that variant is a
code-supported concern, not an additional reproduced bug in this audit.

## Failure boundary

Canonical renewal COMMIT → payment application/sync-job/status COMMIT. The
process-local RLock cannot cover a restart or a separate Database/process.

## Suggested fix

Freeze the authoritative payment contract before entitlement mutation and make every
editor/canceller consult the same durable state. Alternatively atomically commit the
payment fact and entitlement with composable transaction ownership. Reconciliation
must detect existing mutation keys before offering cancellation; do not subtract days.

## Regression test required

Crash/reopen → cancel, edit duration/plan/reference, and retry; force interleavings
across two independent SQLite connections. Assert payment snapshot, term, mutation,
application and sync job all agree, including failure before the final bookkeeping.

## Dependencies / rollout concerns

Audit existing PENDING/CANCELLED records against deterministic entitlement mutation
keys before repair. Compensation/refund is a separate owner-approved operation. Fix
this fence before adding automatic manual-payment retries (PH8-09).

# BUG-002 — Manual WL packages are sold but omitted by WL enforcement

Severity: P1
Confidence: CONFIRMED
Status: OPEN
Production reachability: YES
Related roadmap: PH5-03, PH5-04, PH5-09, PH6-04, PH6-06, PH6-08, PH7-10

## Симптом

A paid +50 GB RUB package leaves 40 GB usable remainder in the entitlement/UI after
110 GB of a 100 GB base period, while the production enforcement decision is EXCLUDED.

## Evidence

- `src/routes/admin_payments.py:124` lists package SKUs; lines 174 and 296 preview/create
  them, with no PH6-08 readiness gate. Apply calls `ManualPaymentStore.apply_record`.
- `src/manual_payment.py:617` `_apply_package_locked` durably grants through PH5-03.
- `src/entitlement_engine.py:237` adds package remainder to base remaining.
- `src/wl_parent_pool.py:104` compares consumption with **base_quota_bytes** only.
- `src/wl_enforcement.py:137` `decide_direction_from_pool` and line 1108 cycle use that pool.
- Timer → `scripts/run_wl_quota_enforcement.py` → reconciliation → enforcement is wired.
- `tests/test_manual_payment_ph509.py:222` verifies package grant; `tests/test_wl_packages.py`
  verifies derived buckets; neither assertion proves paid-package enforcement.

## Reproduction

Harness case `manual_package_enforcement`: purchase synthetic WL base, create and
apply a real store-level RUB package record for 139, write 110 GB through the actual
ledger. Actual entitlement returns 40,000,000,000 remaining bytes; actual pool decision
returns EXCLUDED. Remote mutation is intentionally not dispatched.

## Root cause

The read model composes package buckets; the enforcement authority still consumes
the original base-only pool. PH6-08 is absent, yet the manual-sales ingress is enabled.

## Impact

A sold entitlement does not restore/retain WL access. This is a reachable product
failure, not merely unfinished optional package UI. Stars package sales remain blocked;
that channel's gate does not protect MANUAL_RUB.

## Failure boundary

Payment/grant committed → subsequent quota decision. The defect occurs without
races, outages or malformed input.

## Suggested fix

Gate incomplete package sales across every channel until the effective-quota contract
is consumed by enforcement, then use one tested effective decision path. Preserve all
paid buckets and consumption. Choosing temporary customer handling requires the owner.

## Regression test required

Full local chain manual preview/create/apply → collector/pool → EXCLUDED/INCLUDED
across base crossing, package depletion, rollover, freeze/resume and unused refund.
Assert read-model and enforcement agreement, not just separate expected outputs.

## Dependencies / rollout concerns

Fix BUG-004 first for trustworthy usage; PH6-08 completion is needed before expanding
sales. Existing paid packages must be inventoried without silently cancelling them.

# BUG-003 — Promo extension followed by ordinary renewal overlaps WL periods

Severity: P1
Confidence: CONFIRMED
Status: OPEN
Production reachability: YES
Related roadmap: PH5-02, PH5-05, PH5-10, PH5-13, PH6-02, PH6-03

## Симптом

An exact-second promo period and the next normal paid WL renewal overlap within
one UTC hour. Two ACTIVE periods can cover the same timestamp.

## Evidence

- `src/subscription_renewal.py:348` `append_promo_wl_period` preserves exact seconds.
- `src/subscription_renewal.py:310` ordinary purchase floors its anchor independently.
- `src/wl_usage_ledger.py:182` resolves `ACTIVE` covering periods with unordered LIMIT 1.
- Period schema guards immutable fields but does not prohibit overlapping windows.
- Real callers: `PromoStore` via bot/LK/admin; Stars/manual/admin-grant callers of renewal.
- `tests/test_promo.py:166` covers extension, `tests/test_subscription_renewal.py` covers
  repeated ordinary renewal; neither covers this combined order.

## Reproduction

Harness case `promo_then_renewal`: ordinary WL purchase at timestamp 1000; append 1-day
promo at 1100; purchase 30d of the same WL plan at 1200. The promo ends at 2679400;
the next paid period starts at 2678400. Proven overlap: 1000 seconds.

## Root cause

Two producers use incompatible anchor rules. Adding `wl_period_id` to sample
uniqueness separates rows but cannot make overlapping producer schedules unambiguous.

## Impact

Usage attribution and the time at which fresh base quota becomes available depend on
which covering row is selected. Paid-period chronology violates the no-overlap contract.
Existing overlapping history cannot be safely fixed by editing immutable timestamps.

## Failure boundary

Successful promo COMMIT → later successful paid renewal COMMIT; manifests and
consumption readers subsequently see the inconsistent schedule.

## Suggested fix

Define the canonical handoff from exact-second promo to commercial hourly periods
with the owner; enforce non-overlap at all writers and reject ambiguous read selection.
Do not silently shorten/extend purchased duration or rewrite existing periods.

## Regression test required

Ordinary → promo → ordinary; trial → paid; expiry admin adjustment → promo → renewal;
all seconds around the hour, both payment channels, retry and existing scheduled terms.
At every instant require at most one accounting period, with explicit gap semantics.

## Dependencies / rollout concerns

OWNER_DECISION_REQUIRED: exact treatment of the partial hour (see ROADMAP decision
queue). Existing approved DL-020 and later promo exact-second requirement must both be
acknowledged; no policy was chosen here. Inventory overlaps before migration.

# BUG-004 — WL counter reset to zero permanently collides with the event replay key

Severity: P1
Confidence: CONFIRMED
Status: FIXED_IN_MAIN (2026-09-06; see "Fix evidence" below — narrow BUG-004-only session)
Production reachability: YES (was; fix has not been production-deployed — no SSH/production
access was used in the fixing session, see Fix evidence)
Related roadmap: PH6-03, PH6-06, PH6-07, PH6-09

## Симптом

After a zero reset, later real traffic is silently discarded as a replay; the cursor
can stay at zero indefinitely while collector calls succeed.

## Evidence

- `src/wl_usage_ledger.py:298` `record_sample` reads cursor_before from current DB state.
- `src/wl_usage_ledger.py:327` decrease is treated as a new reset delta.
- Event uniqueness in `src/wl_usage_ledger_schema.py` is
  `(child_intent_id,node_id,cursor_before)` with no reset epoch.
- `src/wl_usage_ledger.py:414` catches IntegrityError and returns the old event after
  rollback, without advancing the cursor or proving the same observation.
- `run_collection_cycle` counts that return as successful, permitting outcome OK.
- `tests/test_wl_usage_ledger.py:287` stops after one nonzero reset; line 248 simulates
  concurrency by manually rewinding SQL cursor state and asserts only unchanged total.

## Reproduction

Harness case `wl_reset_cursor_collision`: feed one fresh child/node cumulative values
100, 0, 50, 200 at consecutive times. Ledger total stays 100 and stored cursor stays 0;
expected new epoch traffic is not recorded. No live reset or remote service used.

## Root cause

A cumulative numeric value is not a unique observation identity across resets.
The first event already occupies cursor_before=0; after reset, every increase from 0
collides with it. Broad IntegrityError handling converts collision into false success.

## Impact

Persistent undercount can prevent quota exhaustion enforcement while freshness looks
healthy. Duration is not bounded by the polling interval, contrary to historical reset
claims. Real reset incidence is UNVERIFIED_PRODUCTION.

## Failure boundary

Reset observation commits the cursor back to an earlier numeric value → next increase
tries to reuse an immutable event key. Restart does not remove the collision.

## Suggested fix

Add a durable reset/observation epoch and fenced observation identity, distinguish
replay from a new transition, and narrow constraint-error handling. Preserve old ledger
rows; missing historical usage cannot be invented from current totals.

## Regression test required

100→0→50→200, repeated zero resets, revisit any prior nonzero cursor, exact replay,
late out-of-order poll and two collectors after lease expiry; require forward progress,
no negative/repeated charge and honest PARTIAL/ERROR on ambiguity.

## Dependencies / rollout concerns

Additive migration for event identity; inspect stuck cursors and resets before rollout.
Coordinate with ledger consumers and package refund finality. Do not repair by resetting
usage or deleting immutable evidence.

## Fix evidence (2026-09-06, narrow BUG-004-only session)

Everything above (symptom/evidence/reproduction/root cause/impact/failure boundary) is
retained unedited as the original finding. This section only records what closed it.

**Root cause confirmed unchanged at the fixing session's HEAD** (`98e27fe9e4ea72727dab36
436e573d701c83c3b7` plus the prior roadmap-reconciliation-only commit `7786b94`): read
`src/wl_usage_ledger.py`, `src/wl_usage_ledger_schema.py` and
`src/wl_usage_ledger_schema_v2.py` before writing any code. v2 (`ph6_10_wl_usage_ledger_
period_bucket_v1`) only changed the *samples* table's bucket key
(`child_intent_id,node_id,sample_hour,COALESCE(wl_period_id,0)`) to be period-aware; it
never touched the *events* table's `(child_intent_id,node_id,cursor_before)` uniqueness
this bug is about. No other commit had touched it. Confirmed still broken with a bounded
local reproduction before any fix: `100 -> 0 -> 50 -> 200` recorded ledger total `100`
(the `50` and the following `150` were both silently discarded), matching the original
finding exactly.

**Fix:** a new additive migration, `src/wl_usage_ledger_schema_v3.py`
(`bug004_wl_usage_ledger_reset_generation_v1`), adds a durable `reset_generation` column
to both `mgboost_wl_usage_cursors` and `mgboost_wl_usage_sample_events`, and changes the
events table's uniqueness key to `(child_intent_id, node_id, reset_generation,
cursor_before)`. `record_sample` in `src/wl_usage_ledger.py` now stamps every event with
the generation active *before* any bump (a reset-closing event still belongs to the epoch
it closes) and advances the cursor's stored generation by exactly one whenever
`reset_detected`. A true replay (same generation, same cursor_before) still collides
exactly as before -- idempotency is unchanged. The `except sqlite3.IntegrityError` around
the event insert was narrowed from a blanket catch to matching the *exact* new unique-
constraint message (`UNIQUE constraint failed: mgboost_wl_usage_sample_events.
child_intent_id, ...node_id, ...reset_generation, ...cursor_before`); any other
constraint failure now propagates instead of being silently treated as a harmless
duplicate.

**Migration is additive/idempotent/non-destructive:** existing cursor rows get
`reset_generation` backfilled to their own already-recorded `reset_count` (deterministic,
not a guess -- the number of resets already durably recorded *is* the current
generation); existing event rows get `reset_generation` backfilled per `(child_intent_id,
node_id)` group in strict `id` order from that group's own already-recorded
`reset_detected` flags (SQLite cannot ALTER an inline UNIQUE constraint, so the table is
rebuilt under one transaction, mirroring the exact rename/copy/verify pattern
`wl_usage_ledger_schema_v2.py` already used for the samples table). Row count and every
`(id, child_intent_id, node_id, cursor_before, cursor_after, delta_bytes,
reset_detected)` tuple are verified unchanged before/after. No row is deleted, no
delta/traffic value is changed or invented.

**Verified with targeted tests only** (no full suite, no browser, no staging, no
production/SSH/network): `tests/test_wl_usage_ledger.py` (47 pre-existing, unmodified in
behavior, still pass), `tests/test_wl_usage_ledger_schema.py` (13, incl. two new BUG-004
migration tests -- one exercising the actual pre-fix-to-post-fix upgrade path over a
hand-built "already stuck" legacy database), and the new
`tests/test_bug004_wl_usage_ledger_reset_generation.py` (11 new tests covering: normal
monotonic; the exact confirmed `100->0->50->200` scenario; repeated zero resets;
return to a previously-seen non-zero cumulative value in a later epoch; exact replay
both at generation 0 and after a real reset; DB restart/reopen between a reset and the
next sample; a second real sqlite3 connection holding a write lock (blocks rather than
races); no delta ever negative; a non-replay `IntegrityError` is never swallowed as a
duplicate; and `run_collection_cycle` never reports `OK` for an unclassifiable write).
Also directly related consumer suites re-run unmodified: `tests/test_wl_parent_pool.py`,
`tests/test_wl_enforcement.py`, `tests/test_wl_reconciliation.py`,
`tests/test_ops_observability_health.py`, `tests/test_ops_observability_redaction.py`,
`tests/test_main_bootstrap.py`. The original `wl_reset_cursor_collision` bounded
reproduction from this file was re-run against the fix and now reports the correct
total (`300`, not `100`).

**Not done in this session:** production, SSH, staging verifier, load/soak, full pytest
suite, browser/Playwright, and no other BUG (001/002/003/005) or roadmap item was
touched.

# BUG-005 — Support ticket history applies the global limit before ownership filtering

Severity: P2
Confidence: CONFIRMED
Status: OPEN
Production reachability: YES
Related roadmap: PH8-06

## Симптом

The AI support tool reports an empty history for a user who has closed tickets,
whenever the newest global tickets belong to other users.

## Evidence

- `src/bot_support.py:339` `execute_tool(get_ticket_history)` calls list_tickets first,
  filters telegram_id afterwards; limit is converted without bounds.
- `src/database.py:1765` `list_tickets` executes global ORDER BY id DESC LIMIT.
- `ask_openrouter_with_tools` invokes this tool during the real support dialogue.
- `tests/test_bot_support.py` covers tool responses but not pagination under another
  customer's newer tickets.

## Reproduction

Harness case `support_history_limit_before_owner`: one closed ticket for synthetic
user 101, followed by three for user 202; request user 101 history with limit=3.
Actual response is «История обращений пуста.», although its ticket exists.

## Root cause

Filtering after pagination changes query semantics. The global window is not the
user's window. User isolation of the returned rows does not fix the missing results.

## Impact

Support gives incorrect answers and misses prior context. No cross-user content
exposure is asserted by this reproduction. Negative/huge tool limits additionally
lack defensive bounds (see RISK-010).

## Failure boundary

Normal read under interleaved customer ticket creation; no crash is necessary.

## Suggested fix

Apply telegram_id and status predicates in SQL before LIMIT, validate a small positive
bounded limit, and return controlled errors for malformed tool arguments.

## Regression test required

Several users with interleaved closed tickets, empty user, exact limit, negative,
zero, huge and malformed limits; captured provider context must contain only own rows.

## Dependencies / rollout concerns

Independent of billing/migration. No schema rewrite is necessary; preserve the admin's
intentionally global ticket listing as a separate query contract.

# Reproduction harness — retained audit evidence

Save the following block as `/tmp/mgboost_audit_repro.py` and, from the repository,
run `timeout 25s env PYTHONPATH="$PWD" PYTHONDONTWRITEBYTECODE=1 python3 /tmp/mgboost_audit_repro.py`.
It uses the local pytest/dotenv dependencies only through fixture helper imports;
it does not invoke pytest. All database writes are under its TemporaryDirectory.
Five JSON lines with `confirmed=true` and exit 0 were observed on the audited HEAD.
This proves the stated local defects only; FakeMarzban is not an upstream contract test.

```python
import os
import tempfile
import json
import socket
from pathlib import Path

# All writes are synthetic and temporary; block network before importing app.
tmp = tempfile.TemporaryDirectory(prefix='mgboost-static-audit-')
os.environ.update(MGBOOST_SKIP_DOTENV='1', DATA_DIR=tmp.name,
    PRIMARY_MGBOOST_ADMIN_ACTOR_ID='owner:mgboost-primary:v1',
    PRIMARY_MGBOOST_ADMIN_LOGIN='authenticated-primary-login')
def no_network(*args, **kwargs):
    raise AssertionError('network forbidden in audit reproduction')
socket.socket.connect = no_network
socket.create_connection = no_network

from src.database import Database
from src.plan_catalog import seed_plan_catalog
from src.wl_package_catalog import seed_wl_package_catalog
from tests.test_manual_payment_ph509 import _capability, _account, _record
from tests.test_wl_packages import _account_with_wl, _excess_usage, _child
from src.wl_parent_pool import resolve_current_parent_wl_pool
from src.wl_enforcement import decide_direction_from_pool

db = Database()
seed_plan_catalog(db.plan_catalog, now=1)
seed_wl_package_catalog(db.wl_package_catalog, now=1)
cap = _capability(db)

# Crash after canonical entitlement commit, before payment bookkeeping.
a = _account(db)
r = _record(db, cap, a['id'], plan='BASIC', ref='audit-crash-payment')
original = db.subscription_renewal.apply_same_plan_purchase
class SimulatedCrash(BaseException): pass
def crash_after_commit(**kw):
    original(**kw)
    raise SimulatedCrash()
db.subscription_renewal.apply_same_plan_purchase = crash_after_commit
try:
    db.manual_payments.apply_record(cap, r['id'], now=200)
except SimulatedCrash:
    pass
db._conn.close()
db = Database()
cap = _capability(db)
before = db.manual_payments.get_record(r['id'])['status']
cancel = db.manual_payments.cancel_record(cap, r['id'], reason='synthetic audit cancel', now=201)
s = db._conn.execute('SELECT status,current_expiry FROM mgboost_subscriptions WHERE account_id=?', (a['id'],)).fetchone()
assert before == 'PENDING' and cancel['status'] == 'CANCELLED' and s['current_expiry'] == 2592200
print(json.dumps({'case':'manual_crash_cancel','record_before':before,'record_after':cancel['status'],'subscription_status':s['status'],'expiry':s['current_expiry'],'confirmed':True}))

# Reachable manual package application versus the real enforcement decision.
a, p = _account_with_wl(db)
r = db.manual_payments.create_record(cap, account_id=a['id'],
    package_sku='WL_PACKAGE_50_GB', external_reference='audit-package-payment',
    recorded_amount_minor=139, payment_method='bank_transfer',
    idempotency_key='audit-package-payment-00001', now=1100)
db.manual_payments.apply_record(cap, r['id'], now=1100)
period = db._conn.execute('SELECT id FROM mgboost_wl_periods WHERE account_id=?', (a['id'],)).fetchone()[0]
_excess_usage(db, a['id'], period, 110_000_000_000)
e = db.entitlements.calculate(account_id=a['id'], now=2000)
pool = resolve_current_parent_wl_pool(db, account_id=a['id'], now=2000)
direction = decide_direction_from_pool(pool)
assert e['wl']['effective_remaining_bytes'] == 40_000_000_000 and direction == 'EXCLUDED'
print(json.dumps({'case':'manual_package_enforcement','remaining_bytes':e['wl']['effective_remaining_bytes'],'direction':direction,'confirmed':True}))

# Exact-second promo followed by hour-floored commercial renewal.
a, p = _account_with_wl(db)
db.subscription_renewal.append_promo_wl_period(account_id=a['id'], days=1,
    quota_bytes=10_000_000_000, operation='PROMO_EXTEND', mutation_source='ADMIN',
    payment_channel='ADMIN_GRANT', actor_type='TEST',
    idempotency_key='audit-promo-extend-0001', now=1100)
db.subscription_renewal.apply_same_plan_purchase(account_id=a['id'], plan_code='WL',
    duration_days=30, payment_channel='TELEGRAM_STARS', mutation_source='DIRECT_PURCHASE',
    actor_type='TEST', idempotency_key='audit-renew-after-promo-0001', now=1200)
periods = list(db._conn.execute('SELECT starts_at,ends_at FROM mgboost_wl_periods WHERE account_id=? ORDER BY sequence_no', (a['id'],)))
overlap = periods[-2]['ends_at'] - periods[-1]['starts_at']
assert overlap == 1000
print(json.dumps({'case':'promo_then_renewal','overlap_seconds':overlap,'confirmed':True}))

# A real zero reset revisits cursor_before=0 from the first observation.
a, p = _account_with_wl(db)
cid = _child(db, a['id'])
for at, total in [(2000, 100), (2001, 0), (2002, 50), (2003, 200)]:
    db.wl_usage_ledger.record_sample(account_id=a['id'], child_intent_id=cid,
        node_id=4, cursor_after=total, collector_id='audit', collected_at=at)
cursor = db._conn.execute('SELECT last_observed_cumulative_bytes FROM mgboost_wl_usage_cursors WHERE child_intent_id=? AND node_id=4', (cid,)).fetchone()[0]
total = db._conn.execute('SELECT SUM(bytes_delta) FROM mgboost_wl_usage_samples WHERE child_intent_id=?', (cid,)).fetchone()[0]
assert cursor == 0 and total == 100
print(json.dumps({'case':'wl_reset_cursor_collision','remote_latest':200,'stored_cursor':cursor,'ledger_total':total,'confirmed':True}))

from src.bot_support import execute_tool
import asyncio
tid = db.create_ticket(101, status='closed')
db.add_ticket_message(tid, 'user', 'synthetic earlier request')
for _ in range(3):
    db.create_ticket(202, status='closed')
reply = asyncio.run(execute_tool('get_ticket_history', {'limit':3}, db=db,
    marzban=None, telegram_id=101, node_states={}, node_names={}))
assert reply == 'История обращений пуста.'
print(json.dumps({'case':'support_history_limit_before_owner','own_tickets':1,'reported_empty':True,'confirmed':True}))
db._conn.close()
tmp.cleanup()
```
