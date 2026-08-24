# PH1-08 Marzban login-notification password redaction

Date: 2026-08-24. This runbook is subordinate to
`docs/PHASE1_BACKWARD_COMPATIBILITY.md` and builds on the completed PH1-07
image. It changes only the argument sent to Marzban's failed-login reporting
path; authentication and response semantics remain unchanged.

## Verified execution path

Marzban 0.8.4 `/code/app/routers/admin.py` validates the submitted password
with `validate_admin`. On failure it passed the same plaintext value to
`report.login`, which can forward it to Telegram and Discord. Successful-login
reporting already sends the fixed `🔒` value.

The PH1-08 image replaces only the failed report argument with `🔒`. The
password remains available only for the immediate authentication check. The
patcher validates the exact two-call AST shape, requires exactly one failed
and one successful report call, is idempotent, and fails the image build if
the upstream source changes or becomes ambiguous.

## Fixed compatibility invariants

- Authentication success/failure and HTTP payload/status: unchanged.
- Marzban application/schema and `python-multipart`: unchanged from PH1-07.
- UUID, legacy subscription URL/token, expiry, tariff and HWID: unchanged.
- VLESS/config generation, nodes, inbounds and data limits: unchanged.
- Successful and failed login notification delivery: preserved; password
  field is always a redaction marker.

## Staging gate

1. Unit-test exact patch, idempotency and refusal of altered/duplicate source.
2. Build target `ph1-08` from the pinned PH1-07 image.
3. AST-inspect the built router and require a redaction constant in both
   report calls and no password argument in any `report.login` call.
4. Exercise a canary failed login against isolated Marzban and capture the
   report function argument without sending Telegram/Discord traffic; require
   the canary password to be absent and `🔒` to be present.
5. Repeat PH1-07 broker all-10, legacy subscription/config and parser checks.

## Staging evidence

The PH1-08 image was built from the exact PH1-07 layer and inspected before
startup. Its router contained exactly two `report.login` calls, both with the
redaction constant and no plaintext password report argument. A direct route
capture preserved failed-login HTTP 401 and captured only `🔒`.

The image then replaced only the isolated localhost staging container while
the prior PH1-07 container remained stopped for rollback. With
`NOTIFY_LOGIN=true`, an HTTP failed-login canary appeared zero times in the
report capture, response and container logs. All ten broker operations,
legacy `/sub` with broker available/unavailable, MGBoost restart, Filin HMAC
create/renew/delete and UUID/config continuity passed. Regression results:
`401 passed, 1 skipped` locally and `400 passed, 2 skipped` under the isolated
production Python 3.10 environment.

The staging comparison exposed and fixed two verifier assumptions, not product
behavior: Marzban's admin-facing `subscription_url` is timestamped after renew
while the old issued alias remains valid, and Reality selects a valid `sid`
per response. Verifiers now preserve strict UUID/proxy/inbound/endpoint/
transport/query-shape checks, test the old alias directly and normalize only
the value of an already-present `sid`.

## Production cutover

1. Capture the masked semantic state with the stable PH1-07 verifier.
2. Retain the exact PH1-07 compose/image reference for rollback.
3. Change only the Marzban image reference to the verified PH1-08 image and
   recreate only Marzban.
4. Verify authenticated broker reads, admin login, legacy subscriptions,
   admin/LK/Stars/Filin, systemd/nginx and exact masked semantic state.
5. Submit one synthetic failed-login canary and prove its password is absent
   from the report argument and all new container/journal/nginx logs without
   printing the canary value.

## Production evidence

Production cutover completed on 2026-08-24 by changing only the Marzban image
from the verified PH1-07 layer to the verified PH1-08 layer. Candidate AST and
direct report capture passed before cutover. After recreate, the authenticated
broker health check and masked semantic state matched exactly.

A valid admin login still returned a token without printing it. With
`NOTIFY_LOGIN=true`, one synthetic failed login returned the established 401;
its canary password appeared zero times in the report argument, HTTP response,
container logs, MGBoost/broker journal and nginx logs. All 25 legacy configs
resolved with zero fetch errors. Identity/expiry, 71 device rows and 71 HWID
locks matched the pre-state. Admin/LK, Stars durable state, Filin HMAC, broker,
backup timer, nginx/systemd and permission checks passed.

Four broker error-signature messages occurred at the exact Marzban recreate
timestamp while the upstream was unavailable. They did not contain the canary;
the subsequent stable ten-minute window contained zero error signatures.
No rollback was required.

Production invariants: UUID changes 0; subscription URL/token changes 0;
HWID changes 0; expiry changes caused by deployment 0; tariff changes 0;
forced client reconfiguration 0; unexpected effective config changes 0.

## Rollback

If any compatibility invariant fails, restore the exact PH1-07 image and
recreate only Marzban. No DB row or user credential changes, so rollback must
not rotate UUIDs, subscription tokens or admin/user credentials. A rollback
must never re-enable plaintext password reporting merely to restore optional
login notifications; if notification delivery itself fails, disable that
notification path until the redacted image is corrected.
