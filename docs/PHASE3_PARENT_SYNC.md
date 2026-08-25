# PH3-08 — parent status/expiry -> active child generations sync

Date: 2026-08-25. Status: dormant. No legacy route, resolver or worker
service imports `src/parent_sync.py`. No sync operation has ever been created
against the production canary or any other production row.

## Core principle

The parent account/subscription is the source of truth for whether its
*current* (non-terminal) child generations should be active in Marzban, and
at what effective expiry:

```
parent desired state (mgboost_entitlement_state, an existing PH3-01 table
that nothing wrote to before this module)
    -> durable per-child sync op (this module's outbox)
    -> typed `child.user.state.sync` broker call
    -> authoritative reread
    -> local ACK
```

PH3-08 never touches individual devices -- revoke/free/rebind stay PH3-05's
job. It only ever flips `status`/`expire` on children that are still the
*live* generation of their slot, and it never touches `proxies`/UUID.

## Suspend is not revoke

This is the property the whole module exists to guarantee, proven both in
`tests/test_parent_sync.py` and against real Marzban 0.8.4:

```
ACTIVE parent               -> child ACTIVE, expiry = parent effective expiry
EXPIRED/DISABLED parent     -> child DISABLED, same UUID, same generation
parent renewed/re-enabled   -> the SAME non-terminal child ACTIVE again
```

The typed `child.user.state.sync` broker operation (`src/broker_operations.py`)
only ever sends `{"status": ...}` or `{"status": "active", "expire": ...}` --
it never includes `proxies` in the Marzban PUT, so a plain suspend/resume can
structurally never rotate a UUID. The handler additionally rereads after the
mutation and raises if the UUID it observes differs from the UUID it observed
before the mutation -- a hard STOP if Marzban itself were ever found to
rotate credentials on a bare status/expire PUT (it does not, confirmed
against real Marzban 0.8.4).

### A real cross-module fix this uncovered

The real-Marzban gate caught a genuine bug before it ever reached production:
PH3-05's `child.user.revoke` broker handler used to treat *any* remote
`status=disabled` child as `ALREADY_REVOKED` and skip re-mutation. Once
PH3-08 can also set `status=disabled` (for a reversible suspend, UUID
untouched), that pre-existing idempotency check would have made a *real*
PH3-05 revoke against a merely-suspended child into a silent no-op -- the
credential would stay alive. Fixed by checking the UUID verifier first: only
a `disabled` child whose UUID no longer matches what the caller has on file
(proof a real revoke already rotated it) short-circuits as `ALREADY_REVOKED`;
a `disabled`-but-same-UUID child (the PH3-08 suspend case) falls through to a
real rotate. Covered by
`tests/test_parent_sync.py::test_ph3_05_revoke_still_rotates_uuid_for_a_ph3_08_suspended_child`
and reproduced against real Marzban in the staging gate below.

## Canonical parent desired state

`src/parent_sync.py:compute_desired_status` is a pure function of only real
`mgboost_accounts.status` / `mgboost_subscriptions.status` /
`current_expiry` fields -- never a caller-supplied status or expiry:

- account not `ACTIVE` -> `DISABLED`
- subscription `UNLIMITED` -> `UNLIMITED`
- subscription `ACTIVE` and `current_expiry > now` -> `ACTIVE`
- subscription `ACTIVE` and `current_expiry <= now` (boundary inclusive)
  -> `EXPIRED`
- subscription `EXPIRED` -> `EXPIRED`
- anything else (`PENDING`/`DISABLED`/`CANCELLED`/`UNKNOWN_LEGACY`)
  -> `DISABLED`

`ParentSyncStore.refresh_desired_state` writes this into PH3-01's existing
`mgboost_entitlement_state(account_id, subscription_id, desired_status,
revision)` table, bumping `revision` only on a real transition (idempotent
refreshes never churn it). `child_target_for` maps the canonical status to
the minimal Marzban target: `ACTIVE` -> `(active, current_expiry)`,
`UNLIMITED` -> `(active, 0)` (this codebase's existing unlimited
convention), `EXPIRED`/`DISABLED` -> `(disabled, None)` -- expire is never
sent when disabling, since it is irrelevant while the status gate blocks
usage.

## Which children participate

`enqueue_current_children` selects only child intents whose slot generation
is currently `ACTIVE` (an SQL join against
`mgboost_device_slot_generations.status='ACTIVE'`) and whose
`desired_state != 'REVOKED'`. This is structural, not conventional: a
PH3-05-revoked or released generation can never be selected, so a parent
renewal can never resurrect a removed device
(`test_revoked_generation_is_excluded_and_never_resurrected_by_renewal`, and
the same scenario against real Marzban in the staging gate).

## Stale-operation / race protection

Every sync op is stamped with the parent revision that produced it
(`derive_sync_operation_id(child_username, parent_revision)` -- a new parent
revision always derives a different operation id). `claim()` re-checks that
stamped revision against the *live* `mgboost_entitlement_state.revision`
immediately before a worker may dispatch the remote mutation; a mismatch
marks the op `SUPERSEDED` and returns `None` without ever calling the broker.
This is what stops a stale queued ENABLE from winning after a DISABLE, and
symmetrically a stale queued DISABLE from winning after a renewal -- both
directions are covered in `tests/test_parent_sync.py` and reproduced against
real Marzban in the staging gate.

## Durable architecture

Additive-only `ph3_08_parent_sync_v1` schema (`src/parent_sync_schema.py`,
depends on the exact PH3-01 and PH3-03 checksums):

- `mgboost_parent_sync_operations` -- one durable row per (child intent,
  parent revision). `UNIQUE(child_intent_id, parent_revision)` gives the same
  convergent-retry idempotency as PH3-03's outbox and PH3-05's lifecycle
  table. States: `PENDING/IN_FLIGHT/RETRY/APPLIED/ERROR/SUPERSEDED`. Identity
  columns are immutable via trigger; no physical DELETE path.
- `mgboost_parent_sync_attempt_events` -- append-only, mirrors PH3-03/PH3-05's
  attempt-events tables.

`ParentSyncStore` follows the exact prepare/claim/acknowledge lease pattern
established by `ChildProvisioningStore`/`ChildLifecycleStore`: `BEGIN
IMMEDIATE` transactions, a lease with `lease_owner`/`lease_expires_at`,
`attempts`/`next_attempt_at` for retry/backoff, and `record_error`/
`retry_later` for the worker loop to classify failures. `run_account_sync_cycle`
(refresh -> enqueue -> claim+dispatch every claimable op) is the single
entrypoint a worker or the panel would call; every step is independently
idempotent, so a crash/restart/duplicate call at any point converges safely.

`aggregate_state(account_id)` reports one of `IN_SYNC`, `PENDING`, `PARTIAL`,
`MANUAL_REVIEW` across all of an account's current children at the live
revision -- this is what "3/6/12 children converge; partial failure visible"
from the ROADMAP accept criteria maps to.

## Typed broker boundary

`child.user.state.sync` is the only new broker operation
(`src/broker_operations.py`). Request: exactly `{operation_id,
child_username, desired_status, desired_expire, uuid_verifier}` -- all
server-derived/verifier values. Server logic: reread; 404 ->
`REMOTE_MISSING` (PH3-08 never auto-creates a remote child -- that stays
PH3-03's job, this outcome is surfaced as a permanent error for
reconciliation/hand-off, never silently ignored); identity check; UUID
verifier check (constant-time, fail-closed on mismatch -- never a blind
patch of a possibly-wrong remote user); if already in the desired
status/expire, return `ALREADY_IN_SYNC` without any PUT at all; otherwise
send the minimal `{status}` or `{status, expire}` PUT, reread, verify
convergence, and assert the UUID is unchanged (a `RuntimeError` STOP
condition otherwise). `mgboost-main` gets this capability automatically
(the broker's operation allowlist is `BROKER_OPERATIONS` minus
`child.user.credentials.get`); the resolver-only `mgboost-sub-resolver`
identity is unaffected -- its allowlist is a fixed literal
`{"child.user.credentials.get"}`, never derived from the general operation
set.

Per PH3-05's own documented architecture note, the broker is intentionally
DB-less and cannot independently re-derive `operation_id` from
`(child_username, parent_revision)` -- the real authorization binding lives
one layer up in `ParentSyncStore.claim()`, which only ever dispatches a
payload read verbatim from an operation that was itself derived server-side
by `enqueue_current_children` from the live DB. There is no code path that
lets a caller pick an arbitrary child or arbitrary target state.

## New child initialization

A child newly created by PH3-05's REBIND inherits the *old* child's stale
outbox `expire` snapshot (unavoidable -- PH3-03's provisioning payload has no
other source at hand, and PH3-08 deliberately does not duplicate or modify
PH3-03/PH3-05's provisioning logic to fix this in place). The safe
integration point is operational: running `run_account_sync_cycle` for the
account immediately after a new child (initial or rebind) is provisioned
corrects it to the *current* parent state on the very next cycle, before the
child should be considered ready for use -- proven in
`test_rebind_new_generation_converges_to_current_parent_state_not_a_stale_snapshot`.
Wiring this into the actual worker loop is a live-integration decision this
task deliberately leaves dormant (see "Not in scope" below).

## Focused tests

`tests/test_parent_sync.py`, 26 passed: pure policy (active/exact-boundary/
expired/disabled/unlimited/pending), revision discipline (creates rev 1,
never churns on a no-op refresh, bumps on a real transition), end-to-end
cycles (active-sync, expire-disables-without-rotation, renewal reactivates
same generation/UUID/no-new-provisioning, already-in-sync is a pure no-op
dispatch, multi-child partial-convergence + aggregate state,
cross-account isolation), lifecycle interaction (revoked generation excluded
and never resurrected, PH3-05 revoke still rotates a PH3-08-suspended
child's UUID, rebind's new generation converges to current parent state),
race protection (stale enable-after-disable and stale disable-after-renewal
both superseded, never dispatched), and broker-level checks (verifier
mismatch, remote-missing never auto-creates, disable never sends `expire`,
malformed operation id rejected).

Full project regression: `704 passed, 3 skipped`.

## Real isolated Marzban 0.8.4 staging (2026-08-25)

Ran against the same immutable digest used by every other PH3-0x gate
(`gozargah/marzban@sha256:8e422c21997e5d2e3fa231eeff73c0a19193c20fc02fa4958e9368abb9623b8d`)
on a disposable loopback instance -- `scripts/verify_ph3_08_parent_sync_staging.py`.
No production child or account was used. A synthetic parent with 3 real
children was created via the existing PH3-03 pipeline. Result: **PASS**, all
16 checks green:

- ACTIVE parent + finite expiry -> all 3 children active with that exact
  expiry, UUIDs unchanged from creation;
- parent EXPIRED -> all 3 disabled, same UUIDs, same generations;
- one child PH3-05-revoked while the parent was expired -> real
  status=disabled + UUID rotation, confirmed by authoritative reread;
- parent renewed -> the 2 non-revoked children active again with the new
  expiry and their original UUIDs, zero new outbox rows (no re-provisioning),
  same slot generations throughout; the revoked child stayed disabled and
  was never resurrected by the renewal;
- a stale enable operation (queued while the parent was still active, then
  superseded by the parent going expired) was never dispatched --
  the child stayed at its correct (disabled) state;
- a full second sync cycle after the above converged cleanly to `IN_SYNC`
  (worker-restart / lost-ACK convergence);
- a Marzban outage during a sync attempt raised rather than reporting a
  false success;
- no raw UUID appeared anywhere in the MGBoost DB dump.

This run is also what caught and proved the fix for the cross-module
ALREADY_REVOKED gap described above: the first attempt at this exact
scenario failed with the pre-fix broker code (a real PH3-05 revoke against
an already-PH3-08-disabled child silently no-opped instead of rotating), and
passed after the fix.

An important integration detail this staging run also confirmed: Marzban
0.8.4 derives a user's *effective* runtime status from `expire` vs. its own
wall clock regardless of the `status` field sent in the PUT (e.g. a PUT with
`status=active, expire=<a timestamp already in the past>` is observed back
as `status=expired`). `child_target_for`'s finite-expiry branch always uses
the parent's real (`current_expiry`) value, so this is a non-issue for
correct callers -- but any future caller of `child.user.state.sync` must
supply a real wall-clock-relative expiry, matching how `current_expiry` is
already stored everywhere else in this codebase.

## Production dormant deploy and read-only evaluation

See `ROADMAP.md` PH3-08 for the exact dormant-deploy cardinality evidence and
the read-only dry-run desired-state evaluation for the existing production
account 1. No real production status/expiry transition was performed --
account 1's existing subscription is not touched by this task, and a genuine
ACTIVE->EXPIRED->RENEWED production canary would require a new throwaway
parent (account 1 cannot itself be safely put through that cycle), which is
an explicit separate owner decision this task does not take on its own.

## Not in scope for PH3-08

Wiring `run_account_sync_cycle` into any live worker loop or scheduled job,
billing/tariffs/Stars/renewal-purchase logic, WL quota/period semantics
beyond "an expiry change never resets one" (there is currently no WL period
implementation to reset -- `mgboost_wl_periods` remains unused, matching its
PH3-01 dormant state), Phase 4, and a real production ACTIVE/EXPIRED/RENEWED
canary transition are all untouched. PH3-08 only ever operates on the
already-computed parent state; it does not decide what that state should be.
