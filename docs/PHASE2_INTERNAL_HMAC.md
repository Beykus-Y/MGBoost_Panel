# PH2-03 durable Internal/Filin HMAC replay and idempotency

Date: 2026-08-24. This rollout changes only the authentication replay store
and introduces an opt-in v2 mutation contract. It does not change current
Marzban operation payloads, user identity, UUID, subscription URL/token, HWID,
expiry, tariff or generated VPN config.

## Current and target contracts

Legacy v1 remains byte-for-byte compatible:

```text
METHOD\nRAW_PATH_WITH_QUERY\nTIMESTAMP\nNONCE\nSHA256(BODY)
```

It keeps HMAC-SHA256, the existing shared key, timestamp window, body binding
and constant-time comparison. The difference is that a verified nonce is
atomically consumed in SQLite, so another process or a restarted process sees
the replay. Only `SHA256(nonce)` and a request digest are stored.

V2 adds a stable operation key for mutations:

```text
v2\nMETHOD\nRAW_PATH_WITH_QUERY\nTIMESTAMP\nNONCE\nSHA256(IDEMPOTENCY_KEY)\nSHA256(BODY)
```

The caller creates one high-entropy idempotency key per logical mutation and
reuses it for every retry, while each HTTP attempt gets a fresh timestamp and
nonce. MGBoost atomically creates a `pending` row before execution and marks it
`completed` before acknowledging the response. Completed duplicates, key/body
conflicts and concurrent pending attempts return `409` without re-execution.
Only key/request/response hashes and status are retained; user payloads,
credentials, UUIDs and response bodies are not stored.

Pending records intentionally do not expire automatically. A crash can happen
after Marzban applied an effect but before MGBoost wrote the acknowledgement;
automatically retrying after a TTL could double-renew or recreate state. Such a
row is an explicit reconciliation case. Completed records default to seven-day
retention through `INTERNAL_API_IDEMPOTENCY_TTL_SECONDS`.

## Compatibility and rollout state

`INTERNAL_API_REQUIRE_V2_MUTATIONS=0` is the compatibility default. V1 reads
and mutations continue to work with their existing payload/status semantics;
all receive durable nonce replay protection. A v2-capable caller can opt in
immediately. Enforcement must remain off until the external Filin mutation
caller uses stable operation IDs for create, renew, delete and every other
mutation. After end-to-end crash/retry reconciliation tests, setting the flag
to `1` rejects v1 mutations with `428`; signed v1 reads remain allowed.

Therefore the durable replay portion can be deployed without user impact, but
PH2-03 remains partial until external caller adoption and enforcement. A new
nonce is not a logical operation ID and must never be used to deduplicate
`add_days` renewals.

## Tests and staging gate

- same nonce through two independent SQLite connections: one success, one 409;
- replay after a new Database instance/process: 409;
- invalid signature does not consume storage;
- expired nonce pruning, bounded capacity and store-outage 503;
- v1 short/legacy nonce and signature compatibility;
- v2 completed retry, conflicting reuse and concurrent/pending crash retry;
- acknowledgement-store failure returns 503 and leaves pending;
- DB contains only fixed-length hashes, never raw nonce/key/body/response;
- full regression including all ten broker operations, Stars, Filin legacy
  create/renew/delete, admin/LK and legacy subscription/config contracts.

## Production gate

1. Capture the established masked user/config/device state and encrypted,
   restore-verified SQLite backup.
2. Start the candidate against a restored copy; assert additive tables/indexes,
   `PRAGMA quick_check=ok`, v1 authenticated status and replay-after-restart.
3. Deploy with `INTERNAL_API_REQUIRE_V2_MUTATIONS=0`; restart only MGBoost.
4. Verify authenticated v1 Filin status plus safe same-nonce 409 without making
   a user mutation. Confirm broker/Marzban/LK/admin/Stars/bot health.
5. Compare masked UUID/config/expiry/device state and scan new logs for secrets
   and stable errors.

No live create/renew/delete canary is required for the compatibility-only
store cutover because their HTTP and Marzban payload paths are unchanged; the
existing real-Marzban all-ten-operation staging contract remains authoritative.

## Rollback

The schema is additive and old code ignores both tables. An emergency rollback
can restore the previous commit and restart MGBoost without changing any user
credential or DB row. That rollback temporarily loses cross-process/restart
replay protection, so external mutation access should be paused until the
durable build is restored. Never delete pending rows or replay a pending
operation without read/reconciliation evidence.
