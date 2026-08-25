# PH4-01 — legacy subscription alias bridge

Date: 2026-08-25. Status: dormant, implemented. `LEGACY_BRIDGE_ENABLED`
defaults off; even when on, zero `mgboost_legacy_bridge_bindings` rows exist
in production, so nothing changes for any real user.

## Core principle

The legacy `/sub/{token}` route stays authoritative. This bridge only adds
an *additional*, opt-in, per-device resolution path inside that same route:
if (and only if) the resolved legacy username has an explicit, root-only
`enabled=1` binding, a supported HWID on that request may receive a
per-device child subscription instead of the shared legacy one. Every other
legacy request -- unmapped account, disabled binding, unsupported/missing
HWID, full capacity -- is completely unaffected.

```
legacy /sub/{token} request
    -> existing get_username_for_token (unchanged)
    -> LegacyBridgeStore.resolve_account_for_legacy_username
       (exact reviewed alias match AND explicit enabled binding, or None)
    -> None: fall through to the exact unmodified legacy response
    -> account_id: resolve_account_device() (shared with PH2-01)
       -> PH3-08 parent state -> PH3-04 HWID gate -> PH3-02 slot
       -> PH3-03 lazy child -> typed subscription fetch
```

## Legacy -> parent mapping is deterministic only

`LegacyBridgeStore.resolve_account_for_legacy_username` is a single SQL
join: `mgboost_legacy_account_aliases.legacy_username` (an already-reviewed,
immutable alias -- never inferred from username shape, HWID, Telegram ID or
URL possession) joined to an `mgboost_legacy_bridge_bindings` row with
`enabled=1`. Missing alias, missing binding, or a disabled binding all
return `None` uniformly -- there is no code path that creates a new parent
account from a guess.

## Per-device, not per-user

One legacy username can have several devices. The dividing line for "did
this bridge attempt do anything durable" is exactly whether
`hwid_gate.evaluate()` returned an ALLOW decision (`KNOWN_SLOT`/
`ASSIGN_FREE_SLOT`) -- every DENY decision (missing/malformed HWID,
unsupported client, full capacity, cross-account HWID) and the
parent-desired-state check both happen strictly *before* `DeviceSlotStore
.claim()` could ever commit a row, so they are side-effect-free by
construction, not by convention. `src/legacy_bridge_resolver.py`'s
`is_fall_through_outcome()` codifies exactly this set. A device that isn't
eligible this time simply keeps getting the legacy response -- it is never
denied outright, and a first migrated device on an account never touches
any other device of that same account.

## Fail-closed after acceptance, never a legacy fallback

Once a slot has been durably claimed for a device, a later failure (lost
broker connection, provisioning retry exhaustion, subscription fetch error)
returns a plain `502` from `_try_legacy_bridge` in `src/routes/sub.py` --
never the legacy shared credential. This is the one hard behavioral
difference from a plain "not bridged" outcome, and it is exactly why
`is_fall_through_outcome()` exists as an explicit, tested classification
rather than an implicit assumption.

## Shared engine with PH2-01

`src/opaque_resolver.py::resolve_account_device()` is the exact same
function both PH2-01's opaque-token resolver and this bridge call once
`account_id` is known -- there is no second, parallel implementation of the
HWID-gate/slot/child/subscription-fetch decision. PH2-01 and PH4-01 can
never drift into two different security postures for the identical
downstream question.

## Minimal bridge state (not a PH4-02 state machine)

`mgboost_legacy_bridge_bindings` (additive, mirrors PH3-03's
`mgboost_shadow_resolver_bindings` pattern exactly): `account_id` (unique),
`legacy_alias_id`, `enabled`, `decision_ref`, `created_by_actor`,
`created_at`. An append-only `mgboost_legacy_bridge_binding_events` table
records `CREATED`/`ENABLED`/`DISABLED` transitions. No raw credential of any
kind is stored. This is deliberately *not* the `LEGACY -> MIGRATING ->
MIGRATED -> ...` state machine PH4-02 will own -- the durable "which
slot/generation/child resulted from this device" record already exists in
the unmodified PH3-02/03 tables (`mgboost_device_slot_generations`,
`mgboost_child_user_intents`), referenced by `account_id`; this schema adds
only the one new capability-defining fact PH4-01 actually needs: the
explicit per-account opt-in decision itself.

## Legacy credential lifecycle untouched

This module never calls `child.user.revoke`, never rotates/deletes the
legacy Marzban user, and starts no grace period. The shared legacy
credential remains fully functional for every not-yet-bridged device,
indefinitely, until an explicit later Phase 4 decision.

## Staged rollout

Two independent gates, mirroring PH2-01's own dormancy pattern: the
`LEGACY_BRIDGE_ENABLED` env flag (default off), and the binding table
itself (empty in production). PH4-03 will later flip specific canary
accounts on via `LegacyBridgeStore.enable()` -- never a global switch, and
`PH3_04_ENFORCEMENT_MODE` remains untouched/`OFF` throughout.

## Focused tests

`tests/test_legacy_bridge.py` (8 passed), `tests/test_legacy_bridge_resolver.py`
(11 passed), `tests/test_legacy_bridge_route.py` (3 passed). Full regression:
`771 passed, 3 skipped`.

## Real isolated Marzban 0.8.4 staging (2026-08-25)

`scripts/verify_ph4_01_legacy_bridge_staging.py`, same immutable digest as
every other PH3-0x/PH2-01 gate. All 12 checks PASS: legacy authoritative
before any binding; explicit binding + supported HWID bridges to a real
PH3-03 child with the shared legacy UUID absent from the bridged body while
the legacy remote user stays untouched/active; idempotent repeat; a second
distinct HWID gets its own second child; missing-HWID/full-capacity/
unmapped-username all fall through to the unmodified legacy response; zero
raw credentials in the MGBoost DB dump.

## Known, documented scope limit

Shared with PH2-01: the bridge adds *additional* devices to an account that
already has at least one child provisioned through the existing PH3-03
pipeline before the bridge goes live for that account. It does not
discover/verify a brand-new source template for an account's very first
device on its own -- a deliberate non-invention of a second "find and
validate the legacy user's shape" mechanism.

## Not in scope for PH4-01

PH4-02's durable migration state machine, PH4-03's real canary migration,
PH4-04's opaque URL rollout, any grace period, legacy revoke, and any real
production bridge activation are all untouched. Production ships with
`LEGACY_BRIDGE_ENABLED` unset and zero bindings.
