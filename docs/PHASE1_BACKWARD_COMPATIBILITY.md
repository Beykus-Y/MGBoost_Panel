# Phase 1 backward-compatibility / no-user-impact gate

Status: **SAFE TO DEPLOY FOR EXISTING USERS**, subject to the production
preflight/cutover sequence in this document.  PH1-05 is implemented locally
but has **not** been deployed to production.  Its ten typed operations passed
direct-call versus broker comparison against an isolated official Marzban
0.8.4 instance; outage, recovery, restart, Filin HMAC, Stars durability,
legacy subscription and direct-mode rollback contracts passed.  PH1-03/PH1-04
were already applied before this gate was requested; their observed production
state is recorded below.

This document is a deployment gate, not an account migration design.  Phase 1
must not create parent accounts or child users, change plans, rotate user UUIDs,
replace legacy subscription tokens, require a supported HWID, or reinterpret
existing expiry/traffic/device records.

## Evidence and production baseline

Read-only review date: 2026-08-23.  Production repository HEAD was
`ccc1b4dafd02b2ec01a3cfecf1530e2244fe99bc` (the local tree also contains
uncommitted Phase 1 work).  No raw token, UUID, username or credential is
recorded in this report.

| Invariant | Read-only production evidence |
| --- | --- |
| Marzban users | 25: 20 active, 5 expired |
| Legacy subscription identities | all 25 have a subscription URL and a VLESS UUID |
| Expiry | 17 positive expiry values, 8 unlimited; aggregate identity/expiry digest `c7459e3b7d010eeb5848f8dbf7c9cd8db897d11d577c954ba43c8664f749fce4` |
| Config generation | all 25 subscriptions fetched successfully; token-to-username mismatch 0; all were base64 profiles |
| UUID preservation by current generator | 0 UUID values changed, 0 removed by current filters, 0 non-information UUIDs added |
| Current generated profile snapshot | 734 upstream links became 884 links after the existing filters/extras/information-node logic; aggregate processed digest `768155a41bc7c695f9b0f8f23ffb21ebbda649bda4ee47b90e012e55713c9e1b` |
| Device state | 71 active rows and 71 matching HWID locks across 24 stored usernames; no active row has a missing/wrong lock |
| Telegram links | 5 linked records; all referenced Marzban users exist |
| Stars history | 2 invoices, both refunded; their Marzban users exist |
| Runtime after PH1-04 | service enabled/active as `mgboost:mgboost`, bound to `127.0.0.1:8001`, no permission/SQLite/readonly errors observed since restart |
| PH1-05 username compatibility recheck | 25 users; 0 fail the typed broker username contract; 0 missing VLESS UUID; 0 missing subscription URL (read-only, 2026-08-24) |

The processed-profile digest is evidence for this point in time, not a stable
business identifier: descriptions can contain time-dependent text and operator
filters/extras may legitimately change later.

### Legacy alias/device drift that must be preserved

The local device table is not a canonical copy of Marzban's current
`subscription_url` field.  Of 71 device rows, 68 rows across 23 usernames hold
a token different from the single URL currently returned by the Marzban user
API; 3 rows reference usernames no longer present in Marzban.  More
importantly, 42 of 45 distinct stored username/token pairs still resolve in
Marzban to the stored username.  Therefore the current Marzban URL field is not
a complete inventory of URLs that existing clients may still use.

Phase 1 must not clean these rows, rotate those tokens, release their locks, or
replace their UUIDs.  The later legacy migration must inventory aliases
explicitly.  This finding also means that PH1-06 may stop new token leakage in
Phase 1, but user-token rotation/reissue cannot happen until the staged Phase 4
migration unless the owner explicitly changes the no-user-impact requirement.

## User-facing flow review

| Flow | Current execution path | Phase 1 compatibility requirement | Evidence/status |
| --- | --- | --- | --- |
| Existing VPN connection | cached VLESS URI connects to Xray with the existing Marzban UUID | no UUID/proxy/inbound/status mutation | current production identity inventory captured; PH1-05 must never send such fields for reads/expiry |
| Old subscription URL | nginx `/sub/` -> MGBoost `handle_sub` -> Marzban `/sub/{same token}` | path and raw token forwarded unchanged | all 25 current URLs fetched; regression test asserts exact token forwarding |
| Username selection | Marzban `/sub/{token}/info` | same token resolves the same username | 25/25 current URLs matched; old device aliases documented above |
| VLESS/config output | `process_subscription`: exact node filters, inbound extras, global/per-user extras, information node and selected headers | identical code and DB state must produce identical bytes/headers | production aggregate captured; unit contract preserves UUID, order, expiry header and config lines |
| Existing supported HWID | `extract_device_metadata` -> `check_device_access` only for `hwid:` | same slot/lock, no rebind, same token accepted | 71 rows/locks consistent; route regression checks the same HWID path |
| Client without supported HWID | fingerprint/request log only; no device-limit enforcement | remain permissive in Phase 1; fail-closed is Phase 3 | regression test proves `fp:` does not claim/check a slot |
| Already-bound devices/LK | SQLite `user_devices`, `hwid_lock`, management session for mutations | no device table migration, rebind or forced re-add | existing LK authorization tests plus compatibility inventory |
| Expiry | authoritative `user.expire` in Marzban | reads unchanged; renew changes only `expire` | production digest and explicit renew/Stars field-preservation tests |
| Manual admin create/edit/delete/reset | browser Marzban JWT currently calls Marzban; PH1-01 stores that user-entered JWT server-side and forwards allowlisted paths | request JSON/method/username semantics unchanged; admin re-login is allowed | admin proxy regression covers exact create/modify/delete payloads; existing proxy tests cover reads/auth |
| Manual external-payment renewal (Filin/internal) | HMAC API -> typed broker -> `get_user` + `modify_user` | exact add-days/base-time/data-limit/status semantics retained | unit contract plus real HMAC create/renew/delete through MGBoost on Marzban 0.8.4 staging passed |
| Telegram Stars | eligibility `get_user`; durable worker writes exactly `{"expire": target}` | same invoice/eligibility/recovery/expiry-only semantics | existing Stars suite plus identity field-preservation test |
| LK info/usage | public token lookup plus privileged broker user/usage reads | same response schema; device mutations remain local | route and broker response/outage tests passed |
| Support bot | TG mapping -> privileged broker `get_user`; device link uses local one-time management code | same messages/linking/renew flow | service facade and existing support regression passed; public token linking no longer obtains an unused SUDO token |
| Bot monitor | privileged broker node list | same node state data and alert behavior | exact node-list direct-vs-broker staging comparison and bot regression passed |
| Filters/config generation | local SQLite settings only after raw subscription fetch | no broker or account-model dependency | production processing probe and subscription tests |
| Traffic/statistics | Marzban user/node usage reads plus local Hysteria counters | same queries, intervals and response shapes | user/node usage direct-vs-broker staging comparison and route regression passed |
| Filin HMAC | timestamp + nonce + body hash + HMAC, then `/internal/v1/*` | signature contract and HTTP payloads unchanged | source/tests reviewed; multi-process replay hardening remains Phase 2 |
| Restart/reboot | systemd MGBoost enabled; Docker Marzban restart policy `always` | public subscriptions recover without broker dependency | non-root staging process passed MGBoost cold start/restart with broker down; broker and Marzban independent restart/recovery passed; both unit files pass `systemd-analyze verify` |

For an existing VPN user, PH1-01/03/04 and the proposed PH1-05 boundary require
**no intended behavior change**.  PH1-01 intentionally changes one operator
flow: existing browser admin JWT state is removed and the administrator must
log in again.  Server-side admin sessions also end on an MGBoost restart in the
current single-process model.  Neither change affects a VPN client.

## PH1-05 operation compatibility matrix

The selected topology is a separate localhost broker service.  The main
MGBoost process must not contain `MARZBAN_ADMIN_USER` or
`MARZBAN_ADMIN_PASS` (or an equivalent SUDO credential) in its environment.
The browser admin's user-entered Marzban credential is a separate server-side
session concern from the service credential catalog below.

| Current operation | Old direct Marzban call | Required broker operation | Compatibility contract |
| --- | --- | --- | --- |
| bot/LK/Stars user lookup | `GET /api/user/{username}` | `legacy.user.get` | return the same Marzban JSON/status; exact username, no inferred account |
| LK/Filin user traffic | `GET /api/user/{username}/usage?start&end` | `legacy.user.usage` | preserve query omission/encoding and response body |
| Filin user list | `GET /api/users?limit&offset` | `legacy.users.list` | preserve pagination and full response shape |
| bot monitor/Filin status | `GET /api/nodes` | `legacy.nodes.list` | preserve node objects/order/status semantics |
| Filin traffic | `GET /api/nodes/usage?start&end` | `legacy.nodes.usage` | preserve optional UTC query values and response shape |
| Filin inbound inventory | `GET /api/inbounds` | `legacy.inbounds.list` | read-only, exact response shape |
| current Filin create | `POST /api/user` | `legacy.user.create` | preserve current validated `username/proxies/inbounds/expire/data_limit/reset/note/status/...` payload; no automatic child/account conversion |
| current Filin add-days renewal | `GET user` then `PUT /api/user/{username}` | `legacy.user.renew` | preserve `max(current_expire, now) + days*86400`, explicit expire, `data_limit=0 -> null` wire payload, status and per-username serialization; Marzban 0.8.4 treats null partial data-limit as unchanged, and the broker deliberately preserves that observed legacy effect |
| Stars renewal | `GET user`, then `PUT` with only `expire` | `legacy.user.get` + `legacy.user.set_expire` | PUT body must contain only `expire`; UUID/proxies/inbounds/data_limit/status remain unchanged |
| current Filin delete | `DELETE /api/user/{username}` | `legacy.user.delete` | preserve exact username and HTTP result; never reinterpret as child/device revoke |

Public `GET /sub/{token}` and `GET /sub/{token}/info` remain direct Marzban
calls from MGBoost and are deliberately **not** broker operations.  This keeps
subscription availability independent of the SUDO boundary.  The previous bot
linking handler obtained an admin token before a public token-info lookup even
though it never used that token; PH1-05 removed this unnecessary dependency
without changing token/link semantics.

The PH1-05 broker now covers exactly all ten rows in the table.  It exposes no
generic path/URL/JWT proxy: each operation has an exact top-level field schema,
legacy create/renew validation, an HMAC-authenticated caller identity,
timestamp, nonce, body hash, constant-time signature check and replay guard.
Audit messages contain operation, caller, outcome, duration and a keyed
pseudonymous target reference rather than username/token/UUID.  The retained
Filin capabilities remain transitional because nginx publishes `/internal/`
to the allowlisted source IP `155.212.142.20`, with Filin HMAC still required
by MGBoost.

Durable Stars renewal is idempotent across unknown-response recovery because
it persists an absolute target expiry and rereads the remote state before any
retry.  Generic Filin `add_days` has no durable operation ID in the established
external contract; a blind retry after an unknown response was already
ambiguous before PH1-05 and remains so.  The broker does not silently change
that API.  Durable/shared mutation idempotency is tracked by PH2-03.

## Production topology changes required for the broker cutover

1. Install a dedicated broker unit and identity.  Bind only to localhost (or a
   permissioned local socket); do not add an nginx route or public listener.
2. Deliver the Marzban service credential only to the broker, preferably via a
   root-owned systemd credential file rather than a broad environment file.
3. Remove `MARZBAN_ADMIN_USER` and `MARZBAN_ADMIN_PASS` from the main MGBoost
   environment and verify `/proc/<mgboost-pid>/environ` by key name after
   restart.  A broker authentication credential is not a Marzban credential
   and must itself be scoped/rotatable/not logged.
4. Configure MGBoost with only the broker endpoint/auth material.  Keep direct
   `MARZBAN_URL` access for the two public subscription endpoints.
5. Do not make MGBoost's public HTTP process lifecycle depend on broker
   liveness.  Order startup after the broker where useful, but a broker crash
   must not stop `/sub/{token}`.  Both services need bounded restart/backoff and
   health checks.
6. Nginx routing for `/sub/`, `/lk/`, `/sub-admin/` and the current allowlisted
   `/internal/` contract need not change for credential separation.  The
   broker must never appear under `/sub-admin-api/`, `/internal/`, or `/`.
7. No SQLite or Marzban user migration is required.  Capture pre/post aggregate
   identity, expiry, inbound and processed-config digests and compare them
   before enabling mutations.

PH1-04's current production unit is enabled, uses `Restart=on-failure`, writes
only `/opt/MGBoost_Panel/data`, and runs without capabilities.  The new broker
unit is configured to run without root/capabilities and with a read-only filesystem.
Staging exercised the equivalent dependency orders independently: MGBoost
started/restarted while broker was down and served byte-identical `/sub`;
broker remained healthy while Marzban was down, returned 503 for privileged
operations, and recovered after Marzban restart; broker process restart on the
same address recovered without client/credential changes.  A physical host
reboot is not required to change user state and remains a deployment smoke,
not an untested application contract.

## Broker outage behavior required

- Existing active VPN tunnels continue because Xray/Marzban credentials are
  unchanged.
- `/sub/{legacy_token}` continues to fetch and generate the same config because
  it does not use the broker.
- LK static/device-list data remains available; privileged status/usage should
  return an explicit temporary-unavailable state rather than fabricated data.
- New Stars checkout must fail before charging if eligibility cannot be read.
  A paid durable invoice must remain queued/retriable; it must not be marked
  applied or issue a second charge.
- Support subscription-info and node-monitor reads degrade with a clear error;
  Telegram linking by a legacy URL must still use the public token-info path.
- Filin privileged reads/mutations return retryable 503/502 and must never
  silently acknowledge a mutation.
- Browser admin can remain independent when an operator supplies a valid
  Marzban credential; its server-side session does not justify exposing the
  broker credential.

These outcomes are covered by unit/contract tests and isolated Marzban 0.8.4
staging.  A deployment operator must still execute the production preflight
checks below before enabling mutations.

## Implementation and staging evidence (2026-08-24)

- `src/broker_protocol.py`, `src/broker_operations.py` and
  `src/broker_server.py` implement an exact ten-operation allowlist over a
  literal-loopback authenticated HTTP listener.  Local socket access without
  the shared HMAC key returns 401/403; unknown operations and generic paths are
  rejected.
- `src/service_marzban.py` is the only privileged service facade used by
  bot/LK/Stars/Filin.  In broker mode its compatibility token is a non-secret
  sentinel, and main startup rejects `MARZBAN_ADMIN_USER/PASS` in its process
  environment.  `src/routes/sub.py` and public token-info remain direct
  non-SUDO Marzban calls.
- `scripts/verify_broker_against_staging.py` verified all ten operations against
  official Marzban 0.8.4, including exact read responses, equivalent mutation
  effects, authoritative reread, broker restart, unchanged proxies/inbounds/
  subscription identity, and cleanup restricted to synthetic prefixed users.
- `scripts/verify_legacy_sub_restart_staging.py` exercised actual MGBoost and
  HMAC Filin HTTP routes.  The same synthetic legacy URL returned a byte-identical
  config with broker up, broker down, and after MGBoost restart while broker
  remained down; the same VLESS UUID remained present.  Filin create/renew/delete
  passed through MGBoost -> broker -> Marzban.
- Real staging outage: broker returned 503 (no false success) while Marzban was
  stopped, its health endpoint stayed 200, and typed reads recovered after
  Marzban restart.  Broker process restart also recovered on the same address.
- Automated result: `375 passed, 1 skipped`.  The skipped Playwright browser
  test belongs to PH1-01 and was previously passed in the dedicated browser
  environment; PH1-05 has no frontend change.  `systemd-analyze verify`, Python
  compile and `git diff --check` pass.

## Rollback procedure

1. Before cutover, store the old code/unit/env templates and masked aggregate
   contract digests; take and verify a recoverable DB backup.  Do not copy raw
   tokens into the rollback report.
2. If shadow comparisons or smoke tests differ, stop the new MGBoost process,
   restore the previous code/unit, restore access to the **current rotated**
   Marzban service credential for the old code, and restart MGBoost.  Do not
   revert to an invalidated weak password.
3. Leave Marzban users, UUIDs, subscription URLs, expiry, inbounds, device rows
   and filters untouched.  Stop/disable the broker only after old MGBoost has
   regained its required privileged reads/writes.
4. Verify a canary legacy URL/config/UUID/expiry and Stars/manual renewal
   recovery, then compare aggregate digests/counts.  Admins may need to log in
   again; VPN users must not re-add anything.

MGBoost can be rolled back without changing any user credential.  The only
credential-specific rollback input is the service-side Marzban credential
after PH1-02 rotation; it is not a user UUID or subscription token.

## Production preflight/cutover gates

- Create the dedicated `mgboost-broker` system identity and install
  `/etc/mgboost/marzban-broker.env` as `0640 root:mgboost-broker`; generate an
  independent >=32-byte CSPRNG broker HMAC key and supply the same value to
  main without copying the Marzban SUDO password.
- Start broker on `127.0.0.1:8002`, verify HMAC typed smoke and confirm `ss`
  shows no non-loopback listener.  Do not add or alter any nginx route for it;
  verify public nginx locations do not proxy port 8002.
- Remove both Marzban SUDO keys from main `.env`, restart MGBoost, and verify
  `/proc/<main-pid>/environ` contains neither key by name.  Confirm broker is
  the only process with the service credential.
- Before enabling mutations, compare masked pre/post counts/digests for all 25
  users, UUIDs, subscription URLs, expiry, inbounds and current generated
  configs; verify the 71 existing device/lock rows are unchanged.
- Smoke one current legacy URL with broker up/down, one eligible pre-checkout,
  one durable Stars retry canary and an explicitly approved Filin canary.  Do
  not create parent/child users or rotate any user credential.
- Keep the prior code/unit and current service credential available for the
  documented direct-mode rollback.  PH1-02 rotates that service credential in
  a separately coordinated step; it does not rotate user credentials.

With these operational gates followed, the verdict is:

**SAFE TO DEPLOY FOR EXISTING USERS**

The deployment itself causes: UUID changes 0; subscription URL/token changes
0; forced client reconfiguration 0; HWID migration 0; tariff migration 0;
expiry changes caused specifically by deployment 0.
