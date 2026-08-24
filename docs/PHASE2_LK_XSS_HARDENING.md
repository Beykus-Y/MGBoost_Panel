# PH2-02 LK device-name XSS and inline-handler hardening

Date: 2026-08-24. This change is limited to browser-side rendering and the
subscription browser copy page. It does not migrate or mutate users, devices,
subscriptions, tariffs, payments, Marzban data or credentials.

## Verified vulnerability and execution path

`GET /lk/api/devices` returned the stored device name to `frontend/assets/lk.js`.
The former renderer concatenated that value into an inline `onclick` JavaScript
string after HTML escaping. HTML escaping is not JavaScript-string escaping, so
quotes and backslashes could leave the string and execute in the LK origin.
Other API-controlled values were also interpolated into `innerHTML` render
paths. The subscription browser page embedded its bearer URL into an inline
script context for clipboard support.

The new renderer constructs nodes explicitly, assigns all untrusted text with
`textContent`, and binds actions with `addEventListener`. Mutation targets are
validated positive integer device IDs copied into opaque `dataset.deviceId`
attributes. Device names never select or authorize a device. The browser copy
page reads the already HTML-escaped visible URL through `textContent` from an
external same-origin script.

## Fixed compatibility invariants

- UUID changes: 0.
- Legacy subscription URL/token changes: 0.
- HWID/device binding migration: 0.
- Expiry changes caused by deployment: 0.
- Tariff or billing changes: 0.
- Forced client reconfiguration: 0.
- LK API paths, request methods, request bodies and responses: unchanged.
- Device rename/delete authorization and backend semantics: unchanged.
- Legacy `/sub/{token}` config responses for VPN clients: unchanged.

## Regression and security gate

- Static tests reject inline event attributes and LK HTML-string sinks.
- Malicious username, node name, API error and device names containing tags,
  entities, quotes and backslashes render as exact inert text.
- Real Chromium exercises LK load, rename and delete under the production CSP.
- Browser subscription URLs with markup characters remain text and are not
  inserted into JavaScript source.
- Full regression covers admin, LK, legacy `/sub`, Stars, Filin and broker.

Staging evidence: `404 passed, 2 skipped` in the base environment (the two
browser-only tests are skipped there) and `406 passed` in the environment with
Chromium/Playwright. JavaScript syntax, Python compilation and `git diff
--check` pass.

## Production cutover and verification

1. Record the expected commit and capture the existing masked user/config,
   expiry and device/HWID state using the established Phase 1 verifier.
2. Pull the exact commit; do not touch `extra_configs.json` or any database.
3. Restart only `mgboost-panel`; nginx, broker and Marzban configuration do not
   change.
4. Verify service health, LK/admin pages and the new cache-busted LK/browser
   assets. Confirm LK HTML contains no inline handler and strict CSP remains.
5. Compare a valid legacy subscription's masked config before/after, then smoke
   admin session, LK APIs, Stars durable state and Filin/broker status.
6. Inspect new application/nginx/journal entries for errors and raw bearer
   leakage. Require every compatibility invariant above to remain zero.

## Production evidence

Production deployment completed on 2026-08-24 from commit `fac7a78`. The
repository fast-forwarded cleanly while preserving the pre-existing untracked
`extra_configs.json`; no data or nginx/Marzban/broker configuration changed.
Only `mgboost-panel` was restarted.

The LK and admin pages, cache-busted LK and browser-copy assets, authenticated
Filin status, durable Stars tables, Telegram bot through the configured proxy,
broker/nginx/Marzban health and DB quick-check passed. OpenRouter retained its
pre-existing HTTP 403 baseline and did not receive any new context from this
change. Application/nginx/journal scans found no new raw subscription path and
no stable runtime error.

The post-deploy masked snapshot exactly matched the pre-deploy snapshot: 25
users, 25 functional legacy configs, zero fetch errors, 71 device rows and 71
HWID locks. UUID, legacy subscription URL/token, HWID, expiry, tariff, forced
client reconfiguration and unexpected effective config changes were all zero.
No rollback was required.

## Rollback

If the LK/browser assets fail or any invariant differs, restore the previous
known-good commit and restart only `mgboost-panel`. No schema, user, device or
credential migration is performed, so rollback must not rotate or alter UUIDs,
subscription URLs/tokens, HWIDs, expiry or tariffs. Never restore the unsafe
inline handlers or name interpolation as a permanent fix; keep the service on
the last safe server-session release while correcting the frontend offline.
