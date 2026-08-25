# PH2-05 — Telegram ownership recovery/rebind

Date: 2026-08-25. Status: dormant, implemented. No route imports
`src/ownership_rebind.py`. Fixed first-rollout policy (OPD-39/DL-041):
primary-admin-only manual rebind, no self-service, no recovery codes.

## What PH1-01 already covered (not duplicated)

PH1-01 (closed `[x]`, production-verified) already fully implements
session logout/revoke/rotation/TTL, CSPRNG opaque session ID with hashed
in-memory lookup, Secure/HttpOnly/SameSite=Strict cookies, constant-time
CSRF checks on every mutation route, and fixation-safe login (341+ tests).
`PrimaryAdminAuthority` (used identically by `InternalEntitlementStore` and
`LegacyBridgeStore` already) is the existing "authz after authn" capability
mechanism. PH2-05 adds no second session/CSRF/fixation implementation --
`OwnershipRebindStore` requires the exact same sealed
`PrimaryAdminCapability` object every other primary-admin-gated store
already requires, so any future HTTP route wiring inherits PH1-01's
protections automatically, the same way `internal_entitlements`/
`legacy_bridge` already do.

The only genuinely new work in this phase is Telegram ownership
recovery/rebind itself.

## Fixed policy (not re-decided here)

OPD-39/DL-041, already CLOSED: manual rebind by the primary MGBoost admin
only; HWID and subscription-URL possession are never proof of ownership;
after a successful atomic rebind the old Telegram binding is immediately
revoked; dual active ownership is forbidden.

## State machine

```
prepare()  -- capability-gated, idempotent-insert durable request
    -> process_rebind(): claim
        -> apply_identity_mutation() [atomic, always]
             re-verify current owner == expected_old_telegram_id (else
             stale-request conflict) -> revoke old (revoke_reason=
             'ownership_rebind:<mode>') -> insert new (provenance=
             'ADMIN_REBIND') -- both inside PH3-01's existing schema, whose
             own partial-unique indexes make "two ACTIVE owners" and "one
             Telegram ID owning two accounts" structurally impossible, not
             just conventionally avoided
        -> [COMPROMISE only] PH2-01 credential rotation
             prepare() + activate() on the SAME account, with abandon+
             reissue on a lost-response retry (see below)
        -> finish() -- terminal APPLIED, immutable from here (schema
           trigger blocks any further state/identity-column change)
```

`ORDINARY` never touches `mgboost_subscription_credentials` at all (the
`CHECK` constraint in the schema enforces `old_credential_id`/
`new_credential_id IS NULL` for that mode) and never touches
`mgboost_child_user_intents`/`mgboost_device_slot_generations` in either
mode -- this is not a device rebind, and it never calls
`src/child_lifecycle.py`.

## Ordinary vs. compromise

- **ORDINARY**: identity mutation only. Opaque credential and child UUID
  are provably byte-identical before/after (tested and proven on real
  Marzban).
- **COMPROMISE**: identity mutation, *and* `process_rebind()` unconditionally
  rotates the account's opaque credential through the existing PH2-01
  `SubscriptionCredentialStore.prepare()`/`.activate()` CAS -- there is no
  parameter that lets a caller request compromise mode while skipping
  rotation. The old opaque token is provably dead
  (`resolve()` returns `None`) and exactly one new `ACTIVE` generation
  exists afterward. Child UUID is still never touched -- credential
  rotation and child-UUID revoke are two structurally separate mechanisms
  (PH2-01 vs. PH3-05), and this module only ever calls the former.

## Lost-response / abandon+reissue

If `process_rebind()` crashes after `SubscriptionCredentialStore.prepare()`
succeeds (raw token generated) but before recording that generation on the
rebind operation row, a retry cannot re-request the same idempotency key
(PH2-01's own store correctly refuses to re-deliver a raw value). The
orchestration catches that specific `SubscriptionCredentialConflict`,
revokes the abandoned `PENDING_DELIVERY` generation
(`revoke_reason='ABANDONED_PENDING'`, an immutable tombstone, never
deleted), and issues a fresh generation with a derived retry key. The old
opaque token stays permanently revoked throughout -- it is never
reactivated, and a retry after a *fully successful* compromise rebind
creates zero further generations (`process_rebind` on an `APPLIED`
operation is a no-op via `claim()`'s own terminal-state check).

## Authorization boundary

`account_id` and `new_telegram_id` come from the admin caller (the primary
admin explicitly decides which account and Telegram ID);
`expected_old_telegram_id` is a caller-supplied CAS expectation checked
against the account's *actual current* active owner at execution time --
never trusted blindly. A mismatch (stale request, concurrent rebind already
applied, or an IDOR attempt naming the wrong account for a Telegram ID that
belongs elsewhere) is rejected before any mutation. `capability` must be a
real `PrimaryAdminAuthority`-sealed object; `None`/a forged string/a
non-primary session all fail with `PrimaryAdminRequired`.

## Audit and privacy

`mgboost_ownership_rebind_operations` (immutable once terminal) plus an
append-only `mgboost_ownership_rebind_events` table record old/new Telegram
ID, primary-admin actor, mode, reason, timestamp and outcome -- exactly the
OPD-39/DL-041 audit requirement. These are dedicated tables, not general
application/access logs; `src/ownership_rebind.py` contains zero
`print`/`logging` calls, matching PH2-01/PH4-01's own convention. No raw
opaque token, child UUID or HWID is ever stored anywhere in this module.

## Focused tests

`tests/test_ownership_rebind.py`, 16 passed: atomic revoke+activate with no
dual owner, ordinary preserves the opaque credential and all PH3-02/03/08
data (child/slot/aliases byte-identical, remote child UUID unchanged),
compromise revokes the old opaque token and issues exactly one new
generation, compromise never rotates the child UUID, mandatory rotation
(no bypass parameter), lost-response abandon+reissue, retry-after-success
creates no `N+2` generation, non-primary/missing-capability denial, stale
old-owner (IDOR-adjacent) rejection, new-Telegram-ID-already-active
(dual-ownership) denial, cross-account IDOR rejection, concurrent
same-account rebind (exactly one winner), idempotency-key-reuse-with-
different-payload conflict, schema-level terminal immutability, zero raw
credential leakage in a full DB dump.

Full regression: `787 passed, 3 skipped`.

## Real isolated Marzban 0.8.4 staging (2026-08-25)

`scripts/verify_ph2_05_ownership_rebind_staging.py`, same immutable digest
as every other PH3-0x/PH2-01/PH4-01 gate. Two independent synthetic
parents, each with a real PH3-03 child. Result: **PASS**, all 12 checks
green -- ordinary rebind: new owner active, old owner revoked, opaque token
verifier/generation unchanged, remote child username/UUID/status untouched;
compromise rebind (fresh setup): new owner active, old owner revoked, old
opaque token denied, exactly one new active generation, remote child UUID
unchanged; zero raw credentials in the MGBoost DB dump across both
scenarios. Ownership rebind never calls Marzban itself -- the gate proves
absence of side effects on the real remote child, not a positive Marzban
interaction.

## Production dormant deploy

See `ROADMAP.md` PH2-05 for the exact cardinality evidence. No real
production Telegram identity was created, changed or rebound; no synthetic
Telegram ID was created in production for evidence purposes (all evidence
is from the isolated gate and the test suite). Additive schema only.

## Not in scope for PH2-05

Self-service recovery, recovery codes (explicitly out of scope per OPD-39),
any live HTTP admin route exposing this capability, and any real production
ownership mutation are all untouched.
