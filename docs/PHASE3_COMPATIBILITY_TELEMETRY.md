# PH3-07 — Privacy-safe HWID/client compatibility telemetry

Date: 2026-08-24

## Scope and non-goals

PH3-07 is strictly observe-only. A valid legacy subscription still follows the
same upstream fetch, permissive legacy HWID check, request history and config
generation path. Telemetry failure is caught before that path continues and
cannot deny, replace or modify a config.

PH3-07 does not create or backfill parent accounts, slots, generations or child
Marzban users. It does not require HWID, rotate credentials, change the existing
71 HWID bindings or switch `/sub/{legacy_token}` to the future resolver.

Browser copy-page requests are excluded. Observations begin only after Marzban
has accepted the bearer and resolved a real legacy username, preventing invalid
token scans from polluting the compatibility sample.

## Classification and dimensions

Each successful non-browser subscription request is assigned exactly one
category:

- `SUPPORTED_HWID_PRESENT`: a supported HWID source produced a bounded,
  well-formed identifier;
- `HWID_MISSING`: no candidate was supplied;
- `HWID_UNSUPPORTED_OR_MALFORMED`: a candidate existed but does not meet the
  future compatibility shape.

The stored dimensions are bounded normalized `client_name`, `client_version`
and `platform`; missing or unsafe/high-cardinality values become `unknown`.
The future fail-closed unsafe share is the latter two categories divided by all
observed requests.

`correlated_subject_count` is not a count of proven physical devices. For an
HWID request it represents the same subscription/HWID observation; without
HWID it can represent only the same subscription/client/version/platform
tuple. `repeat_request_count` therefore measures repeated request subjects,
not cryptographic hardware identity.

## Privacy boundary

Raw subscription tokens, UUIDs, usernames, full HWIDs, User-Agent strings, IPs,
device names and request headers are absent from both telemetry tables.

Correlation uses `HMAC-SHA256` under the dedicated
`COMPAT_TELEMETRY_HMAC_KEY`. The input is scoped by the existing non-replayable
subscription verifier, compatibility category, in-memory candidate and bounded
client dimensions. The resulting `hmac-sha256:<hex>` cannot be joined across
subscriptions without the service key. The key is supplied through the
root-managed production environment, must contain at least 32 bytes and is
never stored in SQLite or emitted by reports/logs.

The daily rollup contains no correlation verifier. Aggregate reports never
select or emit the detailed `client_ref`.

No HTTP/API endpoint exposes this data. Read access is limited to the primary
owner/root and the `mgboost` service identity through the existing protected DB
file; the checked-in report script emits aggregate dimensions and counts only.

## Schema and concurrency

`mgboost_hwid_compat_subjects` stores a daily HMAC-correlated subject with
first/last seen and a monotonic request counter.

`mgboost_hwid_compat_daily` stores identifier-free daily totals, correlated
subject count and repeat count per client/version/platform/category.

Every observation opens a separate SQLite connection with a 50 ms busy timeout
and performs subject lookup, monotonic update/insert, daily rollup and cleanup
inside one `BEGIN IMMEDIATE` transaction. This is the cross-process correctness
boundary; no process-local lock is relied upon. Per-day caps are 10,000 detailed
subjects and 2,000 rollup dimensions. Capacity/lock/schema errors drop only the
observation and never the VPN response.

## Retention and operations

The fixed operational policy follows DL-042:

- HMAC-correlated detail: 30 days;
- identifier-free operational rollups: 60 days;
- regular encrypted DB backups retain their already-approved 90-day policy;
- a credential present in a backup still requires rotation when applicable.

Expiry is enforced opportunistically during writes and independently by the
daily hardened `mgboost-compat-telemetry-cleanup.timer`. The cleanup output is
only deleted row counts and `raw_identifiers_emitted=false`. During a first
startup race or application-only rollback, an absent telemetry schema is a
successful no-op rather than an availability/error condition.

## Tests and staging

Focused tests cover supported, missing, malformed and unknown clients;
repeated observations; concurrent independent SQLite connections; monotonic
rollups; token/HWID/UUID/key absence in DB bytes and logs; 30/60-day cleanup;
DB and logger outage fail-open; exact legacy response bytes; permissive missing
HWID; and zero account/slot/generation/device creation.

Focused result: `42 passed`. Full regression: `487 passed, 3 skipped`.

The exact additive migration was applied twice to a fresh online production DB
copy (`true`, then `false`). It preserved the digest of all 32 pre-existing
non-marker tables, retained 71 legacy device rows and 71 HWID locks, kept
account/slot/generation counts at zero and passed `quick_check=ok` with zero FK
violations. A synthetic observation created exactly one detail and one rollup
row while raw token/HWID/key canaries remained absent from DB bytes.

## Production gate and rollback

Before restart:

1. capture masked 25-user/config and 71-device/HWID state;
2. create and verify an encrypted root-only DB backup;
3. provision a new high-entropy `COMPAT_TELEMETRY_HMAC_KEY` without printing it;
4. install/verify the cleanup service and timer;
5. assert parent/slot/generation tables remain empty.

After restart, repeat the masked state and valid legacy `/sub`, admin/LK,
Stars, Filin, broker, Telegram, SQLite and token-safe log gates. Confirm that
telemetry contains only approved fields and collect an aggregate report over an
explicitly stated observation window.

Rollback disables/removes only the telemetry environment key and returns the
previous application commit/unit state. The two additive tables may remain and
are ignored by the older binary. No UUID, bearer, HWID binding, expiry, tariff
or client configuration changes are required.

## Production evidence

Production completed on 2026-08-24 from implementation commit `be1299d` plus
startup-safe retention fix `d69ee39`. A verified encrypted root-only DB backup
completed before cutover. The dedicated telemetry key is 64 characters in the
protected service environment; only presence/length were inspected, never its
value. The migration marker, `quick_check=ok` and zero FK violations passed.

The first immediate cleanup start raced the `Type=simple` application startup
before its additive migration committed and returned `no such table`. The main
service remained active and user traffic was unaffected. Cleanup was changed to
a tested successful no-op when an older/starting application has not created the
schema; the final oneshot result is success and the daily timer is enabled.

Exact masked pre/post state matched:

- 25 Marzban users and 25/25 fetched configs, zero fetch errors;
- 71 legacy device rows and 71 HWID locks;
- parent accounts, slots and generations: 0/0/0;
- Stars state unchanged at two refunded historical invoices;
- valid legacy `/sub`: HTTP 200 and functional VPN links;
- admin, LK, signed Filin, broker, Telegram proxy, nginx and systemd: healthy;
- application/broker/nginx error count: 0 after the gate;
- telemetry fail-open warnings: 0;
- raw subscription paths and UUID patterns in checked new journal/nginx data: 0.

Deployment-caused changes are all zero: UUID, legacy URL/token, HWID binding,
expiry, tariff, parent account, slot/generation, child user, forced client
reconfiguration and unexpected effective config.

### Initial compatibility sample

The first live window, 2026-08-24 15:59:02–16:07:14 UTC, is deliberately
reported with its small denominator: six `SUPPORTED_HWID_PRESENT` requests
(100%), zero missing and zero malformed. Five are organic and one is a
controlled replay of a different historical real header set. Observed clients
are Happ 2.7.0/Windows and 3.26.3/Android, Incy 2.5.2/iOS, and v2rayTun
2.4.7/iOS plus 5.25.81/Android. This is not a representative request-rate
sample and cannot justify fail-closed.

A separate read-only classification of 204 pre-existing deduplicated
`sub_requests` rows found 115 supported (56.37%) and 89 missing/unsupported
(43.63%); malformed candidates were zero. These rows are historical subjects,
not request rate, and include known gate/tool traffic.

Observed supported families/versions include:

- multiple Happ releases from 1.5.2 through 4.12.0 across Windows, Android and iOS;
- multiple v2rayTun releases from 2.2 through 5.25.81 across iOS, Android and Windows;
- multiple Incy releases from 2.2.3 through 3.5.4 across iOS, Android and Windows;
- one historical `vpn-subscription-client/3.1.6` observation.

Observed missing-HWID families include Streisand 41/42/43/48, HiddifyNext
2.5.7/4.1.1, v2rayN 7.18.0/7.23.4, Throne 1.0.11–1.2.1, Exclave 0.17.21 and
unknown/no-parse rows. Curl, WhatsApp and known PH1/PH3 gate UAs are retained in
the historical inventory but are not treated as proof of an active VPN client.

Conclusion: PH3-07 collection works, but PH3-04 remains blocked until a
representative live window and an explicit compatibility/recovery plan exist
for missing-HWID clients. Runtime remains permissive.
