# PH3-03 prerequisites — aliases, child intent, outbox and broker contract

Date: 2026-08-25

## Activation boundary

This change is implemented and verified locally/on a disposable production DB
copy, but is not deployed to production. There is no worker, HTTP route,
subscription resolver or Marzban call wired to `ChildProvisioningStore`.
Applying the additive migration creates empty tables only. Creating the approved
parent, aliases, slot generation, outbox row or remote child remains a separate
owner-approved production mutation.

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
the outbox. After broker success, only separate SHA-256 verifiers and bounded
masks are persisted for the child VLESS UUID and Shadowsocks password. Raw
generated credentials exist only in the authenticated localhost broker response
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
3. accepts only VLESS, optionally with Shadowsocks;
4. preserves exact inbound membership, VLESS flow and allowed Shadowsocks
   method while omitting all source credentials from the create payload;
5. asks Marzban to generate a fresh VLESS UUID and, when present, fresh
   Shadowsocks password;
6. rereads and verifies username, expiry, active status, unlimited data,
   protocols/inbounds/options and that credentials differ from legacy;
7. returns `CREATED` or the same verified `EXISTING` effect.

The companion typed `child.user.credentials.get` operation accepts only the
server-derived operation/username, approved source-contract hash, exact expiry
and both stored credential verifiers. It rereads Marzban, verifies the complete
remote contract and compares both credentials in constant time before returning
them ephemerally to the subscription response path. It offers no list/generic
lookup. MGBoost DB, application logs and broker logs must never receive the raw
values.

The read-only production source contract for `beykusios` currently has VLESS +
Shadowsocks, 25 VLESS inbound and zero Shadowsocks inbound, VLESS flow
`xtls-rprx-vision`, Shadowsocks method `aes-128-gcm`, unlimited expiry/data and
normalized hash `b4798b928c481570bf1388cb06b73907a1afd8295e047d39cfee715e27ca0f98`.
No credential or inbound tag is included here.

## Lost-ACK reconciliation

If Marzban creates the child but the local ACK is lost, the lease expires and a
worker claims the same immutable operation again. `child.user.ensure` first
rereads the same server-derived username, verifies its complete controlled
contract and returns `EXISTING`; the local transaction records a `RECONCILED`
event and converges to `APPLIED`. It never blindly creates another username and
does not roll back a verified remote effect.

If the broker or Marzban is unavailable, desired state remains retryable and no
success is acknowledged. Contract drift/collision fails closed for manual
review. A future periodic reconciler must use the same operation and reread
rules.

## First canary manifest (not executed)

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

## Isolated Marzban 0.8.4 gate — FAIL (2026-08-25)

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

An owner-approved resolution is required before rerunning the gate: either
change the required proxy contract, introduce an intentional versioned
Shadowsocks topology, or use a supported Marzban version/API that can preserve a
disabled legacy proxy. Direct writes to Marzban's private SQLite schema are not
an approved workaround because they bypass Marzban validation/events/Xray
reconciliation and are version-coupled.

The credential-refresh contract remains fail-closed: a temporary Marzban outage
returns generic 503 through the authenticated broker and no raw credential is
available from MGBoost storage. Already installed client configs continue to
work while Xray accepts their credentials, but a future child subscription
refresh cannot be generated until Marzban is reachable. Existing legacy
`/sub/{token}` remains on its current direct read path and is unaffected.

## Verification and remaining gate

Focused child/broker/staging-guard tests pass (`37 passed`) and the full suite
passes (`518 passed, 3 skipped`). They cover schema idempotency,
one-parent/many-alias immutability,
forged actor denial, account isolation, atomic intent+outbox insertion,
idempotent prepare, typed payload rejection, fresh VLESS/Shadowsocks credentials,
broker/direct equivalence, and remote-created/local-ACK-failed reconciliation.

On a disposable current production DB copy, first/second migration apply is
`true/false`, all 38 pre-existing table digests are unchanged, legacy
device/HWID counts remain 71/71, all new tables contain zero rows, quick check is
`ok` and foreign-key violations are zero.

Before any production mutation, still required:

1. resolve the disabled-Shadowsocks API incompatibility above without changing
   the approved legacy contract implicitly, then rerun the full real
   create/reread/idempotency/lost-ACK gate to PASS;
2. deploy the dormant code/schema with an empty-table verification gate;
3. configure dedicated slot HMAC key and the primary-admin mapping only through
   protected service configuration;
4. obtain explicit owner approval for the exact manifest above;
5. create parent/aliases and only the single selected slot generation, then stop
   again before dispatching its child outbox operation.

After a separate future approval and only after a PASS, the production mutation
set is: deploy the dormant additive schema/code; install protected slot-HMAC and
primary-admin mapping; create one reviewed internal plan/parent, Telegram link
and three immutable alias rows; claim only slot 1/generation 1 for the approved
privacy reference; atomically write one child intent/outbox operation; dispatch
typed `child.user.ensure`; persist only verifier/mask plus observed state after
reread; then verify the dormant child. The old legacy UUID, URL/token, config,
HWID rows and all other observations remain unchanged and unreclaimed.
