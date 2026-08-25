# AGENT_HANDOFF — PH4-03 (crash-safe, update after every major checkpoint)

Updated: 2026-08-26, mid-PH4-03: internal cohort proven, reviewed DIRECT
enrollment/Stars/external-payment foundation added (additive, dormant, no
route wiring), still awaiting owner-authorized real DIRECT/Stars and
DIRECT/external-payment candidate identities.

## HEAD / git status

- HEAD after this session's commit (see `git log -1`); pushed to
  `origin/main`, deployed to production (pull + `mgboost-panel` restart),
  production HEAD verified to match.
- Working tree clean except pre-existing untracked `extra_configs.json`.

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
  was created anywhere, including production. `client_buy_1` (or any other
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

## NOT yet done

### Cohorts 2 and 3: "several DIRECT/Stars" + "several DIRECT/external-payment" real subscriptions

**Blocker per user's own explicit instruction: do not guess a real external
paying user.** There is currently no reviewed/vetted list of real
production DIRECT/Stars or DIRECT/external-payment subscribers available to
this agent session that would let it safely select specific real
candidates without guessing. Production today has only two real accounts:
account 1 (INTERNAL, just canaried above) and account 2 (INTERNAL,
`DISABLED`, PH3-08's own throwaway canary — not a real paying user either).

**Exact requirement to unblock this**: the owner must supply (or point to
an already-existing reviewed source for) at least one real, currently
active legacy Marzban username to enroll as a reviewed DIRECT account for
the Stars cohort, and at least one more for the external-payment cohort,
with explicit owner authorization to enroll it and observe a real
migration. As of this session, the reviewed-enrollment pipeline itself
already exists and is tested (`db.direct_enrollment.enroll_direct_account()`
/ `process_direct_stars_enrollment()` / `record_external_payment()`, see
above) — what's still missing is the owner-authorized real identity to run
it against, and then a bridge binding + `LEGACY_BRIDGE_ENABLED` migration
exactly as done for account 1. Until that identity is supplied, this agent
will not enroll, create a binding, or attempt a migration for any real
non-internal account.

### Remaining accept-criteria items once real DIRECT/Stars/external-payment cohorts are authorized

- Real PH3-05 device revoke on a real (not synthetic) representative
  client's device.
- Real PH2-05 admin ownership rebind proof, if the owner wants it performed
  live (currently only test-proven, see item 2 above) — same
  invasiveness caveat as for account 1.
- metrics/support runbook (ROADMAP's own accept line mentions this — not
  yet drafted; low effort, can be done alongside docs finalization).

## Known non-blocking backlog

- None discovered this session beyond what's already noted above (the
  PH3-05 REVOKE-without-FREE "stuck at PROVISIONING_PENDING" behavior is
  confirmed correct-by-design, not a defect).

## Explicitly NOT started

PH4-04 (opaque URL rollout), PH4-05 (grace), PH4-06 (production legacy
revoke), mass migration, PH5+.

## Exact next step if resumed

1. Ask the owner (or find, if a reviewed list already exists somewhere
   this agent hasn't checked) for the specific real DIRECT/Stars and
   DIRECT/external-payment account identities to use for cohorts 2 and 3.
2. Once supplied: re-verify each candidate's current production state first
   (status, existing child, payment channel) before creating any binding —
   same discipline as was used for account 1 in this session.
3. Repeat the exact same controlled-canary methodology used for account 1:
   new synthetic HWID on a free slot (never the customer's real live
   device) for the first proof, `LEGACY_BRIDGE_ENABLED` is already `1`
   globally (no further flag change needed), just per-account binding
   creation.
4. After both remaining cohorts are proven: update `ROADMAP.md` PH4-03 to
   `[x]`, `CHANGELOG.md`, write/confirm `docs/PHASE4_INTERNAL_CANARY.md` (or
   similar), commit, push, verify production HEAD parity, final report.
5. If quota runs out before an owner response arrives: this file plus
   `git diff`/`git log` is sufficient for a fresh agent to resume exactly
   from "waiting on real external cohort candidate identities."
