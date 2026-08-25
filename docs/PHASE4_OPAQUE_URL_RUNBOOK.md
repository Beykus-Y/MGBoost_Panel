# PH4-04 opaque subscription URL runbook

No secrets, raw tokens, UUIDs, HWIDs, or real Telegram IDs/usernames belong
in this file or in any support ticket derived from it -- use the account's
numeric id or `public_id` instead. All read-only queries below use
`sqlite3 'file:.../db.sqlite3?mode=ro' "..."` against
`/opt/MGBoost_Panel/data/db.sqlite3`.

## Issue a new credential (account has none yet)

Preferred: admin route (clearest, strongest auth boundary) --
`POST /admin/accounts/{account_id}/subscription-credential/issue` with
`{"reason": "..."}`, authenticated admin session + CSRF + primary-admin
capability. The response's `raw_token`/`canonical_url` appear **exactly
once** -- copy it immediately; the server cannot show it again.

Alternative: the account's own canonical Telegram owner can send `/newsub`
to the bot in a private chat (only works if their Telegram identity is
already linked as `OWNER` in `mgboost_telegram_identities`), or, for an
account with an active LK management session, `POST
/lk/api/opaque-subscription/issue`.

## Check credential status (never shows the raw token)

```sql
SELECT generation, status, created_at, activated_at, revoked_at, revoke_reason, last_used_at
FROM mgboost_subscription_credentials WHERE account_id = ? ORDER BY generation DESC;
```

`ACTIVE` is the one currently resolvable at `https://sub.beykus.fun/<token>`.
Everything else (`PENDING_DELIVERY`, `REVOKED`) is terminal history.

## A delivery failed or was lost (user says they never got the link)

This is expected to self-heal on the *next* issue/reissue call --
`issue_or_reissue_credential()` (`src/subscription_credential_issuance.py`)
always abandons any stale `PENDING_DELIVERY` row for the account before
preparing a fresh one. Just issue again (admin route, `/newsub`, or LK). Do
not try to manually reconstruct or guess the lost token -- it cannot be
recovered, only replaced.

If a `PENDING_DELIVERY` row is visible in the status query above and you
want to confirm it's the abandoned one before reissuing:

```sql
SELECT id, created_at FROM mgboost_subscription_credentials
WHERE account_id = ? AND status = 'PENDING_DELIVERY';
```

## Rotate/reissue an already-active credential

Same action as "issue a new credential" above -- issuance and rotation are
the same call. The prior `ACTIVE` credential is atomically revoked
(`revoke_reason='ROTATED'`) in the same transaction that activates the new
one; there is never a moment with two `ACTIVE` credentials for one account.
The underlying child/device/UUID are never touched by a credential
rotation -- only the external bearer changes. The old URL starts returning
the same uniform `404` as any unknown token immediately.

## Revoke without reissuing (e.g. suspected compromise)

There is no dedicated route for this yet -- use
`db.subscription_credentials.revoke(credential_id=..., account_id=...,
reason_code='COMPROMISE_SUSPECTED', actor_ref=..., idempotency_key=...)`
directly via a short-lived root-only script. The account is left with no
`ACTIVE` credential until a fresh one is issued.

## Pause new issuance (without touching anything already issued)

There is no separate "pause" flag today -- the accepted way to pause is
operational: stop calling the issue/reissue routes (e.g. take the admin
route out of use, disable the bot's `/newsub` handler registration, or
front the routes with a maintenance response at the nginx layer). Existing
`ACTIVE` credentials keep working; this only stops *new* issuance.

## Disable the external opaque route entirely (rollback)

Set `OPAQUE_SUBSCRIPTION_ENABLED=0` in `/opt/MGBoost_Panel/.env` and restart
`mgboost-panel`. Every request to the opaque route (nginx keeps routing it
to the app) immediately gets the same uniform invalid response regardless
of DB state -- exactly the pre-canary dormant behavior. No credential row
is deleted or modified; re-enabling the flag later resumes exactly where
things left off, with the same `ACTIVE` credentials still valid.

To also stop nginx from reaching the app for this path (deeper rollback),
restore the pre-PH4-04 `sub.beykus.fun` config from
`/root/config-backups/ph4-04/sub.beykus.fun.pre-ph4-04.bak` and reload
nginx (`nginx -t` first). This is a heavier step than the flag alone and
should not be needed for an ordinary pause.

## Verify no log/leak exposure after any operation

```bash
grep -rEo '[A-Za-z0-9_-]{43}' /var/log/nginx/*.log
journalctl -u mgboost-panel --since '<window>' --no-pager | grep -Eo '[A-Za-z0-9_-]{43}'
sqlite3 'file:.../db.sqlite3?mode=ro' "SELECT token_hash, length(token_hash) FROM mgboost_subscription_credentials;"
```

The first two must return nothing. The third must only ever show 64-hex-
character values (SHA-256 verifiers) -- a 43-character value anywhere is a
real incident, not a false positive.

## A user lost their opaque URL and has no admin/LK/bot access configured yet

They cannot self-recover a lost raw token by design (the server never
stores it). Confirm their identity through whatever channel already proves
account ownership for this account (the same bar as any other sensitive
support action -- HWID or possession of the old link is *not* sufficient,
per PH2-05), then issue a fresh credential for them via the admin route and
deliver it through a channel you control.

## Out of scope for this runbook

PH4-05 (grace period), PH4-06 (legacy credential revoke), PH4-07 (full
observability/cleanup tooling), PH5-09 (manual-payment renewal UI), and any
bulk/mass reissue tooling -- none of that exists yet.
