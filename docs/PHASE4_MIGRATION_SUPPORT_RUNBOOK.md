# PH4-03 migration support runbook

Minimal support reference for the reviewed-DIRECT-account migration cohort
(PH4-01/02/03). No secrets, tokens, UUIDs, raw Telegram IDs, or real legacy
usernames belong in this file or in any support ticket derived from it --
use the account's numeric id or `public_id` instead.

All queries below are read-only (`sqlite3 'file:.../db.sqlite3?mode=ro' "..."`)
against the production DB at `/opt/MGBoost_Panel/data/db.sqlite3`.

## See an account's migration state

```sql
SELECT operation_id, account_id, hwid_verifier, state, attempts, updated_at
FROM mgboost_migration_bindings WHERE account_id = ?;
```

States: `MIGRATING` (in progress), `MIGRATED` (done, per-device lineage
active), `ERROR_RECONCILE` (an ambiguous downstream signal -- see below),
`LEGACY_REVOKE_PENDING`/`LEGACY_REVOKED` (PH4-06 territory, not yet used).
Each device (HWID) the account uses gets its own row.

For the full event history of one binding:

```sql
SELECT attempt_no, event_type, from_state, to_state, safe_error_class, reason, created_at
FROM mgboost_migration_binding_events WHERE migration_binding_id = ? ORDER BY id;
```

## See a compat entitlement's device limit (`Dn`)

```sql
SELECT s.account_id, s.status, s.current_expiry, p.plan_code, p.device_limit, p.wl_mode
FROM mgboost_subscriptions s JOIN mgboost_plan_versions p ON p.id = s.current_plan_version_id
WHERE s.account_id = ?;
```

`plan_code` starting with `LEGACY_PAID_COMPAT_V1_D` is a migration-only
compatibility entitlement (`src/legacy_paid_compat.py`), never a commercial
catalog entry -- `Dn` is the exact number after `D`. `wl_mode='UNLIMITED'`
with `wl_quota_bytes IS NULL` is expected and correct for every legacy
compat entitlement; a `100`/`150` GB-style quota on one of these rows would
be a bug, not an intended feature.

## Verify exact legacy expiry preservation

Compare `mgboost_subscriptions.current_expiry` (above) against the
account's reviewed legacy alias:

```sql
SELECT legacy_username, legacy_status, legacy_expiry, observed_device_count
FROM mgboost_legacy_account_aliases WHERE account_id = ? AND alias_role = 'PRIMARY';
```

These two `expiry` values must always match exactly for a compat
entitlement -- never extended, shortened, or rounded.

## Verify ownership/payment provenance

```sql
SELECT ownership_evidence, decision_ref, reviewed_by_actor, created_at
FROM mgboost_direct_account_reviews WHERE account_id = ?;

SELECT decision_ref, attested_by_actor, created_at
FROM mgboost_owner_attested_legacy_payments WHERE account_id = ?;

SELECT payment_channel, record_status, external_reference, created_at
FROM mgboost_payment_records WHERE account_id = ?;
```

A reviewed DIRECT account has exactly one row in the first query. A legacy
account whose historical payment channel was owner-attested (not a real new
payment with known amount/date/reference) has exactly one row in the
second query and, correctly, **zero** matching rows in
`mgboost_payment_records` for that same historical fact -- a real new
`TELEGRAM_STARS`/`EXTERNAL_PAYMENT` transaction is what shows up there
instead.

## Recognize and react to `ERROR_RECONCILE`

`ERROR_RECONCILE` means a downstream provisioning signal was ambiguous (the
resolver could not tell whether the remote side actually committed) --
never a silent retry, never a fallback to the shared legacy credential.
`src/migration_lifecycle.py::reconcile_binding()` is the only code that
moves a binding out of this state, by comparing durable local state against
the authoritative slot/child tables:

- if the anchored slot generation is no longer `ACTIVE` (revoked/rebound
  elsewhere) -> stays `ERROR_RECONCILE` for manual review, on purpose;
- if there is no child intent yet, or it is not yet `ACTIVE` -> back to
  `MIGRATING` (safe to let it retry on the next request);
- if the child intent is already `ACTIVE` -> `MIGRATED` (a lost
  acknowledgement, not a real failure).

To force reconciliation manually, call `reconcile_binding(db, binding, now=...)`
from a short-lived root-only script -- never hand-edit `state` directly.

## A canary/migration attempt failed -- what to do

1. Do **not** re-enable/retry blindly. Read the binding's own event history
   (query above) for the `safe_error_class`.
2. Confirm the account still has a valid `mgboost_subscriptions` row with
   the correct `plan_code`/expiry (see above) -- a missing/mismatched
   subscription is the single most likely cause of an immediate
   `INTERNAL_ERROR`/`PROVISIONING_UNAVAILABLE` outcome, and must be fixed
   before ever creating or re-enabling a `mgboost_legacy_bridge_bindings`
   row for that account.
3. If a canary device's child was already created and needs to be
   cleaned up, use the existing PH3-05 `prepare_revoke` -> `process_revoke`
   -> `prepare_free` -> `process_free` sequence (`src/child_lifecycle.py`)
   on that device's own slot only -- never touch a slot serving the
   account's real, currently-connected device.
4. The account's real legacy Marzban user is never modified by any of the
   above; if in doubt, re-check its `status`/`expire` directly against
   Marzban before and after any action.

## Explicitly out of scope for this runbook

PH4-04 (opaque URL rollout), PH4-05 (grace period), PH4-06 (legacy
credential revoke), PH4-07 (full observability/cleanup tooling), PH5-09
(manual-payment-driven renewal UI). This file only covers what PH4-03
itself needs.
