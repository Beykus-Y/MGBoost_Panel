# PH2-01 opaque subscription token contract

Date: 2026-08-24 (design), 2026-08-25 (implementation). Status: dormant,
implemented. The schema/API/resolver below are fully built and tested
(`src/subscription_credential_schema.py`, `src/subscription_credentials.py`,
`src/opaque_resolver.py`, `src/routes/opaque_sub.py`); the "Phase 4 legacy
bridge" section remains design-only and is deliberately deferred to PH4-01
per explicit owner sequencing. See `ROADMAP.md` PH2-01 for the full
implementation/staging/deviation record.

## PH2-07 closure note (2026-08-25)

PH2-07 ("no persistent raw upstream token in new resolver") was verified
closed `[x]` without any new production code: the resolver below already
never reads, stores, forwards or logs a shared legacy subscription bearer.
See `ROADMAP.md` PH2-07 and `tests/test_ph2_07_no_persistent_legacy_token.py`
for the full data-flow proof and permanent regression guard.

## Implementation notes (2026-08-25)

- **AEAD envelope simplified to synchronous single-response delivery.** The
  "Issuance, delivery, rotation and revoke" section below recommends an
  encrypted-at-rest pending envelope for crash-safe multi-step delivery. The
  actual implementation never persists the raw token anywhere, encrypted or
  not: `SubscriptionCredentialStore.prepare()` returns it once, synchronously,
  inside the same authenticated HTTP response the design doc's own API
  section describes ("returns/delivers the raw token"). This meets the
  identical hard requirement -- raw tokens absent from DB/audit/backups/logs
  -- without adding a new symmetric-encryption dependency (this project has
  none today). The tradeoff, made explicit in code and tests: a `prepare()`
  response lost before reaching its caller cannot be recovered and must be
  explicitly revoked (`revoke_reason='ABANDONED_PENDING'`) before a fresh
  generation may be issued -- exactly the fallback this document already
  allows ("if delivery ultimately fails, admin issues another generation").
- **Resolver reuses PH3-02/03/04/08 verbatim.** No parallel slot-claim or
  child-creation code exists; `src/opaque_resolver.py` calls
  `DeviceSlotStore.claim()`, `ChildProvisioningStore`, `hwid_gate.evaluate()`
  and `parent_sync.compute/refresh_desired_state` exactly as PH3-0x already
  implemented them.
- **New typed broker operation, not a generic proxy.** `child.user.subscription.get`
  fetches a child's own rendered Marzban subscription body server-side (same
  mechanism the legacy resolver already uses) and never returns the child's
  subscription bearer path to the caller -- only the rendered body/headers.
- **Known, deliberate scope limit.** The resolver adds *additional*
  devices/slots for an account that already has at least one child, but does
  not itself discover or verify a brand-new source template for an account's
  very first device -- that trust decision (verifying a live legacy Marzban
  user on every request) belongs to PH4-01's legacy bridge, not here.
- **Dormant by two independent gates.** `OPAQUE_SUBSCRIPTION_ENABLED`
  defaults to off (uniform invalid response regardless of DB state), and
  separately neither `sub.beykus.fun` nor `panel.beykus.fun`'s production
  nginx vhost proxies a root path to the application today -- verified
  against the live config, not assumed.

---

Design-only text below this line, kept for the original contract record.
Status: design-only. This document fixes the token, schema,
API and migration contract but deliberately creates no table, endpoint or
credential. Implementation depends on the Phase 3 parent-account identity and
the Phase 4 staged legacy bridge.

## Current execution path and constraint

Current legacy requests are `GET /sub/{legacy_token}`. `src/routes/sub.py`
forwards that raw request token to Marzban's public subscription endpoint,
resolves a Marzban username, applies the permissive legacy HWID/device check,
then runs `src/subscription.py` filters/config generation. PH1-06 changed local
request/device references to `sha256:<hex>` but did not rotate the upstream
Marzban bearer. Marzban remains authority for the legacy alias and UUID.

The future token cannot be keyed to Telegram ID or directly to a Marzban
username. The stable authorization target is the Phase 3 MGBoost parent
account. Creating a transitional Telegram/username token table now would bake
the wrong identity into a security boundary and require another live migration.

## Canonical token and route

- Generate exactly 32 bytes with the operating system CSPRNG.
- Encode base64url without padding: exactly 43 characters matching
  `[A-Za-z0-9_-]{43}`.
- Canonical URL: `https://sub.beykus.fun/<opaque_token>`.
- Never accept the token in query parameters, fragments forwarded to the
  server, Telegram ID, username, email or a caller-selected value.
- Match reserved routes (`/sub/`, `/lk/`, `/assets/`, `/sub-admin/`, API and
  hidden diagnostic names) before the exact root token route.
- A syntactically valid but unknown/revoked token gets the same status, body,
  headers and bounded timing behavior. No fallback to a similarly shaped
  legacy token is allowed at the root route.

The legacy namespace remains `GET /sub/{legacy_token}` throughout Phase 4.
This separation avoids ambiguous format inference and preserves every existing
bookmark/client while the new root route is rolled out.

## Persistence schema

The target additive table is conceptually:

```text
subscription_credentials
  id                    immutable local identifier
  account_id            FK -> mgboost_accounts, NOT NULL
  token_hash            32-byte SHA-256 verifier, UNIQUE, NOT NULL
  version               token contract version
  generation            monotonic per account/purpose
  purpose               EXTERNAL_SUBSCRIPTION
  status                PENDING_DELIVERY | ACTIVE | REVOKED | EXPIRED
  created_at             UTC
  activated_at           UTC nullable
  revoked_at             UTC nullable
  revoke_reason          bounded enum/code nullable
  rotated_from_id        self-FK nullable
  last_used_at           coarse/bounded telemetry nullable
```

A partial unique constraint permits at most one ACTIVE external credential per
account. `(account_id, generation)` is unique and rotation uses compare-and-set
against the expected active generation. Revoked history is immutable and a
revoked verifier can never transition back to ACTIVE.

SHA-256 is a verifier here, not a password hash: the input is a uniformly
random 256-bit bearer, so an offline DB attacker cannot feasibly enumerate it.
No salt or password KDF is needed. A keyed verifier would add a second secret
without meaningful brute-force benefit. Compare only fixed-length verifier
bytes; DB indexes select candidates and application comparisons use
constant-time primitives where values are compared in memory.

Raw tokens are absent from the credential table, audit log, backups, URLs in
logs, analytics, exception text and list/detail API responses. Admin/UI lists
may show only a non-security hash reference or credential ID, never token
prefixes copied from the bearer itself.

## Issuance, delivery, rotation and revoke

One-time delivery must be crash-safe. Recommended state machine:

```text
none/ACTIVE -> PENDING_DELIVERY -> ACTIVE
old ACTIVE  ----------------------> REVOKED (same activation transaction)
```

The new raw token is generated server-side. Until an authorized delivery
channel acknowledges it, the old credential normally remains active and the
new raw value may be retained only as a short-lived AEAD-encrypted envelope
under a dedicated application delivery key that is not in the DB/backups.
Envelope ciphertext binds credential ID/account/generation as associated data,
has a short TTL, is single-use, and is deleted on ACK/expiry. Expiry abandons
the pending credential without revoking the old one.

For a confirmed compromise, policy DL-041 requires immediate old-token revoke;
the pending encrypted envelope permits retryable delivery without restoring
the compromised token. If delivery ultimately fails, admin issues another
generation. Ordinary Telegram ownership rebind does not enter this state
machine and does not rotate token or child UUID.

Explicit revoke is immediate and independent. Config caches cannot grant
future refreshes; Phase 3 child credential enforcement determines whether an
already cached UUID remains usable. Token revoke alone must not be represented
as device/UUID revoke.

Every issue/activate/rotate/revoke/expire action records actor, account,
credential IDs/generations, reason, correlation/idempotency key, old/new state
and timestamp, but no raw/ciphertext/token hash in the general audit payload.

## API contract

Public:

- `GET /<opaque_token>` resolves verifier -> ACTIVE credential -> parent
  account -> HWID/device slot -> child Marzban config.
- Sensitive response headers follow PH2-04; PH2-06 supplies trusted-IP rate,
  burst/body/deadline and uniform-failure controls.
- Resolver never accepts account/TG/username/slot identifiers from the caller
  to select a different subject.

Administrative, names illustrative until the account API is implemented:

- `POST /admin/accounts/{account_id}/subscription-credentials/prepare`
  creates an idempotent pending generation and returns/delivers the raw token
  only to the authorized one-time channel.
- `POST .../{credential_id}/activate` atomically activates expected generation
  and revokes the previous active credential.
- `POST .../{credential_id}/revoke` immediately revokes one credential.
- List/detail responses return lifecycle metadata only.

All mutations require PH1 admin session/CSRF, account-level authorization,
idempotency key and audit event. Account ID comes from the authenticated route
and DB relation, never a frontend-supplied reseller/TG identity. Generic token
lookup/Marzban proxy operations are forbidden.

## Phase 4 legacy bridge

Backfill an explicit legacy-alias relation from verified Marzban/admin evidence:

```text
legacy_subscription_aliases
  legacy_token_hash, account_id, legacy_marzban_username,
  migration_state, created_at, revoke_after, revoked_at
```

Only the hash is stored locally. A request supplies the raw legacy token, which
is hashed to find its mapped account and may be forwarded in-memory to the
existing public Marzban endpoint during the bridge. The relation preserves
DIRECT/manual-external/internal provenance, expiry, tariff evidence, devices
and Marzban username; it never infers identity from token contents or username
prefix.

Migration states remain LEGACY -> MIGRATING -> MIGRATED ->
LEGACY_REVOKE_PENDING -> LEGACY_REVOKED. Issuing a new opaque URL does not
automatically revoke the legacy alias. Phase 4 canary/grace policy controls
remote revoke of the old shared credential after child-device migration.

## Mandatory tests and gates

- entropy/length/alphabet, collision retry and deterministic rejection of
  malformed/reserved paths;
- DB/source/backup leak cannot reconstruct a URL; raw-token canary absent from
  DB, nginx/application/journal, audit, exceptions and API lists;
- valid/tampered/unknown/revoked uniform response and bounded timing;
- same token cannot select another account/slot by headers or IDs;
- concurrent issue/activate/revoke CAS; duplicate callback and crash at every
  delivery/activation boundary; pending expiry preserves old token;
- ordinary ownership rebind preserves token; compromise recovery revokes old
  and cannot reactivate it;
- legacy/new route collision corpus; old `/sub/{legacy}` remains functional
  until explicit Phase 4 revoke;
- revoke plus child UUID/cache semantics end-to-end;
- rate/burst/trusted-XFF tests from PH2-06;
- masked production canary proves zero UUID, legacy URL/token, HWID, expiry,
  tariff and config changes before any staged Phase 4 action.

## Rollback boundary

Before Phase 4 revoke, application rollback may disable the new root resolver
and keep legacy aliases working; it must not delete account/credential audit
history. Once a credential is revoked, rollback must never reactivate it.
Pending encrypted delivery data is discarded/abandoned safely if its code is
rolled back. No Phase 2 design-only step changes current users or credentials.
