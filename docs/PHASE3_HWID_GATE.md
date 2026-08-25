# PH3-04 — HWID fail-closed compatibility gate

Date: 2026-08-25. Status: dormant, `PH3_04_ENFORCEMENT_MODE=OFF` everywhere.
No legacy route imports `src/hwid_gate.py` or `src/compat_registry.py`. This
document is the versioned compatibility list and the design/evidence record.

## Contract

- No supported HWID -> config is not issued.
- Unknown-but-well-formed HWID + free slot -> atomically assign the next
  slot/generation (reuses `src/device_slots.py` `DeviceSlotStore.claim`
  exactly as PH3-02 built it; PH3-04 adds no new provisioning path).
- All slots full -> deterministic refusal. Nothing is evicted, no old device
  is chosen for removal, no entitlement is silently raised, no child is
  created.
- Known HWID -> always resolves to the same account-owned slot and the same
  active generation. Repeats are idempotent.
- HWID is a practical device identifier only. It is never authentication, is
  never accepted as proof of Telegram ownership, never authorizes ownership
  recovery or parent-account transfer, and never allows cross-account slot
  substitution -- a HWID already active under a different account is a
  deterministic deny (`DECISION_DENY_CROSS_ACCOUNT_HWID`), not a takeover.

## Accelerated compatibility strategy (DL-047)

The owner is on a deadline and does not want a multi-day statistical
observation window. This does **not** mean unknown clients become trusted.
Instead: only `(client, version, platform)` tuples with positive,
non-fabricated evidence that the real client sends a well-formed HWID become
`SUPPORTED`. Every other tuple -- including a different version of an
otherwise-supported client -- is `UNKNOWN`, and `UNKNOWN` is treated
identically to "not compatible" by the gate. No substring/fuzzy matching
(`"Happ" in user_agent`) is ever used as identity or compatibility proof.

## Registry mechanics

`src/compat_registry.py` is a git-tracked, human-reviewable Python module
(not a database table). Each entry is `(client, version, platform) ->
classification`, normalized through the exact same bounded/lowercased
dimension function PH3-07 telemetry already uses
(`compat_telemetry._dimension`), so a registry lookup and a telemetry
observation always agree on the same identity space. At import time the
module validates: no duplicate keys, every entry already stored in its
normalized form, classification restricted to `SUPPORTED` /
`UNSUPPORTED_MISSING_HWID` / `UNSUPPORTED_MALFORMED_HWID`, and every entry
carries a non-empty `evidence_type` (`ORGANIC_LIVE` / `CONTROLLED` /
`HISTORICAL`) plus a `caveat`. `classify()` is an exact dict lookup; a miss
is `UNKNOWN` and is never treated as supported.

## Compatibility matrix (2026-08-25 production PH3-07 snapshot)

Source: `scripts/report_ph3_07_compatibility.py` against the live
`mgboost_hwid_compat_daily` rollup, window covering all telemetry since PH3-07
activation on 2026-08-24 (113 total requests). Rows for
`mgboost-owner-verification`, `mgboost-ph3-postcanary` and `python-urllib`
are excluded from this matrix -- they are this project's own controlled/gate
traffic from earlier PH3-03 sessions, not real VPN clients, and PH3-07's own
docs already establish that known gate/tool traffic must not be read as
compatibility evidence.

| Client | Version | Platform | Classification | Evidence | Date | Caveat |
|---|---|---|---|---|---|---|
| happ | 3.26.3 | android | `SUPPORTED` | organic live | 2026-08-25 | 20 requests / 5 subjects |
| v2raytun | 2.4.7 | ios | `SUPPORTED` | organic live | 2026-08-25 | 19 requests / 8 subjects |
| incy | 2.5.2 | ios | `SUPPORTED` | organic live | 2026-08-25 | 17 requests / 7 subjects; includes the approved PH3-03 dormant canary device |
| incy | 3.5.4 | android | `SUPPORTED` | organic live | 2026-08-25 | 12 requests / 4 subjects |
| happ | 2.7.0 | windows | `SUPPORTED` | organic live | 2026-08-25 | 11 requests / 3 subjects |
| v2raytun | 5.25.81 | android | `SUPPORTED` | organic live | 2026-08-25 | 9 requests / 6 subjects |
| v2raytun | 2.4.4 | ios | `SUPPORTED` | organic live | 2026-08-25 | 5 requests / 2 subjects |
| incy | 3.3.0 | android | `SUPPORTED` | organic live | 2026-08-25 | 3 requests / 1 subject -- low sample |
| happ | 3.24.1 | android | `SUPPORTED` | organic live | 2026-08-25 | 2 requests / 1 subject -- low sample |
| v2raytun | 2.4.6 | ios | `SUPPORTED` | organic live | 2026-08-25 | 1 request / 1 subject -- single observation only |
| streisand | 48 | darwin | `UNSUPPORTED_MISSING_HWID` | organic live | 2026-08-25 | sends no HWID candidate |
| streisand | 41 | darwin | `UNSUPPORTED_MISSING_HWID` | organic live | 2026-08-25 | sends no HWID candidate |
| hiddifynext | 2.5.7 | windows | `UNSUPPORTED_MISSING_HWID` | organic live | 2026-08-25 | sends no HWID candidate |

Historical evidence from the earlier PH3-07 gate (204 deduplicated
`sub_requests` rows, 2026-08-24) additionally observed missing-HWID families
`v2rayN`, `Throne`, `Exclave` and unknown/no-parse rows, and a broader
historical range of Happ/v2rayTun/Incy versions. That analysis is **not**
carried forward into this registry unverified -- it remains recorded as
historical evidence in `docs/PHASE3_COMPATIBILITY_TELEMETRY.md` and is not
reclassified here. Any future addition to this file requires its own fresh
evidence and a `REGISTRY_VERSION` bump.

## Policy layer (`src/hwid_gate.py`)

`evaluate()` takes only: an already-server-resolved `account_id`, normalized
client/version/platform, the request's own HWID presence/well-formedness
flags, the raw HWID (ephemeral, in-memory only, exactly as `device_slots`
already handles it), and the slot-verifier HMAC key. It accepts **no**
caller-supplied slot id, generation, child username, child UUID or Telegram
ownership proof -- there is no parameter for any of them, so there is
nothing for a caller/frontend to forge. It returns one of:

- `KNOWN_SLOT` / `ASSIGN_FREE_SLOT` (allowed);
- `DENY_UNSUPPORTED_CLIENT`, `DENY_MISSING_HWID`, `DENY_MALFORMED_HWID`,
  `DENY_SLOT_LIMIT`, `DENY_CROSS_ACCOUNT_HWID`, `INTERNAL_ERROR` (denied,
  fail-closed).

It never creates or modifies a parent account, Telegram identity link, child
intent or outbox row -- the only mutation path is `DeviceSlotStore.claim`,
unchanged from PH3-02.

## PH2-05 ownership boundary

PH2-05 (admin/user session and ownership lifecycle) has not started --
`ROADMAP.md` still lists it `[ ]`. There is currently no ownership-recovery
or account-rebind route anywhere in the codebase, so there is no existing
path where HWID could be used as ownership proof. PH3-04 depends only on
*this* narrow guarantee (OPD-39/DL-041 already fix the policy: first-rollout
recovery is primary-admin-only, HWID/token possession are never proof), not
on the rest of PH2-05 (session TTL, CSRF, logout, etc.), and does not build
any part of PH2-05 itself. `tests/test_hwid_gate.py` proves the new code has
zero coupling to `mgboost_telegram_identities` or `mgboost_accounts`
mutation across every decision path, including denied ones.

## Feature flag

`PH3_04_ENFORCEMENT_MODE` (`src/config.py`) defaults to `OFF` and currently
has no runtime effect at all, because nothing imports `src/hwid_gate.py`.
It exists only so a future, separately approved caller (the PH4 migration
path) has a staged `OFF -> CANARY -> ENFORCE` rollout knob instead of a
single switch. This task never sets it to `ENFORCE` and never wires it into
`src/routes/sub.py`; the real legacy `/sub/{token}` resolver is completely
unaffected by this deploy.

## Tests

`tests/test_compat_registry.py` (14 passed): exact match, no fuzzy/substring
match, unknown version/platform, schema/duplicate-key/normalization
validation, no raw-identifier shapes in the registry.

`tests/test_hwid_gate.py` (29 passed): exact supported/unsupported/unknown
client tuples, spoofed client label without a registry match, missing/
malformed HWID denial, known-HWID idempotent same-slot/generation
resolution, unknown+free-slot assignment, exact 3/6/12 paid baselines,
INTERNAL unlimited (technical cap 99), full-capacity deterministic refusal
without eviction, concurrent same-HWID convergence to one generation,
concurrent different-HWID near capacity never exceeding the limit,
cross-account HWID deny (not a takeover), copied-HWID-within-one-account
practical-identity limitation (explicitly not claimed as cryptographic
uniqueness), no caller-suppliable slot/generation/child/Telegram parameter,
stale-generation-can-never-reactivate, reinstall with a free slot (new slot,
old slot untouched) and with full slots (clear refusal, no silent
replacement), zero Telegram/account/child-intent/outbox mutation on every
path, and no raw HWID in the DB dump or in the decision object's `repr`.

Full project regression: `631 passed, 3 skipped`.

## Isolated staging gate (2026-08-25)

Run against a `sqlite3 .backup`-consistent copy of the live production DB
(never the live file), retrieved and later securely deleted from both the
production host and the local machine. `hwid_gate`/`compat_registry` add no
new schema at all -- the gate reuses the existing PH3-02 tables -- so the
gate proved end-to-end behavior directly against the real production schema
and the real approved canary account's entitlement row:

- exact 3/6/12 capacity allow-then-deny: PASS;
- same-HWID idempotency (identical slot/generation on repeat): PASS;
- missing/malformed/unknown-client denial: PASS;
- cross-account HWID denial: PASS;
- reading the real canary account's (`account_id=1`) capacity state
  succeeded without raising and without changing its active-slot count;
- `src/routes/sub.py` does not import `hwid_gate`/`compat_registry` (feature
  OFF is exactly the pre-PH3-04 code path, not just a flag check);
- every pre-existing production row (accounts, slots, generations, aliases,
  child intent, outbox, shadow binding, `user_devices`, `hwid_lock`,
  Telegram identities) was byte-identical before and after the gate.

Result: `ALL_PASS`.

## Production deployment

PH3-04 introduces no new schema/config beyond the dormant
`PH3_04_ENFORCEMENT_MODE=OFF` default and is not imported by any running
route, so deployment is a plain code-only fast-forward with no schema
migration and no new production mutation surface. See `CHANGELOG.md` for the
production evidence gate result.
