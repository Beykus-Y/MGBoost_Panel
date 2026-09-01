# ADMIN_CSS_REDESIGN — admin panel visual/CSS redesign: final architecture

Status: **complete.** All 9 waves of the approved plan (session plan file
`luminous-leaping-moth.md`) are implemented. This document records the
actual final state for future agents/humans — read this instead of
re-deriving the CSS architecture from scratch.

Produced: 2026-09-01 (Wave 9 completion). No backend/API/routing/auth/CSRF
change; no functional behavior change. Purely visual/CSS + the DOM/JS
changes strictly required to consume the new CSS (documented in ROADMAP
section below).

---

## 1. Final CSS architecture

The single 321-line `frontend/assets/admin.css` (plus its "Wave B / PH7-10"
light-theme color collision, duplicate `button.danger`, and five
independent badge-color functions scattered across JS) has been replaced by
**9 target stylesheets**, no build chain, loaded via `<link>` in
`frontend/index.html` with the project's existing manual `?v=` cache-bust
convention (bump only the file(s) that changed):

| File | Owns |
|---|---|
| `tokens.css` | `:root` design tokens (surfaces, text, borders, semantic colors, spacing, radius, motion baseline) |
| `base.css` | Reset, base typography, `:focus-visible`, `prefers-reduced-motion`, small base-level responsive rules (e.g. body font-size at ≤560px) |
| `shell.css` | App shell (`#app`/`#main`), sidebar/nav, page-header/actions, login page, responsive nav collapse |
| `components.css` | Every shared, cross-domain primitive: buttons, form controls, table, badge/dot/notice, tabs, loading/toast, modal (unified visual contract), progress/traffic bars, cards, stat tiles, section headings, kv-grid, timeline/queue widgets, a handful of small utilities (`.w-full`, `.muted`, `.plain-list`, etc.) |
| `domain-accounts.css` | Account workspace (accounts.js/device_ops.js): identity header, tab-scroll, device cards, credential/expiry/technical blocks, grace period, Accounts-list card-collapse |
| `domain-operations.css` | Node fleet cards + settings modal (operations/nodes.js), host delivery routing (routing.js — "Роутинг хостов" lives under the Операции nav section) |
| `domain-payments-support.css` | Product picker, payment-row status, ADMIN_GRANT marker, ticket workflow, Telegram Stars |
| `domain-technical.css` | Raw Marzban user management + global/per-user configs + inbound extras |
| `domain-dashboard.css` | Dashboard-exclusive rules (sys-stats online counter, node list/traffic rows) |
| `domain-settings.css` | Settings page ("Система" nav section: bot/support/general config forms) |

**Why 9, not the original plan's 8:** the approved plan named 8 target
files. Wave 9 found two more single-item, distinctly-nav-labeled screens
(Dashboard, Settings) with real exclusive CSS and no honest home in any of
the 8 — forcing their rules into an unrelated domain file would have
misrepresented ownership the same way the old `legacy.css` did. Both got a
minimal, justified ninth/tenth file each, following the same
one-file-per-screen pattern as the rest, rather than being left inline-
styled or dumped into a mismatched domain.

**The transitional `legacy.css`** (introduced in Wave 0 specifically to
avoid a big-bang rewrite) has been **deleted**. Every rule still in use as
of Wave 8 was re-homed to its correct owner above; the handful with zero
consumers anywhere in the app (`.small-value`, `.technical-row`,
`.code-wrap`) were removed after confirming via git history and a full
codebase grep (both static `class="..."` and dynamic
`badge-${...}`/`.className=`/`classList.*` construction) that they predated
this redesign and were never referenced.

## 2. Design tokens (categories, `tokens.css`)

Surfaces (`--bg`/`--bg2`/`--bg3`/`--bg4`), text hierarchy
(`--text`/`--text2`/`--text3`), borders (`--border`/`--border2`), semantic
state colors (green/red/amber + one new: `.badge-purple` for the
ADMIN_GRANT classification, added in `domain-payments-support.css` since
it's a non-status, non-decorative classification, not a repurposed
success/warning/error color), plus a `prefers-reduced-motion` baseline in
`base.css`. No new token *category* was added this redesign beyond what
the original file had — the fixes were to consistency of usage, not the
token model itself.

## 3. Component consolidation (summary)

- **kv-grid**: `.detail-grid`, `.technical-fields`/`.technical-field`,
  `.compact-list`/`.compact-row` consolidated into `.kv-grid` (+
  `--copyable` modifier) in `components.css`. `.ops-dl` and
  `.user-detail-grid` were kept as separate names (genuinely different
  consumers/shapes) rather than force-merged.
- **Badges**: five independent `value -> badge-color` functions
  (`ops_health.js`, `technical/marzban_users.js`, `operations/nodes.js`,
  `promo_ops.js`'s inline ternary, and `routing.js`'s — found during the
  Wave 9 audit, see below) consolidated into `core.js` as the single
  source of truth (`healthBadgeClass`, `nodeImportanceBadgeClass`,
  `marzbanStatusBadgeClass`, `promoStatusBadgeClass`,
  `redemptionStatusBadgeClass`, `hostClassBadgeClass`), wired through each
  module's existing dependency-injection factory pattern.
- **Modal**: `.modal-overlay`/`.modal` and `.ops-modal-overlay`/`.ops-modal`
  share one visual contract (background/border/radius/shadow/sizing,
  `.modal--lg` size modifier) in `components.css`. Their JS lifecycles were
  **not** merged (`kernel.js`'s `closeModal`, `modals.js`'s
  `openModal`/`confirmFlow` are untouched) — a CSS-only unification was
  explicitly scoped that way after an earlier attempt at an open/close
  transition broke Playwright's visibility detection in
  `test_admin_browser_e2e.py`; that transition remains a follow-up item,
  not shipped.
- **Responsive tables** (per-table, not one blanket rule): Accounts list
  and Legacy Transitions queue get card-collapse below ~560px; Stars
  payments/orphans get priority-column-hiding (`.col-hide-mobile`); Marzban
  Users stays a compact `<table>` with its existing detail modal as the
  drill-in surface; Nodes stays card-based.

## 4. Real bugs found and fixed along the way

- Wave 0: the "Wave B / PH7-10" block hardcoded a light-theme palette
  disconnected from `:root` tokens (broke `.ops-form`, `.product-card`,
  `.queue-block`, `.tl-src-*` visually); `button.danger` was defined twice
  with conflicting colors.
- Wave 3: `.account-tabs` had zero CSS (a prior audit wrongly called it
  dead code and Wave 0 removed its only rule) — restored. `.grace-progress`
  had never had any CSS at all.
- Wave 5/7: `support/tickets.js` and `payments/stars_legacy.js` built
  status colors from raw inline hex (`_TICKET_STATUS_COLORS`,
  `_STARS_STATUS_COLORS`) instead of the shared badge system.
- Wave 8: `.ops-close` (the dynamic ops-modal's close button) had no CSS at
  all, rendering as a default bordered button instead of the icon-style
  close every static modal uses.
- Wave 9: `routing.js`'s `classBadge()` emitted `badge err`/`badge warn`/
  `badge ok` — none of those modifier classes exist anywhere in CSS (only
  `badge-green/red/amber/gray/blue` do), so the Routing page's host-
  classification badges rendered with zero color. Fixed via
  `core.js:hostClassBadgeClass()`.
- Wave 9: `.pay-status` (every payment row's amount+status wrapper) had no
  CSS rule at all.

## 5. Accessibility / contrast

`button.primary`'s text color was bumped from `#aaaaff` (4.19:1 on
`--accent2`, failing WCAG AA's 4.5:1 for normal text) to `#b3b3ff`
(4.57:1). A targeted audit of every `var(--text3)` usage bumped the
selectors that render genuinely significant operator-read data —
`.cell-sub`, `.stat-sub`, `.account-subtitle`, `.config-uri-text`,
`.puconfig-uri`, `.nf-group-host` (a real node IP), `.node-traffic-sub`,
`.node-billing-note`, `.ticket-message-meta`, `.ticket-col-created`,
`.stars-reason-cell`, `.node-usage-pct` — to `var(--text2)` (≥4.59:1 on
every surface token used). Purely decorative/structural uses of
`--text3` (section eyebrows, card titles, empty-state placeholders,
loading text, uppercase nav-section labels) were left as-is; the
`--text3` token itself was not changed, since it's used in dozens of
places app-wide and retuning it is a broader density/visual-weight
judgment call, not a point fix.

## 6. Known, intentionally-not-fixed items

- **`button.success`** (the green button variant) has zero consumers
  anywhere in the app — confirmed via full static + dynamic grep. Not
  removed: it's a whole variant in the original 4-color button system
  (default/primary/danger/success), plausibly an intentionally-designed-
  but-currently-unwired capability rather than incidental dead weight.
  Flag for product confirmation before deleting.
- **Modal open/close transition**: deferred (see section 3 above) —
  requires a JS-side delay before removal/hide to animate the exit, which
  touches `modals.js`'s lifecycle; out of scope for a CSS-only pass.
- Data-driven inline `style="width:${pct}%"` bindings (4 total, in
  `accounts.js`'s grace/campaign progress bars,
  `technical/marzban_users.js`'s per-node usage bars, and
  `app/router.js`'s dashboard node-traffic bars) are intentionally kept
  inline — they're computed percentages, not static layout choices, the
  same class of exception the rest of the app's progress bars already
  used before this redesign.

## 7. Verification performed for Wave 9 (final)

- Brace-balance + full selector-inventory diff across all 10 CSS files
  before/after every change (nothing lost except the 3 confirmed-dead
  classes above).
- Two-directional class audit: every statically-used class (incl.
  `classList.add/toggle/remove` and `.className=` assignment patterns)
  cross-referenced against every defined selector, and vice versa, with
  every dynamically-constructed (`${...}`) class expression manually
  traced to its source.
- `node --check` on every touched JS file.
- Full project `pytest` regression (not just admin-scoped).
- A real headless-Chromium Playwright walk (not the existing fixture
  tests, a purpose-built script) through all 12 top-level pages, all 8
  Account workspace tabs, and a 375px-viewport check confirming the
  Accounts list card-collapse actually renders — zero console errors,
  zero literal `"undefined"` text anywhere.
