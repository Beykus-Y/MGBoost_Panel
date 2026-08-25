# PH3-05 — durable device revoke/free/rebind lifecycle

Date: 2026-08-25. Status: dormant. No legacy route, resolver or worker
service imports `src/child_lifecycle.py`. No lifecycle operation has ever
been created against the production canary or any other production row.

## Fundamental rule

A device removal/free is only considered complete once the old child
credential is durably confirmed unusable on the VPN server. There is no
"free the local slot now, try to revoke remotely later" path anywhere in
this module.

```
requested revoke -> durable desired state -> typed broker call
    -> Marzban disable + UUID rotation -> remote reread/verify
    -> only then local lifecycle state updates -> slot may become free /
       a new generation may start
```

## Three operations

- **REVOKE** — the child stops working. `child.user.revoke` disables the
  Marzban user (`status=disabled`) and rotates its VLESS UUID to a fresh
  random value in the same mutation, then rereads and verifies both took
  effect. The old UUID is unusable even if status handling ever changes.
- **FREE** — `apply_free` refuses unless the matching REVOKE lifecycle
  operation is `APPLIED`; only then does it call the existing PH3-02
  `DeviceSlotStore.release()`. Slot history (the old, now-`RELEASED`
  generation) is never deleted.
- **REBIND** — device replacement on the *same* stable slot. Enforced
  order: revoke the old child first (reread-verified) -> atomically release
  the old generation and claim generation N+1 on the exact same slot
  (`DeviceSlotStore.rebind`, a single transaction, so no concurrent
  unrelated claim can steal the freed slot) -> hand off to the *existing*
  PH3-03 `child_provisioning.prepare_child_ensure`/outbox/worker pipeline
  for the new remote child. PH3-05 does not create the new remote child
  itself and does not duplicate any PH3-03 code.

## Ordering guarantee

For one slot, rebind can never be "successful" locally while the old child
UUID still works remotely. `process_rebind` always revokes-and-verifies the
old child before touching the slot's generation. A crash between old-revoke
and new-child-creation leaves the account briefly without a working device
on that slot rather than ever running old and new credentials at once --
this is an explicit, deliberate trade-off per the task brief, not an
oversight.

## Schema

Additive only (`ph3_05_child_lifecycle_v1`, depends on the exact PH3-03
`ph3_03_child_prerequisites_v1` checksum):

- `mgboost_child_lifecycle_operations` — one durable row per REVOKE/FREE/
  REBIND request. `UNIQUE(old_child_intent_id, operation_kind)` and a
  hash-only idempotency key give exactly the same convergent-retry
  guarantees as PH3-03's outbox. Identity columns (including `reason`) are
  immutable via trigger; the row itself can never be deleted. REBIND rows
  additionally carry `new_slot_generation_id`/`new_child_intent_id`,
  writable exactly once (a trigger blocks a second write).
- `mgboost_child_lifecycle_attempt_events` — append-only, mirrors PH3-03's
  `mgboost_outbox_attempt_events` exactly.

No physical DELETE path exists for either table (or for the PH3-03 tables
they reference) -- matching this codebase's existing permanent-tombstone
precedent. DL-019/038's 180-day retention is implemented as a pure,
injectable-clock eligibility check
(`child_lifecycle.is_eligible_for_physical_cleanup`): `>=180 days since
revoke AND no live reference`. Enabling an actual physical-cleanup process
is explicitly out of scope here -- it would need its own future schema
change to lift the immutability triggers under exactly that condition, and
is never part of an ordinary user-facing revoke/free/rebind request.

## Typed broker boundary

`child.user.revoke` is the only new broker operation. Its request accepts
exactly `{operation_id, child_username, uuid_verifier}` -- all three
server-derived/verifier values, never a caller-supplied username, UUID,
proxies, inbound list or arbitrary Marzban patch body. There is no
`marzban.request`/generic `user.patch` escape hatch. Server-side logic:

1. reread the child; if absent, return `ALREADY_ABSENT` (safe because
   `child_username` is a deterministically-derived, globally unique
   identity -- a 404 on that exact identity cannot be a different real
   target);
2. if already `status=disabled`, return `ALREADY_REVOKED` without
   re-mutating (idempotent, no double rotation);
3. otherwise verify the caller's `uuid_verifier` matches the currently
   active credential (constant-time compare) -- a mismatch is a contract
   drift/ambiguous-state condition and fails closed with `ValueError`,
   never silently proceeding;
4. mutate (`status=disabled` + fresh random UUID in one PUT), reread, and
   verify `status=disabled` before returning `REVOKED`.

`mgboost-main` (the existing broker identity) gets this capability; the
`mgboost-sub-resolver` identity introduced in PH3-03 does **not** -- it
remains scoped to only `child.user.credentials.get`, unaffected by this
change.

## Generation invariants

Enforced by the existing PH3-02 schema triggers plus the new
`DeviceSlotStore.rebind()` method (one transaction combining release+claim
on the exact same `slot_id`, avoiding the TOCTOU where a concurrent
unrelated claim could otherwise grab the just-freed slot):

- monotonic `generation+1`, immutable history, at most one `ACTIVE`
  generation per slot (all pre-existing PH3-02 guarantees, unchanged);
- a stale/terminal generation can never become current again --
  `rebind()` re-validates `current_generation == expected_generation`
  under `BEGIN IMMEDIATE` and raises `StaleSlotGeneration` otherwise;
- retrying the exact same rebind request converges on the exact same new
  generation (idempotent `EXISTING`), never creating `X+2`;
- the caller never supplies a generation number -- `evaluate`-style
  functions here take no such parameter, exactly like PH3-04's gate.

## Ownership / cross-account security

- `_prepare()` resolves the child intent strictly by
  `(id=old_child_intent_id, account_id=account_id)` -- an account can never
  prepare a lifecycle operation against another account's device.
- `DeviceSlotStore.rebind()` reuses the exact PH3-02
  `CrossAccountHWID`/same-account-different-slot checks: a HWID already
  active under a different account is a deterministic deny, never a
  takeover; a HWID already active on a different slot of the *same*
  account is also rejected (`DeviceSlotError`).
- Nothing in this module reads or writes `mgboost_telegram_identities` or
  `mgboost_accounts`. Tests assert those tables are byte-identical before
  and after every REVOKE/FREE/REBIND path, including denied ones. HWID is
  never accepted as Telegram ownership proof, never triggers a Telegram
  rebind, and never changes a parent account. PH2-05 (admin/user session
  and ownership lifecycle) has not started and has no existing
  ownership-recovery/rebind route to misuse -- there is nothing for this
  module to couple into.

## Reinstall semantics

A new HWID after reinstall is always treated as a new device candidate. If
the account has a free slot it may claim one; if slots are full it gets a
deterministic refusal. The old slot is never automatically deleted or
transferred -- replacing it requires the explicit, approved REBIND lifecycle
above, never an implicit "new HWID showed up so evict the old one" path
(which would belong to PH3-04's compatibility gate, and PH3-04 does not do
this either).

## Failure matrix (focused + staging)

Broker/Marzban unavailable, remote child missing, remote already revoked,
verifier mismatch (contract drift), lost ACK after remote success but
before local ACK, retry exhaustion (slot stays occupied, never force-freed),
cross-account HWID, cross-account intent access, caller-supplied
slot/generation/child/UUID (there is no such parameter to supply), stale
lease reclaim by a second worker, two simultaneous revokes of the same
device, two simultaneous rebinds of the same device (exactly one new
generation), and DB-dump/log privacy leakage are all covered by
`tests/test_child_lifecycle.py` (27 passed) and
`tests/test_child_lifecycle_retention_and_broker.py` (15 passed).

Full project regression: `673 passed, 3 skipped`.

## Real isolated Marzban 0.8.4 staging (2026-08-25)

Ran against the same immutable digest
(`gozargah/marzban@sha256:8e422c21997e5d2e3fa231eeff73c0a19193c20fc02fa4958e9368abb9623b8d`)
used by every other PH3-03/04 gate, on a disposable loopback instance with
synthetic credentials -- `scripts/verify_ph3_05_lifecycle_staging.py`. No
production child or account was used. Result: **PASS**, all 18 checks green:

- a real child was created (active), then really revoked: Marzban's own
  authoritative reread showed `status=disabled` and a rotated VLESS UUID
  distinct from the original;
- a duplicate revoke prepare returned the identical operation and could not
  reclaim the already-`APPLIED` lease; an independent second dispatch of
  the same typed revoke call returned `ALREADY_REVOKED` and performed no
  second rotation;
- `FREE` only succeeded after the revoke was confirmed `APPLIED`, and the
  slot's local `desired_state` only became `FREE` at that point;
- on a second slot, `REBIND` revoked the old child first (confirmed
  `disabled` before any new provisioning began), then handed off to the
  exact same PH3-03 outbox pipeline, which produced exactly one new remote
  child (verified idempotent `EXISTING` on immediate repeat) while the old
  child remained `disabled` throughout and after;
- a Marzban outage during a revoke attempt raised rather than reporting a
  false success;
- no raw UUID (original, rotated, or either new child's) appeared anywhere
  in the MGBoost DB dump.

This gate proves Marzban's own authoritative API state (disabled status +
a UUID unrelated to the original) rather than a local flag. A direct
network/Xray-protocol handshake attempt with the old UUID was not
additionally performed in this session; the evidentiary bar used here
matches every other PH3-0x gate in this project, which likewise treats a
verified Marzban API reread as the ground truth for credential state.

## Production canary gate (2026-08-25) -- PH3-05 closed `[x]`

Before any production mutation, the `child.user.revoke` broker operation's
authorization model was reviewed (see `tests/test_child_lifecycle_authorization_binding.py`,
6 passed): PASS, no code change needed -- the real DB-bound authorization
lives in `ChildLifecycleStore.claim()`, one layer above the intentionally
stateless broker.

A throwaway canary was created on a new, server-allocated slot 2 of the
existing reviewed `INTERNAL_OWNER_PRIMARY` account (`account_id=1`), using
its existing 10-device entitlement (only 1 of 10 slots was previously used,
so no entitlement change was needed). The existing PH3-03/04 dormant canary
(slot 1/generation 1/`mgc_sgg6v7t6he43yytsqmkdczzfpa`, its enabled shadow
binding) was read-verified unchanged before, during and after every step,
and no prepare/claim/mutation call ever targeted it.

Full REVOKE -> FREE -> REBIND -> functional-check -> cleanup sequence ran
against the real production broker/Marzban/worker (not a simulation):
real `active -> disabled` + UUID rotation on revoke, confirmed by
authoritative reread; idempotent duplicate revoke with zero re-rotation;
free refused until revoke was confirmed, then succeeded, leaving a
`RELEASED` tombstone with an `end_reason`; a second throwaway child on
generation 2; rebind revoked it, swapped `generation 2 -> 3` in one atomic
transaction, and handed off to the unmodified PH3-03 pipeline for exactly
one new remote child (idempotent `EXISTING` on repeat, no generation 4 on a
duplicate rebind request); the new credential was retrievable only via the
resolver-only ephemeral capability, while the same typed reread of the
just-revoked credential was denied. All three throwaway generations were
then themselves revoked and freed, leaving zero active test credentials and
a permanent tombstone history (no physical delete). A wrong-account revoke
attempt and a terminal-operation reclaim attempt were both safely rejected
using only throwaway data. Full evidence, exact masked cardinality and the
security checks are recorded in `ROADMAP.md` PH3-05.

## Not in scope for PH3-05

PH3-08 (parent-wide expiry/status propagation to all children), Phase 4,
legacy revoke, real client switch, and global PH3-04 enforcement are all
untouched. PH3-05 only ever operates on one device slot/generation per
lifecycle operation.
