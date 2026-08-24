# PH3-03 dormant production child canary

Status: **owner-approved; tooling verified locally; production mutation not yet
executed**.

This runbook is deliberately limited to the single reviewed
`INTERNAL_OWNER_PRIMARY` canary. The operator cannot supply another account,
alias, Telegram identity, device row, slot, generation, child username or
operation ID. Expanding the scope requires a new owner decision and code
review.

## Fixed manifest

- parent mapping: `INTERNAL_OWNER_PRIMARY`;
- aliases: `beykus`, `beykusios`, `BeykusLaptop`;
- Telegram identity: `905302972` (identity link, not a credential);
- source: INTERNAL, billing disabled, unlimited WL/expiry, device capacity 10;
- source alias: `beykusios`;
- legacy device evidence: `user_devices.id=56`, `corr_701f5982b4`;
- selected client: iPhone 17 / INCY 2.5.2 / iOS;
- slot 1, generation 1;
- VLESS-only child, exact approved 25-inbound source contract, active,
  unlimited expiry/data, `xtls-rprx-vision` flow.

The parent public identity, child username and logical operation ID are derived
server-side from this manifest. Raw legacy request keys, legacy subscription
tokens and VLESS UUIDs are neither printed nor persisted by the canary tool.

## Deployment and preflight

1. Run `git diff --check`, the focused provisioning tests, and the full test
   suite. Scan the diff for secrets and raw identifiers.
2. Commit and push only the reviewed canary tool, tests and documentation.
3. On production, create and restore-verify a fresh encrypted MGBoost backup.
4. Pull the exact commit and confirm production `HEAD`.
5. Run `scripts/configure_ph3_03_canary.py` as root with the exact confirmation.
   It derives the primary login from the protected broker environment, creates
   an independent slot-HWID HMAC secret when absent, atomically updates the main
   environment and writes a root-only environment backup. It never prints a
   secret.
6. Restart only `mgboost-panel`. The normal `Database` startup applies the
   additive checksum-pinned PH3-03 schema. Verify service health and confirm all
   new tables remain empty.
7. Run `scripts/run_ph3_03_production_canary.py --preflight` with the fresh
   encrypted backup artifact. Any failed schema, key-separation, real
   server-authenticated primary-admin session, loopback broker, typed-operation,
   topology, source, device-evidence, legacy-subscription or restore check is a
   hard stop.

The main MGBoost environment must continue to contain no Marzban SUDO username
or password. The broker remains loopback-only and has no nginx route.

## The one permitted mutation

With the exact confirmation string, `--execute` performs only:

1. one reviewed internal plan/parent/subscription and Telegram identity;
2. one immutable alias group containing the three approved aliases;
3. one claim of slot 1/generation 1 using the dedicated HMAC verifier;
4. one transactional child intent and one immutable logical outbox operation;
5. typed `child.user.ensure`, authoritative reread and hash/mask-only ACK;
6. a second ensure proving `EXISTING`, typed ephemeral credential reread, exact
   remote-contract comparison, legacy pre/post comparison and raw-secret scan.

There is no legacy resolver switch. The source user, old UUID, old subscription
URL/token, existing HWID rows and all existing configs remain authoritative.
No other observed device is claimed and no other account or child is created.

## Failure and reconciliation

- A failure before remote mutation leaves a durable unambiguous local intent;
  it must be inspected before retry.
- If Marzban may have created the child but the local ACK failed, do **not**
  delete the child and do not prepare a new operation. After the lease/retry
  boundary, rerun the same fixed operation with `--reconcile`; deterministic
  username lookup and exact reread converge to the existing remote child.
- Unexpected remote state is not success. Keep the child dormant, record the
  reconciliation/error state, and stop.
- If the typed credential reread cannot reach Marzban, a future fresh child
  subscription response must fail safely; raw credentials are intentionally not
  cached locally. This dormant canary does not affect legacy `/sub/{token}`.
- Configuration rollback restores the protected pre-canary environment and
  restarts `mgboost-panel`. Additive local evidence and a remotely created child
  are not deleted blindly. Legacy credentials never need rotation for rollback.

## Acceptance evidence

The canary is accepted only when local cardinality is exactly 1 parent, 1
Telegram identity, 1 plan/subscription/mutation/review, 3 aliases, 1 slot and
generation, 1 child intent and 1 logical outbox operation; Marzban has exactly
one derived child; repeated ensure is `EXISTING`; the child has exactly the 25
approved VLESS inbound; and the legacy masked identity/config/device/HWID/Stars
snapshot is byte-for-byte equal before and after. Raw child UUID/token leakage
counts must be zero in MGBoost DB, application/nginx logs and the deployment
journal window.

Marzban 0.8.4 serializes `UserResponse.subscription_url` by minting a fresh
timestamped alias; multiple aliases remain valid until `sub_revoked_at`. That
volatile admin-API presentation is not a credential rotation and is excluded
from the identity digest. The gate instead hashes the unchanged durable tokens
already present in legacy `user_devices`, fetches every distinct persisted
token, and compares its functional config before/after. The local device digest
also covers those stored values without exposing them.

PH3-03 remains partial after this one dormant canary. PH3-04, fail-closed HWID,
legacy migration and client switching remain explicitly out of scope.
