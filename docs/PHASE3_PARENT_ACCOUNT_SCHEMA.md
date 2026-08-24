# PH3-01 — Parent account / identity / entitlement schema

Date: 2026-08-24

## Scope and compatibility boundary

PH3-01 is an additive dormant schema foundation. Legacy Marzban users remain
the only production runtime authority. No HTTP, LK, bot, Stars, Filin,
subscription resolver or Marzban code path reads or writes the new tables.

This phase does not create accounts, plans, subscriptions, child users, UUIDs,
tokens, identity links, entitlement rows or WL periods. The sole automatic row
is the schema marker `ph3_01_parent_account_v1` with a source-controlled schema
checksum.

## Entities

| Table | Purpose and durable boundary |
|---|---|
| `mgboost_schema_migrations` | Idempotent migration ID/checksum/applied time. A changed checksum fails closed. |
| `mgboost_accounts` | Stable MGBoost identity independent of Telegram and Marzban username. Sources: `DIRECT`, `INTERNAL`, `UNKNOWN_LEGACY`. |
| `mgboost_telegram_identities` | Revocable Telegram owner-link history. Telegram ID is a unique active identity link, never a credential. One active owner per account. |
| `mgboost_plan_versions` | Immutable semantic plan snapshot: billing, limited/unlimited devices, WL mode/quota/period and unlimited non-WL traffic. |
| `mgboost_plan_durations` | Immutable duration variants. `duration_days` is generic, so 30/60/180 and later approved values need no schema change. |
| `mgboost_subscriptions` | Account-owned current subscription state and expiry pointer, including expired/unlimited/unknown legacy states. |
| `mgboost_entitlement_mutations` | Immutable operation provenance with distinct payment channel, mutation source, actor/reference and idempotency verifier. |
| `mgboost_subscription_terms` | Immutable per-purchase/renewal plan and duration snapshots. Sequence numbers represent same-plan stacking without rewriting prior terms. |
| `mgboost_entitlement_state` | Versioned current desired entitlement status for later workers/outbox. |
| `mgboost_entitlement_overrides` | Expiring, reasoned billing/device/WL overrides. Limited and unlimited values are distinct. Authorization/90-day policy belongs to PH3-06. |
| `mgboost_wl_periods` | Parent-owned sequential base-quota periods. A 60-day term can reference two independent 30-day rows; the future usage ledger remains Phase 6. |

Future `device_slots` and child-user mappings must reference
`mgboost_accounts.id` and the account-scoped subscription/entitlement state;
they must not use Telegram ID or Marzban username as account identity.

## Isolation and integrity

- SQLite foreign keys are enabled before migration.
- Every subscription child lookup is scoped by both `account_id` and resource
  ID. Composite foreign keys prevent a mutation, term, override, entitlement
  state or WL period from referencing another account's subscription.
- Partial unique indexes allow only one active account for a Telegram ID and
  one active owner link per account while retaining revoked link history.
- Plan versions, duration variants, subscription terms and mutation evidence
  are immutable through database triggers.
- Commercial device limits are first-class integer data (current 3/6/12,
  schema ceiling 99); `UNLIMITED` is a separate mode, not magic zero.
- WL uses integer bytes; approved decimal GB conversion remains
  `1 GB = 1,000,000,000 bytes`.
- `TELEGRAM_STARS`, `EXTERNAL_PAYMENT` and `ADMIN_GRANT` are distinct payment
  channels. `DIRECT_PURCHASE`, `MANUAL_PAYMENT`, `ADMIN`, `MIGRATION`,
  `INTERNAL` and other approved mutation sources remain separately auditable.
- Unknown evidence is represented explicitly as `UNKNOWN_LEGACY`; it is never
  inferred from username, prefixes, notes or price similarity.

## Stacking representation

The schema does not implement Phase 5 billing. It can record the approved rule
without alteration:

```text
new_expiry = max(current_expiry, now) + purchased_duration
```

Each unique payment creates one immutable mutation and one sequenced term. An
idempotency-key verifier prevents the same operation identity being recorded
twice. A 60-day WL term can produce two 30-day base periods; a future 180-day
duration is an ordinary duration row and can produce six such periods. Package
rollover remains a separate Phase 5/6 ledger concern.

## Production migration preview

The aggregate-only read-only preview emitted no Telegram IDs, usernames,
tokens, UUIDs, HWIDs or payment references:

- authoritative live Marzban users: 25;
- distinct legacy usernames observed across local evidence: 26;
- legacy device rows / HWID locks: 71 / 71, across 24 usernames;
- Telegram links: 5 across 4 usernames;
- usernames with exactly one Telegram link: 3 review candidates;
- usernames with multiple Telegram links: 1 mandatory ownership review;
- observed local usernames without a Telegram link: 22;
- durable paid/applied Stars events: 2, concerning one username;
- current new-plan assignments provable from legacy invoices: 0.

The 26-vs-25 difference is historical/local evidence, not authority to create
an extra account. It must be reconciled against retained Marzban/payment/audit
evidence in Phase 4.

### What can be proven

- An existing `tg_users` row proves that the old system recorded a link event;
  it does not prove that a single Telegram identity is the sole parent owner.
- A durable Telegram charge proves that specific event used the Stars channel.
  It does not prove the current six-plan catalog SKU or the origin of every
  day currently present in Marzban expiry.
- Existing device/HWID rows prove legacy observations for a username. They do
  not prove a parent account, physical-device identity or future slot owner.
- Current expiry/config/UUID remain authoritative in Marzban and are not copied
  by PH3-01.

Therefore the automatic backfill plan for PH3-01 is exactly zero accounts,
identity links, plan assignments, subscriptions, entitlements and periods.
Ambiguous records remain `UNKNOWN_LEGACY` candidates for reviewed Phase 4
migration.

## Tests and staging evidence

- schema first apply and repeat apply (`True` then `False`), one checksum marker;
- representative legacy DDL/data digest unchanged and old-code writes still
  work after additive migration;
- production online-copy migration twice: 20 legacy tables, exact masked legacy
  digest before/after, all ten new runtime tables empty;
- `PRAGMA quick_check=ok`, zero foreign-key violations;
- active Telegram uniqueness, one-owner rule, revoked-history rebind support and
  five repeated concurrent last-claim tests;
- account-scoped lookup and cross-account composite-FK rejection;
- immutable plan/duration/term/mutation snapshots;
- 3/6/12 and internal unlimited device modes, WL/non-WL, expired/unlimited;
- generic 180-day duration and two sequential 30-day periods for a 60-day term;
- distinct Stars/external/admin/unknown payment and mutation provenance;
- aggregate preview contains no raw identity/device/credential values.

Final local regression: `452 passed, 3 skipped`; focused PH3-01 suite:
`19 passed`.

## Production gate

1. Capture the existing masked 25-user config/identity and 71-device/HWID
   baseline.
2. Create and verify a recoverable encrypted/online database backup.
3. Pull the exact commit and restart only `mgboost-panel`; startup applies the
   transactional schema.
4. Assert one matching migration marker and zero rows in all ten new runtime
   tables; run quick/FK checks.
5. Smoke legacy `/sub`, admin, LK, Stars, Filin, broker and service health.
6. Compare exact masked pre/post legacy state and scan journals for migration
   errors or secrets.

## Rollback

Rollback is application-only: restore the previous commit and restart
`mgboost-panel`. The old binary ignores all `mgboost_*` tables. Do not drop the
new tables during rollback; destructive down-migration is unnecessary and
would remove future audit/migration evidence. If startup migration fails, its
single SQLite transaction rolls back and the new checksum marker is absent.
No user credential change is part of either deployment or rollback.

## Follow-up dependency graph

- Critical device lane: PH3-02 (atomic slots) -> PH3-03 (lazy child creation).
- Compatibility lane: PH3-07 observe-only telemetry -> PH3-04 fail-closed gate.
- Entitlement lane: PH3-06 internal/versioned entitlement evaluation, then use
  internal accounts as the first canary cohort; no username allowlist.
- Provenance lane: PH3-09 supplies reviewed account/payment mutation creation
  before Phase 4 legacy account population and Phase 5 billing.

PH3-02 must not start automatically as part of this change.

## Production evidence

Production deployment completed on 2026-08-24 after a verified encrypted
root-only backup. The exact application commit was pulled and only
`mgboost-panel` restarted. Startup created one matching migration marker and
zero account/identity/plan/subscription/entitlement/override/WL rows.

Post-deploy results:

- SQLite `quick_check=ok`; foreign-key violations: 0;
- masked Marzban identity/config digest: exact for 25/25 users, zero fetch
  errors;
- masked local device/HWID digest: exact for 71/71 rows;
- admin and LK: 200; uniform invalid legacy subscription: 404;
- signed Filin status and localhost broker health: 200;
- Stars ledger: the same two historical refunded invoices, no new mutation;
- Telegram runtime through configured proxy: pass; OpenRouter retained its
  pre-existing baseline 403;
- MGBoost/broker/nginx active and Marzban container running;
- new journal/nginx raw subscription paths, UUID-shaped values and migration
  error markers: 0.

No UUID, legacy token/URL, HWID, expiry, tariff, child user, configuration or
client reconfiguration changed. PH3-02 was not started.
