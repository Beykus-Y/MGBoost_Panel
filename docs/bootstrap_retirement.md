# Synthetic migration bootstrap retirement

No deployment or production cleanup accompanies this change.

`legacy_grace_migration.migrate_bootstrapped_account` claims the deterministic
per-account synthetic HWID, prepares/provisions its canonical child, and only
then enables the legacy bridge. Normal real-device requests use a different
verifier and claim their own generation through the existing resolver. The
migration wrapper records the generation and child and advances to `MIGRATED`
only after an OK resolver outcome. `opaque_resolver` records canonical PH8-06
telemetry after exact request/generation matching. No retirement runs in HTTP.

The original capacity failure was a correctly counted ACTIVE synthetic
generation that nobody retired. Capacity is unchanged: the synthetic generation
counts until the existing release transaction commits. A freed slot is reusable
with a new generation through normal claim, including slot 1.

## Proof and state machine

`src/bootstrap_retirement.py:classify` is the shared read-only classifier for
preview, admin Devices, preparation guards and worker fences. It requires the
configured HMAC key and the authoritative `is_genesis_hwid_verifier`; slot
numbers are output only. It requires exact account/generation/child topology,
ACTIVE account and enabled PRIMARY bridge, successful provisioning, no
conflicting lifecycle or outstanding parent sync, and a different current ACTIVE
non-genesis child with exact migration binding and canonical telemetry.

Accepted migration states are `MIGRATED` and terminal `LEGACY_REVOKED`.
`LEGACY_REVOKE_PENDING`, `MIGRATING` and `ERROR_RECONCILE` are refused.
Telemetry must match account, generation ID and full verifier; legacy UI
telemetry fallbacks, device names, timestamps and masked identifiers never
qualify. No evidence or migration lineage is edited to make a candidate pass.

The sweep prepares deterministic account/generation/operation keys and uses
existing `process_revoke` with typed `child.user.revoke`. Only durable REVOKE
APPLIED permits FREE preparation, followed by `process_free(strict_generation=True)`.
The existing lifecycle operations and immutable attempt events store the bounded
retirement reason, STARTED/SUCCEEDED events and the release reason. No new audit
schema exists. Preview exposes only bounded states and internal numeric row IDs.

Evidence is rechecked inside lifecycle prepare's BEGIN IMMEDIATE transaction
and before/after lifecycle effects. Existing leases allow retries after remote
failure or lost acknowledgement. A release-before-FREE-ack replay is accepted
only for the exact released generation, matching reason and unchanged FREE slot.
A replacement generation fails closed and is never released; an interrupted
FREE whose slot has already been reused remains pending for review.

Remote effects are limited to revoking the exact bootstrap child. Broker UUID
verification and deterministic revoke retry handling remain intact. There is no
CREATE, entitlement change, WL change, bridge disable, PRIMARY mutation or
real-child rotation in retirement. Provisioning worker skips REVOKED intents.
The existing enabled-bridge early return prevents migration recreation; an
additional historical genesis guard refuses recreation when a bridge is later
disabled, preserving that disabled state for review.

## Local read-only preview and activation

With the real device-slot HMAC key configured in the environment (never pass it
on the command line):

```sh
python scripts/preview_bootstrap_retirement.py --database /path/to/db.sqlite3
```

Optional repeated `--account-id` restricts the preview. The script opens SQLite
with `mode=ro` and `query_only=ON`; it does not initialize Database or run schema
migrations and has no Marzban client. Run only where explicitly authorized.

The existing `child_worker_main.py` loop calls the sweep after its existing
provisioning and parent reconciliation phases. `BOOTSTRAP_RETIREMENT_MODE`
defaults to `preview`; `active` enables automatic discovery and retirement of
both historical and future eligible generations. Other values disable it.
An absent/invalid HMAC key cannot identify candidates. Existing worker enable,
operation allowlist and broker permissions remain prerequisites; retirement
uses the broker's existing revoke permission, never expands it. `--json` emits
the bounded preview or sweep summary. Deployment and activation are separate
operator actions, not performed by this implementation.

Devices retains “Служебный bootstrap” and “Это не подтверждённое устройство
клиента.”, adds bounded refusal/pending explanations, and naturally shows FREE
after release rather than an ACTIVE bootstrap. No broad UI redesign.

## Validation

The local touched-domain run passed 217 tests (248.39 seconds), covering
retirement, legacy grace migration, slots, lifecycle including authorization and
broker/retention checks, legacy bridge and migration, canonical telemetry,
projection, admin read models, and child worker/reconciliation.
A subsequent focused run passed 48 tests (84.79 seconds), including all 29 new
retirement cases and stale provisioning acknowledgement protection. The latter
adds SQL guards preventing old reconciliation results from overwriting REVOKED.
Python compile, JavaScript syntax and `git diff --check` passed.
The existing grace migration fixture now supplies an explicit legacy expiry,
as required by the current entitlement contract.

The 4-slot fixture proves capacity denial before retirement, 3 ACTIVE real
devices after retirement, normal claim into slot 1 generation 2, and 4 ACTIVE
real devices afterward. Real remote records, migration bindings, telemetry,
bridge, PRIMARY alias and subscription records remain unchanged by retirement.
Full project suite and browser visual testing were not run. No production
records or Marzban users were modified; no deployment was performed.
