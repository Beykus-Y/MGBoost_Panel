# Changelog

Здесь фиксируются пользовательски и операционно заметные изменения MGBoost Panel.

Формат основан на секции `Unreleased` и категориях Added, Changed, Deprecated, Removed, Fixed, Security, Operations и Documentation. При release записи переносятся под версию и дату, но не удаляются.

## Правила

- Changelog обновляется в том же commit/PR, что и изменение.
- Запись является частью Definition of Done.
- Обязательно отражать тарифы/цены, billing, WL, devices, subscriptions/migration, auth/session, admin, API/clients, purchase/renewal, reseller catalog/capabilities/billing/migration, Marzban и production behavior.
- Security entry описывает эффект исправления без рабочих exploit payload и секретов.
- Internal refactor без наблюдаемого эффекта можно не включать, если он не меняет security/operations/API.
- Не добавлять задним числом «когда-нибудь»: незавершённое остаётся в `ROADMAP.md`, а не в changelog.

## Unreleased

### Added

- Bot UX redesign, slice C1-C2 (implemented locally, NO push/deploy):
  единая карточка подписки «📱 Моя подписка» в боте на новом read-only
  слое `src/entitlement_read_model.py` (композиция существующих сторов:
  entitlement engine → тариф/статус/срок, `DeviceSlotStore.get_capacity_state`
  → устройства X/Y из canonical slot-модели, WL-остаток «WL: X / Y GB ·
  текущий период до DD.MM» из `wl.current_period` — без слова «сброс»,
  статус opaque-ссылки). Карточка едина для canonical и legacy-когорт
  (legacy — через `tg_users` + живой Marzban + legacy-счётчик устройств);
  русские статусы вместо сырых `ACTIVE`/`EXPIRED` (перевод — в UI-слое,
  правило отображения границы WL-периода единое, OPD-16). Inline-кнопки
  карточки: «🔗 Ссылка» (только canonical; тело совпадает с /newsub —
  перевыпуск по-прежнему через явный 2-шаговый confirm, /newsub остаётся
  скрытым алиасом), «➕ Продлить», «💻 Устройства», «🔄 Обновить» (edit
  собственного сообщения). Главное меню сжато до 5 кнопок
  (📱 Моя подписка · 🛒 Купить / Продлить · 💻 Устройства · 🎟 Промокод ·
  🆘 Поддержка); старые надписи кнопок работают как алиасы (Telegram
  кэширует reply-клавиатуры на клиенте). /start для существующего
  пользователя показывает карточку-приветствие; для нового — выбор
  «🛒 Выбрать тариф / У меня уже есть подписка».
- Bot UX redesign, slice C3-C7 (implemented locally, NO push/deploy):
  покупка и продление объединены в одну воронку «🛒 Купить / Продлить»
  (старые `kb_tariffs`/`cb_stars_buy`/`msg_stars_menu` удалены);
  canonical-аккаунту предлагается ТОЛЬКО его текущий тариф (backend всё
  равно не оформит другой — `PlanChangeRequired`, PH5-06), кнопка
  «🔄 Сменить тариф» ведёт на экран поддержки; настоящий Back
  (`buy_back_plan`, «⬅️ К тарифам») вместо бывшей кнопки «Назад»,
  отменявшей покупку. Owner rule: legacy-привязанному клиенту без
  canonical-аккаунта тихая вторая CANONICAL_SIGNUP-подписка запрещена —
  guard в `msg_buy_vpn`/`cb_buy_pay` ведёт в поддержку. «💻 Устройства»:
  canonical — счётчик через `DeviceSlotStore` (read-only), web-LK ссылка
  canonical не обещается (LK пока работает на legacy-модели устройств) —
  только поддержка; legacy — счётчик + одноразовая mgmt-ссылка как раньше.
  «🆘 Поддержка» — единая точка входа (ассистент + явная кнопка оператора);
  закрытие тикета оператором теперь сбрасывает FSM в `in_dialog` с kb_main
  (раньше пользователь навсегда оставался в `waiting_human`, и его
  сообщения терялись). Сообщения после оплаты несут кнопку «📱 Моя
  подписка». Промокод: выход по «❌ Отмена», после скидки — кнопка
  «🛒 Открыть покупку»; stale «❌ Отмена»/«⬅️ Назад к боту» в диалоге
  синхронизируют клавиатуру вместо утечки в AI-тикеты. Промо-резерв
  больше не теряется на входе в воронку покупки (state.clear убран).
- PH5-13 PURCHASE_DISCOUNT (implemented locally, NO push/deploy): машина
  резервации `RESERVED → COMMITTED → REDEEMED` с терминальным `CANCELLED`.
  Резерв — `PromoStore.reserve_purchase_for_telegram_user` (durable,
  `reserved_until`, per-user limit действует и на резерв). Привязка к
  инвойсу — внутри транзакции `create_invoice` (immutable снапшот скидки:
  `promo_redemption_id`/`original_stars_price`/`discount_minor` на
  `stars_invoices`, триггер неизменяемости + unique index); итоговая цена
  инвойса дисконтирована (floor 1 ⭐). Гейт COMMITTED — на pre_checkout
  (CAS; проигранная гонка → `ok=False`, деньги не списываются); REDEEM —
  в той же транзакции, что и перевод инвойса в `paid` (одна оплата = ровно
  одна redemption). TTL-cleanup (`release_expired_reservations` в воркере
  Stars) снимает только unbound-резервы past-TTL и привязанные к
  канонически неоплатимым инвойсам; COMMITTED не трогает никогда —
  финансовый double-spend race закрыт по построению. Бот: промокод со
  скидкой резервируется из диалога и применяется к следующему счёту
  «⭐️ Продлить подписку».
- PH5-13 admin UI промокодов (implemented locally, NO push/deploy): backend
  `src/routes/admin_promo.py` — `POST/GET /admin/promo/definitions`,
  `POST /admin/promo/definitions/<code>/disable`,
  `GET /admin/promo/redemptions` (все за `require_admin_auth` + primary
  capability, bounded-валидация); фронт `frontend/assets/admin/promo_ops.js`
  — модалка-менеджер (кнопка «Промокоды…» на странице аккаунтов): список
  definitions с созданием/отключением и последние redemptions для саппорта
  (включая CANCELLED резервы).
- PH5-13 Promo codes v1, self-service redemption slice (implemented locally,
  NO push/deploy): новый user-scoped `PromoStore.redeem_for_telegram_user`
  (без primary-admin capability — действующий принципал это PROVEN Telegram
  OWNER identity; только существующие аккаунты, только эффекты через
  `append_promo_wl_period`: TRIAL_GRANT и EXTEND на WL/LIMITED; STANDARD/NONE
  продление и авто-создание аккаунта остаются support-флоу); ingress — кнопка
  «🎟 Ввести промокод» и FSM-флоу в боте, `POST /lk/api/promo/redeem` в ЛК
  (гейт `_require_mgmt_session`, обязательный клиентский `request_id`).
  Идемпотентность детерминирована событием: бот —
  `promo-redeem-v1:{chat_id}:{message_id}` (повторная доставка Telegram
  реплеит ту же redemption), ЛК — `promo-redeem-v1:lk:{tg}:{request_id}`;
  сервер никогда не генерирует ключ сам.
- `per_user_limit` на promo definitions (снимок `per_user_limit_snapshot` в
  redemptions): повторное применение single-use кода тем же пользователем —
  `PromoConflict`; race-backstop — partial unique index
  `ux_promo_single_use_user` (SQLite-индекс без JOIN, снимок в строке).

### Fixed

- Домен opaque-ссылки подписки больше не хардкодится: доставка в боте
  (`stars.py`, `/newsub`), выдача в LK (`/lk/api/opaque-subscription/issue`)
  строят URL из `PUBLIC_HOST` через `config.subscription_base_url()`;
  без настроенного `PUBLIC_HOST` доставка/выдача fail-closed (у LK — 503,
  у бота — администраторский алерт, activation не выполняется).
- Дублирование форматирования WL-квоты устранено: единый
  `bot_support._wl_quota_text` для экранов тарифов и описания инвойса
  (тексты идентичны прежним, расхождение исключено по построению).
- (retro за 7697b0c) Якорь промо WL-периода
  `max(MAX(wl_periods.ends_at), subscription.current_expiry, now)` — промо
  больше не может воткнуть период внутрь уже оплаченного срока после
  расхождения от ADMIN_EXPIRY_ADJUSTMENT; PlanMismatch-исключение для
  истёкшего бесплатного плана сужено строго до `plan_code == 'WL_TRIAL'`.
- Схема PH5-13 расширена ДО деплоя (миграция ещё не применялась в проде):
  `per_user_limit`/`per_user_limit_snapshot`, статус `COMMITTED` задел под
  PURCHASE_DISCOUNT slice.


### Added

- Commercial WL wiring (2026-08-28, implemented locally, checkpoint only —
  NO push/deploy, реальная покупка не выполнялась): тарифы WL 199/349⭐,
  Расширенный 249/399⭐ и Семейный 299/449⭐ (30/60 дней, device limit
  3/6/12, quota 100/150/150 GB на каждый 30-дневный период) стали
  purchasable через существующий canonical Telegram Stars signup/renewal
  flow — без нового payment flow. 30d = один immutable WL period, 60d =
  ровно два последовательных периода по полной quota (remainder не
  переносится, история не мутируется); delivery LIMITED-аккаунта =
  STANDARD + точная PH0-05 WL topology (посредством per-account
  `tpl-<public_id>` шаблона; BASIC остаётся STANDARD-only с нулевым
  WL-пересечением); packages (`WL_PACKAGE_*`) остаются server-side
  непокупаемыми (PH6-08 отсутствует); upgrade/downgrade по-прежнему
  контролируемый отказ (PH5-06). Минимальное расширение PH6-06: первый
  INCLUDED-оп свежего LIMITED-аккаунта выводит baseline из pinned
  provisioning-шаблона (hash-verify + allowlist-фильтр, fail closed)
  вместо прежнего `NO_BASELINE_FOR_INCLUDE`. UI-каталог показывает
  квоту per-period («100 GB каждые 30 дней (2 периода по 100 GB)»),
  никогда как удвоенный общий лимит. Новый suite
  `tests/test_commercial_wl_wiring.py` (27 тестов, включая сквозной
  runtime-путь convergence/disable/restore и dispatcher successful_payment
  path); обновлены тесты, пинившие прежний 3-SKU sellable gate.
- PH6-09 overshoot/outage fail-safe поверх production PH6-07 (2026-08-28,
  implemented locally, checkpoint only — NO push/deploy): new systemd-юниты
  `mgboost-wl-usage-collector.{service,timer}` (канонический PH6-03
  collector теперь реально работает по 10-минутному таймеру — раньше
  scheduler отсутствовал и enforcement читал устаревший ledger), freshness
  contract (`src/wl_freshness.py`, технический bound 1800 с, не SLA),
  access-increasing WL решения (restore, auto-add нового approved inbound)
  только при свежей telemetry/topology/entitlement — иначе fail closed;
  уже-ACTIVE пользователи при collector/node outage не массово отключаются
  (stale ledger может только under-count); DL-059: ACTIVE LIMITED child
  автоматически получает newly-approved exact WL inbound через существующий
  PH6-07 drift path с доказуемой scope-механикой (append-only
  `mgboost_wl_topology_versions` реестр версий); observability:
  `collector_freshness`, `overshoot_bounds`, per-cycle `ph6_09` счётчики
  в identifier-free read model. Headroom не введён (exact quota threshold);
  commercial overshoot/SLA значения — осознанно НЕ выбраны (owner STOP).

- PH5-11 first commercial STANDARD signup flow (2026-08-27, implemented
  locally, pending independent review + deploy; canary not started):
  «Купить VPN» в Telegram для новых клиентов — 3 non-WL тарифа
  (BASIC/BASIC_PLUS/BASIC_PRO) × 30/60 дней (ровно 6 SKU, цены только из
  активного immutable-каталога, подмена callback не влияет на
  plan/price), canonical Stars invoice, и только после подтверждённой
  оплаты — самостоятельное создание DIRECT-аккаунта (fill-once, ровно один
  аккаунт на Telegram-владельца при retry/duplicate), подписка через
  существующий PH5-02 renewal engine, инфраструктурный provisioning-шаблон
  (system-owned, без выдачи клиенту UUID/ссылки шаблона; anti-tamper
  source-contract verification сохранён полностью), выдача opaque-ссылки
  после применения платежа (потеря доставки восстановима, существующая
  ссылка не вращается), первый device получает собственного child-юзера с
  собственным UUID. Повторная покупка того же тарифа — renewal; другой
  тариф — контролируемый отказ (PH5-06 не начат).
- PH5-12 operational delivery routing (2026-08-27, implemented locally,
  pending independent review + deploy): план → delivery profile → host
  membership как отдельная operational-конфигурация (не тариф-версия):
  смена хостов не требует перепокупки. Admin-страница «Роутинг хостов»
  показывает живые inbounds с exact PH0-05 классификацией и управляет
  STANDARD membership (auth + CSRF + primary-admin capability + reason +
  idempotency + CAS; stale update = 409; всё аудируется в append-only
  ledger). Backend-гарантия: exact-WL хост структурно не может попасть в
  STANDARD (три независимых слоя — профиль, пиннинг шаблона, render-
  boundary; corrupted state → fail-closed MANUAL_REVIEW/ERROR, никогда не
  partial-выдача).
  **Независимый review этого среза (2026-08-28): APPROVED WITH FIXES для
  найденных implementation-дефектов; deploy BLOCKED отдельным
  архитектурным вопросом (`tpl-<public_id>`, см. ниже) — требуется owner
  decision, самостоятельно не решался.**
  Найдены и исправлены реальные дефекты (не гипотетические):
  P0 — `src/bot_support.py::on_pre_checkout`/`on_successful_payment`
  проверяли только `invoice_kind == "CANONICAL_PLAN"`, поэтому реальный
  Telegram-платёж нового клиента (`CANONICAL_SIGNUP`) либо отклонялся на
  pre-checkout (несуществующий `signup-<tg_id>` Marzban-юзер), либо (если
  бы прошёл) уходил через legacy `mark_invoice_paid` мимо `capture_paid`,
  никогда не создавая аккаунт — вся коммерческая покупка была нерабочей
  end-to-end несмотря на зелёные store-level тесты; воспроизведено и
  закрыто регресс-тестами, дублирующими реальный `on_pre_checkout`/
  `on_successful_payment` диспатч. P1 — `CommercialSignupStore.
  ensure_signup_account` вызывал `link_telegram_owner` ПОСЛЕ освобождения
  общего lock; два разных signup-инвойса одного нового Telegram-плательщика
  могли создать два независимых orphan-аккаунта (детерминированный
  repro-тест `test_owner_link_lock_scope_prevents_orphan_account_race`).
  P1 — `scripts/seed_delivery_routing.py` изначально содержал захардкоженный
  список из 13 тегов ("STANDARD = эти теги, потому что 27.08 их было 13") —
  переписан на честный live-topology-minus-exact-WL вывод с fail-closed
  topology-assertion; regression-тест включает свежий тег, которого не было
  в старом списке. P2 — провал доставки первого opaque-credential не
  уведомлял ни клиента, ни админа (только тихий лог); добавлен admin-алерт,
  зеркалящий существующий паттерн для `OPAQUE_SUBSCRIPTION_ENABLED=off`.
  P3 — тест `test_first_rollout_purchase_gate_rejects_non_standard_plans`
  проходил вхолостую из-за опечатки в kwarg (`plan=` вместо `plan_code=`),
  маскируя `TypeError` под ожидаемый reject; исправлен на точный
  `PlanNotSellable`. **Архитектурный вопрос "`tpl-<public_id>` —
  system-owned template или скрытый source-user-per-customer костыль"
  ОСТАЁТСЯ ОТКРЫТЫМ — два независимых review этого diff'а дали
  противоположные выводы, и ни один не принимает решение самостоятельно.**
  Один прочтения (Вариант A) указывает, что текущий (до PH5-11)
  `child_provisioning.py::prepare_child_ensure` требует, чтобы
  `source_alias_id` принадлежал ТОМУ ЖЕ `account_id` — с этим конкретным
  интерфейсом, без изменений, общий template невозможен. Другое прочтение
  (Вариант B) возражает: это ограничение защищает от кросс-tenant утечки
  ДИФФЕРЕНЦИРОВАННОГО контента (например, WL-инбаунды одного legacy-клиента
  не должны клонироваться в child другого) — а не различающийся,
  одинаковый для всех STANDARD-клиентов system-template этому risk-модели
  не подвержен в принципе, поэтому требование 1:1 — не security-необходимость,
  а следствие переиспользования существующей per-account alias-таблицы
  вместо небольшого дополнения интерфейса (в духе уже существующего
  `system_actor` паттерна из `delivery_routing.py`). Дополнительный
  подтверждённый по коду факт: `close_account()`
  (`src/account_consolidation.py`) не знает о `mgboost_provisioning_
  templates` вообще — при закрытии/consolidation аккаунта (уже реальная
  операция, см. DL-057) per-account `tpl-<public_id>` Marzban-пользователь
  остаётся бессрочно, без cleanup/reversal политики. **Решение — за
  владельцем; до тех пор production deploy этого среза не рекомендуется.**
  Полный regression: 1396 passed, 0 failed (после фиксов).
- PH6-06 exact inbound-only WL quota-enforcement state machine
  (2026-08-27, **independently reviewed, one real P0 found and fixed
  (`apply_decision` could orphan a sibling's still-open op on a
  same-direction late arrival mid-transition, masking partial success as a
  terminal state), production-deployed application-code-only; dormant, no
  scheduler/route/UI wiring**): per-account
  `ACTIVE -> DISABLE_PENDING -> DISABLED` /
  `DISABLED -> ENABLE_PENDING -> ACTIVE` /
  `ERROR_RECONCILE` machine over a new additive checksum-pinned schema
  (`mgboost_wl_enforcement_states`/`_ops`/`_events`), epoch-gated so a
  superseded desired state can never be dispatched. Desired state is
  derived only from the PH6-04 shared-pool read model over the PH6-03
  ledger (LIMITED exceeded -> remove; LIMITED available -> restore;
  Non-WL/UNLIMITED structurally abstain). Remote effect is one new narrow
  typed broker operation `child.user.wl.set`: reread -> UUID-verifier
  fail-closed -> minimal partial update of ONLY the child's own
  `inbounds.vless` member list (exact PH0-05 WL tags removed/restored;
  non-WL hosts, proxies/UUID/expire/data_limit never touched) ->
  reread/verify byte-stability. Exactly-once behavior is observational
  (replays settle `ALREADY_IN_SYNC` with zero writes), manifest-frozen
  first-writer-wins for crash/restart replay safety, bounded retries with
  `ERROR_RECONCILE` fail-closed recovery, fresh PH6-01 topology assertion
  gating every destructive pass. Operator entrypoint:
  `python -m scripts.run_wl_quota_enforcement` (on-demand only).

- PH7-13 account consolidation / merge-supersession primitive, DL-057
  (2026-08-27, **production-deployed and executed 2026-08-27, fast-forwarded
  to `d5ed3b7`**): a minimal, additive canonical primitive for merging two
  already-independent
  parent accounts that turn out to be the same real person (the concrete
  case: `MegochelPC` + `MegochelAndroid` -> `Megochel`), since neither
  `mgboost_legacy_alias_groups` (one alias group per account, set once at
  bootstrap) nor any existing store supports reassigning history across
  accounts -- every account-scoped table's identity, including
  `legacy_username`, is immutable by design. New checksum-pinned schema
  `mgboost_account_merges`/`mgboost_account_merge_events` (append-only,
  event-sourced `ACTIVE`/`REVERSED` state -- reversal is a new event plus a
  CAS status flip, never a `DELETE`, and never resurrects the absorbed
  account's revoked child/generation) and `mgboost_account_display_names`
  (a purely cosmetic owner-set label, modeled like
  `mgboost_telegram_identities`'s revoke-and-reinsert pattern, unrelated to
  any legacy alias). New `src/account_consolidation.py`:
  `resolve_account_id()` (the one shared canonicalizer), `create_merge()`/
  `reverse_merge()` (self-merge, chain/cycle -- strict permanent bipartition,
  bounded to depth 1 forever -- and conflicting-survivor rejection; replay-
  and concurrency-safe), `close_account()`/`reopen_account()` (fail-closed
  preconditions: no active Telegram OWNER identity, no non-terminal child
  intent, no ACTIVE device slot generation; cancels any live subscription
  with immutable evidence, in the correct order relative to
  `ProvenanceStore`'s own CLOSED-account write guard), `set_display_name()`.
  New `legacy_paid_compat.increase_device_limit()`: the one canonical way to
  raise an *already-provisioned* legacy-compat subscription's device limit
  in place (`ensure_legacy_paid_compat_entitlement()` only ever bootstraps a
  brand-new entitlement and hard-conflicts on any existing different plan --
  it has no upgrade path) -- changes only `current_plan_version_id`, never
  expiry/status/WL semantics, never a second subscription row, refuses any
  billed/commercial or non-`LEGACY_PAID_COMPAT_V1_D{n}` plan outright (PH5-06
  upgrade/downgrade territory, not implemented). Resolver-coverage audit
  beyond the obvious legacy-bridge path found and fixed a real gap:
  `legacy_grace_registration.bind_telegram_after_registration()`/
  `resolve_ambiguous_telegram_ownership()` resolved an alias's raw
  `account_id` and called `link_telegram_owner()` directly, which raises
  `AccountSchemaError` (uncaught by these functions) for a CLOSED absorbed
  account instead of the intended `IdentityConflict` -- both now
  canonicalize through `resolve_account_id()` first. `subscription_admin_ops`
  (PH7-01 expiry ops) gained an explicit CLOSED-account guard it was missing.
  `admin_read_models`/`accounts.js` show the owner-set `display_name` first,
  falling back to the existing note/primary-alias/public_id chain unchanged;
  account detail exposes a new `consolidation` block (absorbed-into /
  absorbs) for both sides of a merge. 34 new focused tests
  (`tests/test_account_consolidation.py`) plus full regression: `1298
  passed, 4 skipped` (was `1264 passed, 4 skipped` -- zero regressions).
  Independently re-verified `9edd42e` (already-implemented device-migration-
  status GLM bugfix, checked in this session): `classify_action()` now
  yields `WAITING_FIRST_DEVICE` for Telegram-`BOUND` + zero real-device
  lineage and `OK_MIGRATED` for any real `mgboost_migration_bindings`
  lineage regardless of Telegram status -- Telegram ownership and technical
  migration status are structurally independent inputs; not modified.
  **Production evidence:** fresh encrypted backup (`--verify` PASS)
  preceded the fast-forward deploy; `mgboost-panel` restarted (only service
  needed), schema applied automatically, `quick_check=ok`/zero FK
  violations. Executed against the real accounts 5 (`MegochelPC`, absorbed)
  and 6 (`MegochelAndroid`, survivor) via a new reviewed script
  (`scripts/dl057_megochel_consolidation.py`, hardcoded to exactly these two
  accounts, no raw SQL write) through the primitives only: real Marzban
  genesis-child `REVOKE`+`FREE` -> `close_account(5)` (subscription
  `CANCELLED`) -> `create_merge(5->6)` -> `set_display_name(6,'Megochel')`
  -> `increase_device_limit(6,+3)` (`LEGACY_PAID_COMPAT_V1_D6`). Two real
  bugs surfaced and fixed live during rollout (a 15-character
  `idempotency_key` one under the 16-minimum; a preflight check that
  couldn't resume once the already-applied `REVOKE` made the genesis child
  intent "terminal") -- both fixed and redeployed before a clean re-run;
  every retry was safe because the underlying primitives are independently
  idempotent (the real Marzban `REVOKE` was never re-rotated). Post-mutation
  verification confirmed: account 5 `CLOSED`/subscription `CANCELLED`/slot
  `RELEASED`+`FREE`/zero `ACTIVE` generations; exactly one `ACTIVE` merge
  row (`5->6`); account 6's Telegram identity, opaque credential and
  subscription id all byte-for-byte unchanged, now `display_name='Megochel'`
  and `LEGACY_PAID_COMPAT_V1_D6`; both legacy aliases (`MegochelPC`->5,
  `MegochelAndroid`->6) untouched; `resolve_account_for_legacy_username
  ('MegochelPC')` now returns 6; both real legacy Marzban users confirmed
  `active` with traffic still accruing, untouched; unrelated accounts/
  cardinalities unchanged; zero errors/5xx across the operation. Local =
  origin = production = `d5ed3b7`.
- Operational admin completion wave 2 (2026-08-27, **local checkpoint commit,
  pending independent review and production deploy -- production NOT touched,
  origin NOT pushed**): the two remaining operational-admin gaps are closed
  over existing primitives with no new engine and no schema migration.
  PH7-01 admin expiry operations: primary-admin preview + adjust routes over
  a new durable `SubscriptionAdminOpsStore` writer that mutates ONLY
  `mgboost_subscriptions.current_expiry` (optimistic CAS against concurrent
  Stars/manual renewals), reuses the DL-044 anchor for +N (an expired term
  resumes from now), supports -N / exact UTC date / end-now, refuses
  admin-granted UNLIMITED, leaves WL periods/terms/packages untouched, drives
  child convergence through the existing PH3-08 sync cycle and appends
  actor/reason/before-after evidence to the existing immutable mutations
  ledger. PH7-05 reversible device pause: new `DeviceSlotAdminStore`
  primitive (the only writer of the schema-blessed slot `DISABLED` state)
  with convergence decided from live state inside one transaction --
  Disable -> Enable -> Disable re-performs a real pause instead of replaying
  a prior result; pause narrows only its own child target in the revision-
  stamped PH3-08 enqueue so no parent transition can resurrect it; Enable
  restores the same generation/UUID; capacity still counts paused slots;
  Free-after-Revoke works on paused slots; Rebind starts its successor
  enabled; a per-slot `/sync` retry route converges crash windows. Mandatory
  reason + confirm on every dialog per DL-055 (owner instruction superseding
  ADMIN-UX-02's lighter note). Both families render in the existing Audit
  timeline via their ledger rows. UI stays account-centric vanilla ES modules:
  new `expiry_ops.js`, extended `device_ops.js`; CSP/no-inline/event-
  delegation gates green.

- Operational admin completion over existing primitives (2026-08-27,
  **independently reviewed, two real defects found and fixed
  (`1854bb9`), production-deployed same day**; PH7-10 `[x]`, PH7-05/PH7-08
  `[~]` -- Disable/Enable and the unified write-side audit remain future
  scope, not this slice): the account-centric admin panel becomes an actual
  operational tool without any new domain engine. Manual external payments
  (PH7-10) are fully wired to
  the already-deployed PH5-09/10 store through new primary-admin routes:
  a server-provided RUB-only catalog endpoint (fixed DL-040 prices are the
  only purchasable facts), a server preview computing same-plan purchasability,
  exact price and the DL-044 expected-new-expiry estimate, and create /
  audited pending-edit / cancel / resolve-review / apply / sync-retry routes;
  apply drives child convergence through the existing PH3-08
  `run_account_sync_cycle` exactly like PH5-05's Stars driver and returns
  old/new expiry plus the PH5-04 entitlement summary; other-plan targets and
  ineligible WL packages render explicitly blocked (`PLAN_SWITCH_REQUIRES_PH5_06`)
  instead of silently landing in MANUAL_REVIEW. Wave B device operations
  (PH7-05 slice) wire Revoke / Free / Rebind as three distinct DL-049 actions
  over the unchanged PH3-05 lifecycle with deterministic per-target idempotency
  keys (double-submit converges), broker-failure RETRY durability, server-side
  Free-after-Revoke ordering enforcement and a confirmed-compromise Rebind that
  creates a successor generation; per-slot availability is derived from the
  lifecycle tables into the account read model, while Disable/Enable stay
  honestly absent because no standalone slot-disable primitive exists yet.
  Telegram ownership rebind (OPD-39/DL-041) is exposed for ORDINARY and
  COMPROMISE modes with stale-owner CAS rejection and a COMPROMISE notice that
  the old opaque URL died. Account detail now carries the authoritative PH5-04
  entitlement block, recent canonical payments, legacy Stars summary, per-slot
  action availability and a unified read-only audit timeline
  (`src/admin_audit_timeline.py`) aggregated from the existing immutable event
  tables with structural secret scrubbing (raw identifiers remain Technical-tab
  only); Dashboard gained operator queues (pending / manual-review /
  child-sync payments + Stars manual review) deep-linking into account detail.
  Frontend stays vanilla ES modules: new `modals.js` two-step consequence
  dialogs, `payments.js`, `device_ops.js`, `timeline.js`. Security posture of
  every mutation: admin session + CSRF, sealed primary-admin capability,
  bounded bodies/strings/integers, IDOR-safe 404s, no trust in any client
  price/plan/account field, no generic delete route (test-asserted).
  Targeted tests `tests/test_admin_operational_admin.py` (24 after review's
  additions) cover the auth matrix, exact catalog prices, tamper rejections,
  duplicate/replay convergence, immutability after apply,
  drift→MANUAL_REVIEW→resolve, package grant, forever-reserved references
  (DL-054), compromise rotation and queue surfacing; browser CSP/XSS gate
  extended to the new tabs.

  **Independent review (2026-08-27) found and fixed two real defects before
  deploy, everything else (manual payments/child-sync mapping vs. the PH5-05
  precedent, ownership rebind CAS/COMPROMISE rotation, credential boundary,
  read-models, dashboard queues, HTTP exception mapping, frontend security)
  reviewed clean:**
  - **P0 fixed** — `src/routes/admin_devices.py`: the route-level
    `_existing_slot_op` guard matched the latest lifecycle op of a kind by
    `slot_number` alone instead of the current generation's
    `old_child_intent_id` (the underlying `ChildLifecycleStore._prepare`
    primitive was already correctly scoped). This let a REVOKE issued after
    an earlier generation on the same slot had already been revoked
    false-converge on that stale REVOKE row (`converged: true`/200) without
    ever touching the *current* generation — a false confirmation that an
    active device had been revoked — and separately made REBIND permanently
    refuse any second rebind of the same slot after the first one ever
    completed. Fixed by scoping the guard to the current intent; regression
    added for both scenarios.
  - **P2 fixed** — `src/admin_audit_timeline.py`: 7 of 8 SQL sections in
    `account_timeline()` had no exception guard (only manual payments did),
    so one anomalous evidence row could take down the whole account-detail
    page (not just the Audit tab), since `account_detail()` calls it
    unconditionally. Every section now degrades independently; regression
    added simulating a corrupted source.

  **Production deploy (2026-08-27):** fresh encrypted backup create/restore
  PASS; preflight `quick_check=ok`, 0 FK violations, cardinalities unchanged
  (accounts=18, subscriptions=18, manual payments=0, ownership rebinds=0);
  pushed reviewed HEAD `1854bb9` to `origin/main`; `git pull --ff-only` on
  production; `mgboost-panel` restart only (no schema migration in this
  slice); post-deploy invariants identical, zero errors in the panel journal,
  all 4 services active; unauthenticated `/admin/accounts`/`/admin/dashboard`
  still 401, bogus legacy `/sub` still 404, all 6 new/changed admin JS
  modules load 200; read-only direct-call verification against 5 real
  production accounts confirmed `account_detail`/`account_timeline`/
  `dashboard_summary` run without error and that `account_timeline` (unlike
  `account_detail`'s intentionally-technical payload) carries zero
  `mgc_`/`sha256:`/`hmac-sha256:`/`Bearer ` markers. No real manual payment,
  device mutation, ownership rebind or credential rotation was created.
- PH5-09 manual external-payment record + PH5-10 same-parent renewal
  (2026-08-27, `[x]`, independently reviewed and **production-deployed**):
  additive migration `ph5_09_manual_payment_v1`
  (`src/manual_payment_schema.py`, checksum-gated on exact PH3-01/PH5-01/
  PH3-09/PH5-03 parent schemas) adds four durable tables:
  `mgboost_manual_payment_records` (one row per manually confirmed RUB
  payment: immutable plan/version/duration/package snapshots, exact expected
  and recorded amount equality CHECK, currency locked to RUB, bounded
  payment method/external reference/comment, primary-admin actor,
  lifecycle `PENDING/APPLIED/CANCELLED/MANUAL_REVIEW`, unique
  idempotency-key hash and unique external reference),
  `mgboost_manual_payment_edits` (append-only before/after pending-edit /
  review-resolution / cancellation audit), `mgboost_manual_payment_
  applications` (single immutable link to the entitlement mutation) and
  `mgboost_manual_payment_sync_jobs` (PH3-08 hand-off for expiry-extending
  payments). SQLite triggers make APPLIED/CANCELLED records and all
  edit/application rows update/delete-proof. `src/manual_payment.py`
  `ManualPaymentStore` reuses -- never duplicates -- the established
  engines: price authority is only the versioned fixed RUB catalog (PH5-01
  plan prices / PH5-03 package prices, pinned by exact rows so a later
  catalog change can never reprice an in-flight record); application goes
  through PH5-02 `apply_same_plan_purchase` (`EXTERNAL_PAYMENT`/
  `MANUAL_PAYMENT`, DL-044 `max(current_expiry, now) + purchased_duration`
  on the SAME subscription/account row), packages through PH5-03
  `grant_paid_package` on a canonical CONFIRMED PH3-09 payment row, proof
  through PH5-04 calculation, child convergence through the existing PH3-08
  outbox. Pending records may be corrected with audited before/after until
  apply; applied facts are immutable at the DB level and have no hidden
  rewrite path (a compensating-operation contract does not exist yet).
  First rollout is deliberately dormant: no admin UI/route/bot wiring, no
  production payment. Focused tests: `tests/test_manual_payment_ph509.py`
  (33 checks) + `tests/test_manual_renewal_ph510.py` (14 checks incl.
  concurrent Stars+manual stacking, crash-recovery between commit and
  proof/replay, partial remote failure recovery, 0/1/3/12-child
  topologies). Independent review confirmed no second entitlement/renewal/
  sync engine, correct `amount_minor` unit convention (matches PH5-01/
  PH3-09's own whole-RUB precedent), and correct applied-immutability
  scoping. **DL-054** records the owner's resolution of one real product
  ambiguity the review found (undocumented `external_reference` permanent
  uniqueness after `CANCELLED`) -- current schema confirmed correct
  unchanged. Full non-browser regression: `1210 passed, 16 deselected`.
  Production: fresh encrypted backup create/restore `PASS`; all parent
  migration checksums verified byte-identical pre-deploy; fast-forward
  `a5c846b` -> `5cbee5c` (`mgboost-panel` restart only); migration
  self-applied and checksum-verified; 4 new tables + 6 immutability
  triggers present; `quick_check=ok`, 0 FK violations; cardinalities
  unchanged `18/18/0/0/0`; all 4 new tables `0` rows (no real manual
  payment created); legacy `stars_invoices` unchanged at `2`; all services
  active; zero new errors in the journal; safe HTTP smoke unchanged. Still
  dormant: no admin UI/route/bot wiring, no real manual payment exists.
- PH5-05 canonical Telegram Stars purchase + renewal (2026-08-27, `[x]`,
  production-deployed, commit `0d2e354`): new durable evidence chain
  (`mgboost_stars_payment_evidence`, `mgboost_stars_purchase_applications`,
  `mgboost_stars_purchase_sync_jobs`) makes a paid Stars charge, its single
  entitlement mutation and the later child-expiry sync independently
  durable and immutable. Legacy `stars_invoices` rows stay untouched
  expire-only snapshots -- a DB trigger blocks any legacy row from ever
  being reinterpreted as a canonical purchase. Child expiry sync goes
  through the existing PH3-08 outbox, not an inline call. Repeated/
  concurrent successful payments each add exactly one term; duplicate
  callbacks never double-grant. Focused tests: `56 passed`. Independently
  production-verified in a follow-up session (rate-limit handoff from the
  implementing session): production `HEAD=0d2e354` matches local/origin;
  real `data/db.sqlite3` `quick_check=ok`, 0 FK violations; migration
  checksum byte-identical to source; all 3 new tables/columns/triggers
  present; accounts/subscriptions/WL periods/package grants/refunds
  unchanged at `18/18/0/0/0`; legacy Stars invoices unchanged at 2 rows,
  both still `LEGACY_EXPIRE`; all 3 new canonical tables at 0 rows (no
  purchase flow live yet, no fictitious payment/application/grant record);
  all 4 services active, no error in the panel journal since its last
  restart; full non-browser regression re-confirmed `1163 passed, 15
  deselected`.
- PH5-04 deterministic entitlement engine (2026-08-27, `[x]`,
  production-deployed): `db.entitlements.calculate(account_id=..., now=...)`
  is one versioned (`ph5-04-entitlement-v1`), side-effect-free SQLite-snapshot
  calculation for effective subscription expiry/status, immutable real
  plan/version, 3/6/12 or explicit INTERNAL device terms, WL access/current
  period/base quota, actual PH6-03/04 usage, PH5-03 FIFO package remainder
  and active durable overrides. It reuses existing canonical stores instead
  of creating another accounting path; package eligibility remains the real
  active commercial WL plan, so Base plus `FORCE_ENABLED` never gains billing
  or package rights. Slot add-ons and adjustments intentionally return
  `NONE`/`0` until PH5-07/PH6-08 have durable state. No schema change,
  purchase/Stars wiring, enforcement or Marzban/network call from calculation.
  Focused related tests: `98 passed`; full regression: `1161 passed, 3
  skipped`. Production pre/post DB invariants stayed subscriptions/WL
  periods/package grants/package refunds `18/0/0/0`, with `quick_check=ok`
  and 0 FK violations; all 18 account calculations completed read-only and
  only `mgboost-panel` restarted.
- PH5-03 versioned WL package catalog (2026-08-27, `[x]`, production-deployed): immutable +50/+100/+250/+500 GB package products and
  exact Stars 79/149/349/599 plus RUB 139/249/579/999 prices reuse PH5-01's
  channel catalog versions. Paid grants snapshot product/catalog/price and
  reuse PH3-09 payment/mutation provenance. Package remainder is derived
  solely from canonical PH6-03 parent usage: current-period base quota first,
  then owner-approved FIFO `granted_at,id`; lapse/Base state freezes rather
  than removes it. Unused-only refund atomically revokes a zero-consumption
  bucket and appends immutable refund evidence. The schema/explicit seed are
  dormant: no Stars flow, sales route, UI, scheduler or enforcement is wired.
  Additive deploy passed fresh encrypted backup/restore and DB integrity
  gates; explicit seed created 4 products/8 prices and re-run was idempotent,
  while grants/refunds stay empty and all existing account/subscription/WL
  cardinalities remain unchanged. Full regression: `1147 passed, 0 skipped`.
- PH6-04 default shared parent WL pool (2026-08-27, `[x]`, production-deployed, accounting/read model only): `src/wl_parent_pool.py` sums the already-durable, already-deduplicated PH6-03 `mgboost_wl_usage_samples` ledger by `(account_id, wl_period_id)`, filtered to the exact PH0-05 WL node allowlist -- WL quota already belongs to the parent account, not any individual device, so every child (ACTIVE or historical/revoked) that ever recorded a sample against a period contributes to that period's pool, and a revoked/rebound generation's already-consumed current-period traffic is never lost. No new schema/table -- a pure SUM read model, one accounting path, matching the roadmap's own "derive desired from ledger, no consumed edit" rollback contract. While building this, found and closed a real gap: `wl_period_lifecycle_schema.py` had already named the `PLANNED -> ACTIVE -> CLOSED` period status machine but explicitly deferred building it, so PH6-03's own `resolve_active_wl_period` (`status='ACTIVE'` filter) could never actually attribute a live purchase's usage to any period -- `WLUsageLedgerStore.sync_wl_period_statuses()` (new, `src/wl_usage_ledger.py`) is the mechanical, purely time-driven completion of that already-declared machine (a period becomes ACTIVE at its own `starts_at`, CLOSED at its own `ends_at`, nothing else), wired into `run_collection_cycle` immediately before its existing resolver call. Zero new tables; only `mgboost_wl_periods.status`, the one column PH5-02's own immutability trigger deliberately left mutable for this. 24 new focused tests (7 for the status sync, 17 for the pool itself, covering every scenario named: several children summed, one child through both WL nodes, duplicate observations never double-counted, revoked-generation retention, the exact period boundary, 30d/60d sequential periods, Non-WL/never-purchased accounts, concurrency/restart/idempotent recomputation, zero raw-identifier leakage, zero enforcement/config side effect). Full regression `1140 passed, 0 skipped`. Deployed application-code-only (no schema migration): encrypted backup+restore PASS, invariants unchanged (accounts=18, grace=17, subscriptions=18, `mgboost_wl_periods`=0), all 4 services active. Not wired to any admin route/UI/scheduler -- dormant/on-demand, matching the PH6-01/02/03 precedent; PH6-06 (disable-at-quota enforcement) remains unstarted and out of this task's own scope.
- PH6-03 durable monotonic WL usage ledger/collector (2026-08-27, `[x]`, production-deployed): read the real production Marzban 0.8.4 source over SSH before writing any schema (`app/jobs/record_usages.py`/`app/db/crud.py`/`app/db/models.py`) rather than assuming semantics -- Marzban's own scheduler already accumulates xray-core deltas (`reset=True` reads) into a durable per-(user,node,UTC-hour) row, so its own usage-query endpoint is always a non-negative interval sum, and the only real decrease vector is an admin-triggered `/reset`, which cascade-deletes that user's entire usage history. `src/wl_usage_ledger.py`'s collector therefore tracks a *last observed cumulative total* per (child, WL node) and treats any decrease as a detected reset (never subtracts, never raises) rather than assuming perfect monotonicity from the source. New additive schema (`src/wl_usage_ledger_schema.py`, `ph6_03_wl_usage_ledger_v1`, three-parent-checksum-gated on PH3-01/PH3-03-prerequisite/PH5-02): `mgboost_wl_usage_cursors`, a DB-trigger-enforced monotonic-non-decreasing `mgboost_wl_usage_samples` per-(child,node,UTC-hour) ledger (mirrors the existing `mgboost_legacy_grace_periods` extension-only pattern), a fully immutable append-only `mgboost_wl_usage_sample_events` audit keyed `UNIQUE(child,node,cursor_before)` (the idempotency mechanism -- duplicate/replayed/no-new-traffic polls all no-op through the same path), and a single-row CAS `mgboost_wl_usage_collector_lease` (mirrors the PH3-03 outbox lease) so any number of collector processes/hosts can race safely with only one doing work per window. Reuses PH6-01's `require_topology_ok()` and PH6-02/PH5-02's `align_to_utc_hour()` instead of duplicating either; needed zero new broker surface (the existing `legacy.user.usage` operation's username validator already accepts real `mgc_*` child usernames). Observe/accounting-only: never mutates Marzban, never touches `mgboost_wl_periods`/subscriptions/entitlements/inbounds, never disables or resets anyone, not wired to any scheduler -- dormant/on-demand via the new `scripts/run_wl_usage_collector.py`, matching the PH6-01/02 precedent. No raw username/UUID/HWID/token in any ledger table or script output. 34 new focused tests; full regression `1115 passed, 0 skipped`. Deployed additive-only: encrypted backup+restore PASS, fast-forward `d11005d`->`ed77b11`, `mgboost-panel` restart only, invariants unchanged (accounts=18, grace=17, subscriptions=18, `mgboost_wl_periods`=0), all 4 services active, unauthenticated `/admin/accounts` still `401`, legacy `/sub` bogus-token still `404`. Real production observe-only verification (not a dry run): a fresh live topology assertion confirmed `ok=True`, then the collector ran twice, 5 seconds apart, against the real production DB/broker -- 31 live children, 62 samples both times (real observed totals ~64.6MB on node 4 / ~5.35GB on node 7 across 10 children with nonzero traffic), the second run correctly added only the genuine new deltas from those same 10 children and no-op'd the other 52 (child,node) pairs through the idempotency path, `quick_check=ok`/0 FK violations held throughout.
- PH0-05 exact versioned WL topology, PH6-01 runtime topology allowlist/assertions, PH6-02 gap-fill (2026-08-26, all `[x]`, production-deployed): `src/wl_topology.py` records the exact live 12 WL inbound tags and the exact two real WL nodes (id 4 "RU ONLY WL"/`84.201.130.217`, id 7 "Selectel"/`5.178.85.8`, both `usage_coefficient=1.0`), confirmed this session directly against live production Marzban (`GET /api/nodes`, `GET /api/inbounds`, plus the `hosts` table to tie each tag to a physical node address) -- exact-set membership only, no `wl` substring/fuzzy matching anywhere, so a node whose own name literally contains "WL" (id 4) is included only because its id is on the allowlist, never because of its name. `src/wl_topology_guard.py` + an additive append-only `mgboost_wl_topology_assertions` log durably record every topology check (config version, ok/mismatch, missing/extra tags, missing nodes, field drift); `require_topology_ok()` is a fail-closed gate for any future PH6-06 destructive enforcement (raises on mismatch *and* on "never checked" -- not wired to any live enforcement path yet, since PH6-06 doesn't exist). PH6-02 gap audit found PH5-02's already-deployed period engine correct on decimal-GB units and full immutability, but missing two things: UTC-hour-aligned period boundaries (DL-020) and an ADMIN_RESET close/successor mechanism -- both closed without a second period engine. `subscription_renewal.align_to_utc_hour()` floors the WL-period anchor (not the subscription's own exact-second DL-044 anchor/expiry) down to the current UTC hour; because every plan duration is a whole multiple of 24h, this keeps every purchase's periods gapless/non-overlapping with every prior purchase's own periods, proven by test. A new additive `mgboost_wl_period_resets` audit table + `WLPeriodAdminResetStore.reset_period()` (same sealed `PrimaryAdminAuthority` capability every other consequential action requires) closes a `PLANNED`/`ACTIVE` period and opens a successor covering the remainder of the original schedule with the same quota -- "never rewrites consumed" holds by construction since there is no `consumed` column on the periods table yet (PH6-03's future ledger keys on period id, so a closed period's own id and any ledger rows tied to it are simply never touched by a reset). 27 new/updated focused tests; full regression (incl. browser E2E venv) `1081 passed, 0 skipped`. Deployed additive-only (two new tables, zero existing-row change, zero schema touch on `mgboost_wl_periods` itself): encrypted backup+restore PASS, fast-forward `dba4749`->`a223f80`, `mgboost-panel` restart only, invariants unchanged (accounts=18, grace=17, subscriptions=18, `mgboost_wl_periods`=0, both new tables=0 rows), all 4 services active, unauthenticated `/admin/accounts` still `401`, legacy `/sub` bogus-token still `404`.
- PH5-02 30/60-day entitlement and WL-period semantics (2026-08-26, `[x]`): `src/subscription_renewal.py` implements DL-044's exact renewal formula (`max(current_expiry, now) + purchased_duration`, one formula for both the active-extends-from-expiry and expired-extends-from-now cases) plus WL-period scheduling on the existing PH3-01 `mgboost_wl_periods` table -- a 60-day purchase creates exactly two sequential, contiguous 30-day periods (never a merged double quota), a Non-WL plan creates zero periods. Idempotent per purchase (`idempotency_key`, replays rather than double-applies), same-plan-only (a different plan is refused, that's upgrade/downgrade policy PH5-06), never overwrites an admin-granted UNLIMITED subscription. A new additive migration adds the immutability guard `mgboost_wl_periods` never had (identity/quota fields locked; `status` stays mutable for Phase 6's own future runtime transitions). Not wired to any live purchase flow yet. 15 new focused tests; full regression `1054 passed` (browser suites included). Deployed additive-only: encrypted backup+restore PASS, fast-forward `6414a59`->`7820443`, `mgboost-panel` restart only, invariants unchanged (accounts=18, grace=17, `LEGACY_REVOKED=0`), all 4 services active.

- PH5-01 versioned six-plan catalog (2026-08-26, `[x]`): new dormant/additive `mgboost_price_catalog_versions`/`mgboost_plan_prices` schema (own checksum-pinned migration `ph5_01_plan_catalog_v1`, requires the exact PH3-01 parent schema, immutable rows, one active catalog version per channel) plus `src/plan_catalog.py`, which idempotently seeds the six owner-approved commercial plans (Базовый/Базовый Плюс/Базовый Про/WL/Расширенный/Семейный: 3/6/12 device limits, 0/100/150 GB per-30-day WL quota) with their 30/60-day durations and both channels' prices exactly matching `ROADMAP.md`'s "Approved product catalog" (Stars) and DL-040 (`RUB-2026-08-23-v1`) -- 12 SKUs per channel, 24 price rows total. Seeding is a separate explicit idempotent script (`scripts/seed_ph5_01_plan_catalog.py`), not run automatically at startup, matching PH3-01/PH3-06's own dormant-schema discipline. Nothing in the legacy/Stars/LK/bot purchase paths reads this catalog yet -- PH5-02/04/05 wire it to a real entitlement/purchase flow. 9 new focused tests; full regression `1039 passed` (browser suites included). Deployed additive-only (new tables, zero existing-row change): encrypted backup+restore PASS, fast-forward `f4a250e`->`6414a59`, `mgboost-panel` restart only, `quick_check=ok`, 0 FK violations, accounts/grace unchanged 18/17, `LEGACY_REVOKED=0`, all 4 services active; catalog seeded in production and re-run confirmed idempotent (0 newly-created on the second run).
- Devices tab now also shows real client-observed evidence per account -- device name (custom label if the admin renamed it, else the reported model), OS/platform, VPN client app + version, last activity -- sourced from the already-existing, continuously-updated `user_devices` table (populated on every real legacy `/sub/{token}` hit, the same path all currently-migrated accounts' real traffic still runs through). Shown as its own block, explicitly separate from the slot/state table, since the two use different technical identifiers and cannot be provably matched 1:1; a genesis/bootstrap placeholder slot never sends a real HTTP request so it can never appear in this list, by construction. Read-only, no raw HWID/UUID/request-key exposed. 1 new focused test; full regression unaffected.

- Wave A corrective UX slice (2026-08-26, owner manual-walkthrough follow-up): after the first Wave A production deploy below, an owner walkthrough surfaced remaining English labels, no visible Marzban `note`, no way to hide internal/service accounts, an inconsistent Telegram/real-device-migration denominator, and an unlabeled raw-identifier Technical tab. Fixed, all read-only/presentation: a single reusable `humanLabel()` map replaces remaining English account-centric labels; the Marzban `note` is fetched read-only with the admin's own session and shown as a presentation-only display identity (never written back or used for linkage/ownership; falls open with an explicit warning if the Marzban fetch fails), searchable alongside every alias and the public ID; a canonical-evidence-only technical-account filter (`account_source=INTERNAL` + no grace-cohort row + a reviewed `ownership_evidence=ABSENT` record with a structured `purpose` — never a username/id heuristic) hides internal/service accounts by default behind an explicit toggle, and any real grace-cohort member is always shown regardless of source; Migration/Grace and the Dashboard now report parent-readiness, the Telegram cohort breakdown, accounts-with-real-lineage vs. without, an absolute total real-lineage count, and active slots as clearly separate numbers (active slots explicitly noted as possibly including a genesis/bootstrap placeholder); a new exact/keyed `is_genesis_hwid_verifier()` check (never inferred from an absent lineage) lets Devices badge a proven genesis/bootstrap slot instead of ever showing it as a real customer device; grace progress gained a fixed-boundary Day X/14 with elapsed/remaining percent and exact end; the Technical tab now shows each raw identifier as its own labeled, individually-copyable field, with historical generations collapsed by default. A real `metadataWarning()` SafeMarkup violation (returned a raw `''` instead of `` html`` ``) was found by the browser gate and fixed before deploy. Full regression `1029 passed, 0 skipped`. Deployed `955f255` -> `76daec6` after a fresh encrypted backup create/restore PASS and a clean pre/post-deploy invariant check (`quick_check=ok`, 0 FK violations, accounts=18, grace rows=17 unchanged, `LEGACY_REVOKED=0`, all 4 services active); verified via unauthenticated API/static checks, DB invariants and the dedicated browser gate — an owner-performed interactive authenticated click-through is still outstanding (no admin credentials were available to, or sought by, this session).
- Wave A account-centric admin foundation: new authenticated read-only account/detail/migration/dashboard APIs and vanilla-JS modules add Accounts as the primary customer surface, Account Overview/Subscription/Devices/Telegram-Ownership/Migration-Grace/Technical tabs, a standalone Migration/Grace view, and a conditional grace-first Dashboard with operational health, expiring-soon and compact ticket counters. Migration actions reuse `legacy_grace_observability.account_grace_snapshot()`/`classify_action()` rather than duplicating policy. Parent readiness/active slots are deliberately shown separately from real per-device migration lineages, preventing genesis children from being reported as migrated customer devices. Internal IDs/verifiers/`mgc_*` names are visible only under Technical; ordinary views use operational state and masked identifiers. The existing production-safe opaque credential issue/reissue flow is exposed with mandatory reason, explicit rotation confirmation and one-time URL delivery. Legacy Marzban Users is preserved under `System / Technical / Marzban Raw Users`; Tickets, Nodes, Extra configs, Stars and Settings remain available. A real headless-Chromium CSP/XSS/Technical-visibility/480px-responsive gate passed; final full regression with browser suites: `1023 passed`. Fresh production DB-copy read gate assembled all 18 account details with `quick_check=ok`, 0 FK violations and unchanged account cardinality. Production deploy completed after encrypted backup/restore: all services active, static ES modules served with correct MIME, unauthenticated API denied, DB/account/grace invariants unchanged. PH4-06 was not started and no shared legacy credential was revoked (`LEGACY_REVOKED=0`).

### Fixed

- P0: provisioning новых устройств для legacy/WL-аккаунтов отравлялся
  собственным STANDARD-бэкстопом; отравленные операции стали
  невосстановимы (2026-08-28, production incident account #8 / POCO Slot 3
  generation 49; local hotfix checkpoint, NO push, NO deploy, production
  read-only). Root cause: PH5-11 render-boundary backstop
  (`WL_INBOUND_IN_STANDARD_CHILD`) применялся безусловно ко всем аккаунтам,
  хотя легитимен только там, где delivery WL запрещён: для
  `LEGACY_PAID_COMPAT` с `wl_mode='UNLIMITED'` (и любых других
  WL-capable entitlement'ов) child корректно клонирует exact-WL inbound'ы
  из legacy-источника, после чего backstop терминально убивал
  provisioning (`fail_permanent`) — системно затронуты все legacy paid
  аккаунты с новым устройством. Политика вынесена в canonical
  `entitlement_engine.exact_wl_allowed_for_delivery()` (единственный
  источник — PH5-04 расчёт, `wl.access_eligible`; никаких special-case по
  account_id/username/source/имени плана/substring "WL"): STANDARD
  (`wl_mode='NONE'`, включая первый commercial canary) по-прежнему
  fail-closed, WL-capable — легитимны. Сопутствующие дефекты того же
  инцидента: (1) `claim()==None` смешивал «занято/lease» и «терминальный
  ERROR» — теперь терминальное состояние отдаётся отдельным typed
  outcome'ом `PROVISIONING_FAILED_PERMANENT` (не парсингом текста), auto-
  retry не возвращён; (2) migration lifecycle для терминальной ошибки
  бесконечно крутил `MIGRATING → RETRY` — теперь явный
  `ERROR_RECONCILE` с typed root cause из durable `last_error_class`,
  reconcile не воскрешает terminal ERROR обратно в MIGRATING (genuine
  pending/busy по-прежнему честно RETRY-ится); (3) diagnostic-дефект:
  `migration_binding.slot_generation_id/child_intent_id` оставались NULL,
  т.к. записывались только из `OUTCOME_OK`-результата — теперь binding
  заполняется из durable local rows (slot claim + child intent существуют
  независимо от исхода ensure), состояние остаётся честным
  terminal/review; (4) user-facing HTTP не притворяется отсутствием
  подписки (502 service unavailable сохранён), typed root cause виден
  оператору в binding events. Добавлен audited idempotent recovery
  primitive (`src/child_recovery.py::repair_child_ensure`, dormant — без
  route/автовызова): только для доказанно owned intent/outbox, только для
  класса `WL_INBOUND_IN_STANDARD_CHILD`, reread текущей политики
  (всё ещё запрещает WL → REFUSED), fresh typed `child.user.observe`
  (remote username/source-contract/UUID provenance; ABSENT → typed
  REMOTE_MISSING без создания второго child; MISMATCH/UUID-verifier
  конфликт → REFUSED на manual review), локальное завершение через CAS
  `recovery_acknowledge` (ERROR → APPLIED/ACTIVE, attempt-event
  RECONCILED), audit actor/reason/idempotency в существующем
  `mgboost_entitlement_mutations` ledger, повтор — safe no-op. Recovery
  production-вызов сознательно НЕ выполнялся (NO PROD MUTATION): реальный
  POCO Slot 3 будет восстанавливаться после независимого review/deploy.
  Regression: RED на baseline `7392b63` (13 failed / 7 passed в новом
  `tests/test_p0_legacy_wl_provisioning_hotfix.py`), после фикса 20/20;
  воспроизведён точный дифференциал инцидента (legacy UNLIMITED + Slot 2
  ACTIVE + новый Slot 3 после PH5-12 → provisioning SUCCESS, child с
  легитимным WL), STANDARD-негатив остался терминальным.
- Bot `/start` treated a paying canonical DIRECT account owner as unlinked (originally implemented 2026-08-28 as checkpoint `c93cdd5` ahead of the P0 hotfix above; independently re-reviewed and rebased onto the post-P0 baseline `243671e` in this session, local checkpoint only, NOT pushed/deployed): after a successful `CANONICAL_SIGNUP` the customer's DIRECT account and non-revoked OWNER telegram identity existed and Stars payment/refund worked, but `cmd_start`/`msg_no_state` in `src/bot_support.py` resolved "linked user" solely from the legacy `tg_users` table — a table canonical signup never writes — so the paying customer was greeted with new-user onboarding («Здесь можно купить подписку или прислать существующую ссылку…») instead of the normal account menu. Fix reuses the one canonical resolver (`AccountStore.get_active_account_by_telegram_id`, the exact read-model already used by Stars signup/renewal, `/newsub` and admin views) as an additional linked-user signal in `/start`, the stateless stray-message fallback, and the AI-support `get_subscription_info` tool (which now reports the canonical entitlement instead of «Подписка не привязана»); `📋 Моя подписка`'s PH5-11 canonical rendering was extracted unchanged into a shared `_canonical_subscription_summary` helper. No second account resolver was introduced; ownership still requires `role='OWNER'` + non-revoked identity + `status='ACTIVE'` account, so revoked identities, CLOSED accounts and unrelated Telegram ids still land in onboarding, possession of a subscription URL/HWID/username still proves nothing, and legacy `tg_users` users keep the exact previous UX. `🔧 Управление устройствами` now recognizes a canonical owner and honestly reports the feature is support-channel-only for their account type instead of pushing them into the `waiting_link` binding loop their opaque URL can never satisfy (the LK management deep link remains legacy-marzban-username-keyed — recorded as a known gap for canonical-only accounts, not silently pretended to work). 15 regression tests (`tests/test_bot_start_resolver.py`) drive every required scenario through a real aiogram `Dispatcher`/`feed_update` (the prior P0 lesson: handler-level unit calls can miss real routing), including the full signup-payment → fresh `Database()` + fresh `Dispatcher` restart path; independently re-reproduced red against clean `243671e` first (**6 failed / 9 passed**, not the originally-claimed 7 -- corrected by this session's own re-verification), green after the fix (15/15). Targeted regression across bot/signup/ownership-rebind/enrollment/Stars/legacy-bridge/account-consolidation plus the P0 hotfix suite itself: `384 passed, 0 failed`. Full regression on the rebased tree: `1431 passed, 4 skipped in 1100.53s` (20 more than the original checkpoint's `1411 passed` -- the exact size of the P0 hotfix's own `tests/test_p0_legacy_wl_provisioning_hotfix.py` suite, confirming it stayed green through this rebase). `git diff --check` clean. **Deployed 2026-08-28T12:13Z** (commit `7f5b18f`): fresh encrypted backup+restore PASS, local `main` fast-forwarded `243671e`->`7f5b18f` and pushed, production fast-forwarded the same way, `mgboost-panel` restart only (no schema migration), invariants unchanged (accounts=19, subscriptions=19, grace=17, `quick_check=ok`, 0 FK violations), all 3 services active, unauthenticated `/admin/accounts` still `401`, legacy `/sub` bogus token still `404`. No payment/refund/routing/tpl-*/child-lifecycle changes.
- Canonical (PH5-05/PH5-11) Stars invoice refund was structurally impossible (2026-08-28, minimal fix ahead of the first real commercial canary's controlled refund): `handle_stars_payment_refund`'s route-level status gate and `begin_invoice_refund`'s own DB-level CAS `WHERE` clause both only recognized the pre-PH5-05 legacy manual-Stars status vocabulary (`applied`/`manual_review`/`apply_retry_exhausted`/`apply_failed_user_missing`); a canonical invoice's terminal success status `canonical_applied` was never in that list, so any admin refund attempt on a canonical/signup purchase failed closed with 409 before ever calling Telegram — safe, but left operators with no way to money-refund a canonical purchase at all. Both gates now additionally accept `canonical_applied` only (owner decision: deliberately NOT `paid` — refunding before the PH5-05/PH5-11 apply pipeline has run would race that pipeline in a way nobody has proven safe yet). Money-only by design and unchanged: `mark_invoice_refunded` still never touches VPN expiry, and this fix adds no new code path — it only widens which existing invoice a already-proven `refund_pending -> refunded/refund_unknown -> reconcile` state machine may act on. No product-reversal semantics were added (account/subscription/credential/child/template are explicitly untouched by any refund). Regression: exact-one Telegram call, concurrent/duplicate refund calls make no second Telegram call, timeout/ambiguous-result reconciliation via the existing history-scan path, refunding a canonical invoice leaves its account/subscription/credential/child/template/purchase-application/payment-evidence rows byte-identical, legacy refund behavior for invoices #1/#2 unchanged, and a merely-`paid` (not yet applied) canonical invoice remains explicitly non-refundable at both the route and DB layer.
- Account-centric admin migration status no longer reuses Telegram semantics (2026-08-27, **independently re-verified and production-deployed 2026-08-27 as part of the PH7-13 rollout below, fast-forwarded to `d5ed3b7`**): `classify_action()`'s fallback labeled every account without real-device migration lineage `WAITING_FOR_REGISTRATION` ("Ожидает Telegram"), so a BOUND owner with zero real devices showed a Telegram-waiting pseudo migration state, and `OK_MIGRATED` additionally required an active slot on top of migrated lineage. The fallback is now device-migration semantics: zero real-device lineage = `WAITING_FIRST_DEVICE` ("Ожидает первого подключения"), any real lineage (`MIGRATING`/`MIGRATED`/`LEGACY_REVOKE_PENDING`/`LEGACY_REVOKED`) = `OK_MIGRATED` ("Миграция штатно"). Telegram ownership stays an independent read-model column via the unchanged `telegram_status()`; RECONCILE_REQUIRED / MANUAL_REVIEW / COMPATIBILITY_BLOCK / CONTACT_USER precedence is unchanged (CONTACT_USER thereby narrows to zero-lineage campaign members, its natural outreach audience). Pure read-model derivation fix, no schema/API shape changes; frontend label map and the PH4-05 daily report blocker text updated; both mandatory regression cases covered at every surface (list, detail, browser render). Production evidence: real accounts checked via `scripts/ph4_05_daily_cohort_report.py` post-deploy show account 6 (`BOUND`+real lineage) -> `OK_MIGRATED` and account 5 (`UNREGISTERED`+zero lineage) -> `WAITING_FIRST_DEVICE`.

### Security

- Fixed a P2 orphan-lock risk in the internal renew route's cross-thread Marzban lock (2026-08-27): an independent read-only audit flagged that `_CrossThreadLockCtx.__enter__` (`src/routes/internal.py`) called `asyncio.run_coroutine_threadsafe(lock.acquire(), loop).result(timeout=10)` without cancelling the scheduled coroutine on timeout -- if the abandoned `acquire()` later succeeded on the bot loop (once the real holder released) with nobody left to release it, that per-username lock would be permanently held, eventually stalling the Stars apply-loop for that username. Reproduced against current `main` with a standalone harness before any fix: a caller-side timeout followed by the real holder's later release left `lock.locked()==True` forever, and a subsequent normal acquire hung/timed out. Fix: `__enter__` now calls the documented `future.cancel()` idiom on timeout to stop the abandoned coroutine, and releases the lock on its own loop in the rare case it had already been acquired the instant before cancellation landed. 1 new regression test proving timeout -> no orphan acquire -> a subsequent normal acquire/release still works; Stars/internal-renew serialization (the existing race test) unaffected. Full regression `1116 passed, 0 skipped` (up from `1115`). Deployed application-code-only (no schema change): encrypted backup+restore PASS, fast-forward `ed77b11`->`096c8d4`, `mgboost-panel` restart only, invariants unchanged (accounts=18, grace=17), all 4 services active, unauthenticated `/admin/accounts`/`/admin/dashboard` still `401`, legacy `/sub` bogus-token still `404`.
- PH4-03 (closed `[x]`, 2026-08-26, mass migration completed): a read-only human-review decision packet was produced for the 4 accounts whose device count/ownership was genuinely ambiguous (built from Marzban, MGBoost, telemetry and event-table cross-checks, HWID masked, no raw secrets), and the owner returned four explicit decisions. Account 8 (shared subscription, two real people): `2105984481` set as sole Telegram owner via new `resolve_ambiguous_telegram_ownership()` -- the one deliberate, capability-gated, audited exception to "ambiguous ownership is never auto-resolved," proven by test that the non-chosen id can never later hijack the account through the ordinary bind path; both historical `tg_users` rows preserved. Device policy D8/D6/exempt/D4 for accounts 8/10/11/13: `PAID_BASELINE_LIMITS` extended `{3,6,12}`->`{3,4,6,8,12}` (anticipated by this project's own prior documentation, never a catalog change); a new `device_limit_exempt=True` path reuses the existing generic `UNLIMITED` plan-version concept (previously INTERNAL-only) for one individually-reviewed `DIRECT` account (the owner's parents), never self-service, never a new tariff; a new `acknowledge_observed_overage=True` parameter lets one explicitly-evidenced admin decision stand below the frozen raw observed-device count without weakening the safety check for any unreviewed account. A new `migrate_bootstrapped_account()` primitive packages the exact genesis-child-then-bridge-enable sequence already proven three times in production (accounts 1/3/4) as a reusable, idempotent, capability-gated batch operation -- no second resolver, proven by test that a real device (not the synthetic genesis placeholder) migrates transparently once the bridge is enabled. Run for real in 4 production batches (3+3+4+4, problem-free accounts first). Final accounting: **17/17 real ACTIVE parent accounts technically migrated** (genesis child `ACTIVE` + bridge `enabled=1`), covering all 19 real ACTIVE legacy usernames; `mgboost_migration_bindings` `MIGRATED=9`/`MIGRATING=0`/`ERROR_RECONCILE=0` unchanged for accounts 1/3/4's own real customer devices; the 14 newly-migrated accounts' real customer devices will populate their own migration lineage organically on next reconnect, exactly as designed (none simulated). 21 new focused tests, full regression `1016 passed, 3 skipped`, zero regressions. Pre/post-batch production invariants (`quick_check`, FK check, service health, legacy `/sub` unaffected, zero raw identifier leakage) verified after every batch. Telegram registration completion is explicitly out of PH4-03's closure scope -- it continues as PH4-05's own ongoing campaign metric. PH4-06 remains its own separate, unstarted, gated phase.
- PH4-05 (closed `[x]`) + PH4-03 (revised, still `[~]`, mass stage now in progress): owner decision 2026-08-26 -- grace-period membership never waits on Telegram registration or prior migration; the 14-day window itself is the mass-migration campaign. `bootstrap_grace_subject()`/`bind_telegram_after_registration()`/`start_grace_cohort()` (`src/legacy_grace_registration.py`) let a real legacy account exist and start its grace clock with zero Telegram claim (`ownership_evidence='ABSENT'`), then link ownership later via the existing bot flow's exact ambiguity bar, never an automatic rebind. A single shared-UTC-boundary production cohort was started for real: `cohort_start_at`/`cohort_end_at` identical across all 17 real parent accounts (the 3 already-migrated plus 14 newly bootstrapped, covering all 19 real ACTIVE legacy Marzban users; 5 `EXPIRED` users and all test/synthetic accounts excluded). The 1 known ambiguous-Telegram-ownership user is in the cohort but untouched (`MANUAL_REVIEW`, zero identity mutation); 4 accounts exceed the default `D3` device limit and correctly have no entitlement yet (`DeviceOverageConflict`, no invented limit) pending an explicit owner-approved increase. Day-0 report: 3 `OK_MIGRATED`, 13 `WAITING_FOR_REGISTRATION`, 1 `MANUAL_REVIEW`. Extended telemetry/observability (`telegram_status`, `last_child_fetch`, `classify_action`) and a new daily monitoring CLI (`scripts/ph4_05_daily_cohort_report.py`) give the owner a concrete per-account action list every day of the window. Finalized (still unsent) public informational post replacing the earlier per-user-push draft, since most cohort members cannot yet receive a personal bot message. 47 new focused tests, full regression `996 passed, 3 skipped`, zero regressions. Pre/post production invariants (`quick_check`, FK check, cardinality, service health, legacy `/sub` behavior) verified unchanged except the additive new rows; zero Telegram messages sent; zero raw legacy/opaque token or username printed anywhere. PH4-03 stays `[~]` -- the clock starting is not migration completing; PH4-06 stays blocked on real migration/exception completion.
- PH4-03 (REOPENED `[~]`, 2026-08-26, same day as the entry below): a read-only production audit (triggered by a legitimate concern raised before authorizing PH4-05) found that PH4-03's own contract required a "mass migration" cohort as its final stage, and that stage was never executed -- only 3 real accounts (the internal-cohort canary plus 2 real DIRECT/`EXTERNAL_PAYMENT` customers) were ever migrated, against a real legacy Marzban user base of 24 (19 currently active). This is not new work discovered to be missing -- it is the original phase's own written accept criterion, never explicitly deferred in the original closing verdict the way PH4-08/PH5-09 were. **Nothing about the original canary evidence is retracted**: the internal-cohort proof and both real DIRECT/`EXTERNAL_PAYMENT` migrations genuinely happened and genuinely passed exactly as documented. Audit found 19 real legacy users with zero parent-account representation: 13 active with no Telegram-bot linkage at all (cannot be safely enrolled without new ownership evidence), 5 expired/lapsed with no linkage (lower priority, needs an explicit keep-or-exclude decision), and 1 active user with an ambiguous multi-Telegram mapping (a second, distinct case from the one already-known excluded ambiguous account) needing its own owner review. Zero of the 19 meet the "single unambiguous Telegram mapping" bar the original cohort selection used -- every cheaply-enrollable user is already enrolled; the remaining gap is an ownership-evidence gap, not an engineering gap. A concrete mass-migration plan (evidence collection via the existing bot-link flow or `OWNER_APPROVED` attestation, explicit ambiguous-case review, an explicit decision on expired users, staged sub-cohort batches reusing the exact already-proven enroll/entitle/migrate sequence unchanged) is recorded in `ROADMAP.md`. No code changes are needed to execute it -- every primitive already exists and is tested. PH4-05 is blocked from starting any real grace period until this is resolved (see PH4-05's own updated entry). No production mutation was performed by this audit (read-only queries only, verified via `PRAGMA quick_check` before/after).
- PH4-05 (new, `[~]` — reversible/dormant part only; the real 14-day grace clock was explicitly NOT started for any account this session, per instruction): additive durable schema for a per-account legacy grace-period clock (`mgboost_legacy_grace_periods`/`_events`) with the fixed 14-day window (OPD-09/DL-023) enforced by a schema `CHECK`, not just application code; `current_end_at` can only ever move forward via an explicit, audited, primary-admin-capability-gated `extend()` (never a silent shrink/reset -- backstopped by a DB trigger), and an account can only ever `start()` once (a second start attempt fails closed, `GraceAlreadyStarted`). New privacy-safe daily activity counters (`mgboost_legacy_grace_activity_daily`, account_id + LEGACY/OPAQUE channel + day only, never a raw token/HWID/UUID/URL) are wired as a fail-open, response-blind observation hook into both the already-live legacy `/sub` path and the dormant opaque route -- mirrors the already-deployed PH3-07 telemetry hook's own exception-swallowing discipline exactly; a write failure here can never change, delay or deny a subscription response. A new read-only observability module composes grace day/remaining time, PH4-02 migration state, active-vs-migrated device counts, last legacy/opaque activity, 24h/72h request counts and inactive-client detection from already-existing tables. A read-only dry-run eligibility CLI (`scripts/ph4_05_grace_eligibility_report.py`) produces the account/migration-state/last-activity/compatibility/blockers/`START_GRACE`\|`HOLD` decision table the owner needs to pick a first real cohort; it was validated against a synthetic local DB only (no SSH/production access was used this session). Draft, unsent Telegram/LK/support-ticket communications and a full runbook are included (`docs/PHASE4_GRACE_PERIOD_COMMS_DRAFT.md`, `docs/PHASE4_GRACE_PERIOD_RUNBOOK.md`). 38 new focused tests (exact `<`/`==`/`>` UTC boundary, restart/persistence, duplicate start, audited extension incl. stale-revision/no-shrink rejection, inactive clients, route-hook fail-open), full regression `969 passed, 3 skipped`, zero regressions. No real account has a grace row, no communication was sent, and PH4-06 (the actual revoke) remains unbuilt.
- PH4-04 (post-closure correction, reopened `[~]`, see the follow-up entry directly below): a browser-vs-VPN-client landing-page regression and an implied-consent rotation gap were found by owner manual testing after the original closure below and have been fixed; the original canary described in the entry immediately below genuinely passed and is not retracted.
- PH4-04 correction, production re-verified (2026-08-26, closed `[x]` again): production deploy of the fix below completed (encrypted backup+restore verified, fast-forward deploy, all 4 services healthy, `quick_check=ok`, 0 FK violations). Real re-verification on the owner's own account: an `ACTIVE` credential's bare-repeat/confirm-click paths left it fully unchanged (no token leaked); the explicit final confirm rotated it for real (old generation immediately `REVOKED`, device-slot/child-intent tables and child-intent ids proven byte-identical before/after); the new URL was fetched for real over the public Internet with a browser `User-Agent` and got the fixed landing page (`200`, correct security headers, zero device/child mutation). The owner's manually-exposed credential (from the manual test that found this regression) was rotated as required, using a transient in-memory/root-only script and a one-time root-only token file, both destroyed immediately after use -- no raw token/UUID/HWID was ever printed. The historical journal was found to still contain a handful of now-fully-`REVOKED` (generations 1-4, dead) opaque tokens from earlier ad hoc testing predating this session's restart; the currently-live credential produced zero journal/nginx-log footprint. See `ROADMAP.md` PH4-04 for the full step-by-step production evidence.
- PH4-04 correction (2026-08-26, fixed in code/tests before the production re-verification above): owner manual production testing of the real `/newsub` flow (beyond the supported-VPN-client path the original canary covered) found two gaps: (1) opening the opaque URL in an ordinary browser returned the uniform invalid response instead of the existing `/sub/{token}` browser landing page; (2) a bare repeat of `/newsub` while a credential was already `ACTIVE` could be misread as consent to rotate a destructive action. Fixes: `src/routes/opaque_sub.py` now reuses `src/routes/sub.py`'s existing single browser-detection/landing-page mechanism (`is_browser_request`/`send_browser_landing`, same `frontend/browser_page.html`, no second parallel UX) via a new `_try_browser_landing` gate that only runs after the token resolves to a live `ACTIVE` credential -- any invalid/revoked/expired-parent token still gets the exact same uniform `404` regardless of `User-Agent`, and the browser hit is read-only (zero slot/child/device mutation, test-proven). `/newsub` (`src/bot_support.py`) now issues directly only when no credential is `ACTIVE`; an existing `ACTIVE` credential instead shows an offer button, then a distinct destructive-action confirm/cancel step, with every callback re-verifying the canonical Telegram OWNER, no raw token held between steps, and a small in-memory per-account guard against a double-tap producing two rotations. The same silent-rotation gap existed in `src/routes/lk.py::handle_lk_opaque_subscription_issue` and `src/routes/subscription_credentials_admin.py::handle_subscription_credential_issue` (both always rotated on a bare POST regardless of existing status) -- both now require an explicit `{"confirm": true}` body field once a credential is `ACTIVE` (bare request returns `409 requires_confirmation`, no mutation); no frontend currently calls either endpoint, so this is a backend-contract-only fix, no UI redesign. 22 new/updated focused tests, full regression `931 passed, 3 skipped`, zero regressions. Remains `[~]` until full production re-verification (browser journey, zero browser mutation, explicit-confirm rotation on all three surfaces, the owner's manually-tested credential rotated via a transient/memory-only mechanism with no raw token/UUID/HWID ever printed, and a post-fix leakage scan) is complete.
- PH4-04 (new, closed `[x]`): PH2-01's dormant opaque subscription credential system is now live in production. Crash-safe issuance/rotation orchestration guarantees exactly one `ACTIVE` credential per account ever exists, even across a failed/lost delivery (a stale pending generation is auto-abandoned on the next attempt, never leaving two actives or a locked-out account). Three presentation surfaces, each within its own already-existing auth boundary, none weakened: admin (session+CSRF+primary-admin capability), a private-chat-only Telegram `/newsub` command (canonical proven Telegram ownership only, never mere possession of an old link), and an LK endpoint gated by the same session boundary every other destructive device action already requires. nginx now routes the exact 43-character opaque path on `sub.beykus.fun` to the app, reusing the existing redacted sensitive-access-log/security-header handling; every reserved path (`/sub/`, `/lk/`, `/assets/`, `/internal/`, `/sub-admin*`) verified to still win. Real production canary on the owner's own account: real external fetch over `https://sub.beykus.fun/<token>` returned a working VLESS child config with zero occurrence of the shared legacy UUID, a second device got its own separate child, rotation immediately invalidated the old token while leaving the underlying child/UUID untouched, and the PH2-06 rate limiter fired for real over the public path. Full post-canary leakage scan: zero raw tokens in nginx logs, the application journal, or the DB (only 64-hex verifier hashes). The account's two real live devices and its real legacy Marzban user were verified unchanged throughout. `OPAQUE_SUBSCRIPTION_ENABLED` is now permanently on (graduated, matching `LEGACY_BRIDGE_ENABLED`'s own precedent) -- only the owner's account has an issued credential so far. A new runbook (`docs/PHASE4_OPAQUE_URL_RUNBOOK.md`) covers issue/lost-delivery/rotate/revoke/pause/rollback/leak-verification without any secrets/PII. 33 new focused tests, full regression `911 passed, 3 skipped`, zero regressions.
- PH2-06 (new, closed `[x]`): subscription-fetch abuse controls added ahead of PH4-04's new public opaque endpoint. A new per-client-IP in-memory rate limiter (`src/subscription_rate_limit.py`, same architecture as the existing admin-login limiter) rejects excess `GET /sub/{token}` and opaque-route requests with a uniform `429`/`Retry-After` before any token parsing, credential lookup or upstream/broker work -- a malformed-token flood shares one IP's ordinary budget, never a special path, and one client's burst never affects another IP's budget. Client identity reuses the existing trusted-XFF boundary (`http_utils.client_ip()`, unchanged) -- an `X-Real-IP` header is honored only when the real TCP peer is the loopback nginx process, never from an untrusted source. A new socket-level request deadline (15s) bounds how long one slow client can occupy the single-threaded server before its request is even fully read, protecting today's architecture without a broader concurrency rewrite. Body/size/malformed-ID/uniform-failure/deadline-after-commit requirements were largely already satisfied by existing code (legacy token length bound, opaque route's exact-length match, bounded HWID regex, shared invalid-response helper, bounded broker-call timeout) and are now covered by regression tests rather than left implicit. 15 focused tests, full regression `884 passed, 3 skipped`, zero regressions.
- PH4-03 (closed `[x]`): a migration-only legacy paid compatibility entitlement was added (owner decision 2026-08-26) so the two real DIRECT/`EXTERNAL_PAYMENT` legacy customers enrolled below could be safely migrated -- NOT a commercial catalog entry: historical default device limit is `3` (never inferred from current device/HWID counts; an owner-approved increase is explicit `3 + N` with recorded evidence), exact legacy expiry preserved, unlimited legacy WL semantics (no quota bytes), no invented price/tariff name, reuses the existing `mgboost_plan_versions`/`mgboost_subscriptions` tables and the existing commercial device-capacity contract unchanged (`src/legacy_paid_compat.py`, 18 focused tests including a full enrollment-to-migrated-child integration test). Both real customers then completed a full real migration canary on production, in order: an initial child was bootstrapped through the existing PH3-03 pipeline before any bridge binding existed (so neither customer's real device was ever exposed to a missing-subscription gap), then a synthetic canary device on each account's own spare slot went through `LEGACY -> MIGRATING -> MIGRATED` with a real new child and working subscription, PH3-05 revoke (confirmed disabled, no resurrection or legacy fallback on retry), and PH3-05 free; one account additionally proved PH3-05 rebind (new generation, working new credential, old child stayed disabled) on its own already-freed canary slot. Neither real customer's actual daily-use device was touched. Full regression: `869 passed, 3 skipped`, zero regressions. `TELEGRAM_STARS` cohort remains a documented owner-approved `N/A` exception (see below) -- its code path is unaffected and fully test-proven; the first real future Stars purchase still requires its own canary before wider rollout. Telegram ownership rebind relies on existing focused tests and PH2-05's own production-proven mechanism, not a new live mutation. A short support runbook (`docs/PHASE4_MIGRATION_SUPPORT_RUNBOOK.md`) covers reading migration state, compat `Dn`, provenance, and `ERROR_RECONCILE` without any secrets/PII. PH4-08 (full legacy-subscription preservation/renewal flow) and PH5-09 remain their own future phases, both still `[ ]`.
- PH4-03 (progress toward the above): real DIRECT/`EXTERNAL_PAYMENT` cohort enrolled on production for two real, owner-verified paying legacy customers (`cohort-2 account #3`, `cohort-2 account #4`; the excluded ambiguous-ownership legacy account excluded per explicit instruction), reusing the existing bot subscription-link Telegram mapping (`tg_users`/`bot_support.py`, no new linking mechanism -- a username already bound to more than one distinct Telegram ID, or a claim contradicting the single recorded one, fails closed) and a new owner decision: every real legacy paying user historically paid the owner directly, never Telegram Stars, before any canonical payment ledger existed. That fact is now recorded via a new additive `mgboost_owner_attested_legacy_payments` table (kept separate from `mgboost_payment_records`, whose CHECK constraints are already checksum-locked by the deployed PH3-09 migration and are never edited in place) with no invented amount/date/reference, clearly distinct from a real new `EXTERNAL_PAYMENT` with known details. `TELEGRAM_STARS` production cohort is an owner-approved `N/A` exception: zero real successful Stars purchases existed at PH4-03 time (the only two historical invoices are refunded test canaries for the excluded ambiguous-ownership legacy account); no artificial purchase was created and no real user was asked to buy Stars for testing, and the Stars code path remains fully covered by focused tests. Real migration/device-rebind/revoke for either new DIRECT account was not attempted: it requires a `mgboost_subscriptions`/plan (device limit/WL mode) that does not yet exist for an unproven historical tariff, and inventing one was explicitly out of scope -- this is PH4-08's job and is the one precise remaining PH4-03 blocker. Verified before and after: legacy Marzban users/expiry completely unchanged, no bridge binding created, all services healthy, zero FK violations.
- PH4-03 (progress, remains `[~]`): reviewed DIRECT account enrollment foundation added, additive and dormant -- no route wires into it yet. `DirectEnrollmentStore` creates DIRECT accounts only via the existing `AccountStore.create_account('DIRECT')`, with a durable pre-creation intent row so a crash/retry converges on one account instead of an orphan duplicate. A separate `mgboost_direct_account_reviews` table (distinct from and never touching PH3-06's INTERNAL-only `mgboost_internal_account_reviews`) records ownership provenance/evidence, actor and decision reference for every reviewed DIRECT account; ambiguous ownership fails closed with zero writes, and one legacy username can never be bound to two accounts. A real, non-refunded `stars_invoices` row (`paid`/`plan_committed`/`applied`) becomes a canonical `mgboost_payment_records` row through the existing `ProvenanceStore`; refused/refunded/manual-review invoices and Stars payer/account-owner mismatches are rejected, and duplicate invoice recording is idempotent. A minimal admin-only primitive records manually-confirmed `EXTERNAL_PAYMENT`/`MANUAL_PAYMENT` payments (a PH5-09 prerequisite only -- PH5-09 itself is out of scope). 16 focused tests plus full regression (`842 passed, 3 skipped`, zero regressions). No real DIRECT account, alias, review or payment record exists in production; enrollment/payment still require the owner's per-cohort authorization before use.
- PH4-02 (new, closed `[x]`): durable migration state machine (`LEGACY -> MIGRATING -> MIGRATED -> LEGACY_REVOKE_PENDING -> LEGACY_REVOKED`, plus `ERROR_RECONCILE`) built on top of PH4-01's legacy bridge -- one durable per-(account, hwid) lineage row (`mgboost_migration_bindings`, additive, dormant), never a second resolver (`process_migration_bridge_request()` wraps the unmodified `resolve_legacy_bridge()`). Explicit transition allowlist plus a `revision` CAS reject stale writers; `LEGACY_REVOKED` is terminal both in application logic and via a DB trigger, and recovery after it never resurrects the old shared credential. A downstream failure after a durable `MIGRATING` commitment fails closed, never falls back to the shared legacy credential; an ambiguous single-signal failure goes to `ERROR_RECONCILE` and is reconciled against the authoritative slot/child tables, never a blind retry. `MIGRATED -> LEGACY_REVOKE_PENDING -> LEGACY_REVOKED` is implemented and admin-capability-gated but not wired into any production path -- PH4-06 owns the real revoke. Focused tests (22 passed, including real crash-boundary fault injection via connection close/reopen) plus full regression (`820 passed, 3 skipped`, zero regressions). Real isolated Marzban 0.8.4 gate: 23/23 checks PASS, including a real crash/lost-ACK convergence proof and a real synthetic legacy-user revoke proving terminal semantics. Production dormant deploy: additive schema only, zero real migration rows, zero real bridge activations, all flags unchanged. See `ROADMAP.md` PH4-02 and `docs/PHASE4_MIGRATION_STATE_MACHINE.md`.

- PH1-10 (new, closed `[x]`): installed and enabled the standard `logrotate` scheduler (Ubuntu package + its systemd `logrotate.timer`, no custom rotation daemon) -- closing PH1-09's own disclosed residual that `logrotate` was not installed at all, so PH1-09's "forced rotation" evidence had been a manual simulation, not a real scheduled run. A real forced `logrotate -f` run rotated `/var/log/nginx/mgboost-sensitive-access.log` for real: new generation `www-data:root 0600`, all archived generations (including the compressed one) equally denied to an unrelated local identity, nginx confirmed reopening and writing to the new inode via a real (non-bearer) request, zero raw `/sub/{token}`/Referer/User-Agent leakage in any generation, 30-day retention (DL-042) unchanged and now durably guarded by `tests/test_ph1_10_sensitive_log_retention_config.py` (6 passed) against the repository's own versioned `ops/nginx/` reference configs. PH1-06 is re-noted (not reopened, stays `[x]`) with the historical fact that nginx-log scheduled retention was not actually mechanically enforced before this task, even though content redaction always was. Full regression `798 passed, 3 skipped`. See `ROADMAP.md` PH1-10/PH1-06 and `docs/PHASE1_SENSITIVE_LOG_PERMISSIONS.md`.

- PH1-09 (new, closed `[x]`): narrowed `/var/log/nginx/mgboost-sensitive-access.log` from world-readable `0644` to `0600`, at the source rather than just the current inode -- the generic `/etc/logrotate.d/nginx` glob was narrowed to exclude it (avoiding a double-rotation hazard), and a new dedicated `/etc/logrotate.d/mgboost-sensitive-nginx` stanza recreates it at `0600` on every future rotation, 30-day retention unchanged (DL-042). The file's *content* was already fully redacted by the pre-existing `mgboost_sensitive` nginx log format (no raw bearer ever appeared in it); only the file mode was more permissive than necessary. Verified via a real forced rotation, two real production requests, and an unrelated local identity correctly denied read access. Discovered as a residual during the PH4-01 valid-`/sub` gate; does not reopen PH4-01 or PH1-06. See `ROADMAP.md` PH1-09 and `docs/PHASE1_SENSITIVE_LOG_PERMISSIONS.md`.

- PH4-01 closed `[x]`. The last remaining evidence gate -- a genuine valid production legacy `/sub` pre/post proof -- was obtained safely: the raw legacy token was read transiently through the already-existing typed `legacy.user.get` broker capability (no new endpoint, no logs/backups/quarantine extraction), used once for a real `GET /sub/{token}` request, and never written to disk/stdout by this session. Result: `HTTP 200`/`no-store`, legacy status/expiry/UUID verifier unchanged before and after, 0 unexpected Shadowsocks, no child credential mixed into the response, and full masked production cardinality/flags (`LEGACY_BRIDGE_ENABLED=False`, `OPAQUE_SUBSCRIPTION_ENABLED=False`, zero bridge bindings) unchanged. No migration, bridge activation or credential mutation occurred. See `ROADMAP.md` PH4-01 for exact evidence.

### Security

- Fixed a real PH2-05 COMPROMISE-rebind crash-window gap found by a targeted point-in-time review before it ever reached production: `process_rebind()` used to run the Telegram identity mutation before the PH2-01 opaque-credential rotation, each its own separately committed transaction, so a real crash between them could durably leave the new Telegram owner active while the old (compromised) opaque credential was still valid, until a manual retry closed the gap. Fixed by rotating the credential first for `COMPROMISE` mode; `ORDINARY` (no credential step) is unaffected. Proven by a real crash simulation -- the SQLite connection is closed mid-sequence and a fresh `Database()` opened against the same on-disk file inspects durable state -- across three crash points plus a fault injection inside the real function (`tests/test_ownership_rebind_compromise_crash.py`, 5 passed; reverting the fix was confirmed to make the test fail). Full regression `792 passed, 3 skipped`; the real isolated Marzban 0.8.4 gate was re-run and passed all 12 checks unchanged. No production Telegram identity was ever affected -- this module remains dormant, no route imports it.

- PH2-05 closed `[x]`, and PH2-01 closed `[x]` as a result. PH2-05 implements Telegram ownership recovery/rebind only -- session logout/revoke/rotation/TTL, CSRF/Origin and fixation were already fully closed by PH1-01 and are not duplicated; `OwnershipRebindStore` reuses the same `PrimaryAdminAuthority` sealed-capability pattern `InternalEntitlementStore`/`LegacyBridgeStore` already use. `src/ownership_rebind_schema.py`/`ownership_rebind.py` (dormant, no route) reuse PH3-01's existing `mgboost_telegram_identities` table verbatim -- its own partial-unique indexes already make dual ownership structurally impossible. `process_rebind()` atomically revokes the old Telegram binding and activates the new one (CAS-checked against the caller's expected old owner, rejecting stale/concurrent/IDOR attempts before any mutation), and for `COMPROMISE` mode only, unconditionally rotates the account's PH2-01 opaque credential (with abandon+reissue on a lost-response retry, never reactivating the old token). Ordinary rebind never touches the opaque credential or any PH3-02/03/08 table; this is not a device rebind and never calls PH3-05. Tests: `tests/test_ownership_rebind.py` (16 passed); full regression `787 passed, 3 skipped`. The real isolated Marzban 0.8.4 gate (`scripts/verify_ph2_05_ownership_rebind_staging.py`) passed all 12 checks across two independent synthetic parents with real children: ordinary rebind left the remote child/opaque credential untouched, compromise rebind denied the old opaque token and issued exactly one new generation while the child UUID stayed unchanged. No real production Telegram identity was created or changed; no synthetic Telegram ID was created in production for evidence. See `ROADMAP.md` PH2-05/PH2-01 and `docs/PHASE2_OWNERSHIP_REBIND.md`.
- PH4-01 (`[~]`, dormant): implemented the legacy subscription alias bridge. `src/legacy_bridge_schema.py`/`src/legacy_bridge.py` add one explicit, root-only, per-account staged-rollout binding (mirroring PH3-03's shadow-resolver-binding pattern) -- no account is ever bridged without an `enabled=1` row created ahead of time; production ships with zero such rows. `src/opaque_resolver.py` was refactored to expose a shared `resolve_account_device()` tail so PH2-01 and PH4-01 share the identical downstream security decision (PH3-08 parent state -> PH3-04 HWID gate -> PH3-02 slot -> PH3-03 lazy child -> typed subscription fetch) after each resolves `account_id` through its own independent authority. `src/routes/sub.py` gained one minimal hook, gated by a new `LEGACY_BRIDGE_ENABLED` flag (default off) -- with the flag off, legacy `/sub` is byte-identical to pre-PH4-01 behavior (proven by test, not assumed). Per-device, not per-user: every deny decision happens strictly before any durable slot claim, so it falls through to the exact unmodified legacy response; only after a slot is durably claimed does a later failure fail closed (502), never silently handing that device the shared legacy credential. Tests: `tests/test_legacy_bridge.py` (8 passed), `tests/test_legacy_bridge_resolver.py` (11 passed), `tests/test_legacy_bridge_route.py` (3 passed); full regression `771 passed, 3 skipped`. The real isolated Marzban 0.8.4 gate (`scripts/verify_ph4_01_legacy_bridge_staging.py`) passed all 12 checks, including the bridged response omitting the shared legacy UUID while the legacy remote user stays untouched. Not yet closed `[x]`: a genuinely valid production legacy `/sub` pre/post proof could not be safely obtained this session (the raw token was not available and was not extracted from logs/backups to get it) -- an owner-assisted gate is the exact remaining step. PH2-01 was re-checked and stays `[~]` too: its own separate ownership-rebind/compromise-flow criterion depends on PH2-05 (not started), unrelated to and not resolved by this bridge. See `ROADMAP.md` PH4-01/PH2-01 and `docs/PHASE4_LEGACY_BRIDGE.md`.
- PH2-07 closed `[x]` -- no new production code required. Proved (rather than reimplemented) that PH2-01's already-built opaque resolver never reads, stores, forwards or logs a raw legacy Marzban subscription bearer: it resolves opaque token -> account -> PH3-08 parent state -> PH3-04 HWID gate -> PH3-02 slot -> PH3-03 child -> a per-device subscription fetched by a new minimal typed broker operation that never returns the child's own bearer path to the caller, let alone a shared legacy one. New static source-scan tests (`tests/test_ph2_07_no_persistent_legacy_token.py`, 12 passed) permanently guard this: no `MarzbanClient` import, no `.get_sub(` call, no legacy-table writes, no logging/print calls anywhere in the resolver/route modules, plus a mandatory negative test that fails the instant a "child resolution failed -> fall back to legacy /sub with the old token" shortcut is ever added. Full regression `749 passed, 3 skipped`; the existing PH2-01 real-Marzban gate already serves as live evidence (its `child.user.subscription.get` calls are traceable to the child's own subscription path only).
- PH2-01 (`[~]`, dormant): implemented the opaque subscription credential schema/API/resolver per the existing design doc. `src/subscription_credentials.py` issues a CSPRNG 32-byte token, stores only its SHA-256 verifier, and rotates via `(account_id, generation)` CAS that atomically revokes the previous `ACTIVE` credential; a schema trigger makes `REVOKED`/`EXPIRED` permanently terminal. The raw token is never persisted anywhere (not even encrypted) -- it is returned once, synchronously, in the issuing caller's own authenticated response, a deliberate simplification of the design doc's "recommended" AEAD envelope that avoids a new crypto dependency while meeting the same "no raw token in DB/backups/logs" bar. A new public route, `GET /<opaque_token>` (`src/routes/opaque_sub.py` + `src/opaque_resolver.py`), resolves the verifier to an account, re-checks the parent's live PH3-08 desired state, applies PH3-04's HWID gate unconditionally, and reuses the unmodified PH3-02 slot allocator and PH3-03 child-provisioning pipeline to return a per-device child subscription -- the shared legacy UUID never appears in a bridged response. A new minimal typed broker operation, `child.user.subscription.get`, fetches a child's own rendered subscription server-side without ever returning its bearer path to the caller. The route is dormant by two independent gates: `OPAQUE_SUBSCRIPTION_ENABLED` defaults off, and separately neither production nginx vhost proxies a root path to the application at all today. Tests: `tests/test_subscription_credentials.py` (15 passed), `tests/test_opaque_resolver.py` (12 passed), `tests/test_opaque_sub_route.py` (5 passed); full regression `737 passed, 3 skipped`. The real isolated Marzban 0.8.4 gate (`scripts/verify_ph2_01_opaque_resolver_staging.py`) passed all 11 checks. Not yet closed `[x]`: per explicit owner sequencing this session, the legacy alias bridge portion of this ROADMAP line's original closure bar is deliberately deferred to PH4-01.
- PH3-08 closed `[x]`. Owner-authorized closing production canary: exactly one throwaway parent (`account_id=2`) was created via the real `create_internal_plan`/`create_reviewed_account` repository methods with the same honest `NOT_APPLICABLE`/`INTERNAL` provenance as the existing account, no Telegram identity, and a finite `current_expiry`. One slot/child were provisioned through the unmodified PH3-02/03 pipeline. A real production `ACTIVE(revision 1) -> EXPIRED(revision 2) -> RENEWED(revision 3)` cycle was driven through the real `parent_sync.run_account_sync_cycle` against the real broker/Marzban: the child disabled without any expire change and with its UUID verifier byte-identical to the ACTIVE baseline, an idempotent re-run made zero further remote calls, and the renewal reactivated the exact same child/generation/UUID with the new expiry and zero new provisioning. The credential was retrievable only through the resolver-only capability post-renewal. The child was then re-suspended and a real PH3-05 `REVOKE` was run against it to prove the cross-module fix live: this time the remote UUID verifier did change, confirming a real revoke against a merely-suspended child still actually invalidates the credential. Cleanup (`REVOKE`->`FREE`) left a `RELEASED` tombstone, and the throwaway parent was set `DISABLED` -- no physical delete, no account-1 mutation. `account_id=1`'s subscription/child/shadow-binding and all 3 pre-existing PH3-05 tombstones were read-verified unchanged throughout, and account 1's own PH3-08 sync operation (from an earlier read-only dry-run) stayed `PENDING`/never dispatched. Zero raw credential leakage in any PH3-01/03/05/08 table or service journal. See `ROADMAP.md` PH3-08 for the full sequence, exact masked cardinality and evidence.
- PH3-08 (`[~]` at the time, dormant): implemented durable parent status/expiry -> active child generations sync. `src/parent_sync.py`/`src/parent_sync_schema.py` compute a canonical parent desired state (pure function of only real account/subscription status+expiry) into PH3-01's previously-unused `mgboost_entitlement_state(revision)` table, then fan it out through a durable per-child outbox mirroring PH3-03/05's exact prepare/claim/acknowledge/lease pattern. A new typed `child.user.state.sync` broker operation performs a strictly reversible suspend/resume -- only `status`/`expire`, never `proxies` -- and asserts the UUID is unchanged after every mutation; an expired/disabled parent disables current children without touching expire, and a renewed parent reactivates the *same* generation/child/UUID with zero new provisioning. Only current, non-terminal generations participate, so a renewal can never resurrect a PH3-05-revoked device. Every sync op is stamped with the parent's live revision and re-checked immediately before dispatch, so a stale enable can never win after a disable (and vice versa). This work also found and fixed a real cross-module gap: PH3-05's `child.user.revoke` broker handler used to treat any remote `status=disabled` child as already-revoked and skip re-mutation, which would have made a real revoke against a merely PH3-08-suspended child silently no-op; the idempotency check now keys off the UUID verifier instead of bare status. Tests: `tests/test_parent_sync.py` (26 passed); full regression `704 passed, 3 skipped`. The real isolated Marzban 0.8.4 gate (`scripts/verify_ph3_08_parent_sync_staging.py`) created a synthetic parent with 3 real children and passed all 16 checks, including the revoke-vs-suspend fix reproduced live and a stale-enable-after-disable race never dispatching. Not yet closed `[x]`: a real production ACTIVE->EXPIRED->RENEWED canary would require a new throwaway parent (the existing production account cannot itself be safely put through that cycle) -- a separate, explicit owner decision this task does not take on its own. See `docs/PHASE3_PARENT_SYNC.md`.
- PH3-05 closed `[x]`. A pre-canary security preflight confirmed the typed `child.user.revoke` broker operation's authorization model is sound: the real account/slot/generation/child binding is enforced by `ChildLifecycleStore.claim()` one layer above the (intentionally stateless) broker, exactly like `child.user.ensure`/`observe`; no code change was needed (`tests/test_child_lifecycle_authorization_binding.py`, 6 passed). A throwaway canary was then created on a new, server-allocated slot 2 of the existing reviewed `INTERNAL_OWNER_PRIMARY` account (within its existing 10-device entitlement, no capacity change), fully exercising REVOKE (real Marzban disable+UUID rotation, idempotent duplicate), FREE (ordering-gated on confirmed revoke, idempotent duplicate), REBIND (generation 2->3 in one atomic transaction, exactly one new remote child via the unmodified PH3-03 pipeline, idempotent duplicate, no generation 4), and a functional check (new credential retrievable only via the resolver-only ephemeral capability; the just-revoked credential's reread was denied through that same path). All three throwaway generations were then themselves revoked/freed, leaving no active test credential and a full `RELEASED`/`disabled` tombstone history (no physical delete). The existing PH3-03/04 dormant canary (slot 1/`mgc_sgg6v7t6he43yytsqmkdczzfpa`, its enabled shadow binding) was read-verified unchanged throughout and never targeted by any mutation. Legacy `/sub`, `user_devices`/`hwid_lock` (71/71), parent/alias counts and Marzban's 25 legacy users were all unaffected; a full DB/journal/nginx scan against all 4 stored child UUID verifiers found zero raw-credential leaks. See `ROADMAP.md` PH3-05 for the full sequence and evidence.
- PH3-05 (`[~]` at the time, dormant): implemented durable device revoke/free/rebind lifecycle. `src/child_lifecycle.py`/`src/child_lifecycle_schema.py` add a hash-idempotent state machine mirroring PH3-03's outbox exactly; the hard ordering guarantee -- never free a slot before the matching remote revoke is durably confirmed -- is structural, not a convention. A new typed `child.user.revoke` broker operation (server-derived `{operation_id, child_username, uuid_verifier}` only, no caller-supplied UUID/proxies) disables the Marzban user and rotates its VLESS UUID in one mutation, then rereads/verifies. Rebind revokes the old child first, atomically swaps to the next generation on the *same* slot (`DeviceSlotStore.rebind()`), then hands off to the unmodified existing PH3-03 provisioning pipeline for the new child -- no parallel code path. Cross-account isolation, no caller-suppliable slot/generation, zero coupling to Telegram ownership/account tables, and DL-019/038's 180-day tombstone retention (as a pure eligibility check; no physical-DELETE path exists) are all covered. Tests: `tests/test_child_lifecycle.py` (27 passed), `tests/test_child_lifecycle_retention_and_broker.py` (15 passed); full regression `673 passed, 3 skipped`. The real isolated Marzban 0.8.4 gate (`scripts/verify_ph3_05_lifecycle_staging.py`) passed all 18 checks: real disable+UUID rotation confirmed by authoritative reread, idempotent duplicate revoke, ordering-gated free, rebind producing exactly one new remote child while the old one stayed disabled, a real outage failing closed, and zero raw credentials in the DB dump. Not yet closed `[x]`: a separate owner-approved destructive production canary gate is required before real production revoke/free/rebind evidence exists; this task's brief explicitly forbade touching the existing approved dormant canary, so none was attempted. See `docs/PHASE3_CHILD_LIFECYCLE.md`.
- PH3-04 closed `[x]`. Production deployment was a code-only fast-forward (no new schema) after a verified encrypted backup; only `mgboost-panel` restarted. `PRAGMA quick_check`/`foreign_key_check`, all parent/slot/child/outbox/shadow-binding/device cardinality, legacy `/sub`, admin/LK/Filin, broker isolation and journal error count all matched the pre-deploy snapshot exactly -- zero UUID/token/HWID/expiry/tariff/config changes. `PH3_04_ENFORCEMENT_MODE` remains `OFF`; the gate is imported by no route.
- Implemented PH3-04's dormant HWID fail-closed compatibility gate: a git-tracked, exact-match-only `(client, version, platform)` allowlist (`src/compat_registry.py`) sourced from a fresh production PH3-07 snapshot (DL-047 accelerated conservative allowlist -- no fuzzy matching, no `UNKNOWN` treated as supported), and a deterministic policy layer (`src/hwid_gate.py`) that accepts no caller-suppliable slot id/generation/child identity/Telegram proof and reuses PH3-02's existing atomic slot-claim primitive unmodified. Neither module is imported by `src/routes/sub.py` or any other route; `PH3_04_ENFORCEMENT_MODE` defaults to `OFF` and has zero runtime effect. Tests (`43 passed`) cover compatibility classification, missing/malformed HWID, exact 3/6/12 and INTERNAL-unlimited capacity, concurrency, cross-account/copied-HWID security, reinstall semantics and zero coupling to ownership/child/outbox tables. Full regression `631 passed, 3 skipped`; an isolated gate against a disposable copy of the live production DB passed with zero drift to any pre-existing row. See `docs/PHASE3_HWID_GATE.md`.
- PH3-03 closed `[x]`. Gate A (accelerated ~19-minute production SHADOW observation) recorded 11 evaluations against the approved canary binding -- 4 organic from the real approved device and 7 clearly-labeled controlled evaluations exercising the same resolver code path without mutating device state -- with 100% `PASS`/`MATCH`, 0 FAIL/MISMATCH, `credential_result=SUCCESS` and `legacy_fallback_success=1` throughout, zero cardinality drift and zero credential leakage in a full DB/journal/nginx scan. Gate B: a single real Marzban-rendered child subscription line was exported to a root-only file, retrieved and QR-encoded entirely locally (no network service, raw value never printed), and scanned by the owner into a **separate temporary INCY profile on a Samsung M21** -- the primary iPhone 17/production INCY profile was untouched. The owner confirmed QR import, parser/import, connection, real traffic and egress verification (`2ip.ru` showed the VPN/server IP) all passed; no field-by-field visual comparison is claimed beyond that. All temporary/export artifacts were securely deleted and their absence verified. A post-Gate-B production check reconfirmed identical cardinality, `PRAGMA quick_check`/`foreign_key_check`, service health, broker isolation and zero leakage. Residual risk (explicitly not a blocker): the SHADOW observation window was accelerated (~19 minutes/11 evaluations) rather than a multi-day soak.
- Production SHADOW-only deployment of the PH3-03 dual-run resolver completed for the single approved canary. A verified encrypted backup preceded a clean fast-forward deploy; the additive schema, an independent `MARZBAN_BROKER_RESOLVER_AUTH_KEY` and the one `enabled=0`-then-enabled shadow binding were installed with only the broker/panel restarted. Production confirmed the capability split live (`mgboost-main` denied, `mgboost-sub-resolver` allowed only `child.user.credentials.get`), and both controlled and organic real device requests kept the actual `/sub` HTTP response legacy (legacy UUID present, child UUID absent) while recording one real `PASS`/`MATCH` shadow metric with zero raw-credential leakage across DB/journals/nginx logs. Parent/slot/child/outbox cardinality, legacy UUID/token/HWID/expiry/tariff and `user_devices`/`hwid_lock` counts are all unchanged. iPhone/INCY's real subscription profile was not switched.
- Implemented the PH3-03 dual-run SHADOW-mode account-aware subscription resolver in `src/shadow_resolver.py`, completing a broker capability split a prior session had left partially wired. The legacy `/sub` resolver still builds every actual HTTP response by itself and is untouched; the shadow resolver runs afterward on a background thread purely for comparison/metrics and cannot change, delay or replace that response under any failure. `child.user.observe` keeps using the existing `mgboost-main` broker identity; only the new `child.user.credentials.get` read moves to a separate `mgboost-sub-resolver` identity/key, so the main broker client can no longer read a raw child credential at all. A binding only exists for a device the new root-only `scripts/configure_ph3_03_shadow_binding.py` tool opts in for the single approved canary, and only ever applies to `hwid:`-locked legacy devices. Functional comparison clones each raw legacy VLESS line and substitutes only the ephemeral child UUID, then asserts every other field (address/port, transport, TLS/Reality/SNI, flow, all other query parameters) is byte-identical, tolerating only the UUID and the display remark/label as expected differences; this also performs an INCY-equivalent parser/format round trip. Metrics are a privacy-safe daily PASS/FAIL aggregate with no raw UUID/token/username/IP/header ever persisted or logged. The real isolated Marzban 0.8.4 gate (`scripts/verify_ph3_03_shadow_resolver_staging.py`) passed against the same immutable digest used by every other PH3-03 gate, reproducing the exact production-live server-derived identity and proving the capability split and all 8 failure-matrix categories with real broker/Marzban actions (real shutdown, real delete, real expire/UUID drift, real `docker pause`). New tests: `tests/test_broker_client_policies.py` (6 passed), `tests/test_shadow_resolver.py` (19 passed) and `tests/test_shadow_binding_tool.py` (13 passed, covering create/duplicate/conflict/wrong-account/wrong-device/stale-generation/not-APPLIED/not-IN_SYNC/unexpected-cardinality). Full regression: `588 passed, 3 skipped`.

- Added and production-verified the PH3-03 durable child worker/reconciler: DB-backed leases, bounded exponential retry, lost-ACK reread convergence, explicit mismatch/manual-review states, privacy-safe operational metrics and a typed read-only `child.user.observe` broker operation. The real isolated Marzban 0.8.4 gate passed exactly-one creation across worker/broker restart, stale lease and outage recovery. Production runs `reconcile_only` against the one allowlisted existing dormant canary; it reached `IN_SYNC` through read-only reread with no create/retry/divergence and no legacy credential/config/device change. Resolver/client behavior remains unchanged.

- Added and production-verified the fixed-scope, root-only PH3-03 dormant canary gate. Exactly one reviewed `INTERNAL_OWNER_PRIMARY` parent, its three approved immutable aliases, slot 1/generation 1, durable child intent/outbox and one VLESS-only Marzban child now exist. The outbox is `APPLIED`, repeated ensure is `EXISTING`, exact 25-inbound/source semantics match and raw child UUID/token leakage is zero. The child remains dormant: no legacy resolver, URL/token, UUID, HWID, expiry, tariff or client config was switched or revoked, and PH3-04 remains off.

- Corrected the production compatibility verifier for Marzban 0.8.4 timestamped `subscription_url` aliases and PH1-06 hash-only legacy token storage. Canary invariants now compare `created_at`/`sub_revoked_at`, all 45 stored token verifiers, full legacy identity/config digests and 71 device/HWID rows without treating newly serialized aliases as credential rotation or trying to use a SHA-256 verifier as a bearer.

### Security

- The Marzban broker's `mgboost-main` client no longer has any way to obtain a raw child VLESS UUID: `child.user.credentials.get` moved to a separate, independently keyed `mgboost-sub-resolver` broker identity that is authorized for no other operation. `BrokerApplication` now supports per-client HMAC keys and an explicit allowed-operation allowlist (`broker_main.py`, `src/broker_server.py`); omitting the new environment variable reproduces the exact prior single-client broker unchanged. Not deployed: the new key is unset in every environment.
- DL-046 makes MGBoost explicitly VLESS-only. The authenticated typed broker cleanup removed retired Shadowsocks proxy metadata from all seven affected production users after canary/reread gates. All 25 live users now have only VLESS proxies; topology and all fetched subscriptions also contain zero Shadowsocks. Existing VLESS UUID, legacy URL/token, inbound, flow, expiry, status, data limit, HWID, tariff and functional config remained unchanged.
- The first PH3-03 isolated Marzban 0.8.4 FAIL remains as historical evidence that disabled legacy Shadowsocks metadata was not a viable child contract. After DL-046 cleanup, the exact-image VLESS-only rerun passed exact-25-inbound create/reread, idempotent retry, lost-ACK reconciliation, ephemeral credential verification, contract-drift failure and Marzban-outage behavior without storing/logging raw child credentials. No production child/account/slot was created.
- PH3-03 prerequisites replace caller-supplied privileged actor strings with a sealed server-derived primary-admin capability: the stable audit actor is usable only after an authenticated server-side admin session matches a protected login allowlist. A typed authenticated localhost `child.user.ensure` operation was added without changing the ten legacy broker operations; it accepts no caller UUID/password/proxy/inbound payload and verifies freshly generated child credentials after reread. These changes are not deployed or activated on production yet.
- PH2-04 is deployed: all application response paths receive a central cache/referrer/frame/nosniff/permissions/CSP baseline, runtime versions and unused docs/debug routes are hidden, subscription failures no longer relay upstream details, and upstream subscription response headers are allowlisted. After a successful report-only gate, strict browser subscription CSP is enforced; nginx hides its version and serves one-year HSTS on active HTTPS product hostnames and sensitive locations. Masked production compatibility remained exact.
- PH2-03 compatibility layer is deployed: Internal/Filin HMAC nonces now use a hashed durable SQLite CAS with TTL/capacity limits and fail closed on store outage; a real signed v1 nonce is rejected after MGBoost restart. Optional signed v2 mutation idempotency blocks completed, conflicting and crash-pending retries without storing raw keys or responses. Legacy v1 remains compatible and enforcement stays disabled until the external mutation caller adopts stable v2 operation IDs, so PH2-03 is not yet marked complete.
- PH2-02 is deployed on production: LK/API-controlled device names, usernames, node names and errors now render through explicit DOM nodes and `textContent`; device actions use validated opaque IDs with `addEventListener`, and all LK inline handlers/HTML-string render sinks were removed. The subscription browser copy page no longer embeds its bearer URL in inline JavaScript. Malicious-value rename/delete Chromium tests, full regression and the production masked compatibility gate pass with zero user credential/config changes.
- Устранён stored/DOM XSS в админ-панели: API-controlled User-Agent, username, note, node/config names, inbound tags и другие динамические значения теперь экранируются единым безопасным render path; inline JavaScript handlers удалены, включена строгая script CSP.
- Marzban JWT удалён из browser `localStorage` и API responses. Админка использует CSPRNG opaque server-side session cookie (`Secure`, `HttpOnly`, `SameSite=Strict`), CSRF protection, TTL, logout/revoke/rotation и защиту от session fixation/login CSRF.
- Прямые browser-вызовы Marzban заменены explicit server-side path/method broker; upstream auth failure отзывает локальную admin session, а legacy browser Bearer authentication больше не принимается.
- Privileged service-интеграция Marzban вынесена в отдельный HMAC-authenticated localhost-only broker с десятью typed legacy operations и без generic proxy. Основной MGBoost в broker mode отклоняет Marzban SUDO credentials в своём environment; public legacy `/sub/{token}` остаётся прямым non-SUDO path.
- Telegram Stars pre-checkout теперь fail-closed проверяет доступность и eligibility целевого Marzban user перед подтверждением новой оплаты; уже полученный `successful_payment` по-прежнему сначала сохраняется durable и остаётся retryable при outage.
- Admin login получил failed-attempt rate limiting с отдельными IP+username/IP-spray budgets, `429`/`Retry-After`, validated proxy IP и bounded hashed in-memory keys; direct public Marzban `/api/admin/token` получил nginx `limit_req` deployment template.
- Production Marzban SUDO username/password заменены атомарно на высокоэнтропийную CSPRNG service-пару после broker isolation. Старые password и admin JWT отозваны; общий JWT signing secret и все legacy subscription credentials намеренно сохранены. Новый active и отдельный fallback credential доступны только владельцу через root-only files и не выводились в логи.
- PH1-06 развёрнут на production: локальные subscription references хранятся как SHA-256 verifier, application paths/query и nginx sensitive routes не логируют raw bearer, а LK удаляет legacy `?token=` из browser URL и далее использует same-origin header. Реальные legacy token/URL не вращались и продолжают работать до staged Phase 4 migration.
- PH1-07 dependency hardening развёрнут на production без обновления Marzban application/schema: immutable Marzban 0.8.4 base использует hash-pinned `python-multipart 0.0.32`, а MGBoost — изолированный hash-locked Python runtime с `aiohttp 3.14.3`/`aiogram 3.30.0`/`aiohttp-socks 0.12.0`. Masked semantic pre/post gate подтвердил нулевые изменения UUID, legacy token/URL, expiry, HWID/device bindings, tariffs и рабочих VPN-конфигураций.
- PH1-08 Marzban login-notification hardening развёрнут на production: failed-login Telegram/Discord report получает фиксированный `🔒` вместо введённого пароля. Узкий AST-validated build patch не меняет password validation, login HTTP contract или пользовательские VPN credentials; production canary при включённых notifications не появился в report/response/container/journal/nginx logs.

### Operations

- PH6-07 production WL enforcement runtime (2026-08-28, реализовано локально
  от `4c9d832`; checkpoint only — НЕ запушено, НЕ задеплоено, production
  read-only): существующий on-demand PH6-06 enforcement превращён в
  постоянно работающий, crash-safe и наблюдаемый runtime БЕЗ второго
  enforcement engine и без второго outbox. Новый systemd timer
  `mgboost-wl-enforcement.timer` + hardened oneshot
  `mgboost-wl-enforcement.service` запускают единственный оркестрированный
  цикл (`src/wl_reconciliation.py::run_wl_reconciliation_cycle` через
  `scripts/run_wl_quota_enforcement.py`): overlap запрещён non-blocking
  flock-локом (параллельный timer/manual запуск → `SKIPPED_BUSY`), crash
  между шагами восстанавливается по durable op-rows, `TimeoutStartSec=900`,
  в journald только агрегатный JSON без идентификаторов; cadence —
  технические настраиваемые 15 минут, БЕЗ claims о максимальном overshoot
  (PH6-09 отдельно). Каждый цикл: fresh PH6-01 topology assertion
  (fail-closed блокирует весь цикл), canonical PH6-04 pool
  (`resolve_current_parent_wl_pool`, inline-дублирование в engine убрано),
  штатный decision/dispatch/finalize pass, затем post-terminal drift scan:
  уже ACTIVE/DISABLED аккаунты пере-наблюдаются на каждом цикле;
  `WL_PRESENT_WHILE_EXCLUDED`/`WL_MISSING_WHILE_INCLUDED` чинятся через
  СУЩЕСТВУЮЩУЮ машину (новый same-direction epoch только по реально
  drift-нутым детям, exactly-once by observation, non-WL membership
  byte-stable); `REMOTE_MISSING`/`UUID_MISMATCH`/`NON_WL_MEMBERSHIP_LOST`/
  `WL_UNEXPECTED_WHILE_INCLUDED` → `ERROR_RECONCILE` без единой мутации,
  без auto-create, без угадываний; transient-сбоны наблюдения не считаются
  drift. Документированный gap «newly-added approved WL inbound» закрыт
  (только после operator-approved versioned baseline update; unknown
  wl-like теги по-прежнему fail-closed). Legacy UNLIMITED/STANDARD
  структурно невидимы (P0 abstain contract сохранён). Наблюдаемость:
  additive миграция `ph6_07_wl_reconciliation_v1` — append-only таблицы
  cycles (heartbeat) + drift evidence и identifier-free read model
  `backlog_snapshot()` (last/ok cycle, topology, состояния аккаунтов,
  op/backlog counts, oldest backlog age, drift counters, last error class).
  Ожидаемый steady-state после deploy на текущем production (0 ACTIVE
  WL-периодов, enforcement таблицы пусты): timer работает, cycles `OK`,
  remote WL mutations = 0, drift = 0 — до создания LIMITED WL canary.
  Regression: новый suite `tests/test_wl_reconciliation.py` 18 passed;
  targeted PH6-01..06 + P0 hotfix + broker 190 passed; full regression —
  см. AGENT_HANDOFF.
- PH6-07 independent senior review (2026-08-28, до продакшн-деплоя): найдены
  и исправлены два реальных дефекта. (1) P1 -- `mgboost-wl-enforcement.
  service` был склонирован с local-only shape `mgboost-compat-telemetry-
  cleanup.service` и не подключал `EnvironmentFile=/opt/MGBoost_Panel/.env`,
  из-за чего каждый scheduled cycle падал бы на пустом Marzban broker
  auth key; юнит приведён к shape `mgboost-child-worker.service` (тот же
  паттерн исходящих broker-вызовов) с полным hardening-набором и
  `Wants`/`After` на broker; добавлен regression-тест, проверяющий
  наличие `EnvironmentFile` в юните. (2) P1/P2 -- TOCTOU в
  `scan_terminal_drift`: `pool`/`desired` читались один раз в начале
  per-account итерации, ДО реальных сетевых round-trip'ов на каждого
  ребёнка; если entitlement менялся в этом окне (закрытие периода,
  concurrent ledger write), repair epoch мог открыться против уже
  устаревшего решения. Исправлено пересчётом `pool`/`desired`
  непосредственно перед `open_repair_epoch`: при рассинхроне repair тихо
  отменяется для этого цикла (регулярный decision path сам себя чинит
  на следующем цикле); добавлен regression-тест
  `test_entitlement_change_mid_scan_never_opens_stale_repair`. Всё
  остальное (open_repair_epoch CAS/epoch guard, latest_include_baseline
  allowlist-intersection safety, legacy.user.get как единственный child
  observation path, flock lock file рядом с БД вне `/tmp`/`PrivateTmp`,
  UNLIMITED/STANDARD structural abstain, newly-added-WL repair для
  suspended, WL_UNEXPECTED_WHILE_INCLUDED conservative flag-only для
  ACTIVE) подтверждено самостоятельным построчным чтением и
  независимым воспроизведением тестов -- без исправлений. Полный
  regression после фиксов: `1451 passed, 4 skipped` (skips -- только
  Playwright e2e, окружение без него). Read-only production preflight
  через SSH подтвердил ожидания checkpoint: HEAD `4c9d832`,
  `quick_check=ok`, `foreign_key_check` пуст, `mgboost_wl_enforcement_
  states/ops`=0/0, 0 wl_mode=LIMITED аккаунтов (2 NONE/STANDARD ACTIVE,
  17 UNLIMITED), STANDARD canary exact WL intersection живым запросом =
  0, легаси UNLIMITED sample содержит все 12 WL-тегов легитимно.

### Changed

- После обновления администратору потребуется войти заново: legacy `mz_token` очищается, а admin asset использует новый cache-buster. Текущие admin sessions являются process-local и завершаются при перезапуске MGBoost.

### Operations

- PH3-03 durable prerequisites remain dormant and are not production-activated: immutable one-parent/many-legacy-alias evidence, account/slot-generation-scoped child intent, leased idempotent outbox and append-only reconciliation events. The approved `INTERNAL_OWNER_PRIMARY` manifest and one `beykusios` canary are documented without creating any production account, alias, slot, outbox row or child. The mandatory real Marzban 0.8.4 VLESS-only gate now passes; the first production parent/slot/child mutation still requires separate owner authorization.
- PH3-09 provenance foundation is deployed as dormant additive immutable payment records and account-scoped payment-to-mutation links. Payment channel and mutation source are explicit and idempotent; username/note inference and cross-account references are rejected. The new tables remain empty, and current Stars/manual expiry behavior, credentials and configs are not connected or changed.
- PH3-06 internal-entitlement foundation is deployed as a dormant additive schema/repository: reviewed internal accounts and reasoned expiring overrides require an explicit primary-admin actor, use immutable versioned plans instead of username rules, and preserve ordinary/legacy runtime. Production leaves that actor unset and all new account/review tables empty, so provisioning remains fail-closed until an owner-reviewed canary is selected; masked legacy state is unchanged.
- PH3-07 privacy-safe client/HWID compatibility telemetry is deployed. It is observe-only and fail-open, stores bounded client/version/platform aggregates plus a subscription-scoped HMAC verifier rather than raw bearer/HWID/UUID/user data, enforces 30-day detail and 60-day identifier-free rollup retention through an enabled daily timer, and leaves legacy `/sub`, accounts, slots and child users unchanged. The initial live sample is intentionally not used to enable fail-closed; historical aggregate evidence identifies missing-HWID client families that require PH3-04 compatibility work.
- PH3-02 dormant device-slot schema is deployed: commercial 3/6/12 and INTERNAL configurable/unlimited capacity, atomic multi-process claims, privacy-safe HWID verifiers and monotonic generation reuse are implemented without connecting legacy users or `/sub`. New parent/slot/generation tables remain empty; exact masked 25-user/config and 71-device/HWID state, Stars, Filin, broker, Telegram and token-safe logging gates passed with zero credential/config/expiry/tariff changes.
- PH3-01 additive parent-account schema is deployed: the transactional/idempotent migration preserved all legacy tables exactly and created no accounts, plans, subscriptions, identity links or backfill. Production started with all ten new runtime tables empty; the 25-user/71-device masked compatibility gate and legacy admin/LK/subscription/Stars/Filin/broker checks remained exact. An older application can roll back safely while ignoring the additive tables.
- Добавлены `ADMIN_SESSION_TTL_SECONDS` и `ADMIN_SESSION_COOKIE_SECURE`; Secure cookie обязателен в production HTTPS, отключение разрешено только для локальной HTTP-разработки.
- Production permissions существующих Marzban secrets/data сужены: `.env`, live SQLite, Xray credential config и найденные config backup copies теперь `0600 root:root`, private Marzban data directory — `0700`. MGBoost active `.env`/SQLite уже соответствовали `0600` и сохранены; runtime, access-denial и временный backup/restore smoke проверены.
- Регулярные MGBoost/Marzban SQLite backups теперь создаются ежедневным root-only systemd timer, шифруются GnuPG AES-256 и считаются успешными только после isolated restore/checksum/quick-check; retention — 90 дней.
- MGBoost production service переведён с root на dedicated `mgboost` system user. Systemd unit теперь ограничивает capabilities, filesystem writes, `/proc`, namespaces/devices/home/tmp и использует restrictive `UMask=0077`; effective exposure score снижен с 9.6 `UNSAFE` до 2.8 `OK`, runtime/SQLite/HTTP smoke пройдены.
- Добавлен обязательный no-user-impact gate перед PH1-05: зафиксированы production legacy subscription/config/UUID/expiry/HWID contracts, отдельная localhost broker topology, exact current Filin/Stars/LK/bot operation matrix, outage/rollback требования и regression tests. До реализации и staging-проверки broker полный security cutover помечен `NOT SAFE TO DEPLOY`.
- PH1-01 и PH1-05 развернуты на production отдельными verification gates. Владелец вручную подтвердил admin login без визуальной регрессии; exact legacy alias, aggregate UUID/expiry/access/config и 71 device/HWID bindings совпали до/после и после restart. PH1-05 broker работает отдельным Unix identity на `127.0.0.1:8002`, не опубликован nginx и не читает repository `.env`; основной MGBoost больше не содержит Marzban SUDO keys. Broker outage оставляет legacy `/sub` рабочим, а privileged/Filin operations fail closed и восстанавливаются после restart. Изменений UUID, subscription URL/token, HWID, тарифов, expiry и обязательной перенастройки клиентов — 0.
- PH1-02 развернут и проверен на production: nginx и application login rate gates активны, новая SUDO service-пара принята, старая пара и старый admin JWT отклоняются. После controlled Marzban recreate frozen legacy `/sub`, config/UUID/expiry, 25-user aggregate, 71 device/HWID bindings, admin/LK/Stars/Filin и broker read contracts совпали; пользовательских credential/config migrations — 0.
- PH1-06 production gate завершён: one-time encrypted quarantine snapshot (180 дней) и pre/post-migration DB backups прошли restore; 198 request и 71 device raw-token rows атомарно заменены verifier-ссылками; strict canary scan подтвердил 0 новых raw bearer в nginx/application/journal/active DB/backups. Masked 25-user/71-device state и legacy config/UUID/expiry остались без изменений.
- PH1-07 staging прошёл на production Python 3.10 и двух isolated Marzban containers (baseline/target): full regression, all-10 broker operations, legacy `/sub`, UUID/config continuity, Filin, restart/outage, parser load и Telegram SOCKS proxy совместимы. OpenRouter возвращает одинаковый pre-existing HTTP 403 на старом и новом runtime; это не dependency regression и зафиксировано как внешний operational residual.
- PH1-07 production cutover выполнен двумя gates с проверенным rollback между попытками: Marzban parser overlay и MGBoost isolated runtime включены, admin/LK/Stars/Filin/broker/25 legacy subscriptions и token-safe logs прошли smoke. Verifier учитывает существующую динамическую info-node и случайный Reality `sid`, но продолжает строго сравнивать credentials/UUID, endpoints, transports/TLS и остальные config fields.
- PH1-08 isolated staging прошёл на реальном Marzban 0.8.4 contract: built-image AST/report capture, настоящий failed login при включённом `NOTIFY_LOGIN`, all-10 broker operations, legacy `/sub` при broker outage/restart, Filin create/renew/delete и full Python 3.10 regression совместимы; canary password не появился в report/response/container logs.
- PH1-08 production image-only cutover и финальный Phase 1 gate завершены: все PH1-01–PH1-08 закрыты, 25 legacy users/configs и 71 device/HWID bindings совпали, admin/LK/Stars/Filin/broker/backups/nginx/systemd healthy, новых raw subscription paths и стабильных runtime errors нет. Изменений UUID, legacy URL/token, HWID, expiry, тарифов и обязательной перенастройки клиентов — 0.

### Documentation

- Admin redesign architecture and owner-approved UX decisions documented; no runtime/admin behavior changed. A read-only audit of the current admin frontend (`frontend/index.html`/`admin.js`) and backend (`src/routes/admin*.py`) found it is still entirely Marzban-username-centric, with zero UI for the parent-account/migration/grace/opaque-credential/device-slot domain that has been live in production since PH2–PH4. Findings, target account-centric navigation, read-model plan (`AccountSummary`/`AccountDetail`/`DeviceSummary`/`MigrationGraceSummary`/`SubscriptionSummary`/`PaymentSummary`/`AuditEvent`) and implementation waves (A: foundation/read-only UI, can start now; B: PH7-01/05/08 safe mutations; C: PH5-backed; D: PH6-backed) are recorded in new `docs/ADMIN_PANEL_REDESIGN.md`. Five owner decisions recorded as `ROADMAP.md` `DL-048`..`DL-052`: technical identifiers (raw `mgc_*`/generation/outbox ids, full UUID/HWID) hidden by default behind a `Technical` tab; PH7-05 Wave B ships Disable/Enable, Revoke, Free and Rebind as four distinct confirmed operations, never one generic delete; the legacy Marzban-username `Users` screen moves immediately under `System/Technical`, not deleted, while `Accounts` becomes the primary top-level surface; Dashboard priority is Grace campaign (conditional, collapses after grace ends) → operational health → expiring soon, with Tickets kept to a compact counter; frontend stays vanilla JS split into ES modules, no framework rewrite. No PH7 item status changed (`PH7-01`..`PH7-11` remain `[ ]`); no schema, runtime code, or production state changed.
- PH2-01 design-only contract is fixed without changing runtime behavior: exact 256-bit opaque token format/root route, hash-only account-bound schema, one-time delivery/rotation CAS, public resolver API, legacy alias bridge and Phase 3/4 implementation dependencies are documented. Existing legacy URLs/tokens are unchanged.
- Добавлен канонический `ROADMAP.md`: current-state baseline, security remediation P0–P3, phases 0–8, утверждённые тарифы/packages, child-device и WL architecture, migrations, тестовые gates, Open Product Decisions и Decision Log.
- Зафиксировано правило обязательного одновременного обновления roadmap и changelog.
- Roadmap дополнен reseller architecture: explicit subscription source/ownership, scoped capabilities, reseller-aware migration, отдельные billing/WL/device semantics, audit/reconciliation и нерешённые продуктовые вопросы.
