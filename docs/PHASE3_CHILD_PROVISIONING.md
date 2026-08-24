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

No raw HWID, subscription bearer, legacy UUID or child UUID is stored in the
outbox. After broker success, only `sha256:<verifier>` and `uuid_<mask>` are
persisted for the child VLESS UUID. Raw generated credentials exist only in the
authenticated localhost broker response long enough to validate/acknowledge the
effect; future subscription delivery must use the account resolver rather than
exposing them through logs or admin lists.

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

## Verification and remaining gate

Focused tests cover schema idempotency, one-parent/many-alias immutability,
forged actor denial, account isolation, atomic intent+outbox insertion,
idempotent prepare, typed payload rejection, fresh VLESS/Shadowsocks credentials,
broker/direct equivalence, and remote-created/local-ACK-failed reconciliation.
The full suite passes.

On a disposable current production DB copy, first/second migration apply is
`true/false`, all 38 pre-existing table digests are unchanged, legacy
device/HWID counts remain 71/71, all new tables contain zero rows, quick check is
`ok` and foreign-key violations are zero.

Before any production mutation, still required:

1. run the create/reread contract against an isolated real Marzban 0.8.4 staging
   instance (the current workstation has no retained staging image/service);
2. deploy the dormant code/schema with an empty-table verification gate;
3. configure dedicated slot HMAC key and the primary-admin mapping only through
   protected service configuration;
4. obtain explicit owner approval for the exact manifest above;
5. create parent/aliases and only the single selected slot generation, then stop
   again before dispatching its child outbox operation.
