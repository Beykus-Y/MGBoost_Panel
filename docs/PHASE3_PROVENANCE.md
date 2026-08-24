# PH3-09 — account/payment/mutation provenance foundation

Date: 2026-08-24

## Explicit model

Account ownership/source, payment channel and mutation source are independent:

```text
account_source:    DIRECT | INTERNAL | UNKNOWN_LEGACY
payment_channel:  TELEGRAM_STARS | EXTERNAL_PAYMENT | ADMIN_GRANT | UNKNOWN_LEGACY
mutation_source:  DIRECT_PURCHASE | MANUAL_PAYMENT | ADMIN | MIGRATION |
                  INTERNAL | UNKNOWN_LEGACY
```

The existing account row remains the parent identity. A payment record never
creates or transfers ownership. A renewal mutation addresses the same account
and, later, its account-scoped subscription. The API requires the channel and
source explicitly and validates their combination. It accepts no username or
note parameter and performs no inference from either.

`UNKNOWN_LEGACY` is a real evidence state, not a synonym for manual/external
payment. Historical Stars or external-payment attribution must only be written
when the corresponding durable charge/admin evidence exists.

## Durable records

`mgboost_payment_records` is an immutable fact record with:

- opaque public ID and parent-account foreign key;
- explicit channel/status;
- optional nonnegative amount/currency and method;
- explicit external reference, actor and structured evidence;
- namespace-separated idempotency verifier and canonical request hash;
- account-scoped composite identity for safe linkage.

`mgboost_mutation_payment_links` immutably connects an entitlement mutation to
a payment belonging to the same account. Composite foreign keys reject
cross-account payment/subscription references. Existing immutable
`mgboost_entitlement_mutations` stores the explicit source/channel, actor,
reason, before/after evidence, external reference and unique idempotency
verifier.

The repository uses `BEGIN IMMEDIATE`. A same-key/same-payload retry returns
the original record. A same key with a changed payload, a repeated external
reference, wrong account or wrong channel fails closed. It does not apply an
entitlement, update expiry or call Marzban; Phase 5 and the future outbox own
those effects.

## Current legacy boundary

The current Stars tables and Filin/manual flows remain authoritative legacy
runtime and are not backfilled or connected by PH3-09. Existing evidence is
therefore unchanged:

- two retained legacy Stars invoice events exist for one legacy username;
- no current six-plan assignment is provable from those rows;
- current external/manual payments have no structured payment record in this
  repository;
- username prefixes/notes cannot establish a payment channel;
- no new account/payment/mutation rows are automatically created.

## Outbox readiness and remaining work

The stable account ID, immutable payment ID, mutation ID and idempotency
verifier are suitable causal references for a durable child-create or
entitlement outbox event. PH3-09 does not yet provide the outbox itself. Before
PH3-03, add an account/slot/generation-scoped desired operation containing a
unique operation key, typed payload digest, pending/applied/error state,
attempts, remote observation and reconciliation timestamps.

The Phase 1 broker must also gain a new child-specific typed surface rather
than reusing caller-controlled generic legacy semantics blindly. Required
PH3-03 operations are: deterministic child lookup/create, exact reread/verify,
and later disable/revoke/update-expiry/update-only-inbounds. The broker must
accept a server-derived child username/plan payload, reject arbitrary
proxies/inbounds/data-limit changes outside the operation, and return enough
remote identity/effect evidence for outbox reconciliation. Existing ten legacy
operations remain unchanged for compatibility.

## Tests and rollback

Focused tests cover all four payment channels, explicit source/channel
validation, immutable rows, same-payload retry, changed-payload/duplicate-ref
conflicts, concurrent duplicate reference, cross-account IDOR, same-parent
renewal and source scan/API proof that username/note inference is absent.

The migration is additive/checksum-pinned and both new tables begin empty.
Rollback is application-only; older code ignores them. Once real evidence is
recorded, do not drop these immutable tables during rollback.

## Production evidence

Production deployment completed on exact commit `08397f3` after a fresh
verified encrypted backup. Only `mgboost-panel` restarted. The migration
marker, SQLite quick check and foreign-key check passed; payment/link,
account/review/slot/generation tables remain empty.

Masked pre/post digests match exactly for 25 Marzban users/configs and 71
legacy device/HWID rows. Admin, LK, uniform invalid subscription, signed Filin,
localhost broker, Telegram proxy, nginx/systemd and token-safe journal/access
logs passed. No existing expiry, credential, config or runtime entitlement was
changed. PH3-03 was not activated.
