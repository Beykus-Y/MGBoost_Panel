# ADMIN_PANEL_REDESIGN — account-centric admin panel: audit + owner-approved plan

Status: **approved design plus active Wave A implementation record.** The first
account-centric read-only UI/API slice is implemented locally; `ROADMAP.md`
`PH7-12` tracks its evidence and remaining work. This document remains the
canonical UX/architecture contract for redesigning the MGBoost admin panel
around `mgboost_account` instead of raw Marzban users.

Produced: 2026-08-26 (read-only audit session, HEAD `8b73843`, unchanged by
this document). No production mutation, no runtime code change, no schema
change happened while producing this document.

**Next agent instruction:** start **Wave A** (see below). Do not start
PH4-06. Do not touch grace/migration runtime behavior. See
`AGENT_HANDOFF.md` for the exact current top-of-file handoff.

---

## 1. Current admin architecture (as audited, read-only)

### Frontend

- `frontend/index.html` + `frontend/assets/admin.js` (~1700 lines): plain
  vanilla JS, **no build chain** (no webpack/vite/bundler), no ES modules, no
  classes except one `SafeMarkup` helper. Procedural code with a safe
  tagged-template renderer (`html`/`esc`/`renderHtml`) that always escapes
  values — this is why there is no XSS-relevant inline-HTML risk today.
- Navigation is a manual SPA-like `.page`/`.nav-item` switch (`showPage()`),
  no URL routing (no `history.pushState`, no hash routes).
- Event dispatch is fully delegated (`document.addEventListener('click'
  /'change'/'input', ...)`) via `data-action`/`data-change-action`/
  `data-input-action` attributes. **No inline `onclick`/`onload` anywhere**,
  no inline `<script>` with logic — `panel.py` already serves a strict CSP
  (`script-src 'self'`, no `'unsafe-inline'`) and the frontend already
  satisfies it.
- Auth: login form → `POST /admin/session/login` (`X-MGBoost-Admin-Login: 1`
  header, JSON body) → server sets an HttpOnly/SameSite=Strict session
  cookie; `csrf_token` is kept only in an in-memory JS variable, sent as
  `X-CSRF-Token` on every non-GET request via a shared `adminFetch()` helper.
  No token/JWT is ever stored in `localStorage` (an old `mz_token` removal is
  even explicitly cleaned up as dead legacy state).

**Current screens:** Dashboard (Marzban system/nodes stats), Users (Marzban
user list/detail/create), Nodes, Extra configs (3 tabs: global/per-user/
inbound-extra), Tickets, Stars (tariffs/payments/orphan payments), Settings
(bot/sub-template/AI-support).

**Users table is 100% Marzban-username-centric**: columns are username+note,
Marzban `status` (active/disabled/expired/limited/on_hold), traffic, expire,
client user-agent, HWID-device count, `online_at`. The identity key
everywhere is the raw Marzban `username` string, never `account_id`.

**Grep across `admin.js` for `account|migration|grace|opaque|bridge|
ownership|device_slot` returns zero matches.** The entire PH2–PH4
parent-account/migration/grace/opaque-credential/device-slot/ownership-rebind
domain has **no UI at all**, even though the backend for all of it is fully
implemented, tested, and live in production. The only exception: one single
route pair (see below) for opaque subscription credential status/issue.

### Backend admin routes

~50 routes registered in `src/server.py`, handlers in `src/routes/admin*.py`:

- **Session** (`admin_session.py`): `POST /admin/session/login`,
  `GET /admin/session`, `POST /admin/session/logout`,
  `POST /admin/session/rotate`. Login literally calls the real Marzban
  `get_token()` — there is no separate MGBoost admin password.
- **Marzban proxy** (`admin_proxy.py`):
  `GET/POST/PUT/DELETE /admin/marzban/{path}` — a narrow allowlist (system,
  nodes, nodes/usage, users, users/usage, inbounds, user CRUD, user/{u}
  /usage, user/{u}/reset, node/{id}/reconnect). Marzban JWT stays
  server-side, never reaches the browser.
- **Legacy own tables, keyed by Marzban `username`**: configs/stats/
  per-user-configs/node-filters/node-settings/bot-settings/settings,
  `user-devices*` (device limit/remove, backed by legacy `user_devices`/
  `hwid_lock` tables), tickets, stars-tariffs/settings/payments/
  orphan-payments (all in `admin.py`).
- **The one `mgboost_account`-native admin surface**
  (`subscription_credentials_admin.py`, PH4-04):
  `GET/POST /admin/accounts/{account_id}/subscription-credential(/issue)`.
  This is also the **only** admin mutation in the whole codebase that
  already meets the PH7-08 target bar: mandatory `reason` (3–300 chars),
  `PrimaryAdminAuthority` capability required, explicit-confirm on
  rotate-while-ACTIVE (`409 requires_confirmation` without `confirm: true`),
  and a durable audit event (`mgboost_subscription_credential_events`). Raw
  token is returned exactly once in the issue response body and never
  logged/stored/re-shown.
- **Everything else (~40 mutation routes, including direct Marzban
  create/modify/delete/reset-traffic via the proxy) has no reason
  requirement, no capability gate, no audit trail** beyond the ordinary
  admin session + CSRF boundary.

### Auth/session model (`src/security.py`, `src/admin_authority.py`)

- `require_admin_auth`: in-memory session lookup by cookie id, CSRF checked
  via `secrets.compare_digest` on unsafe methods.
- Admin login rate-limited per IP+username (`_ADMIN_LOGIN_LIMITER`, `429`/
  `Retry-After`); successful login always mints a fresh session id and
  revokes whatever was in the cookie before (no session-id reuse).
- `PrimaryAdminAuthority` (`admin_authority.py`): a narrower capability layer
  on top of an ordinary admin session — it authorizes only the **one**
  Marzban login hardcoded as primary admin in server config
  (`hmac.compare_digest` on username), producing a sealed
  `PrimaryAdminCapability` (per-process random seal, `compare_digest`
  verified on use). This is the boundary every PH3-06/PH4-01..05 sensitive
  mutation (grace start/extend, bridge enable, migration revoke-pending,
  ownership ambiguity resolve, credential issue) already requires in the
  domain layer — but only one live HTTP route (`subscription_credential_
  issue`) actually wires it in today.

### What is UI-live vs backend-only today

| Live in admin UI | Backend-only (no admin UI at all) |
|---|---|
| Users (Marzban), Nodes, Extra configs, Tickets, Stars, Settings | Parent accounts, migration lifecycle, legacy grace/bridge, device slots (mutations), ownership rebind, internal entitlements, unified provenance/audit trail |

Migration/Grace visibility today is **CLI-only**:
`scripts/ph4_05_daily_cohort_report.py`, run manually over SSH against a DB
copy. It has no HTTP API and no UI.

### Existing read-model building blocks (do not reinvent)

- `src/legacy_grace_observability.py::account_grace_snapshot(db, account_id,
  now)` + `classify_action(snapshot)` → already produces exactly the
  `OK_MIGRATED|WAITING_FOR_REGISTRATION|CONTACT_USER|MANUAL_REVIEW|
  COMPATIBILITY_BLOCK|RECONCILE_REQUIRED` classification the Migration/Grace
  dashboard needs. Currently only consumed by the CLI script. **Wrap this in
  a read-only admin HTTP route — do not re-derive the classification
  logic in the frontend or in a new backend module.**
- `src/internal_entitlements.py::effective_entitlements(account_id, now)` —
  already reads plan + overrides for the effective entitlement view.
- `src/device_slots.py::DeviceSlotStore.list_for_account(account_id)` —
  slot/generation list, but does not join migration state or activity; a
  device read-model still needs to combine this with
  `migration_lifecycle` state and `legacy_grace_activity.last_seen()`.
- No existing `get_account_detail()` — account state is spread across
  `mgboost_accounts`, `mgboost_telegram_identities`, `mgboost_subscriptions`,
  `mgboost_plan_versions`, device slots, migration bindings, grace, bridge,
  credential, and internal-review tables. **This aggregator does not exist
  yet and is Wave A's main new piece of code.**
- No unified audit timeline function — account-level history requires
  joining 7 separate event tables: `mgboost_entitlement_mutations`,
  `mgboost_payment_records`, `mgboost_migration_binding_events`,
  `mgboost_legacy_grace_events`, `mgboost_ownership_rebind_operations`
  events, `mgboost_subscription_credential_events`, and
  `mgboost_telegram_identities` (linked_at/revoked_at) — plus the old
  pre-mgboost `audit_log` table as legacy evidence (per PH7-08's own
  migration note in `ROADMAP.md`).

---

## 2. Target architecture

Primary entity of the new admin UI: **the MGBoost parent account**
(`mgboost_account`), never a raw Marzban user.

### Target navigation

```
Dashboard
Accounts
  └── Account
      ├── Overview
      ├── Subscription
      ├── Devices
      ├── Telegram / Ownership
      ├── Migration / Grace
      ├── WL
      ├── Payments
      ├── Audit
      └── Technical
Migration / Grace
Payments
Tickets
Nodes
Extra configs
System / Health
```

Rule of thumb for where a screen belongs:

> Working with a customer → **Accounts**.
> Working with Marzban internals directly → **System / Technical**.

---

## 3. Owner-approved UX decisions (binding, do not re-litigate)

These are **owner decisions**, made in the read-only audit session that
produced this document. A future agent implementing Wave A/B must follow
them as given, not re-propose alternatives, unless the owner explicitly
revisits them.

### ADMIN-UX-01 — Technical depth

All internal technical identifiers are **hidden by default** and shown only
under the `Account → Technical` tab:

- raw `mgc_*` child identifiers
- child intent IDs
- generation IDs
- outbox IDs
- full/internal UUID
- full/internal HWID identity
- any other internal implementation ID

Ordinary `Overview`/`Devices` screens show only operational concepts, e.g.:

```
Slot 2
ACTIVE
REVOKED
MIGRATED
Child ACTIVE
desired / observed
```

Masked HWID/UUID (partial, non-reversible display) may appear outside
Technical where genuinely useful for troubleshooting; full/raw identity
stays Technical-only. A future global Developer/Advanced mode is possible
later but is explicitly **out of scope for the initial rollout**.

### ADMIN-UX-02 — PH7-05 Wave B device mutations

Wave B needs all three mutation groups, but they are **four distinct
operations**, never one generic "Delete device" button:

1. **Disable / Enable** — reversible pause of a device/slot. UX must make
   the reversibility visually obvious (e.g. a toggle, not a destructive-red
   button). No mandatory reason.
2. **Revoke** — terminal revoke of the *current* credential generation. The
   old UUID/credential must never resurrect. Requires preview + reason +
   explicit confirmation + immutable audit event.
3. **Free** — a **separate, subsequent** operation from Revoke: releases the
   slot after Revoke so a new device can claim it. Requires reason +
   explicit confirmation + audit event. Do not merge Revoke+Free into one
   click.
4. **Rebind (compromise/replacement flow)** — old generation is terminally
   revoked, a new generation is created for a (possibly different) device.
   Highest risk, **the most demanding confirmation** of the four (two-step,
   explicit acknowledgement of suspected compromise where applicable).
   Ordinary rebind and suspected-compromise rebind remain distinct
   operations at the UX level too, matching the existing ownership-rebind
   rule ("HWID/URL possession is not ownership proof").

Backend primitives for all four already exist (`device_slots.py`,
`ownership_rebind.py`, `child_lifecycle.py`) — Wave B work here is mostly
admin route + confirm-UX, not new domain logic.

### ADMIN-UX-03 — Legacy Users cutover

The old Marzban-username-centric `Users` screen is **removed from top-level
navigation immediately** (not left coexisting in parallel), but **not
deleted**. It moves to approximately:

```
System / Technical
  └── Marzban
      └── Raw Users
```

(Exact naming can be adapted to the final navigation implementation.) It
stays as a compatibility/debug escape hatch until the Accounts replacement
is proven to cover equivalent functionality — nothing gets deleted
prematurely. `Accounts` becomes the primary top-level surface for all
customer-facing work.

### ADMIN-UX-04 — Dashboard priority

Priority order, current state:

1. **Grace campaign progress** (highest, while PH4 grace is active): Day
   N/14, exact end/remaining, Telegram BOUND count, WAITING_FOR_REGISTRATION
   count, real devices observed vs. real devices child-backed vs. still
   legacy, reconcile/compatibility blockers. **Must not show a misleading
   "17/17 MIGRATED"** when what is actually true is parent/genesis
   readiness — genesis placeholder ≠ real migrated customer device (see
   Section 5). This widget must be built as a **conditional block** (only
   rendered while an active grace cohort exists) so it naturally
   disappears/collapses once PH4 grace ends, rather than being permanently
   wired into the Dashboard layout.
2. **Operational health**, compact: MGBoost/broker/Marzban/child-worker/
   nodes status, resolver errors, `ERROR_RECONCILE` count, compatibility
   problems. Compact when healthy; visually escalates when something is
   wrong.
3. **Expiring soon**: today / ≤3 days / ≤7 days / ≤30 days, a handful of
   nearest accounts. This is a permanent operational block, not tied to the
   grace campaign.

**Tickets**: no big support-analytics dashboard block for now — a compact
`N open / N unanswered` counter linking into the Tickets screen is enough.

After the grace campaign ends, the vacated dashboard space is expected to be
taken over by billing/WL/operational alerts (Wave C/D territory) — the
Dashboard must not be architecturally coupled to the migration campaign.

### ADMIN-UX-05 — Frontend technology direction

**No React/Vue/Svelte adoption.** Stay on the existing vanilla-JS approach,
but split the current monolithic `admin.js` into ES modules. Target example
layout (exact decomposition may be adjusted after real code-boundary
analysis):

```
frontend/assets/admin/
  core.js          # shared safe primitives: html/esc render helpers,
                    # adminFetch, CSRF handling, common dialogs,
                    # formatting/status utilities
  navigation.js
  dashboard.js
  accounts.js
  account-detail.js
  devices.js
  migration.js
  telegram.js
  subscription.js
  tickets.js
  nodes.js
  configs.js
  system.js
```

Must not regress: CSP (`script-src 'self'`, no inline), event delegation
pattern, safe text rendering (`html`/`esc`), server-side session model, CSRF
model.

---

## 4. Read-model direction

The new admin UI must **not** assemble account state client-side from dozens
of independent requests, nor should any new backend module become a second
business-logic engine. Target reusable server-side presentation models:

- `AccountSummary` — for the Accounts list.
- `AccountDetail` — for the Account page tabs.
- `DeviceSummary` — slot + generation + migration state + last activity,
  joined once server-side.
- `MigrationGraceSummary` — **must wrap/reuse
  `legacy_grace_observability.account_grace_snapshot()` and
  `classify_action()` directly**; must not re-derive or duplicate the
  classification logic that already lives in
  `scripts/ph4_05_daily_cohort_report.py` or in
  `legacy_grace_observability.py`.
- `SubscriptionSummary` — must source effective entitlement from the
  canonical `internal_entitlements.effective_entitlements()` /
  `mgboost_subscriptions` path, never a frontend-computed approximation.
- `PaymentSummary` — from `mgboost_payment_records` (+ note: the Stars
  worker still writes to the older `stars_invoices` table and is not yet
  unified with provenance — do not paper over this gap silently in the
  read-model; surface it or flag it explicitly).
- `AuditEvent` — the unified account-timeline join across the 7 event
  sources listed in Section 1.

**The frontend is never the authority** for status/state — every displayed
status must come from a backend read-model call, never be computed or
cached client-side beyond simple formatting.

---

## 5. Current PH4 migration/grace state (verify before relying on this)

As of the last known state (`AGENT_HANDOFF.md` top section, HEAD `8b73843`):

- PH4-03: `[x]` closed (mass migration technically complete).
- PH4-05: `[x]` closed (grace campaign live in production).
- PH4-06: **NOT STARTED**, not scoped, gated on its own explicit owner
  authorization plus PH4-03 exception review — the clock alone does not
  authorize it.
- 17 real ACTIVE parent accounts, covering all 19 real ACTIVE legacy Marzban
  usernames.
- Technically parent-ready / bridge-enabled: **17/17** (genesis child
  `ACTIVE` + `mgboost_legacy_bridge_bindings.enabled=1`).
- Telegram `BOUND`: 4 accounts (1, 3, 4 pre-existing + 8 newly resolved this
  session via the one deliberate, capability-gated
  `resolve_ambiguous_telegram_ownership()` exception).
- `WAITING_FOR_REGISTRATION`/`UNREGISTERED`: 13 accounts — expected, this is
  PH4-05's own ongoing campaign metric, not a blocker.
- `MANUAL_REVIEW`: 0 (the one previously-ambiguous account, id 8, was
  resolved this session).
- Grace window: `started_at` = 2026-08-26 14:08:25 MSK, `current_end_at` =
  2026-09-09 14:08:25 MSK (exactly 14 days, `cohort_ref=
  'PH4-05-MASS-COHORT-2026-08-26'`).

**Critical nuance for the future Dashboard/PH4-06 decision gate:**
**17/17 parent-ready does NOT mean 17/17 real customer devices are
migrated.** For the 14 newly-bootstrapped accounts, the only thing that
exists today is a synthetic genesis-child placeholder on slot 1 (never a
real customer device, has no `mgboost_migration_bindings` row). Real
per-device migration lineages for those 14 accounts' actual customer
devices appear **organically**, one at a time, the next time each
customer's own client hits the unchanged legacy `/sub/{token}` URL — exactly
as designed, nothing was simulated or forced. `mgboost_migration_bindings`
counts (`MIGRATED=9`, `MIGRATING=0`, `ERROR_RECONCILE=0`) are unchanged for
accounts 1/3/4's own real devices and are the only real per-device migration
evidence that exists today. Any future Migration/Grace dashboard or PH4-06
readiness check must keep these two numbers (parent-ready count vs. real
device-migrated count) visually and semantically separate — this is a
recurring point of confusion this document exists partly to prevent.

Before building anything against this section, re-read `ROADMAP.md`
(PH4-03/05/06 entries) and the top of `AGENT_HANDOFF.md` — this state
changes daily during the grace window (new registrations, new real device
migrations land automatically).

---

## 6. Implementation waves (approved direction)

### Wave A — Foundation + current-state UI (**can start now**)

Scope:

- Modularize frontend into the ES-module layout in Section 3
  (ADMIN-UX-05).
- New top-level navigation (Section 2).
- `Accounts` list screen backed by a new `AccountSummary` read-model.
- `Account` detail page: Overview, Subscription (read + existing
  issue/reissue opaque-credential flow, already safe and live),
  Telegram/Ownership (read state), Devices (new `DeviceSummary`
  read-model, read-only), Migration/Grace tab (reuses
  `account_grace_snapshot`).
- Standalone Migration/Grace dashboard, wrapping
  `legacy_grace_observability` + `classify_action` in a new read-only admin
  HTTP route — no new business logic.
- New Dashboard home per ADMIN-UX-04.
- Move legacy `Users` under `System / Technical` per ADMIN-UX-03.
- Re-integrate existing Tickets/Nodes/Extra configs/System screens under the
  new navigation without functional loss.
- Responsive desktop/narrow layout.

Wave A is almost entirely **read-only**, except the already-existing and
already-production-proven opaque-credential issue/reissue flow (PH4-04),
which Wave A simply surfaces better.

### Wave B — Safe administrative mutations (after Wave A)

- PH7-01 expiry operations, if `PH3-08`/outbox dependency is actually ready
  (verify current state, do not assume).
- PH7-05 in full: Disable, Enable, Revoke, Free, Rebind — per ADMIN-UX-02.
- PH7-08 unified administrative audit/timeline (the `AuditEvent` join from
  Section 4).
- Telegram ownership/rebind UI on top of the existing secure ownership
  lifecycle (`ownership_rebind.py`).

Real destructive mutations here require the same production-canary
discipline already established in PH4 work (dry-run against a DB copy,
staged rollout, pre/post invariant checks) — do not skip it just because
it's "only an admin UI."

### Wave C — PH5-backed capabilities (blocked until PH5 exists)

Catalog, plan/entitlement admin, billing, manual external payments, unified
payment UX. Do not build any of this against a temporary/invented tariff —
PH5 is the only source of catalog authority (see `ROADMAP.md` DL-031).

### Wave D — PH6-backed capabilities (blocked until PH6 exists)

WL quota breakdown, consumption, packages, adjustments, reset, override,
allocations. No WL consumption/allocation data exists anywhere in the schema
today — this is not a UI gap, it is a genuine backend absence.

**Never use a temporary direct Marzban/SQLite shortcut instead of waiting
for PH5/PH6** — this repeats the project's own standing rule against
inventing catalog/tariff data ad hoc.

---

## 7. Cross-references

- `ROADMAP.md` — Phase 7 (`PH7-01`..`PH7-11`), Decision Log `DL-048`..
  onward (this document's decisions), PH4-03/05/06 current status.
- `AGENT_HANDOFF.md` — top-of-file current handoff, exact next action.
- `docs/PHASE4_GRACE_PERIOD_RUNBOOK.md`,
  `docs/PHASE4_MIGRATION_SUPPORT_RUNBOOK.md` — operational runbooks this
  design must not duplicate or fork logic from.
- `src/legacy_grace_observability.py`,
  `scripts/ph4_05_daily_cohort_report.py` — canonical Migration/Grace
  business logic, reuse only, never re-derive.

---

## 8. Wave A implementation status

First completed slice (2026-08-26): `src/admin_read_models.py` and
`src/routes/admin_accounts.py` add authenticated read-only Accounts/detail,
Migration/Grace and Dashboard APIs. `frontend/assets/admin/core.js` and
`frontend/assets/admin/accounts.js` add the new account-centric UI while the
legacy screen code remains functional in `admin.js`. Navigation now follows
ADMIN-UX-03, Dashboard follows ADMIN-UX-04, and the UI explicitly distinguishes
parent readiness/active slots from real migration lineages.

This is not full Wave A closure: legacy screen code still needs to be split
into per-domain ES modules and the browser/production rollout gates remain.
Canonical progress/evidence lives in `ROADMAP.md` `PH7-12`.
