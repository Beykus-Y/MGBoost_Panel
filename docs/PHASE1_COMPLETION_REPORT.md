# Phase 1 completion report

Date: 2026-08-24. Verdict: **PHASE 1 COMPLETE — READY FOR PHASE 2**, subject
to the per-task dependencies and residual risks below. This report records the
deployed production state; it does not authorize or begin Phase 2.

## Completed tasks

- PH1-01: admin stored/DOM XSS removal and server-side admin sessions.
- PH1-02: CSPRNG Marzban SUDO rotation and login rate limiting.
- PH1-03: minimum production permissions for Marzban/MGBoost data and secrets.
- PH1-04: dedicated `mgboost` service identity and systemd hardening.
- PH1-05: authenticated localhost-only typed Marzban broker.
- PH1-06: raw subscription leakage containment, encrypted backups/quarantine
  and retention controls without legacy user-token rotation.
- PH1-07: pinned multipart/aiohttp/aiogram dependency hardening.
- PH1-08: failed-login notification password redaction.

## Production security posture

- MGBoost runs as `mgboost` from the isolated PH1-07 Python runtime and has
  zero `MARZBAN_ADMIN_USER/PASS` variables in its process environment.
- The typed broker is the only component with SUDO credentials, listens only
  on `127.0.0.1:8002`, requires HMAC authentication and has no nginx route.
- Marzban runs the immutable PH1-08 image: unchanged 0.8.4 application/schema,
  `python-multipart 0.0.32`, and two redacted login-report calls.
- Active MGBoost DB is `0600 mgboost:mgboost`; Marzban `.env`, DB and Xray
  credential config are `0600 root:root`; MGBoost `.env` is
  `0640 root:mgboost`.
- Daily encrypted backup timer is enabled/active. The fixed 30/60/90/180-day
  retention policy and one encrypted legacy quarantine snapshot remain in
  force.
- New subscription traffic uses token-safe application/nginx logging and
  verifier references; legacy bearer credentials remain valid by explicit
  Phase 4 migration policy.

## Regression and production gates

- Local full suite: `401 passed, 1 skipped`.
- Isolated production Python 3.10 suite: `400 passed, 2 skipped`.
- PH1-05 all ten broker operations and direct-vs-broker effects: PASS.
- Legacy `/sub` with broker available/unavailable and MGBoost restart: PASS.
- Filin HMAC create/renew/delete staging and authenticated production status:
  PASS; missing HMAC fails closed.
- Telegram proxy runtime: PASS. OpenRouter retained its pre-existing HTTP 403
  without credentials or user context in test output.
- Admin valid/failed login, LK, Stars durable state, nginx/systemd, DB quick
  check, backup timer and token-safe log scans: PASS.
- Final masked production state: 25 users/configs, 71 device rows and 71 HWID
  locks; zero config fetch errors and exact semantic equality.

## Existing-user invariants

- UUID changes: 0.
- Legacy subscription URL/token changes or revocations: 0.
- HWID/device binding changes: 0.
- Expiry changes caused by deployment: 0.
- Tariff migrations: 0.
- Forced client reconfiguration: 0.
- Unexpected effective VPN config changes: 0.

Reality `sid` values are selected dynamically by existing Marzban behavior,
and MGBoost's legacy information node contains live text. Verification
normalizes only the `sid` value and excludes only the exact non-connection
information-node shape; UUID/credential, endpoint, port, transport/TLS,
remaining query values and fragments stay strict.

## Residual risks

- Existing legacy subscription bearer URLs remain usable until the staged
  opaque-token/account migration in Phase 4. Historical token evidence remains
  under the approved encrypted retention policy.
- The broker deliberately retains ten legacy operations and holds a SUDO
  credential. Its exposure is much smaller than before, but broker compromise
  remains high impact until later child-user/entitlement operations replace the
  transitional surface.
- Admin sessions, rate-limit state and internal HMAC nonce replay state are
  process-local. Shared durable replay/state work remains Phase 2/8 and must be
  completed before multi-worker deployment.
- Marzban's bundled Starlette 0.40.0 advisories require a controlled whole-app
  upgrade in Phase 8; PH1-07 fixed the directly applicable multipart parser
  issue without an unsafe in-place framework upgrade.
- OpenRouter support completion currently returns HTTP 403. Telegram bot/proxy
  is healthy, but OpenRouter support remains externally unavailable until its
  authorization/account/egress condition is corrected.
- Legacy HWID remains client-controlled and is not cryptographic device proof;
  Phase 1 intentionally did not enable fail-closed HWID or parent/child users.
- Production retains the known untracked operational `extra_configs.json`;
  Phase 1 did not read, modify, commit or remove it.

## Phase 2 readiness

There are no unresolved non-deferred product decisions blocking Phase 1–3.
Phase 2 may begin, but task dependencies still apply: in particular PH2-01's
opaque-token rollout depends on the Phase 3 account identity and staged Phase 4
migration bridge. Start with Phase 2 work whose dependencies are already met;
do not silently pull account/device/tariff migration into a security task.
