# AGENT_HANDOFF — PH4-03 (crash-safe, update after every major checkpoint)

Updated: 2026-08-26, PH4-03 CLOSED `[x]`. A migration-only legacy paid
compatibility entitlement (owner decision) closed the previous session's
subscription/plan blocker; both real DIRECT/EXTERNAL_PAYMENT cohort
accounts (`cohort-2 account #3`/`#4`, account ids 3/4) completed a full
real production migration canary (migrate + revoke + one rebind proof)
with zero impact on either real customer's own device. TELEGRAM_STARS
cohort remains an owner-approved `N/A` exception (zero real Stars purchases
ever existed).

## HEAD / git status

- HEAD after this session's commits (see `git log -1`); pushed to
  `origin/main`, deployed to production (pull + `mgboost-panel` restart),
  production HEAD verified to match.
- Working tree clean except pre-existing untracked `extra_configs.json`.

## THIS SESSION (part 3): legacy paid compat entitlement + real migration canary PASS -> PH4-03 `[x]`

Owner decisions this session:
1. Legacy paid compatibility entitlement is migration-only, never a
   commercial catalog entry: historical default device limit `3`, never
   inferred from current device/HWID counts; an owner-approved increase is
   explicit `3 + approved_extra_device_slots` with recorded evidence.
   Unlimited legacy WL (no quota bytes). Exact legacy expiry preserved.
2. Cohort-2 accounts #3/#4 (already reviewed-enrolled DIRECT accounts from
   the prior session) are confirmed real paying customers; proceed with
   their real migration canary once the compat entitlement exists.

### Code (new)

- `src/legacy_paid_compat.py` -- `ensure_legacy_paid_compat_entitlement()`.
  No new schema: reuses `mgboost_plan_versions`/`mgboost_subscriptions`
  (PH3-01) as-is. Creates/reuses a `LEGACY_PAID_COMPAT_V1_D{n}` plan
  version (immutable, `plan_kind='COMMERCIAL'`, `billing_required=0`,
  `wl_mode='UNLIMITED'`, `wl_quota_bytes=NULL`) and one live subscription
  per account with the account's exact already-reviewed legacy
  expiry/status (a terminal `DISABLED`/`EXPIRED` state is preserved as-is,
  never promoted to `ACTIVE`). Guards: requires an existing DIRECT review
  + owner-attested legacy payment; `observed_device_count > derived limit`
  fails closed (`DeviceOverageConflict`); a differing existing subscription
  fails closed (`SubscriptionConflict`); idempotent retry via the existing
  one-live-subscription-per-account partial unique index.
  **Important discovered constraint:** `DeviceSlotStore._entitlement_capacity`
  hard-requires `plan_kind='COMMERCIAL'` and `device_limit` in the existing
  `PAID_BASELINE_LIMITS={3,6,12}` frozenset for any `DIRECT` account -- a
  future compat `Dn` outside that set (e.g. `D4`/`D5`) would need that
  frozenset extended in `src/device_slots.py` (a plain code constant, not
  schema-locked) before it could actually claim a slot; not needed this
  session since both real accounts got `D3`.
- `tests/test_legacy_paid_compat.py` -- 18 focused tests (device-limit
  derivation incl. D4/D6, plan-variant reuse, device-rows-don't-raise-quota,
  overage fails closed, exact expiry, WL unlimited, no price reconstruction,
  idempotent retry, no duplicate/conflicting subscription, missing
  prerequisite guards, expired/disabled legacy never gets a fresh paid
  period, identity/provenance unchanged, and a full reviewed-enrollment ->
  attestation -> compat entitlement -> PH4-02 migration -> child
  integration test). Full regression: `869 passed, 3 skipped` (zero
  regressions from `851`).
- Deployed: encrypted backup+restore-verified beforehand
  (`scripts/secure_db_backup.py`, PASS/PASS; production state had changed
  since the prior backup). No schema change (pure application code) --
  fast-forward pull, minimal restart, `quick_check=ok`, 0 FK violations,
  only the two new rows in `mgboost_subscriptions`/`mgboost_plan_versions`
  (assigned in the next step) changed cardinality.

### Real production assignment + migration canary -- DONE, both accounts PASS

Fresh re-verification immediately before any mutation (both accounts):
still `active` in Marzban with unchanged `expire`, still exactly one
unambiguous Telegram mapping each, zero evidence anywhere (notes, tickets,
audit log, node filters, per-user configs) of an individually approved
device-limit increase -> both assigned the historical default
`LEGACY_PAID_COMPAT_V1_D3` (their own real usage is ~2 devices each, so D3
does not constrain them). Method: a short-lived root-only script per step
(dry-run-verified against a downloaded copy of the production DB first,
then run for real with `cd /opt/MGBoost_Panel` so `DATA_DIR` resolves
correctly), deleted immediately after each use.

Migration canary sequence, run on account A (cohort-2 account #3) first,
then account B (cohort-2 account #4) only after A fully passed:

1. **Genesis child** (real broker, before any bridge binding exists):
   `resolve_account_device()` never invents an account's first child -- it
   requires an already-established `source_contract_hash` from
   `mgboost_child_user_intents`, or it returns `PROVISIONING_UNAVAILABLE`
   (not a fall-through outcome). A real child was bootstrapped directly
   through the existing PH3-03 `child_provisioning` pipeline (real
   `get_user`/`ensure_child_user` broker calls) on the account's own slot 1,
   entirely BEFORE any `mgboost_legacy_bridge_bindings` row existed -- so
   the real customer's own device was never exposed to this gap even for a
   moment. **This bootstrap step is a real discovered prerequisite for any
   brand-new DIRECT account's first migration, not specific to these two
   accounts** -- worth remembering for any future cohort.
2. **Bridge binding created + enabled** (`db.legacy_bridge.create_binding`,
   `enabled=1`, per-account decision_ref).
3. **Migration proof**: a synthetic canary device (never the customer's own
   HWID) on the account's own spare slot, through the unmodified
   `process_migration_bridge_request` -- real `LEGACY -> MIGRATING ->
   MIGRATED`, real new child, real working subscription body, legacy
   Marzban user confirmed `active` throughout, no shared legacy UUID.
4. **PH3-05 revoke** the canary child -- confirmed `REVOKED`. A same-device
   retry afterward returned `PROVISIONING_PENDING` (NOT `OK`, NOT a
   fall-through outcome) -- no resurrection, no silent legacy fallback,
   exactly matching account 1's own earlier internal-canary proof.
5. **PH3-05 free** the canary slot -- returned to `FREE`.
6. **(Account A only) supplementary PH3-05 rebind proof**, on the
   already-freed canary slot (zero real-device impact): a new canary device
   claimed the slot (generation 2), then `process_rebind()` moved it to
   generation 3 with a different synthetic HWID. One real snag hit and
   fixed live: `process_rebind()` only prepares the new child's outbox
   entry -- it hands off actual provisioning to the worker/next resolver
   call, it does NOT ensure/acknowledge synchronously. The production
   `mgboost-child-worker` hadn't picked up the pending row within ~40s of
   polling by the time this was checked, so the new child was finished
   manually via the exact same `child_provisioning.claim()` ->
   `ensure_child_user()` -> `acknowledge()` sequence the worker itself
   would use -- then verified end-to-end (`process_migration_bridge_request`
   for the new HWID returned `OK` with the new child). Old (generation 2)
   child confirmed `REVOKED` throughout. Cleaned up afterward with the same
   revoke+free sequence, leaving the slot `FREE` again.

Verified before and after every step, both accounts: exact legacy
expiry/status, account `public_id`, ownership review, and owner-attestation
rows all unchanged; `mgboost_payment_records` stayed empty throughout (no
invented payment was ever created); the real legacy Marzban user's
`status`/`expire` were read-verified unchanged at multiple checkpoints;
`quick_check=ok`, 0 FK violations; all 4 services stayed active. Final
state per account: slot 1 = the real permanent genesis child (`ACTIVE`),
slot 2 = `FREE` (all canary/rebind-proof children on it are permanent
`REVOKED` tombstones, matching this project's own retention convention),
slot 3 never claimed -- 2 free slots remain for each customer's real ~2
devices going forward.

### TELEGRAM_STARS and Telegram ownership rebind -- unchanged from the prior session

Still a documented owner-approved `N/A` exception (zero real purchases ever
existed) and still relying on existing focused tests + PH2-05's own
production-proven mechanism (no real customer's Telegram identity was
mutated this session either). See the prior "THIS SESSION (part 2)" section
below for the full reasoning, unchanged.

### Metrics/support runbook -- DONE

`docs/PHASE4_MIGRATION_SUPPORT_RUNBOOK.md`: how to read an account's
migration state/lineage, a compat entitlement's `Dn`/expiry/WL semantics,
ownership/payment provenance, how to recognize and react to
`ERROR_RECONCILE`, and what to do (and not do) when a canary/migration
attempt fails. Deliberately excludes secrets/PII -- every example is by
account id.

## PH4-03 verdict: `[x]` -- CLOSED

All ROADMAP accept-criteria items are satisfied: internal cohort PASS
(prior session); several real DIRECT/`EXTERNAL_PAYMENT` subscriptions PASS
(this session, both accounts, migrate+revoke+one rebind proof); `TELEGRAM_STARS`
documented `N/A` exception; account identity/payment provenance/manual
renewal semantics preserved (same account/plan/PH3-08 sync model
throughout, untouched); Telegram ownership rebind sufficiently proven via
existing tests/PH2-05 evidence without an unnecessary live mutation;
metrics/support runbook exists; full regression clean (`869 passed, 3
skipped`). PH4-08 (full legacy-subscription-preservation/renewal flow) and
PH5-09 remain explicitly their own future phases, both still `[ ]` --
PH4-03 only needed and built the minimal migration-compatibility
prerequisite, not PH4-08's full scope.

## Exact next step

PH4-04 (new opaque URL rollout) is the next ROADMAP phase, but was
explicitly NOT started this session and requires the owner's separate
authorization to begin, per instruction.

---

## PRIOR SESSION HISTORY (kept for continuity, all still accurate)

## THIS SESSION (part 2): real DIRECT/EXTERNAL_PAYMENT cohort + owner decisions

Owner supplied 3 authoritative product decisions this session:
1. All real legacy paying users historically paid the owner directly, never
   Stars; record this as `OWNER_ATTESTED_LEGACY_EXTERNAL_PAYMENT` with no
   invented amount/date/reference.
2. Zero real Stars purchases ever existed in production; TELEGRAM_STARS
   cohort = owner-approved `N/A` exception, not a failure, must be
   documented, not silently skipped.
3. Reuse the existing bot Telegram-linkage flow (`tg_users`/`bot_support.py`,
   the `waiting_link` state that resolves a pasted subscription URL to a
   `marzban_username` via `marzban.get_username_for_token` then calls
   `db.save_tg_user(message.from_user.id, username)`) instead of building a
   second mechanism. That flow proves POSSESSION of the subscription link,
   not ownership by itself (confirmed by reading it: `save_tg_user` will
   happily rebind a username to a different Telegram ID with no ownership
   check at all -- this is exactly why the excluded ambiguous-ownership legacy account has two conflicting
   `tg_users` rows). PH2-05's "HWID/URL is not ownership proof" rule is
   therefore NOT weakened: `enroll_direct_account()` treats a bot-linked
   mapping as evidence only when combined with owner review/attestation, and
   now cross-checks it defensively (new `TelegramMappingConflict`/ambiguity
   checks, see below).

### Code changes (commit `b31e3a1`)

- `src/legacy_payment_attestation_schema.py` (new) — additive
  `mgboost_owner_attested_legacy_payments` table + immutability triggers +
  a validate trigger requiring an already-reviewed DIRECT account. Its own
  `MIGRATION_ID`/checksum, parented on `direct_enrollment_schema`'s
  checksum. Deliberately NOT a change to `mgboost_payment_records` --
  that table's CHECK constraints are already checksum-locked by the
  deployed PH3-09 migration (`apply_provenance_schema` would raise
  `RuntimeError` on every future startup if that file's `_SCHEMA_STATEMENTS`
  were edited in place). This is the general rule for ALL of this project's
  schema files, not just this one: never edit an already-shipped
  `_SCHEMA_STATEMENTS` tuple; add a new sibling migration instead.
- `src/direct_enrollment.py`:
  - `DirectEnrollmentStore.record_owner_attested_legacy_payment()` — no
    caller-supplied idempotency key; the natural key is `account_id` itself
    (at most one attestation per account, `UNIQUE(account_id)` in schema
    too). Same full payload (decision_ref/note/evidence) twice ->
    idempotent, same row returned. Different payload for an account that
    already has one -> `OwnerAttestationConflict`, nothing changed. Also
    writes a `mgboost_entitlement_mutations` row
    (`operation='OWNER_ATTESTED_LEGACY_EXTERNAL_PAYMENT'`,
    `mutation_source='MANUAL_PAYMENT'`, `payment_channel='EXTERNAL_PAYMENT'`
    -- already an allowed combination in `ProvenanceStore`, no schema
    change needed there) so it appears in the same canonical audit trail as
    every other provenance mutation, even though it lives in a sibling
    table rather than `mgboost_payment_records`.
  - `enroll_direct_account()` now cross-checks `tg_users` before accepting
    `PROVEN`: more than one distinct Telegram ID already linked to this
    legacy username -> `AmbiguousOwnershipRejected`; caller asserts a
    Telegram ID that contradicts the single bot-recorded one ->
    `TelegramMappingConflict` (new exception). Both fail closed, zero
    writes. If `tg_users` has no row at all for the username, no
    cross-check is possible and enrollment proceeds on the caller's
    evidence alone, same as before.
- `tests/test_direct_enrollment.py`: +9 tests covering all of the above
  (owner-attested no-fabricated-data, idempotent retry, conflicting details
  rejected, requires-reviewed-account, bot-mapping-reused-not-duplicated,
  conflicting-bot-mapping-fails-closed, ambiguous-two-Telegram-IDs-fails-
  closed, Stars validation unchanged, new schema idempotent). Total in this
  file: 25 passed. Full regression: `851 passed, 3 skipped` (zero
  regressions from the 842 baseline).
- Deployed: encrypted backup+restore-verified BEFORE the schema change
  (`scripts/secure_db_backup.py`, PASS/PASS), fast-forward pull, minimal
  restart, post-deploy `quick_check=ok`, 0 FK violations, only the new
  table appeared (all other cardinalities identical), no journal errors.

### Real production DIRECT/EXTERNAL_PAYMENT enrollment — DONE

Candidates were the 2 identified in this session's earlier read-only
discovery (only 2 users in all of production have unambiguous, evidenced
Telegram ownership outside the excluded/internal set): `cohort-2 account #3`
(account id 3) and `cohort-2 account #4` (account id 4).

Pre-mutation re-verification (fresh, same session, immediately before
running): both still `active` in Marzban, same `expire` as discovery,
`tg_users` still exactly one distinct Telegram ID each (unchanged from
discovery), `tickets` corroborates `cohort-2 account #4`, no pre-existing
`mgboost_accounts`/alias/review/payment row for either, no Stars invoices
for either username. Zero drift, zero conflict -- proceeded.

Method: a short-lived root-only script (`/root/ph4_03_direct_cohort_enroll.py`,
0700, deleted immediately after use -- same discipline as account 1's
session), first dry-run-verified against a real downloaded COPY of the
production DB (caught and fixed a real bug: the script's first production
run used the wrong `DATA_DIR`/cwd and would have created/touched a stray
`/root/data/db.sqlite3` instead of the real database -- this was caught
before it mattered, verified the real production DB was untouched, deleted
the stray file, and re-ran with `cd /opt/MGBoost_Panel` so `DATA_DIR=./data`
resolved correctly, exactly matching the real service's own
`WorkingDirectory`). Real run: called `enroll_direct_account()` then
`record_owner_attested_legacy_payment()` for each username, via
`db.primary_admin_authority.authorize_session()` using the real
`PRIMARY_MGBOOST_ADMIN_LOGIN` from production `.env`.

Result: 2 new `ACTIVE` `DIRECT` accounts (ids 3/4), 1 reviewed alias each
(`EVIDENCE_PROVEN`), 1 Telegram `OWNER` identity each linked via the
existing `AccountStore.link_telegram_owner` (reusing, not duplicating, the
bot's own `tg_users` mapping), 1 `mgboost_owner_attested_legacy_payments`
row each (no invented amount/date/reference). `mgboost_legacy_bridge_bindings`
unchanged (still 1 row, only account 1) -- these enrollments are additive
and dormant, zero effect on live legacy traffic. Post-mutation verification:
real Marzban `cohort-2 account #3`/`cohort-2 account #4` completely unchanged (`active`,
same `expire`), `quick_check=ok`, 0 FK violations, all 4 services active.

### TELEGRAM_STARS cohort — owner-approved N/A exception

Zero real successful Stars purchases exist in production history. The only
2 `stars_invoices` rows ever created are both `refunded` test canaries for
the excluded ambiguous-ownership legacy account. Per owner decision: this is documented as
`N/A -- no real production population existed at PH4-03`, not silently
skipped and not faked. No artificial purchase was created, no real user was
asked to buy Stars to satisfy this phase. The Stars code path
(`record_stars_payment`/`process_direct_stars_enrollment`) remains fully
covered by focused tests. **The first real successful Stars purchase after
launch requires its own real canary gate before any wider Stars rollout --
this is a standing requirement, not yet satisfied by anything in this
session.**

### THE one remaining PH4-03 acceptance blocker: real migration on a DIRECT account

Not a missing candidate, not a missing mechanism gap in the enrollment
code -- a genuine architectural prerequisite gap discovered this session:

`resolve_account_device()` (the shared PH2-01/PH4-01 tail that
`process_migration_bridge_request` ultimately calls) calls
`db.parent_sync.refresh_desired_state(account_id)`, which raises
`ParentSyncError("account has no subscription to derive entitlement from")`
if the account has no `mgboost_subscriptions` row --
`mgboost_subscriptions.current_plan_version_id` is `NOT NULL` unless
`status='UNKNOWN_LEGACY'`. That exception is caught as a generic
`Exception` -> `OUTCOME_INTERNAL_ERROR`, which is **not** in
`_FALL_THROUGH_OUTCOMES` -- so `_try_legacy_bridge()` in `routes/sub.py`
would NOT fall through to the normal legacy response; it would return a
fail-closed error response instead.

`enroll_direct_account()` deliberately does not create a
`mgboost_subscriptions`/`mgboost_plan_versions` row, because doing so would
require declaring a device_limit/WL mode for `cohort-2 account #3`/`cohort-2 account #4`'s
historical (unproven-tariff, legacy-Marzban-never-enforced-a-device-cap)
plan -- exactly the invented catalog tariff the owner explicitly forbade
this session ("Не назначать новый catalog tariff, если исторический tariff
не доказан").

Critically, this is NOT a risk that a synthetic-canary-only device
sidesteps: `LegacyBridgeStore.resolve_account_for_legacy_username()` is
username-level, not per-device -- the moment an `enabled=1`
`mgboost_legacy_bridge_bindings` row exists for one of these accounts (and
`LEGACY_BRIDGE_ENABLED` is already `1` globally in production), the
customer's OWN real device would hit the exact same missing-subscription
path on its very next ordinary legacy `/sub` request and get the fail-closed
error too -- a real outage for a real paying customer, not a contained
canary risk. So no bridge binding was created for either account, and no
migration/revoke/rebind was attempted.

This is precisely PH4-08's own scope ("Preserve legacy manual/
external-payment subscriptions... plan/conditions... ambiguous provenance
получает UNKNOWN_LEGACY", depends on "authoritative payment/admin
evidence" -- now available via this session's owner attestation) and was
correctly out of this session's scope, not something to improvise around.

Real PH2-05 ownership rebind on a non-internal account was likewise not
attempted (per owner instruction: existing focused integration tests +
account 1's real production mechanism proof are sufficient; do not mutate
a real customer's Telegram identity solely to check a box).

## PH4-03 verdict this session: remains `[~]`

## THIS SESSION: reviewed DIRECT enrollment/payment foundation (additive, dormant)

Added the DIRECT-cohort counterpart PH4-03 needs before any real DIRECT/Stars
or DIRECT/external-payment cohort can be enrolled. Nothing here is wired into
any live HTTP/bot route -- it is only new, importable, tested store code plus
new empty tables.

- `src/direct_enrollment_schema.py` — `mgboost_direct_enrollment_intents`
  (durable, pre-account-creation idempotency anchor; `account_id` is
  fill-once, enforced by a DB trigger) and `mgboost_direct_account_reviews`
  (separate from and never touching PH3-06's INTERNAL-only
  `mgboost_internal_account_reviews`; its own DB trigger requires
  `account_source='DIRECT'`). Parent schema gate: PH3-03
  (`child_provisioning_schema`), same as PH4-01's legacy bridge schema.
- `src/direct_enrollment.py` — `DirectEnrollmentStore`
  (`db.direct_enrollment`):
  - `enroll_direct_account()` — creates the account only via the existing
    `AccountStore.create_account('DIRECT')` (as explicitly required), reuses
    the already-generic PH3-03 `mgboost_legacy_alias_groups`/
    `mgboost_legacy_account_aliases` tables unchanged, writes the DIRECT
    review audit row (legacy username, ownership evidence, actor,
    decision_ref), and links the Telegram owner via the existing
    `AccountStore.link_telegram_owner()` if ownership is `PROVEN`.
    Ambiguous ownership (anything other than exactly `PROVEN`/`ABSENT`)
    fails closed with zero writes. One legacy username can never bind to two
    accounts (checked in-application before any account is created, and
    backstopped by the existing DB `UNIQUE(legacy_username)` constraint).
    Crash-safe: a durable intent row is claimed BEFORE
    `AccountStore.create_account()` is ever called, so retrying with the
    same idempotency key after a crash at any point converges on exactly one
    account/alias/review, never a duplicate.
  - `record_stars_payment()` — a real `stars_invoices` row only becomes a
    canonical `mgboost_payment_records` row (via the existing
    `ProvenanceStore.record_payment`) if its status is `paid`/
    `plan_committed`/`applied`; `refunded`/`refund_unknown`/`manual_review`/
    `created` are rejected (`InvoiceNotPayable`). The invoice's
    `marzban_username` must match the account's reviewed legacy username,
    and its `payer_telegram_id` must match the account's reviewed Telegram
    owner (`PayerMismatch` otherwise). Duplicate invoice recording is
    idempotent (same invoice -> same payment row, no duplicate).
  - `record_external_payment()` — minimal admin-only primitive for
    `payment_channel='EXTERNAL_PAYMENT'`/`mutation_source='MANUAL_PAYMENT'`,
    the low-level PH5-09 prerequisite only (PH5-09 itself -- renewal/plan
    changes on manual payment -- is NOT implemented and NOT marked done).
    Duplicate `external_reference` is rejected by the existing
    `ProvenanceStore` `UNIQUE(payment_channel, external_reference)`
    constraint.
  - `process_direct_stars_enrollment()` — the one orchestration flow tying
    enrollment + Stars payment together; proven by test to converge to
    exactly one account/alias/review/payment across a simulated crash
    between steps and a full-flow retry.
- `tests/test_direct_enrollment.py` — 16 focused tests: happy path, retry/
  idempotency, idempotency-key-reused-with-different-payload conflict,
  ambiguous ownership fail-closed, cross-account alias conflict,
  unauthorized review, paid/refunded/manual-review Stars, payer mismatch,
  duplicate Stars invoice, external payment, duplicate external reference,
  crash/retry across the orchestration flow. All pass.
- Full regression: `842 passed, 3 skipped` (was `826 passed, 3 skipped`
  before this session's 16 new tests — zero regressions).
- Production deploy: additive schema only (new tables start and remain
  empty), no route/worker calls any of this code, `LEGACY_BRIDGE_ENABLED`
  and all other flags/state from the prior internal-canary session are
  unchanged. Post-deploy invariants verified: services active, `quick_check
  =ok`, 0 FK violations, new tables present and empty.

### NOT done by this session

- No real DIRECT account, alias, review, Stars payment or external payment
  was created anywhere, including production. the excluded ambiguous-ownership legacy account (or any other
  real paying legacy user) was NOT touched or enrolled.
- PH5-09 itself (manual-payment-driven renewal/plan changes) is intentionally
  NOT implemented — only its low-level `EXTERNAL_PAYMENT`/`MANUAL_PAYMENT`
  provenance primitive exists now.
- PH4-03 remains `[~]` in `ROADMAP.md` — cohorts 2/3 (real DIRECT/Stars,
  real DIRECT/external-payment) are still blocked on the owner supplying (or
  authorizing selection of) real candidate identities; see the existing
  "Cohorts 2 and 3" section below, which is still accurate and unchanged by
  this session.

## PH4-03 goal (ROADMAP.md, `Depends: PH3-06/09, PH4-01/02`)

Controlled canary migration, cohort order: internal users -> several
DIRECT/Stars subscriptions -> several DIRECT/external-payment subscriptions
-> mass migration. Internal-only is explicitly NOT sufficient. Accept:
representative clients migrate/device-rebind/revoke + admin-only Telegram
ownership rebind; account identity/payment provenance/manual renewal
preserved.

## Done so far

### 1. Live route wiring (code, committed+deployed)

- `src/routes/sub.py::_try_legacy_bridge` now calls PH4-02's
  `process_migration_bridge_request()` instead of the bare
  `resolve_legacy_bridge()`. Same resolver, durable per-device lineage now
  recorded on every real activation. Flag-off/no-binding behavior proven
  byte-identical (`tests/test_legacy_bridge_route.py`).
- Commit `8058772`, pushed, pulled to production, `mgboost-panel` restarted,
  all 3 services active, HTTP 200.

### 2. Focused tests (all pass)

- `tests/test_ph4_03_migration_cohort_integration.py` (6 passed): migration
  on a real (non-internal) DIRECT account preserves payment provenance
  (TELEGRAM_STARS + EXTERNAL_PAYMENT channels, zero new provenance rows
  written by migration) and account identity; coexists with PH3-08 manual
  renewal (`refresh_desired_state` reflects renewal, lineage untouched);
  ORDINARY ownership rebind preserves lineage + opaque token; COMPROMISE
  ownership rebind rotates the opaque token but does NOT touch/replace the
  migration lineage and never creates a second parent account.
- Full regression: `826 passed, 3 skipped` (was 820 before PH4-03 — zero
  regressions).

### 3. Real production internal canary — DONE

Identity used (pre-verified against live production before use, matched
exactly): account id `1`, public_id `acct_435p4hjeoxeq3bzg4ifkdut4veower4r`,
primary legacy alias `beykusios`, Telegram owner `905302972`, actor
`owner:mgboost-primary:v1`.

Steps actually performed on production (all via short-lived root-only 0600
scripts, deleted immediately after use — none left on disk):

1. Created `mgboost_legacy_bridge_bindings` row for account 1 (enabled=1) —
   via `db.legacy_bridge.create_binding`, real admin capability.
2. Backed up `/opt/MGBoost_Panel/.env` to `/root/config-backups/ph4-03/`,
   appended `LEGACY_BRIDGE_ENABLED=1`, restarted `mgboost-panel` (confirmed
   with user permission — this restart was blocked once by the auto-mode
   classifier and explicitly re-authorized by the user before proceeding).
3. Ran a controlled real migration for a NEW synthetic device HWID
   (`ph4-03-internal-canary-device-1`) on slot 2 (FREE) — deliberately did
   NOT touch slot 1 (the account's real live daily-use device) to avoid any
   risk of disrupting real connectivity. Result: `OK`, real new child
   `mgc_pdj7eq4i2v4y6nuw2l65j4322u`, `MIGRATED` binding
   `mg_bdsxk2vjthv2rycuu5v3ldfgau`, legacy user (`beykusios`) confirmed
   still `active`/untouched, new child confirmed `active` with 25 VLESS
   inbounds.
4. Real PH3-05 REVOKE on that same canary child — `APPLIED`, remote child
   confirmed `disabled`. Follow-up migration attempt for the same device
   correctly returned `PROVISIONING_PENDING` (fail-closed, NOT a
   fall-through/legacy-fallback outcome) — proves "no silent shared-UUID
   fallback" and "no resurrection of a revoked generation" empirically on
   real production, not just in tests. (Root cause understood: PH3-05
   REVOKE alone does not free the slot by design — a device stays
   deliberately non-functional until FREE/REBIND; this is correct, expected
   behavior, not a bug.)
5. Real PH3-05 FREE to release slot 2 back to `FREE` (cleanup after the
   controlled canary proof) — `APPLIED`.
6. Post-canary invariants verified: `quick_check=ok`, 0 FK violations, slot
   1 (real device) completely untouched throughout, legacy user untouched,
   `mgboost_migration_bindings` = 1 row (the canary's own historical
   lineage, correctly `MIGRATED`, preserved as permanent audit trail — not
   deleted, matching this project's convention).

Real Telegram-ownership-rebind on the REAL account 1 was deliberately NOT
performed in production (would rebind the actual owner's real Telegram
identity — too invasive/irreversible-feeling for a proof-of-mechanism);
that requirement is instead satisfied by the focused tests in item 2 above,
which exercise the real `process_rebind()` orchestration end-to-end.

### Current live production state

- `LEGACY_BRIDGE_ENABLED=1` (changed from the PH4-02 baseline of `0` —
  this is intentional and is THE PH4-03 canary activation, not a residual).
- `mgboost_legacy_bridge_bindings`: 1 row (account 1, enabled).
- `mgboost_migration_bindings`: 1 row (account 1's canary device, state
  `MIGRATED`).
- Slot 1 (account 1's real live device): untouched, still on its original
  child `mgc_sgg6v7t6he43yytsqmkdczzfpa`. If/when that device's own client
  next hits `/sub`, it WILL now migrate transparently (same already-existing
  child, by design) via the new durable route wiring — this is expected and
  intentional per the internal cohort's own purpose, not an open risk.
- No other account has a binding. `OPAQUE_SUBSCRIPTION_ENABLED=False`,
  `PH3_04_ENFORCEMENT_MODE=OFF` unchanged.

## NOT yet done (superseded by "THIS SESSION (part 2)" above — kept for history)

Cohorts 2/3 candidate selection is DONE (`cohort-2 account #3`/`cohort-2 account #4`,
enrolled as reviewed DIRECT/`EXTERNAL_PAYMENT`, see above). What remains is
the single architectural blocker documented above (`mgboost_subscriptions`/
plan prerequisite for real migration), not a candidate-identity gap.

### Remaining accept-criteria items — updated

- Real device migrate/revoke/rebind on a non-internal account: **blocked**
  on the subscription/plan prerequisite gap above (PH4-08 territory) —
  requires an owner product decision on device_limit/WL semantics for
  unproven-tariff legacy accounts before it can proceed safely.
- Real PH2-05 admin ownership rebind proof on a non-internal account: not
  attempted, per owner instruction that existing focused tests + account
  1's real mechanism proof are sufficient.
- metrics/support runbook (ROADMAP's own accept line mentions this — still
  not drafted).

## Known non-blocking backlog

- None discovered this session beyond what's already noted above (the
  PH3-05 REVOKE-without-FREE "stuck at PROVISIONING_PENDING" behavior is
  confirmed correct-by-design, not a defect).

## Explicitly NOT started

PH4-04 (opaque URL rollout), PH4-05 (grace), PH4-06 (production legacy
revoke), PH4-08 (legacy plan/device-capacity preservation — now the actual
blocker), PH5-09, mass migration, PH5+.

## Exact next step if resumed

1. This is now a product decision, not a data-gathering task: ask the owner
   how device_limit/WL semantics should be set for a reviewed DIRECT
   account whose historical legacy tariff is unproven (legacy Marzban never
   enforced a device cap at all) — e.g. an explicit `UNLIMITED` device
   plan to literally preserve legacy behavior, vs. drafting PH4-08 properly
   first. Do not invent an answer unilaterally.
2. Once decided: implement the minimal piece needed (likely a small
   addition to `DirectEnrollmentStore.enroll_direct_account()` or a
   dedicated PH4-08 module) that creates a `mgboost_subscriptions` +
   `mgboost_plan_versions` row consistent with that decision, for the 2
   already-enrolled accounts (ids 3/4) — do NOT re-enroll them, they
   already exist and are reviewed.
3. Only then create `mgboost_legacy_bridge_bindings` for account 3 first
   (enabled=1), re-verify current production state immediately before, and
   watch the very next real legacy `/sub` request for that username
   (should now migrate transparently instead of failing closed) — same
   "prove on `cohort-2 account #3` first, then `cohort-2 account #4`" order the owner
   already specified.
4. After both are proven migrated (and revoke/FREE proven on a synthetic
   device the same way account 1's canary was), update `ROADMAP.md` PH4-03
   to `[x]`, `CHANGELOG.md`, commit, push, verify production HEAD parity,
   final report.
5. If quota runs out before an owner decision arrives: this file plus
   `git diff`/`git log` is sufficient for a fresh agent to resume exactly
   from "waiting on owner's device/WL semantics decision for unproven-tariff
   DIRECT accounts."
