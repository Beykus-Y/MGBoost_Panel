# PH3-03 prerequisites — aliases, child intent, outbox and broker contract

Date: 2026-08-25

## Activation boundary

The owner-approved dormant parent/slot/child canary is production-proven. A
durable worker/reconciler is now implemented and real-Marzban-staged, but its
production activation remains gated. There is still no subscription resolver
or automatic intent creation from legacy devices. The only permitted production
worker target is the already-APPLIED canary operation in `reconcile_only` mode.

Legacy `/sub/{token}`, UUID, token, HWID binding, config, expiry and tariff paths
do not import or read the new repository. PH3-04 remains disabled.

## One parent to many legacy aliases

`mgboost_legacy_alias_groups` gives one parent a stable reviewed mapping key.
`mgboost_legacy_account_aliases` stores every original legacy username in its
own immutable row, including role, observed status/expiry, masked evidence and
device/HWID counts. A legacy username is globally unique; an account can have
many aliases and exactly one primary alias. No source record is renamed,
collapsed or deleted.

The approved but not-yet-written manifest is:

```text
mapping_key: INTERNAL_OWNER_PRIMARY
account_source: INTERNAL
Telegram identity: 905302972 (identity link, never credential)
aliases:
  beykus       -> 3 practical device candidates
  beykusios    -> 4 practical device candidates (PRIMARY migration alias)
  BeykusLaptop -> 2 practical device candidates
entitlement: billing_required=false, WL=unlimited, devices=10, expiry=unlimited
```

The nine observations are practical slots, not proof of nine physical devices.
No cross-client automatic deduplication is permitted.

## Primary-admin authority

The stable audit actor is `owner:mgboost-primary:v1`, linked by owner decision to
Telegram identity `905302972`. Neither value authenticates a request.
`PrimaryAdminAuthority` mints an in-process capability only after a server-side
authenticated `AdminSession.username` matches protected
`PRIMARY_MGBOOST_ADMIN_LOGIN`. Entitlement writes accept that sealed capability,
not an `actor_id` string from a request. Both actor and login configuration must
be present; otherwise writes fail closed. Production values remain unset until
this boundary and the canary mutation are separately approved.

## Durable desired state

- `mgboost_child_user_intents` binds exactly one account-owned slot generation
  to one server-derived child username and one immutable source alias/contract.
- `mgboost_outbox` records a single typed `CHILD_USER_ENSURE` operation with a
  hash-only idempotency key, canonical request hash, lease, attempts and retry
  state.
- `mgboost_outbox_attempt_events` is append-only evidence for start, failure,
  success and reconciliation.
- Composite foreign keys and `BEGIN IMMEDIATE` reject cross-account alias,
  slot and generation substitution.
- Child username and operation ID derive deterministically from the immutable
  parent public ID plus slot number/generation. Caller/frontend cannot provide
  either value.

No raw HWID, subscription bearer, legacy UUID or child credential is stored in
the outbox. After broker success, only a SHA-256 verifier and bounded mask are
persisted for the child VLESS UUID. Raw generated credentials exist only in the
authenticated localhost broker response
long enough to validate/acknowledge the effect; future subscription delivery
must use the account resolver rather than exposing them through logs or admin
lists.

## `child.user.ensure` broker contract

The existing ten `legacy.*` operations are unchanged. The additional operation
accepts exactly:

```json
{
  "operation_id": "op_<server-derived-base32>",
  "child_username": "mgc_<server-derived-base32>",
  "source_username": "exact reviewed legacy alias",
  "source_contract_hash": "64 lowercase hex characters",
  "expire": 0
}
```

There is no generic path/method/payload proxy and no caller-controlled UUID,
password, proxies, inbound tags, data limit, status or note. Under a per-child
lock the broker:

1. reads the reviewed legacy source;
2. verifies an exact normalized contract hash;
3. accepts only VLESS under DL-046;
4. preserves exact inbound membership and VLESS flow while omitting the source
   UUID from the create payload;
5. asks Marzban to generate a fresh VLESS UUID;
6. rereads and verifies username, expiry, active status, unlimited data,
   protocols/inbounds/options and that credentials differ from legacy;
7. returns `CREATED` or the same verified `EXISTING` effect.

The companion typed `child.user.credentials.get` operation accepts only the
server-derived operation/username, approved source-contract hash, exact expiry
and the stored UUID verifier. It rereads Marzban, verifies the complete remote
contract and compares the UUID verifier in constant time before returning it
ephemerally to the subscription response path. It offers no list/generic
lookup. MGBoost DB, application logs and broker logs must never receive the raw
values.

After the DL-046 cleanup, the approved source contract for `beykusios` is
VLESS-only, with the exact 25 VLESS inbound, flow `xtls-rprx-vision`, unlimited
expiry/data and normalized contract hash
`52bd127165402fd429e47b4fa485a53566f8870af2514f6c82d4de204287ff47`.

## Lost-ACK reconciliation

If Marzban creates the child but the local ACK is lost, the lease expires and a
worker claims the same immutable operation again. `child.user.ensure` first
rereads the same server-derived username, verifies its complete controlled
contract and returns `EXISTING`; the local transaction records a `RECONCILED`
event and converges to `APPLIED`. It never blindly creates another username and
does not roll back a verified remote effect.

If the broker or Marzban is unavailable, desired state remains retryable and no
success is acknowledged. Contract drift/collision fails closed for manual
review. The periodic reconciler uses typed read-only `child.user.observe`,
durable DB leases and the same immutable operation/digest rules. See
`docs/PHASE3_CHILD_WORKER.md`.

## First canary manifest (executed dormant; legacy runtime unchanged)

- source alias: `beykusios`;
- selected legacy observation: `user_devices.id=56`, privacy reference
  `corr_701f5982b4`, iPhone 17 / INCY 2.5.2 / iOS;
- future parent slot: slot 1, generation 1;
- deterministic future parent public ID:
  `acct_435p4hjeoxeq3bzg4ifkdut4veower4r`;
- deterministic child username: `mgc_sgg6v7t6he43yytsqmkdczzfpa`;
- deterministic operation ID: `op_lw33pjhqhnvorrgh4p754bnc34`;
- desired expiry: unlimited (`expire=0` wire normalization).

The selected stored legacy request key is already a one-way practical HWID
candidate and will be HMAC-verifier-bound under a dedicated production key at
the approved claim transaction. Raw HWID is neither required nor recovered.

During this first canary, all three legacy aliases, all nine legacy device rows,
the old shared UUID and the legacy subscription URL remain authoritative and
working. The child UUID is new and is not substituted into legacy `/sub` yet.
There is no revoke, redirect, fail-closed HWID or config change. This produces
overlap for testing by design; revoke belongs to the later migration stage.

## Historical isolated Marzban 0.8.4 gate — FAIL (2026-08-25)

The approved gate ran against the exact official image
`gozargah/marzban@sha256:8e422c21997e5d2e3fa231eeff73c0a19193c20fc02fa4958e9368abb9623b8d`
on literal loopback with a disposable SQLite DB and synthetic credentials. The
staging Xray topology contained the exact 25 effective VLESS tags from current
`beykusios` plus two VLESS decoys. Read-only production reread independently
confirmed 25 global VLESS inbound, zero global Shadowsocks inbound, and source
shape VLESS+Shadowsocks with effective inbound counts 25/0.

The gate cannot reach child creation. Real Marzban 0.8.4 rejects both:

- direct creation of the exact legacy source/child proxy shape; and
- creating VLESS first and then adding the Shadowsocks proxy by partial update.

Both return HTTP 400 because Shadowsocks is disabled when the server has no
Shadowsocks inbound. With a synthetic global Shadowsocks inbound, Marzban treats
an empty requested Shadowsocks inbound list as the whole available set, so that
topology produces 25/1 rather than the approved 25/0 contract. It is not an
acceptable equivalence proof. Temporarily enabling a production Shadowsocks
inbound would be a topology/access change and is outside this gate.

Consequently the real end-to-end claims `CREATED`, repeated `EXISTING`, and
remote-created/local-ACK-failed have **not** passed real staging. Their unit
contracts pass, but they are not substitutes for this mandatory gate. PH3-03 is
not ready for a production child mutation.

DL-046 subsequently selected the VLESS-only product contract and typed
retirement of this non-functional metadata. The failed result remains here as
historical evidence and is not a current product ambiguity. Direct writes to
Marzban's private SQLite schema remain prohibited.

The credential-refresh contract remains fail-closed: a temporary Marzban outage
returns generic 503 through the authenticated broker and no raw credential is
available from MGBoost storage. Already installed client configs continue to
work while Xray accepts their credentials, but a future child subscription
refresh cannot be generated until Marzban is reachable. Existing legacy
`/sub/{token}` remains on its current direct read path and is unaffected.

## Post-cleanup isolated Marzban 0.8.4 gate — PASS (2026-08-25)

After the typed production retirement completed, the gate was rerun against the
same immutable official image digest on literal loopback with a fresh disposable
SQLite database, synthetic admin credentials and exactly 25 VLESS inbound / zero
Shadowsocks inbound. No production account, slot, outbox row or child was used.

The source and child API contracts each contained the exact approved 25 VLESS
inbound, `xtls-rprx-vision`, active status, unlimited expiry and unlimited data.
The child differed only by its deterministic server-derived username and fresh
Marzban-generated UUID. Marzban 0.8.4 renders subscription display titles from
the username rather than the inbound tag; after normalizing only this approved
username difference and the UUID, all 25 subscription lines were functionally
identical. Exact inbound membership remained independently strict in both API
contracts.

The gate proved:

- durable intent and stable payload digest existed before the remote mutation;
- first `child.user.ensure` returned `CREATED` and exactly one create call;
- repeated ensure returned `EXISTING` without another child;
- simulated remote-created/local-ACK-failed reclaimed the same operation and
  converged through `EXISTING`;
- `child.user.credentials.get` reread the raw UUID ephemerally, matched the
  stored verifier/mask and returned no generic user lookup capability;
- unexpected remote expiry drift was rejected and recorded as reconciliation
  `ERROR`, never acknowledged as success;
- unreachable Marzban returned 503 during credential refresh because MGBoost
  intentionally has no stored raw fallback credential;
- raw child UUID/token occurred zero times in the MGBoost DB and captured
  broker/application log; `legacy.user.create` was not dispatched.

The final focused cleanup/child/broker/staging-guard regression is `43 passed`;
the complete project regression is `524 passed, 3 skipped`.

Already installed child configs would keep working during a Marzban API outage
while Xray still accepts their UUID. A fresh subscription refresh cannot be
rendered until Marzban becomes reachable, and fails safely with 503 rather than
using stale persisted credentials. The current legacy `/sub/{token}` path is
unchanged and remains independent of this dormant resolver.

## Verification and remaining production gate

Focused child/broker/staging-guard tests pass (`37 passed`) and the full suite
passes (`518 passed, 3 skipped`). They cover schema idempotency,
one-parent/many-alias immutability,
forged actor denial, account isolation, atomic intent+outbox insertion,
idempotent prepare, typed payload rejection, fresh VLESS UUID,
broker/direct equivalence, and remote-created/local-ACK-failed reconciliation.

On a disposable current production DB copy, first/second migration apply is
`true/false`, all 38 pre-existing table digests are unchanged, legacy
device/HWID counts remain 71/71, all new tables contain zero rows, quick check is
`ok` and foreign-key violations are zero.

Before any production child mutation, still required:

1. deploy the dormant code/schema with an empty-table verification gate;
2. configure dedicated slot HMAC key and the primary-admin mapping only through
   protected service configuration;
3. obtain explicit owner approval for the exact manifest above;
4. create parent/aliases and only the single selected slot generation, then stop
   again before dispatching its child outbox operation.

After a separate future approval and only after a PASS, the production mutation
set is: deploy the dormant additive schema/code; install protected slot-HMAC and
primary-admin mapping; create one reviewed internal plan/parent, Telegram link
and three immutable alias rows; claim only slot 1/generation 1 for the approved
privacy reference; atomically write one child intent/outbox operation; dispatch
typed `child.user.ensure`; persist only verifier/mask plus observed state after
reread; then verify the dormant child. The old legacy UUID, URL/token, config,
HWID rows and all other observations remain unchanged and unreclaimed.
