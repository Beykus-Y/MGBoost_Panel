# PH3-06 — reviewed internal entitlements

Date: 2026-08-24

## Runtime boundary

PH3-06 is additive and dormant. `src/internal_entitlements.py` is not imported
by any HTTP, subscription, LK, Stars, Filin, bot or Marzban route. Startup only
applies the checksum-pinned schema and constructs a repository object. An empty
`PRIMARY_MGBOOST_ADMIN_ACTOR_ID` disables every PH3-06 write operation; the
identifier is a server-side authorization input, not a secret or a browser
claim.

No legacy username is used to grant access. An internal account must reference
an immutable `plan_kind=INTERNAL` plan with `billing_required=0`. The plan can
set a configurable device limit from 1 through 99 or semantic `UNLIMITED`,
which still resolves to the technical cap 99. WL is explicit `NONE` or
`UNLIMITED`.

## Reviewed account creation

The transactional creation operation requires:

- the configured primary-admin actor;
- an exact legacy username as migration evidence, not as authorization logic;
- an immutable versioned internal plan;
- current legacy status/expiry and aggregate device/HWID evidence;
- a reason, confidence, structured evidence and a stable idempotency key;
- ownership evidence classified as `PROVEN` or `ABSENT`.

`AMBIGUOUS` is rejected before an account or Telegram link is written. A
Telegram owner link is created only for `PROVEN`; `ABSENT` creates no binding.
The account, subscription, mutation, review and optional identity link are one
`BEGIN IMMEDIATE` transaction. The review and mutation are immutable. Retrying
the same operation returns the same account; reusing its key with another
payload fails closed.

Production provisioning is deliberately not performed by deployment. A later
primary-admin reviewed action must select the exact candidates. This prevents
an unlimited expiry, recognizable username or note from silently becoming an
INTERNAL entitlement.

## Effective entitlement and overrides

Only an `account_source=INTERNAL` account with an internal plan can use this
evaluator. Ordinary accounts fail closed and are not modified. Without an
active override, the result is the immutable plan (`AUTO`). An explicit
override:

- requires primary-admin authorization, a non-empty reason and future expiry;
- may live no longer than 90 days;
- is account/subscription scoped and idempotently recorded as an immutable
  entitlement mutation;
- can set limited/unlimited devices, WL access or WL quota;
- disappears from effective evaluation exactly at expiry, restoring plan/AUTO;
- increments a durable per-account revision under the same SQLite write
  transaction.

No process-local lock is the correctness boundary. SQLite `BEGIN IMMEDIATE`,
foreign keys, account-scoped joins and unique idempotency verifiers serialize
concurrent writers and reject cross-account references.

## Read-only production canary inventory

The following rows are candidates for owner review only. No parent account,
plan assignment or Telegram binding was created. “Telegram evidence” means an
old `tg_users` link exists; by PH3-01 policy it is not sufficient proof of sole
ownership. Device evidence is aggregate only; no raw HWID is included.

| Legacy username | Telegram evidence | Current Marzban state | Device evidence | Internal reason in durable evidence | Proposed plan | Confidence / action |
|---|---|---|---:|---|---|---|
| `beykus` | absent | active, expiry unlimited | 3 | absent | INTERNAL_UNLIMITED v1 | medium operational indicator; primary-admin review required |
| `beykusios` | one legacy link, not independently proven | active, expiry unlimited | 4 | absent | INTERNAL_UNLIMITED v1 | medium; verify owner before binding |
| `BeykusLaptop` | absent | active, expiry unlimited | 2 | absent | INTERNAL_UNLIMITED v1 | medium operational indicator; review required |
| `MegochelPC` | absent | active, expiry unlimited | 0 | absent | INTERNAL_UNLIMITED v1 | low/medium; review purpose and ownership |
| `MegochelAndroid` | absent | active, expiry unlimited | 3 | absent | INTERNAL_UNLIMITED v1 | low/medium; review purpose and ownership |
| `German` | absent | active, expiry unlimited | 7 | absent | INTERNAL configurable or unlimited v1 | low; unlimited expiry alone is insufficient |
| `Pensioner` | absent | active, expiry unlimited | 8 | absent | INTERNAL configurable or unlimited v1 | low; unlimited expiry alone is insufficient |
| `SUKA` | absent | active, expiry unlimited | 0 | absent | INTERNAL configurable or unlimited v1 | low; review whether this is a retained test account |
| `client_buy_9` | absent | active, far-future finite expiry | 1 | absent | no automatic proposal | low; do not infer INTERNAL from name or expiry |

Result: the runtime/account mapping for these live usernames is exact, but the
reason for INTERNAL classification is not durably proven. Therefore zero are
automatically provisioned by PH3-06. After primary-admin review, the first
three owner-operated candidates are the strongest initial cohort; missing
Telegram ownership must remain unbound until separately established.

The required exclusions are confirmed and untouched:

- orphaned local, non-live legacy username: `test`;
- live username with two Telegram links: `client_buy_1`.

Both remain Phase 4 manual-review cases.

## Tests and rollback

Focused PH3-06 tests cover migration repeatability, primary/non-primary
authorization, ambiguous ownership rejection, idempotent reviewed creation,
ordinary-account isolation, configurable/unlimited limits, technical cap 99,
reason/expiry/AUTO fallback, concurrent idempotent mutation, cross-account
isolation and a source scan for prohibited username special cases.

Rollback is application-only. An older binary ignores both additive PH3-06
tables. Do not drop immutable review evidence once provisioning is used. Since
deployment creates no plans/accounts/reviews/overrides and no legacy route
reads these tables, rollback requires no user credential or Marzban change.
