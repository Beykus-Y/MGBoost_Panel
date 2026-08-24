# PH3-03 dormant production child canary

Status: **PASS — the single owner-approved dormant production child was created
and reconciled on 2026-08-25; no legacy runtime switch occurred**.

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
from the identity digest. Since PH1-06 stores only `sha256:` verifiers rather
than recoverable historical bearers in `user_devices`, the gate verifies those
45 distinct stored references are unchanged and includes Marzban
`created_at`/`sub_revoked_at` in the identity contract. It also renders a fresh
functional config for all 25 live legacy users before/after. The local device
digest covers the verifier rows without exposing them.

PH3-03 remains partial after this one dormant canary. PH3-04, fail-closed HWID,
legacy migration and client switching remain explicitly out of scope.

The subsequent durable worker rollout is separately gated by
`docs/PHASE3_CHILD_WORKER.md`. It may observe/reconcile only this already
`APPLIED` operation. It must not create another intent, slot, account or child,
and it does not change this document's dormant/runtime boundary.

## Production evidence

The encrypted backup `mgboost-db-20260824T200256Z.tar.gpg` was root-only and
passed an independent restore before any canary configuration or data write.
Production pulled the fixed tooling commits through `8c29fd6`; the main service
was restarted only to load the independent slot-HMAC key, primary mapping and
additive empty schema. Main retained zero Marzban SUDO environment keys. The
broker remained authenticated on `127.0.0.1:8002` with no nginx route.

Created local state is exactly:

- account id 1 / `acct_435p4hjeoxeq3bzg4ifkdut4veower4r`;
- plan/subscription ids 1/1, one entitlement mutation and one reviewed mapping;
- Telegram identity `905302972` and exactly three approved aliases;
- slot id 1 / generation id 1, masked HWID `hwid_46609d7eddbb`;
- child intent id 1 / `child_6cKbHwxFtfZ1WT6NTOyo7tHt`;
- child username `mgc_sgg6v7t6he43yytsqmkdczzfpa`, masked UUID
  `uuid_d4ae1519`;
- outbox id 1 / `op_lw33pjhqhnvorrgh4p754bnc34`, attempt 1 `APPLIED`, with
  `CREATED` followed by repeat/reconciliation `EXISTING`.

Marzban has exactly one such child and 26 total users. Reread proves VLESS-only,
active, unlimited expiry/data, `xtls-rprx-vision`, exact approved 25 inbound,
the approved source-contract hash and a new UUID different from the source.

The first post-effect verifier stopped rather than accepting a changing
Marzban-generated timestamped subscription alias. Source inspection established
that this field is freshly generated for every response and aliases remain
valid until `sub_revoked_at`. A later diagnostic also stopped before ensure
when it attempted to treat PH1-06 `sha256:` verifiers as raw bearers. The final
gate correctly compares creation/revocation state, 45 stored verifier rows and
functional config. These two stops produced no second child or legacy mutation.

Final pre/post state matches exactly for 25 legacy identities/configs, 45 token
verifiers, 71 device rows, 71 HWID locks and Stars tariffs. A real MGBoost
legacy `/sub` response contains the unchanged source UUID in 37 VLESS links and
returns `Cache-Control: no-store`. Signed Filin status is 200 and unsigned is
401; public admin/LK are 200; MGBoost/broker/nginx/Marzban and SQLite integrity
are healthy. The child raw UUID/token occurs zero times in MGBoost DB,
application/nginx logs and the full two-hour deployment journal window.

Legacy UUID, subscription URL/token, HWID binding, expiry, tariff, forced
client reconfiguration and unexpected config changes caused by the canary are
all zero. The legacy source and all existing clients remain authoritative.
