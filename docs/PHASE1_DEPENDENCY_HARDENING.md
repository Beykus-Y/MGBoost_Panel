# PH1-07 dependency hardening and compatibility gate

Date: 2026-08-24. This runbook is subordinate to
`docs/PHASE1_BACKWARD_COMPATIBILITY.md`. It changes dependencies/runtime only;
it must not migrate users, credentials, subscriptions, devices or tariffs.

## Fixed compatibility invariants

- UUID changes: 0.
- legacy subscription URL/token revocations or rotations: 0.
- HWID/device binding changes: 0.
- expiry or tariff changes caused by deployment: 0.
- forced client reconfiguration: 0.
- unexpected effective VPN config changes: 0.

## Production inventory and applicability

| Component | Before PH1-07 | PH1-07 target | Reason and boundary |
| --- | --- | --- | --- |
| Marzban application | 0.8.4 image digest `sha256:8e422c...9623b8d` | same exact digest | No Marzban/API/schema upgrade in Phase 1. |
| `python-multipart` | 0.0.7 | 0.0.32 | Public `/api/admin/token` uses FastAPI form parsing. Versions before the fixed releases have remotely reachable CPU/memory DoS parser paths. |
| MGBoost `aiogram` | 3.15.0 | 3.30.0 | 3.15.0 constrains `aiohttp<3.11`; pair upgrade is required. |
| MGBoost `aiohttp` | 3.10.11 | 3.14.3 | Used as an outbound Telegram/OpenRouter client; 3.14.3 fixes the current response-parser DoS advisory and the intervening client advisories. |
| MGBoost `aiohttp-socks` | 0.11.0 | 0.12.0 | Compatible proxy connector for the patched aiohttp stack. |

The Marzban base remains pinned rather than rebuilt from `latest`.
`deploy/marzban-hardening/python-multipart.lock` pins the parser wheel hash.
`requirements-runtime.lock` pins and hashes the Python 3.10 MGBoost runtime.
The runtime is installed into a root-owned isolated directory; packages are
not upgraded in-place in system Python.

Transitive inventory also reports advisories against Marzban's bundled
Starlette 0.40.0. FastAPI 0.115.2 constrains that dependency family, so a
safe fix requires the controlled Marzban application upgrade/staging work in
Phase 8, not an unsupported single-package replacement in Phase 1. The public
form-parser DoS in PH1-07 is independently fixed by the compatible
`python-multipart` overlay. Remaining Starlette exposure stays a documented
residual risk and must be covered by proxy request bounds/rate limits until
the controlled application upgrade.

Primary upstream evidence:

- <https://github.com/Kludex/python-multipart/security/advisories/GHSA-5rvq-cxj2-64vf>
- <https://github.com/Kludex/python-multipart/security/advisories/GHSA-pp6c-gr5w-3c5g>
- <https://github.com/Kludex/python-multipart/releases/tag/0.0.32>
- <https://github.com/aio-libs/aiohttp/security/advisories/GHSA-cq5v-8q36-5273>
- <https://github.com/aiogram/aiogram/releases/tag/v3.30.0>

## Staging evidence

Staging used two localhost-only containers with separate synthetic SQLite and
VLESS configs: the immutable production baseline and the PH1-07 target.
Production DB, Xray config, users and listeners were not mounted.

- local Python 3.14 regression: `394 passed, 1 skipped`;
- isolated production-Python 3.10 regression: `393 passed, 2 skipped`;
- both hash-locked requirement sets: `pip-audit` reported no known
  vulnerabilities;
- all ten legacy broker operations: PASS;
- direct-vs-broker response/effect comparison: PASS;
- identity fields changed by synthetic broker mutations: 0;
- legacy config body differences: 0;
- forced timestamp-boundary renew: old legacy alias still resolved the same
  UUID/config on baseline and target;
- MGBoost restart with broker down, legacy `/sub`, Filin HMAC
  create/renew/delete: PASS;
- 32 KiB semicolon parser benchmark: baseline 0.097190 seconds, target
  0.000030 seconds; 1 MiB target case 0.000108 seconds;
- 100 sequential valid staging logins: PASS;
- Telegram `getMe` through the configured SOCKS proxy on the new runtime:
  PASS; no message was sent;
- OpenRouter completion smoke returned the same HTTP 403 on both old and new
  runtimes. This is a pre-existing credential/account/egress condition, not a
  PH1-07 regression; no user/support context was sent (synthetic one-token
  prompt only). It remains an operational residual until the external service
  authorization is corrected.

`scripts/verify_runtime_integrations.py` is strict by default. The explicit
`--expected-openrouter-status 403` compatibility mode is permitted only after
the old runtime has independently produced that same baseline; it reports the
condition rather than treating OpenRouter as functional.

Marzban's `subscription_url` is timestamped and can advance after a legacy
renewal. The compatibility contract is therefore not equality of the admin
API's current URL across an intentional renewal. The real contract, verified
on both baseline and target, is that the already-issued alias continues to
resolve the same UUID/config and existing clients require no reconfiguration.

## Production evidence

Production cutover completed on 2026-08-24 in two steps: first the immutable
Marzban parser-overlay image, then the isolated MGBoost Python runtime. The
first full-body comparison changed and therefore triggered the documented
rollback before the runtime step. A repeated baseline proved that the existing
config output is intentionally non-deterministic: the synthetic information
node contains live description data and Marzban selects a Reality `sid` value
per response. No credential, endpoint, port, query key, non-`sid` query value,
transport/TLS field or fragment changed.

The masked verifier now excludes only the exact synthetic information-node
shape and normalizes only the value of an already-present `sid`; it continues
to hash the complete remaining VPN links. Two rollback-image captures were
then identical, and the same digest matched after both production deploy
steps. All 25 users/configs were fetched with zero errors; identity/expiry,
71 device rows and 71 HWID locks also matched. Admin/LK, Telegram proxy,
Stars durable state, authenticated Filin status, missing-HMAC denial, broker,
systemd/nginx and new-log token scans passed. OpenRouter retained its known
baseline HTTP 403 without credential output.

Production invariants: UUID changes 0; subscription URL/token changes 0;
HWID changes 0; expiry changes caused by deployment 0; tariff changes 0;
forced client reconfiguration 0; unexpected effective config changes 0.

## Production preflight and cutover

1. Verify clean diff, secret scan and expected commit/production HEAD.
2. Run `scripts/capture_phase1_masked_state.py` and retain only its aggregate
   digests in `/tmp`; it emits no usernames, UUIDs, tokens or HWIDs.
3. Back up the current `mgboost-panel.service`, Marzban compose file and exact
   old image ID/digest. Do not copy `.env` into the repository.
4. Build `/opt/mgboost-venvs/ph1-07.new` using `requirements-runtime.lock`
   with `--require-hashes`, run imports/tests, make it root-owned/read-only,
   then atomically rename it to `/opt/mgboost-venvs/ph1-07`.
5. Build the Docker target `ph1-07` and verify labels, base digest,
   `python-multipart==0.0.32`, FastAPI/Starlette imports and image ID.
6. Change only the Marzban compose image reference to the locally built exact
   PH1-07 image. Recreate Marzban, wait for health, and verify admin/broker,
   legacy `/sub`, config and masked state before touching MGBoost runtime.
7. Install the repository `mgboost-panel.service`, daemon-reload and restart
   MGBoost on the isolated runtime. Verify service identity/hardening,
   admin/LK/Stars/Filin, bot/proxy, broker and legacy `/sub`.
8. Capture the post state and require exact equality of user identity,
   effective config, device and HWID-lock digests plus zero fetch errors.
9. Scan nginx/application/journal tails for errors and raw credentials without
   printing any matching secret.

Do not continue to PH1-08 on any mismatch.

## Rollback

Rollback does not modify user credentials or DB rows:

1. restore the prior Marzban compose file/image ID and recreate only Marzban;
2. restore the prior `mgboost-panel.service` (`/usr/bin/python3`) and restart
   MGBoost;
3. verify health and compare the same aggregate masked snapshot;
4. retain the failed image/venv only as root-only diagnostic evidence until
   the incident decision, then remove it under the normal retention process.

Because neither dependency step changes Marzban user rows, subscription
tokens, UUIDs, expiry, HWIDs or tariffs, rollback requires no client action.
