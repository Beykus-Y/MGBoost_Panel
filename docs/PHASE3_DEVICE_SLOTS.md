# PH3-02 — Atomic device slots with generation

Date: 2026-08-24

## Scope boundary

PH3-02 adds a dormant slot repository and two additive tables. No legacy
subscription, LK, Stars, Filin, bot or Marzban path calls this repository.
Production starts with zero parent accounts, slots and generations. Existing
`user_devices`/`hwid_lock` remain the authoritative legacy device runtime.

Explicitly absent: child Marzban users, UUID creation/revoke, existing-device
backfill, fail-closed HWID, opaque subscription resolver and plan migration.

## Schema

### `mgboost_device_slots`

- stable `(account_id, slot_number)` and immutable slot identity;
- `slot_number` is 1..99;
- kind: `BASE`, reserved `ADDON`, or `INTERNAL`;
- monotonic `current_generation`, initially zero;
- local `desired_state` and `observed_state` for future reconciliation;
- row version and timestamps;
- composite identity `(id, account_id, slot_number)` for child-account FK
  isolation.

Slots are never deleted. A free slot retains its ID, number and generation
counter.

### `mgboost_device_slot_generations`

- immutable `(account, slot, number, generation)` identity;
- versioned keyed `HMAC-SHA256` HWID verifier and HMAC-derived short mask only;
- lifecycle `ACTIVE -> RELEASED|REVOKED`;
- a terminal generation cannot become active again and cannot be deleted;
- one globally active HWID verifier and one active generation per slot.

The raw HWID and verifier key are never persisted or returned. PH3-02 accepts a
dedicated key at the dormant repository boundary; production does not need the
key until a later activation phase provisions a root-managed secret.

## Capacity contract

- Commercial accounts accept only immutable plan baselines 3, 6 or 12.
- PH3-02 rejects commercial `UNLIMITED`, arbitrary 1..99 baseline and upward
  override; device add-on sales are not active.
- INTERNAL plans accept configurable 1..99 or semantic `UNLIMITED`.
- All claims, including INTERNAL unlimited, retain the technical cap of 99.
- Active, non-expired account/subscription entitlement is required. An
  `UNKNOWN_LEGACY` account cannot claim until reviewed.
- A downward device override may make `active_count > effective_limit`. The
  repository reports `conflict/overage`, refuses a new HWID and does not select,
  release or disable any existing device.

## Transaction and generation algorithm

Every claim/release uses `BEGIN IMMEDIATE`:

1. Compute an HMAC verifier in memory; raw HWID never enters SQL.
2. If the verifier already has an active generation for the same account,
   return that exact slot/generation. If it belongs to another account, reject.
3. Resolve the account's immutable plan plus currently active device override
   inside the same database transaction.
4. Count active generations. Refuse conflict/full state before selecting a
   slot.
5. Reuse the lowest free stable slot or create the lowest unused number up to
   99.
6. Insert generation `current+1`, then compare-and-set the slot from FREE to
   ACTIVE/current+1 in the same commit.

Release requires `(account_id, slot_id, expected_generation)` and a bounded
reason. It terminates the exact active generation before marking the slot FREE.
A stale request cannot release a later generation. Reuse inserts a new
generation; the previous row remains terminal.

The Python `RLock` only prevents concurrent use of one sqlite connection. Two
independent connections/processes serialize at SQLite and are protected by
unique indexes, composite FKs, monotonic triggers and CAS predicates.

## Tests

The focused suite covers:

- additive/idempotent migration and exact PH3-01 dependency;
- commercial 3/6/12 limits and rejection of unapproved commercial capacity;
- INTERNAL configurable and unlimited through the technical 99 cap;
- duplicate HWID convergence;
- final-slot race across independent connections;
- final-slot race across two spawned OS processes;
- duplicate-HWID two-worker race;
- stable slot reuse with generation increment;
- terminal-generation reactivation rejection and stale release CAS;
- cross-account HWID, lookup, release and composite-FK rejection;
- entitlement reduction conflict without automatic device choice;
- raw HWID/key absence from result, list API and SQLite bytes;
- expired and `UNKNOWN_LEGACY` denial;
- source scan proving no legacy table, token, UUID or child integration.

Focused result: `21 passed`. Full regression: `473 passed, 3 skipped` on two
consecutive clean runs.

## Production-copy staging

The exact schema was applied twice to a fresh online copy of production DB:

- first apply: true; second apply: false;
- exact digest preserved for all 30 pre-existing non-marker tables;
- parent accounts: 0;
- slot rows: 0; generation rows: 0;
- legacy device rows/HWID locks: unchanged 71/71;
- migration checksum marker matched;
- SQLite quick check: ok; FK violations: 0.

Rollback remains application-only: an older binary ignores the additive tables.
No destructive down migration is needed.

## Read-only legacy anomaly note

The forensic script emitted no usernames, Telegram IDs, token, UUID or HWID.

### Local historical username absent from live Marzban

Exactly one local-only identity produced 14 subscription requests from
2026-04-23 through 2026-05-06 and three active legacy device rows first/last
seen 2026-04-27 through 2026-05-04. It has no Telegram link, ticket, Stars
invoice, node filter, per-user config or lifecycle audit evidence.

Classification: `ORPHANED_LOCAL_USAGE_DEVICE_EVIDENCE_FOR_NONLIVE_MARZBAN_USER`.
The evidence proves a formerly used legacy subscription identity; it cannot
distinguish deletion from rename because lifecycle audit did not exist then.
Phase 4 must retain/review it as non-live historical evidence, create no account
automatically and delete nothing.

### One live legacy username with two Telegram links

The live user has two `tg_users` links registered on 2026-06-22, fourteen
minutes apart, eight active legacy device rows, five tickets, one node filter,
18 subscription requests and two later refunded Stars invoices. The bindings
predate commit `ed5bf99` (2026-08-21), which introduced `tg_bound/tg_rebound`
audit; their missing bind events are therefore explained by code history.

Classification: `LEGACY_PRE_AUDIT_M_TO_1_TELEGRAM_BINDING`. It is consistent
with the explicitly supported old M:1 shared-subscription model, but neither
Telegram identity may be selected automatically as the sole new owner. Phase 4
requires primary-admin ownership review and preserves all evidence.

## Readiness and blockers

PH3-07 is ready: aggregate client/HWID-presence telemetry is independent and
must remain observe-only.

Before PH3-03 production activation:

- define/add a typed broker child-create operation with deterministic naming,
  no generic Marzban proxy and idempotent remote reread;
- add durable create intent/outbox and remote-created/local-ACK-failed
  reconciliation;
- complete PH3-06 internal entitlement canary and PH3-09 mutation provenance
  sufficiently to create reviewed test accounts;
- provision a dedicated HWID HMAC key before any real slot claim;
- forbid local release once a child credential exists until PH3-05 proves the
  remote credential is revoked;
- keep `/sub` legacy and HWID permissive until PH3-07/PH3-04 compatibility gate.

PH3-03 is not part of this change.
