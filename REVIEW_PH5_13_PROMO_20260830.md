# PH5-13 Promo Codes — Review of 5 commits since baseline (43d27f1)

Range: `7697b0c..f71618a` (5 commits). Effort: code-review `high`.

## Findings, ranked by severity

### 1. Double-spend / race: uncommitted transaction left open on CAS loss
**`src/stars_purchase.py:259`**
When the pre-checkout CAS in `commit_reservation_locked` (`src/promo.py:175-187`) matches 0 rows (e.g. the TTL sweeper cancelled the reservation just before a delayed `pre_checkout` arrives), `PromoConflict` is raised and re-raised as `StarsPurchaseError` without ever calling `self._conn.rollback()`. Since `Database` uses ONE shared sqlite3 connection (`check_same_thread=False`) across every store, the connection is left in an open, uncommitted transaction. The next write anywhere in the app that issues `BEGIN IMMEDIATE` raises `sqlite3.OperationalError: cannot start a transaction within a transaction`, breaking invoice creation, promo redemption, and the sweeper itself.

### 2. `cb_stars_buy` never wired to promo reservation
**`src/bot_support.py:1228`**
`cb_stars_buy` ("⭐️ Продлить подписку") was not updated to read `promo_reservation_id` from FSM state or pass `promo_redemption_id` to `create_invoice`, unlike the sibling `cb_buy_pay` handler which this diff did update. The bot's own reply after a successful reserve says "Выберите «⭐️ Продлить подписку»" — following that instruction creates a full-price invoice, the reservation is silently never bound/consumed, and it still counts against `per_user_limit` (RESERVED, non-CANCELLED) until the TTL sweeper cancels it up to an hour later.

### 3. Per-user limit race for `per_user_limit > 1`
**`src/promo.py:523` and `:636`**
The per-user redemption-limit check (`SELECT COUNT(*) ...; if prior >= per_user_limit: raise`) runs before `with self._lock:` is acquired. The partial unique index `ux_promo_single_use_user` only backstops `per_user_limit_snapshot = 1` (confirmed by the code's own comment). Two near-simultaneous redemption requests (bot + LK, distinct idempotency keys) for a `per_user_limit=2` promo can both read `prior=1` before either inserts, both pass, both commit — exceeding the configured limit. Same unsynchronized pattern duplicated in `reserve_purchase_for_telegram_user` (line 636-642).

### 4. FSM state not reset on promo failure paths
**`src/bot_support.py:1311`** (and `1269-1277`)
Every failure branch of `_promo_redeem_message` (NotFound/Conflict/Ineligible/Error/generic) and `_promo_reserve_for_purchase` returns without resetting FSM state back to `SupportStates.in_dialog` — only success paths do. The handler is registered with no text filter, so after one failed attempt any subsequent message (including tapping "🛒 Купить VPN" or "⭐️ Продлить подписку") gets captured as a promo-code attempt and fails again. User is stuck until they find "⬅️ Назад к боту".

### 5. Unlocked read before transaction start
**`src/stars_purchase.py:97`**
`_resolve_promo_discount_locked` (and `_reservation_row_locked`) reads the promo reservation row before `with self._lock:`/`BEGIN IMMEDIATE` are established in `create_invoice`, violating the `_RedemptionTxMixin` methods' own documented precondition. On the shared single connection this can observe another thread's uncommitted write (no statement-level snapshot isolation within one connection). The final CAS in `bind_reservation_locked` re-validates atomically, so this mostly surfaces as a spurious rejection under contention rather than corruption — but it's a real deviation from the stated concurrency contract.

### 6. Immutability trigger gap for NULL→non-NULL snapshot writes
**`src/promo_schema.py:176`**
`trg_stars_invoices_promo_snapshot_immutable`'s `WHEN OLD.promo_redemption_id IS NOT NULL` guard means the trigger only fires once a discount snapshot already exists. A first `UPDATE ... WHERE promo_redemption_id IS NULL` that attaches `promo_redemption_id`/`original_stars_price`/`discount_minor` is completely unguarded. Latent today (only INSERT-time writes exist), but any future backfill or the still-unwired MANUAL_RUB discount-binding path (see below) could forge a discount onto an existing invoice with no DB-level guard.

### 7. Percent-discount floor bypassed at catalog_price == 0
**`src/promo.py:131`**
`_discount_from_effect_params`'s percent branch: `return max(1, ...) if catalog_price > 1 else int(catalog_price)` — for `catalog_price == 0` this returns `0`, contradicting the docstring's stated invariant ("final price clamped to >= 1 — a Stars invoice cannot be free"), inconsistent with the `discount_minor` branch two lines below which correctly floors to 1 in the same case. Low reachability today since catalog prices are enforced positive elsewhere, but the helper itself provides no such guarantee.

### 8. Cross-module reach into PromoStore internals
**`src/stars_purchase.py:152`**
`StarsPurchaseStore` calls `PromoStore`'s underscore-prefixed methods directly (`bind_reservation_locked`, `commit_reservation_locked`, `redeem_reservation_locked`, `_reservation_row_locked` at line 289) instead of through a typed/public interface. A future refactor of PromoStore's row shape or locking contract has no import-time signal that `stars_purchase.py` depends on it.

### 9. Admin redemptions view: hand-rolled SQL misses new columns
**`src/routes/admin_promo.py:129`**
`handle_admin_promo_redemptions` hand-writes a raw SQL JOIN against `db._conn` instead of calling a PromoStore method, contradicting the module's own docstring ("No new domain logic here — pure route-layer wiring"). The same diff adds `bound_kind`/`bound_invoice_id` columns to `mgboost_promo_redemptions` for the PURCHASE_DISCOUNT lifecycle, but this SELECT's column list omits them — the admin support view can't show which invoice a PURCHASE_DISCOUNT reservation is bound to, which is exactly what support needs to diagnose a stuck reservation.

---

## Not yet independently verified against this diff (flagged, still open)
Items from the original review scope not covered above because the background review focused on the crash/race and idempotency surface — still need a pass:
- **WL_TRIAL → subsequent regular-plan purchase** interaction and the WL-period anchor (`max(wl_max_ends_at, current_expiry, now)`) correctness under the new PURCHASE_DISCOUNT flow.
- **Stars pre_checkout → payment capture** full path beyond the CAS-loss/rollback bug in #1.
- **cleanup/sweeper** behavior on stuck COMMITTED (not just RESERVED) rows.
- **MANUAL_RUB discount wiring**: confirmed absent from this diff (`src/promo.py`, `src/routes/admin_promo.py` have no MANUAL_RUB binding path) — needs a check of `AGENT_HANDOFF.md`/`ROADMAP.md`/UI copy to confirm neither claims it's implemented.
- **Seed WL_TRIAL as manual step vs. idempotent migration/startup**: needs an explicit decision — not resolved by this diff.

---

## Throne client — what it would take to get it "working"

Current state (verified against prod `mgboost.ddns.net`, 2026-08-30):

- **`src/compat_registry.py`** has zero entries for Throne — not `SUPPORTED`, not `UNSUPPORTED_MISSING_HWID`. Any Throne request classifies as `UNKNOWN`.
- **Live rolling telemetry** (`mgboost_hwid_compat_subjects`, ~5-day retention window, 2026-08-24…08-29): no Throne rows at all in this window — inconclusive, not evidence of anything.
- **Historical `sub_requests` table** (not rotated) has 5 real Throne observations, April–July 2026, versions 1.0.11 → 1.2.1: every single one has a non-empty `fingerprint` field (16 chars) but `device_id` is NULL and `platform`/`os` are both empty in every row.

What's blocking it, concretely:

1. **No live evidence through the current pipeline.** The historical `fingerprint` values are from an older ingestion path (`sub_requests`) and there's no confirmation they map into the current `device_headers.py` `HEADER_ALIASES` set feeding `mgboost_hwid_compat_subjects`. Need a fresh live/controlled request from an actual Throne client to see which header it sends and whether `extract_device_metadata()` recognizes it as a valid HWID candidate under `_SUPPORTED_HWID_RE`.
2. **`platform` is never populated for Throne in any observed row.** `compat_registry.classify()` requires an exact `(client, platform)` match (only `version` got the min-baseline loosening on 2026-08-29) — with platform empty/missing, no `(client, platform)` pair can ever be added, since there is nothing to key the record on. Need to find why platform isn't captured for Throne specifically (check its actual headers/UA against `_parse_user_agent` in `device_headers.py` — it may use a platform-signaling header/UA pattern the current alias list doesn't cover, similar to the Happ-specific UA heuristic already carved out there).
3. **Once (1) and (2) are resolved**, add a reviewed `CompatibilityRecord("throne", "<version>", "<platform>", SUPPORTED, "ORGANIC_LIVE"|"CONTROLLED", "<date>", "<evidence note>")` to `_REGISTRY` in `src/compat_registry.py`, bump `REGISTRY_VERSION`, and add a corresponding case to `tests/test_compat_registry.py` — following the exact pattern used for happ/v2raytun/incy on 2026-08-25.
4. **Separately note:** `src/hwid_gate.py` (the thing that would actually enforce this registry) is dormant — no route imports it. So even a correct Throne registry entry changes nothing in production behavior until the gate itself is wired into a real route. If the goal is "Throne users are not blocked," that's already true today (gate isn't enforced anywhere); if the goal is "Throne is enforced as SUPPORTED once the gate goes live," steps 1-3 above are the prerequisite.

---

## Closure update — 2026-08-30

1. **FIXED:** `validate_invoice_for_checkout` теперь владеет явным `BEGIN
   IMMEDIATE` и на любом exception выполняет rollback. Regression моделирует
   `RESERVED → CANCELLED → delayed pre_checkout`, проверяет
   `connection.in_transaction == False` и успешную следующую write
   transaction.
2. **FIXED:** `cb_stars_buy` читает `promo_reservation_id` из FSM, передаёт
   его в `create_invoice` и очищает только после успешного invoice. Реальный
   callback fixture проверяет `promo_redemption_id` и immutable discounted
   snapshot.
3. **FIXED:** count и INSERT сериализованы одним `BEGIN IMMEDIATE`; тесты
   покрывают concurrent distinct keys для `per_user_limit=1` и `=2`.
4. **FIXED:** все ожидаемые promo failure branches возвращают FSM в
   `SupportStates.in_dialog` с `kb_main()`.
5. **FIXED:** promo reservation read перенесён внутрь lock +
   `BEGIN IMMEDIATE` invoice transaction.
6. **FIXED:** additive `ph5_13_promo_codes_v2_snapshot_immutable` заменяет
   trigger и защищает NULL→non-NULL; marker/checksum уже применённого v1 не
   переписывается. Есть production-like v1→v2 regression.
7. **FIXED:** percent helper теперь всегда применяет floor `max(1, ...)`,
   включая zero edge price.
8. **FIXED:** Stars использует явный public transaction-bound interface
   `purchase_reservation_locked`/`*_purchase_reservation_locked`, не private
   PromoStore internals.
9. **FIXED:** read-model перенесён в `PromoStore.list_recent_redemptions` и
   включает `bound_kind`/`bound_invoice_id`.

Открытые review-направления закрыты тестами существующей suite: WL anchor и
`WL_TRIAL` paid transition, duplicate/delayed Stars capture, sweeper и
COMMITTED terminality, deterministic bot/LK idempotency и request-hash
mismatch. `MANUAL_RUB` намеренно **не реализован** для PURCHASE_DISCOUNT:
v1 explicitly TELEGRAM_STARS-only в коде и документации. `WL_TRIAL` seed
теперь idempotent startup bootstrap с exact-shape verification.

Throne update: production feature flags `LEGACY_BRIDGE_ENABLED=1` и
`OPAQUE_SUBSCRIPTION_ENABLED=1`; поэтому gate не dormant на фактическом
subscription path. Upstream Throne `dev` `HTTPRequestHelper.cpp` подтверждает
`User-Agent: Throne/<version>`, `x-hwid`, `x-device-os`, `x-ver-os`,
`x-device-model` при enabled setting. Parser теперь принимает exact format и
derived platform only из exact known `x-device-os`; registry остаётся
`UNKNOWN` до controlled/live request, поэтому статус Throne — SAFE-DEFER,
не SUPPORTED.
