# PH2-04 HTTP/cache/error hardening

Date: 2026-08-24. This task hardens response metadata and error handling after
PH2-02 removed LK/browser inline handlers. It does not migrate or rotate user
credentials and does not change a valid legacy subscription's generated VPN
configuration.

## Application response baseline

The main HTTP handler fills missing headers on every success and error path:

- `Cache-Control: no-store`, except explicitly versioned static assets which
  retain `public, max-age=3600`;
- `Referrer-Policy: no-referrer`;
- `X-Content-Type-Options: nosniff`;
- `X-Frame-Options: DENY` and CSP `frame-ancestors 'none'`;
- a restrictive camera/microphone/geolocation/payment/USB Permissions-Policy;
- default-deny CSP for non-HTML/API/error/config responses.

Admin and LK keep their existing executable CSPs. The browser subscription
page now needs only same-origin CSS/JavaScript and has no inline handler/script.
Its rollout flag `SUB_BROWSER_CSP_ENFORCE` sends strict CSP as report-only while
false, alongside a non-breaking object/base/frame baseline. After the
production asset/violation gate it can be set to true to enforce the strict
policy.

The HTTP `Server` value is a stable product name without Python/stdlib patch
versions. Known unused discovery routes (`/docs`, `/redoc`, `/openapi.json`,
`/debug`, `/version`) return the same JSON 404 rather than falling through to
the admin SPA. The localhost broker likewise removes its protocol version and
uses the response-security baseline.

## Subscription error and header contract

For non-browser subscription clients, upstream 401/403/404 responses now map
to one fixed 404 body after a small minimum response floor. Upstream/network
failures map to a fixed generic 502 body. Neither response includes an upstream
URL, exception string, username or token. Browser requests remain a copy/help
page for syntactically bounded tokens and do not probe token existence.

Valid subscription resolution, filters, base64 body, UUID/config links and the
established Marzban metadata allowlist are unchanged. Non-base64 upstream
responses now use that same allowlist instead of forwarding arbitrary headers;
CR/LF or oversized values are dropped before `send_header`. This prevents
upstream `Set-Cookie`, debug/version and header-injection values from reaching
clients.

Operational exceptions returned by internal/admin Stars paths are reduced to
stable messages; durable ambiguous refund state stores only an exception class,
not a URL-bearing exception string. Application logs emit exception classes on
LK failure paths.

## Nginx requirements

- apply `Strict-Transport-Security: max-age=31536000` at this TLS-terminating
  boundary, not the localhost HTTP application;
- set `server_tokens off` in the `http` scope;
- add `Strict-Transport-Security: max-age=31536000` with `always` to every
  HTTPS MGBoost/Marzban server block;
- repeat HSTS in child locations that define any `add_header`, because nginx
  stops inheriting the parent set there;
- retain the PH1-06 sensitive route log format and no-store/no-referrer rules.

`includeSubDomains` and preload are intentionally not enabled: ownership and
HTTPS readiness of every possible subdomain are not asserted by this task.

## Test and staged rollout

1. Run the real HTTP header matrix across LK, admin, session errors, static
   assets, explicit hidden routes and unknown methods.
2. Verify uniform invalid subscription status/body/floor and generic outage;
   verify a valid legacy config digest remains unchanged.
3. Run admin/LK/browser Chromium suites; strict browser-copy CSP must produce
   zero `securitypolicyviolation` events and the copy handler must execute.
4. Deploy application with `SUB_BROWSER_CSP_ENFORCE=0`. Validate production
   report-only header, assets, browser page and journals before enforcement.
5. Apply nginx changes with `nginx -t` before reload. Confirm HSTS and version
   hiding externally on all three HTTPS hostnames and sensitive locations.
6. Set `SUB_BROWSER_CSP_ENFORCE=1`, restart MGBoost, verify the strict enforced
   header and repeat browser/assets/config/admin/LK/Stars/Filin smoke.
7. Compare masked pre/post user/config/expiry/device state and token-safe logs.

## Rollback

For browser CSP incompatibility, set `SUB_BROWSER_CSP_ENFORCE=0` and restart;
do not restore inline scripts/handlers. For application regression, restore the
previous commit and restart MGBoost; no DB/schema/user rollback is involved.
For nginx failure, restore the root-only pre-change config copies, run
`nginx -t`, then reload. HSTS cannot be instantly revoked from browsers once
observed, so it is added only to hostnames already HTTPS-only in production.
No rollback step may rotate UUIDs, legacy subscription tokens, HWIDs or expiry.
