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

## Production evidence

The compatibility layer was deployed on 2026-08-24 from commit `500375e` with
`INTERNAL_API_REQUIRE_V2_MUTATIONS=0`. Before restart, the regular encrypted
backup completed successfully and an online DB copy passed restore,
`quick_check`, additive-table and independent-connection CAS tests. The full
suite passed locally (`415 passed, 2 skipped`; `417 passed` with browser
dependencies). Production's intentionally minimal runtime has no pytest
package, so no package was installed during the gate.

After the additive active-DB migration, a real signed v1 status request
succeeded. MGBoost was restarted, the exact same signed nonce returned the
expected 409, and a fresh nonce succeeded. SQLite contained the expected
SHA-256 nonce reference and not the raw nonce. A first probe immediately after
systemd reported active exposed an existing startup-readiness race (the port
was not listening yet); the gate was repeated with an explicit readiness loop
and passed. This did not involve a user mutation.

Broker, Marzban, admin/LK, durable Stars tables, Telegram through the configured
proxy and support runtime passed. OpenRouter retained its pre-existing HTTP 403
baseline. The masked pre/post snapshot matched exactly for 25 users/configs,
zero config fetch errors, 71 device rows and 71 HWID locks. UUID, legacy
subscription URL/token, HWID, expiry, tariff, forced client reconfiguration and
unexpected effective config changes were all zero. Logs contained no raw
subscription path or stable replay/database error. No rollback was required.

## Rollback

The schema is additive and old code ignores both tables. An emergency rollback
can restore the previous commit and restart MGBoost without changing any user
credential or DB row. That rollback temporarily loses cross-process/restart
replay protection, so external mutation access should be paused until the
durable build is restored. Never delete pending rows or replay a pending
operation without read/reconciliation evidence.
