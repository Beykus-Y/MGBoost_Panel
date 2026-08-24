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
