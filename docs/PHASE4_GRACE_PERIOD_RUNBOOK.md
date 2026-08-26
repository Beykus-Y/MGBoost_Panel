# PH4-05 legacy grace-period runbook

No secrets, raw tokens, UUIDs, HWIDs, or real Telegram IDs/usernames belong
in this file or in any support ticket derived from it -- use the account's
numeric id or `public_id` instead. All read-only queries below use
`sqlite3 'file:.../db.sqlite3?mode=ro' "..."` against
`/opt/MGBoost_Panel/data/db.sqlite3`.

This document is reversible-side documentation only. It does not authorize
starting any account's real grace clock -- that is a separate, explicit
owner decision per cohort, made with the dry-run report below in hand.

## What PH4-05 is, and is not

PH4-05 is the fixed 14-day clock (OPD-09/DL-023) that starts a countdown
after which PH4-06 (not built yet) is *allowed* to actually revoke the
shared legacy URL/UUID for an account. PH4-05 itself never revokes, denies,
or degrades anything -- `mgboost_legacy_grace_periods`/`_events` are pure
bookkeeping, and no route currently reads them at all.

## Status: is an account's grace period running?

```sql
SELECT account_id, cohort_ref, started_at, original_end_at, current_end_at, revision
FROM mgboost_legacy_grace_periods WHERE account_id = ?;
```

No row = never started. `current_end_at == original_end_at` = never
extended. Compute "active" yourself: `now < current_end_at` (strict --
`now == current_end_at` already means expired, this project's exact
boundary rule, see `tests/test_legacy_grace.py`).

Full audit trail (immutable, append-only):

```sql
SELECT event_type, from_end_at, to_end_at, actor_ref, reason, evidence_ref, created_at
FROM mgboost_legacy_grace_events WHERE account_id = ? ORDER BY id ASC;
```

## Dry-run eligibility report (read-only, no mutation)

```
python -m scripts.ph4_05_grace_eligibility_report --db <COPY-of-db.sqlite3> --format table
```

Always run this against a **downloaded copy**, never the live production
file directly (matches this project's own PH4-03/04 canary discipline).
One row per account with a legacy alias: migration state, active vs
migrated device counts, last legacy/opaque activity, a compatibility note,
concrete blockers, and a `START_GRACE`/`HOLD` recommendation. `START_GRACE`
only ever means "no blocker was found" -- it is not itself authorization to
start; the owner still decides per cohort.

## Starting a grace period for a real cohort (requires owner authorization)

This is the one action in this document that is genuinely consequential --
see "What becomes hard to walk back" below before running it for any real
account. `db.legacy_grace.start()` requires the same sealed
`PrimaryAdminAuthority` capability every other PH3-06/PH4-01..04
consequential action already requires; there is no route wired to it yet
(deliberately -- starting for a real account today is a short-lived
root-only script, exactly like every PH4-03/04 real canary this project has
run).

```python
db.legacy_grace.start(
    account_id=<id>, cohort_ref="PH4-05-COHORT-<label>",
    capability=capability, reason="<owner decision reference>",
    idempotency_key="<unique, >=16 chars>", now=<utc epoch seconds>,
)
```

- One account can only ever start once (`GraceAlreadyStarted` on a second,
  differently-keyed attempt) -- there is no "restart the clock" operation.
- `original_end_at`/`current_end_at` are set to exactly `started_at +
  1209600` (14 days, DL-023's fixed value, enforced by a schema `CHECK`, not
  just application code).

## Extending (explicit, audited, monotonic-forward only)

```python
db.legacy_grace.extend(
    account_id=<id>, expected_revision=<current revision>,
    new_end_at=<new end, strictly greater than current_end_at>,
    capability=capability, reason="<why>", evidence_ref="<ticket/decision ref>",
    now=<utc epoch seconds>,
)
```

There is no code path and no DB trigger that allows `current_end_at` to
move backward or stay the same -- both are rejected
(`GraceTransitionError`). Every extension writes an immutable `EXTENDED`
event with `from_end_at`/`to_end_at`/`reason`/`evidence_ref`; there is no
silent extension in this system.

## Support: a user is inside their grace window and asks for help

1. Pull the status query above -- `day_of_14`/`seconds_remaining` (see
   `src/legacy_grace.py::day_index`/`seconds_remaining`) give an exact,
   honest "day N of 14" figure, never a guess.
2. Check `last_legacy_activity`/`last_opaque_activity`
   (`src/legacy_grace_observability.py::account_grace_snapshot`) --
   distinguishes a user who has already switched (opaque activity after
   `started_at`) from one who is still only using the legacy URL.
3. A device that appears in `inactive_since_grace_start=True` has not been
   seen on either channel since the clock started -- likely a spare/rarely
   used client, not necessarily an at-risk migration. Do not treat this as
   an error state by itself.
4. If the user has a legitimate reason they cannot migrate before their
   deadline (unsupported client, unresolved provisioning error visible in
   `resolver_errors_72h`/`reconciliation_failures_72h`), use the explicit
   `extend()` path above with a real reason and evidence reference. Never
   edit `mgboost_legacy_grace_periods` directly with raw SQL outside this
   API -- the identity/monotonic triggers exist specifically so that a raw
   `UPDATE` cannot silently shorten or reset a clock, but going around the
   store also skips the audit event.

## Exceptions (accounts that should stay HOLD indefinitely)

An account with a legitimate reason to never enter the grace cohort (e.g.
unresolved compatibility, an open support case) simply never gets a
`start()` call -- there is no "excluded" flag to set, and none is needed.
Record the reason in the existing ticket/decision-log process, same as
every other PH4-03/04 exception this project has documented (e.g. the
TELEGRAM_STARS `N/A` cohort).

## Metrics visibility during a live 14-day window

All of these are already assembled per-account by
`account_grace_snapshot()` and are exactly what the dry-run report and any
future admin surface should read from:

- grace day / seconds remaining (`grace.day_of_14`/`grace.seconds_remaining`)
- migration state counts (`migration_state`, from PH4-02's own bindings)
- `last_legacy_activity` / `last_opaque_activity`
- `legacy_requests_24h`/`_72h`, `opaque_requests_24h`/`_72h`
  (new PH4-05 `mgboost_legacy_grace_activity_daily` counters, account_id +
  channel + day only -- never a raw token/HWID/UUID/URL)
- `active_devices` vs `migrated_devices`
- unsupported client/version: existing PH3-07 aggregate rollup
  (`mgboost_hwid_compat_daily`, `HWID_UNSUPPORTED_OR_MALFORMED`) -- global,
  not joinable per-account (pseudonymous by design), used as an ops signal
- resolver/broker/Marzban errors, reconciliation failures, revoke/rebind
  events: existing PH4-02 `mgboost_migration_binding_events`
  (`resolver_errors_72h`, `reconciliation_failures_72h`,
  `revoke_rebind_events_72h`)
- inactive clients: `inactive_since_grace_start`

## What becomes hard to walk back once a real cohort actually starts

- The account's own `started_at`/`original_end_at` are immutable forever
  (schema trigger). `current_end_at` can only move forward. There is no
  "pause" or "reset" operation anywhere in this code -- by design, matching
  DL-023's Rollback clause ("extension только explicit/audited").
- The moment a real communication (Telegram/LK, drafted but NOT sent by
  this session, see `docs/PHASE4_GRACE_PERIOD_COMMS_DRAFT.md`) goes out to
  a real user, that is a customer-facing commitment independent of the DB
  row -- reversing the message's promise costs trust even though the row
  itself is still technically extendable.
- PH4-06 (the actual revoke, a separate future phase, not built) is what a
  grace period's expiry is *for*. Starting PH4-05 for a real account is the
  direct predecessor to that eventually-irreversible action, even though
  this phase itself performs no revoke.

## PH4-05 accept criteria mapped to what exists now

- Per-account/cohort start/end: `mgboost_legacy_grace_periods`
  (`started_at`/`original_end_at`/`current_end_at`/`cohort_ref`). Done.
- Communications: drafted, not sent -- `docs/PHASE4_GRACE_PERIOD_COMMS_DRAFT.md`.
- Support and metrics: this runbook + `account_grace_snapshot()`. Done.
- Exact UTC boundary at 14 days: `tests/test_legacy_grace.py` (`<`, `==`,
  `>` all covered) + schema `CHECK(original_end_at = started_at + 1209600)`.
- Inactive clients: `inactive_since_grace_start` +
  `tests/test_legacy_grace_observability.py`.
- Rollback (extension only explicit/audited; revoked UUID never reopens):
  `extend()`'s CAS + monotonic-forward DB trigger + immutable event log;
  "revoked UUID never reopens" is PH4-06's own terminal guarantee (already
  proven at the migration-lifecycle layer by `LEGACY_REVOKED`'s existing
  terminal DB trigger in PH4-02) and is not re-implemented here.

Not done by this document/session, deliberately: no real account has a
grace row; no communication was sent; PH4-06 itself.
