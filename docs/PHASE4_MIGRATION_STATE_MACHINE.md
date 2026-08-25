# PH4-02 — durable migration state machine

Date: 2026-08-25. Status: dormant, implemented. No route/worker wires
`process_migration_bridge_request` into any live path; production ships
with zero `mgboost_migration_bindings` rows.

## States

`LEGACY -> MIGRATING -> MIGRATED -> LEGACY_REVOKE_PENDING -> LEGACY_REVOKED`,
plus `ERROR_RECONCILE`. `LEGACY` is the implicit absence of a binding row --
mirrors PH4-01's own "no binding = fall through" pattern. A row is created
only once `resolve_legacy_bridge()` has already returned a non-fall-through
outcome, i.e. only after a slot has already been durably claimed by the
unmodified PH3-02/03 machinery.

One row per `(account_id, hwid_verifier)` -- one logical device, one
authoritative migration lineage, enforced by a `UNIQUE(account_id,
hwid_verifier)` constraint, never two.

## Identity stored

Only immutable/non-secret identity: `account_id`, `legacy_alias_id`,
`hwid_verifier` (the same keyed HMAC form PH3-02 already uses),
`slot_generation_id`/`child_intent_id` (references into the unmodified
PH3-02/03 tables). Never a raw legacy token, opaque token, child UUID or
HWID. The caller cannot choose an arbitrary child/generation/UUID -- both
references are only ever filled from values `resolve_account_device()`
itself already committed.

## Transition allowlist

```
MIGRATING          -> MIGRATING (retry), MIGRATED, ERROR_RECONCILE
ERROR_RECONCILE     -> MIGRATING, MIGRATED, ERROR_RECONCILE (reconcile)
MIGRATED            -> MIGRATED (no-op), LEGACY_REVOKE_PENDING
LEGACY_REVOKE_PENDING -> LEGACY_REVOKE_PENDING, LEGACY_REVOKED, ERROR_RECONCILE
LEGACY_REVOKED       -> (terminal, no outgoing transition, ever)
```

Enforced twice: an explicit `_ALLOWED_TRANSITIONS` dict in
`src/migration_lifecycle.py` (no arbitrary `UPDATE state=?` anywhere), and a
DB trigger (`trg_migration_bindings_terminal_immutable`) that independently
blocks any further mutation once `state='LEGACY_REVOKED'`. Every mutation
also carries an optimistic-concurrency `revision` CAS -- a stale
worker/request is rejected (`MigrationStaleRevision`), never silently
overwritten.

## PH4-01 integration -- no second resolver

`process_migration_bridge_request()` wraps the unmodified
`legacy_bridge_resolver.resolve_legacy_bridge()` unchanged and adds only a
durable lifecycle record on top:

```
legacy request -> resolve_legacy_bridge() [PH4-01, unmodified]
    -> fall-through outcome: return unchanged, zero binding touched
    -> durable outcome: ensure/advance the mgboost_migration_bindings row
       (prepare -> record slot/child refs -> mark MIGRATED, or retry/
       error-reconcile on failure)
```

Once a binding exists in `MIGRATING` (or beyond), a downstream outage never
silently falls back to the shared legacy credential -- `is_fall_through_
outcome()` is reused verbatim from PH4-01, and the caller
(`src/routes/sub.py::_try_legacy_bridge`) already fails closed on any
non-OK, non-fall-through outcome.

Failure classification: `PROVISIONING_PENDING`/`PROVISIONING_UNAVAILABLE`
(clearly retryable) stay `MIGRATING` and retry; `INTERNAL_ERROR` (ambiguous
-- cannot tell from a single signal whether the remote side committed) goes
to `ERROR_RECONCILE` instead of a blind retry.

## Reconciliation

`reconcile_binding()` compares the durable desired state against the
authoritative `mgboost_device_slot_generations`/`mgboost_child_user_intents`
rows -- never a single signal:

- anchored slot generation no longer `ACTIVE` (superseded by a PH3-05
  rebind/revoke) -> stays `ERROR_RECONCILE`, manual review, never
  reassigned;
- slot `ACTIVE`, no child intent yet -> `MIGRATING` (safe retry);
- slot `ACTIVE`, child intent not yet `ACTIVE` -> `MIGRATING` (safe retry);
- slot `ACTIVE`, child intent `ACTIVE` -> `MIGRATED` (lost ACK, not a real
  failure -- never creates a second child).

## Legacy revoke boundary (dormant, isolated-test/gate-only)

`MIGRATED -> LEGACY_REVOKE_PENDING -> LEGACY_REVOKED` is implemented and
capability-gated (`PrimaryAdminAuthority`, same sealed-capability pattern as
PH2-05/PH4-01) but no production code path invokes it. After
`LEGACY_REVOKED`: terminal, no rollback, ever; recovery is only via a new
child/credential lifecycle, never a resurrected shared credential. PH4-02
never autonomously starts a revoke -- that remains an explicit later Phase 4
decision (PH4-06).

## Tests

`tests/test_migration_lifecycle.py` (22 passed): idempotency, illegal
transitions, stale-revision rejection, terminal immutability (store and DB
trigger), real crash-boundary fault injection (connection close + fresh
`Database()` reopen against the same file), duplicate-operation
convergence, ambiguous-failure/lost-ACK reconciliation, stale-slot
reconciliation, concurrency (same device, two devices), fail-closed after
durable commitment, zero binding on fall-through, full end-to-end lifecycle
with an immutable event trail, the full revoke lifecycle, cross-account
isolation, zero raw-HWID storage. Full regression: `820 passed, 3 skipped`.

## Real isolated Marzban 0.8.4 gate (2026-08-25)

`scripts/verify_ph4_02_migration_lifecycle_staging.py`, same immutable
digest as every other PH3-0x/PH4-01 gate. Requires `--network host` for this
image (it binds uvicorn to loopback-only without TLS; bridged Docker
networking cannot reach it). All 23 checks PASS:

- Scenario A (forward lifecycle): `LEGACY -> MIGRATING -> MIGRATED`, lazy
  PH3-03 child, absent shared legacy UUID in the migrated body, legacy
  remote user untouched, idempotent repeat, and a real crash/lost-ACK
  convergence proof (subscription fetch failed mid-attempt, durable state
  confirmed `MIGRATING` via a freshly reopened `Database()`, retry converged
  to `MIGRATED` with exactly one child).
- Scenario B (revoke boundary, separate disposable account):
  `MIGRATED -> LEGACY_REVOKE_PENDING -> LEGACY_REVOKED` with a REAL revoke
  of the synthetic legacy Marzban user (disabled + UUID rotated), an
  explicitly refused rollback attempt after `LEGACY_REVOKED`, and the
  migrated child continuing to resolve correctly afterward.

Zero raw credentials in the MGBoost DB dump.

## Production dormant deploy (2026-08-25)

Additive-schema-only (`mgboost_migration_bindings`,
`mgboost_migration_binding_events`); no route/worker wired. Encrypted
backup + restore verification passed before the pull. `PRAGMA quick_check
=ok`, 0 FK violations. Post-deploy: 0 migration binding rows, 0 events;
`LEGACY_BRIDGE_ENABLED`/`OPAQUE_SUBSCRIPTION_ENABLED` stayed `False`,
`PH3_04_ENFORCEMENT_MODE` stayed `OFF`; masked cardinality
(accounts/aliases/slots/generations/child_intents/telegram_identities/
bridge_bindings) unchanged pre/post; all services stayed active.

## Not in scope for PH4-02

PH4-03's real canary migration, PH4-04's opaque URL rollout, any grace
period, and any real production legacy revoke (PH4-06) are all untouched.
