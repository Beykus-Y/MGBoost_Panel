# MGBoost Panel — canonical roadmap

Статус: **главный источник истины для модернизации MGBoost Panel**.
Production implementation commit: `70accb7` на 2026-08-24; исходный audited baseline: `ccc1b4d` на 2026-08-23.
Владелец продуктовых решений: пользователь/владелец MGBoost Panel.

## Правила ведения

Статусы:

- `[ ]` — не реализовано;
- `[~]` — реализовано частично; обязательно описать остаток;
- `[x]` — подтверждено в `main` тестами и документацией;
- `[!]` — отменено/неактуально; сохранить причину и дату.

После каждой задачи:

1. Обновить статус по фактическому состоянию `main`.
2. Добавить обнаруженные подзадачи со стабильным ID.
3. Не оставлять реализованное как `[ ]`.
4. Не удалять неактуальное бесследно.
5. Обновить `CHANGELOG.md` в том же commit/PR.
6. Обновить Decision Log, если принималось продуктовое решение.

### Changelog обязателен

Канонический changelog — `CHANGELOG.md`. Изменение кода без одновременной записи в changelog не завершено. Это обязательно для тарифов, цен, WL, device slots, subscriptions, migration, admin/security behavior, API, клиентов, purchase/renewal, manual/external-payment behavior и Marzban integration. Security entry не должна раскрывать exploit payload или секрет.

### Не принимать продуктовые решения за владельца

При неописанной продуктовой неоднозначности исполнитель обязан остановиться, задать вопрос, предложить 2–4 варианта с последствиями и рекомендацией, дождаться ответа, записать его в Decision Log и лишь затем продолжить. Нельзя самостоятельно выбирать цены, сроки package, rollover, downgrade, отключаемые devices, grace period, child deletion, максимальный device limit, billing или refund semantics. Стандартные безопасные технические детали без продуктового эффекта можно выбирать самостоятельно и покрывать тестами.

## Каноничность и superseded

- До этого файла в репозитории не было TODO, roadmap или changelog.
- `README.md` остаётся operational overview и не является roadmap.
- Упоминания старых Phase 1/2 и внешнего `phase2_stars_design.md` в комментариях `src/database.py`/tests — исторический контекст. Их будущие планы superseded этим документом; уже реализованное поведение проверяется по коду.
- Security-аудит 2026-08-23 использован как обязательный evidence baseline; его F-01–F-16 перенесены в задачи ниже.

# Current-state baseline

## Текущая архитектура

    Internet
      -> nginx TLS
         -> MGBoost HTTPServer 127.0.0.1:8001
            -> SQLite data/db.sqlite3
            -> Marzban API с полным SUDO
            -> Telegram bot thread
            -> OpenRouter
         -> Marzban UI/API 127.0.0.1:8000
            -> Marzban SQLite
            -> Xray main/remote nodes

Подтверждено кодом:

- `src/server.py` использует single-threaded `HTTPServer`;
- `mgboost-panel.service` запускает MGBoost как `root`;
- после PH1-01 `src/security.py` использует opaque process-local admin sessions, а Marzban JWT хранится только server-side; browser Marzban calls идут через explicit broker `src/routes/admin_proxy.py`;
- `src/routes/internal.py` позволяет lookup/create/delete/renew через HMAC API;
- `src/routes/sub.py` принимает legacy Marzban bearer и передаёт client headers upstream;
- `src/database.py` хранит raw token в `sub_requests`, `hysteria_stats`, `user_devices`;
- `src/device_headers.py` извлекает client-controlled HWID; без `hwid:` enforcement пропускается;
- Stars сейчас продлевает `expire`, но не реализует account/plan/WL architecture.

### Текущий manual/external-payment flow и реальные boundaries

В продуктовой терминологии владельца «reseller subscription» означает не стороннего reseller, а прямую оплату пользователем вне Telegram Stars: перевод/иной внешний способ, после которого основной MGBoost admin вручную продлевает существующую подписку через админку. Отдельного reseller tenant, reseller identity, balance, wholesale/margin или customer ownership нет и не требуется.

Текущий MGBoost не имеет отдельной payment/order сущности для такого действия: администратор меняет Marzban user/expiry, но система не сохраняет структурированные `EXTERNAL_PAYMENT` amount/currency/method/reference и не связывает их с entitlement transaction.

Внешний Filin-подобный automation caller — отдельный unrelated integration. Он использует общий HMAC boundary из `src/security.py` и generic routes `src/routes/internal.py`:

- `GET /internal/v1/users` перечисляет Marzban users без reseller scope;
- `POST /internal/v1/users` принимает caller-supplied username, proxies, inbounds, expiry, data limit, note, status и next plan;
- `POST /internal/v1/users/{username}/renew` меняет expiry, data limit и status;
- `DELETE /internal/v1/users/{username}` удаляет указанного Marzban user;
- caller identity не связывается с payment/order record или account ownership;
- MGBoost выполняет операции своим полным Marzban SUDO;
- create/renew/delete не создают external-payment-specific immutable audit trail.

Production nginx дополнительно ограничивает internal route известным source IP, а HMAC содержит method/path/timestamp/nonce/body hash. Это защищает transport/caller secret, но generic API всё равно имеет широкий scope. Его нельзя считать manual-payment admin session или доказательством оплаты.

Следовательно, payment channel нельзя восстанавливать по username/prefix/note. Историческая manual/external-payment provenance существует только там, где есть отдельное доказательство перевода/admin action.

Текущий flow:

    legacy Marzban token
      -> /sub/{token}
      -> Marzban subscription + info(username)
      -> optional local HWID check
      -> один shared Marzban username/UUID
      -> filtered config

Telegram ID — идентификатор, не credential. Текущая БД содержит таблицы `sub_requests`, `settings`, configs/filters/stats, `user_devices`, `hwid_lock`, `tg_users`, tickets, `audit_log`, management codes/sessions и Stars tariffs/invoices/orphans. Нет parent accounts, plan versions, entitlements, slots/generations, child mapping, WL periods/ledger/outbox; foreign keys и DB-level max-slots constraint отсутствуют.

## Production baseline

- Deployed HEAD совпадает с `ccc1b4d`.
- На production уже есть untracked `extra_configs.json`; не удалять без inventory/retention решения.
- Marzban 0.8.4; partial-update contract перепроверять при каждом upgrade.
- Два физических WL nodes, usage coefficient на момент аудита `1.0`.
- Exact live WL tags на 2026-08-23:

  1. `wl-selec-grpc-direct`
  2. `wl-selec-grpc-direct-5post`
  3. `wl-selec-grpc-direct-yandex-maps`
  4. `wl-selec-grpc-smart`
  5. `wl-selec-grpc-smart-5post`
  6. `wl-selec-grpc-smart-yandex-maps`
  7. `wl-tcp-direct`
  8. `wl-tcp-direct-5post`
  9. `wl-tcp-direct-yandex-maps`
  10. `wl-tcp-smart`
  11. `wl-tcp-smart-5post`
  12. `wl-tcp-smart-yandex-maps`

Это baseline, ещё не runtime versioned allowlist. Fuzzy `wl` matching запрещён: ранее найдены stale WL-like host records.

Production Stars: inactive canary 1 day/1 Star; active 30 days/199 Stars; active 60 days/349 Stars. Последние цены совпадают с будущим WL plan, но current schema не хранит plan identity, device limit, WL quota или два WL periods. Нельзя считать новую сетку реализованной.

## Целевая архитектура

    MGBoost account
      -> account source: DIRECT | INTERNAL | future source
      -> payment channel: TELEGRAM_STARS | EXTERNAL_PAYMENT | ADMIN_GRANT
      -> mutation source: DIRECT_PURCHASE | MANUAL_PAYMENT | ADMIN | MIGRATION | INTERNAL
      -> plan/version + effective entitlements
      -> opaque subscription token verifier
      -> immutable WL periods/adjustments
      -> Slot 1 -> HWID -> child Marzban user -> UUID
      -> Slot 2 -> HWID -> child Marzban user -> UUID
      -> Slot N -> HWID -> child Marzban user -> UUID

    collector -> monotonic ledger -> desired state + outbox
                                           -> narrow Marzban broker
                                           -> reread/verify/reconcile

# Security gates

| Severity | Задачи | Блокирует |
|---|---|---|
| P0 | PH1-01 | Любое расширение админки |
| P1 | PH1-02, PH1-03, PH1-04, PH1-05, PH1-06 | Новый subscription/device rollout |
| P2 | PH1-07, PH1-08, PH2-01, PH2-02, PH2-03, PH2-06, PH6-06, PH8-01 | Соответствующий production rollout |
| P3 | PH2-04, PH8-03 | Production readiness/hardening |

# Approved product catalog

Все цены — Telegram Stars.

| Тариф | 30 дней | 60 дней | WL на каждые 30 дней | Устройств |
|---|---:|---:|---:|---:|
| Базовый | 99⭐ | 169⭐ | 0 GB | 3 |
| Базовый Плюс | 139⭐ | 199⭐ | 0 GB | 6 |
| Базовый Про | 169⭐ | 249⭐ | 0 GB | 12 |
| WL | 199⭐ | 349⭐ | 100 GB | 3 |
| Расширенный | 249⭐ | 399⭐ | 150 GB | 6 |
| Семейный | 299⭐ | 449⭐ | 150 GB | 12 |

Утверждено: Non-WL безлимитен. 60 дней создают два последовательных 30-дневных WL periods с новым лимитом, не единый двойной pool. Child users создаются lazy.

| Дополнительный WL | Цена |
|---|---:|
| +50 GB | 79⭐ |
| +100 GB | 149⭐ |
| +250 GB | 349⭐ |
| +500 GB | 599⭐ |

Packages доступны только WL/Расширенный/Семейный и не превращают Base plans в WL без нового решения.

## External payment — RUB catalog `RUB-2026-08-23-v1`

| Тариф | 30 дней | 60 дней |
|---|---:|---:|
| Базовый | 169 ₽ | 279 ₽ |
| Базовый Плюс | 239 ₽ | 339 ₽ |
| Базовый Про | 279 ₽ | 399 ₽ |
| WL | 349 ₽ | 579 ₽ |
| Расширенный | 399 ₽ | 679 ₽ |
| Семейный | 499 ₽ | 749 ₽ |

| Дополнительный WL | Цена |
|---|---:|
| +50 GB | 139 ₽ |
| +100 GB | 249 ₽ |
| +250 GB | 579 ₽ |
| +500 GB | 999 ₽ |

Stars и RUB — независимые утверждённые retail price tables. Первый external-payment rollout принимает только RUB и использует immutable/versioned snapshot `RUB-2026-08-23-v1` (DL-034–036/040). Recorded amount обязан совпадать с ценой выбранного SKU/version; плательщик/frontend/admin не передаёт arbitrary plan/price. Другую currency нельзя принять до появления отдельно утверждённой versioned table. То же правило относится к будущей цене дополнительных device slots.

# Definition of Done

Каждая задача закрывается только когда:

- код/schema/config реализованы;
- automated и необходимые negative tests добавлены;
- production migration и рискованный rollback/compensation описаны;
- `ROADMAP.md` и `CHANGELOG.md` обновлены;
- Decision Log обновлён при решении;
- README/API/runbook обновлены;
- нет скрытого stub/placeholder без TODO ID;
- regression suite прошёл;
- secrets/raw token/HWID/UUID не попали в diff/logs/fixtures;
- remote Marzban/Xray state reread и verified;
- статус отражает `main`, а не только production hotfix.
- для `MANUAL_PAYMENT` сервер сам проверяет primary-admin actor, account, versioned currency catalog/price, exact amount/currency/reference и idempotency; frontend IDs/price не являются authority;
- migration test сохраняет account identity, payment-channel provenance при наличии evidence, expiry, plan/conditions, devices, legacy username, Telegram mapping и renewal history.

# Phase 0 — Current-state baseline

## [x] PH0-01 — Repository/security inventory

**Evidence:** main, Git history, routes, secrets classes, dependencies и production runtime проверены 2026-08-23; findings F-01–F-16 отражены ниже.
**Проверка:** local main и deployed HEAD сопоставлены. Remediation этим пунктом не считается выполненной.

## [x] PH0-02 — Current/target architecture diagrams

**Готово:** boundaries nginx/MGBoost/SQLite/Telegram/OpenRouter/Marzban/Xray и target parent/child/outbox приведены выше.
**Правило:** diagram обновляется вместе с runtime change/changelog.

## [x] PH0-03 — Current DB gap analysis

**Готово:** current tables и отсутствующие target entities зафиксированы.
**Файл:** `src/database.py`. **Проверка:** schema diff при каждой migration.

## [~] PH0-04 — Production inventory/drift control

**Сделано:** HEAD/topology/permissions baseline и untracked JSON обнаружены; PH1-03 закрыл live sensitive-file modes; PH1-04 перевёл MGBoost на dedicated UID/GID и зафиксировал writable boundary.
**Осталось:** versioned deploy manifest, полный volume/regular-backup/port matrix и policy для legacy JSON. На 2026-08-23 регулярных MGBoost/Marzban backup jobs/archives не обнаружено; их создание/retention относится к PH1-06/DL-042.
**Depends:** PH1-03/04, PH8-03. **Test:** reproducible staging + secret-safe drift report.
**Migration:** log/DB-backup/token-evidence retention утверждена DL-042; cleanup только по controlled deletion после verified backup/restore и confirmed rotation/reissue strategy. Untracked legacy JSON требует отдельного ownership/content inventory и не удаляется автоматически.

## [x] PH0-05 — Exact versioned WL topology

**Сделано (2026-08-26, production-deployed):** `src/wl_topology.py` — exact
versioned baseline (`WL_TOPOLOGY_VERSION="2026-08-26-v1"`): 12 live WL
inbound tags (unchanged from the 2026-08-23 baseline) and the exact two
real WL nodes, confirmed directly against production Marzban this session
(`GET /api/nodes`, `GET /api/inbounds`, and the `hosts` table's `address`
column to tie each live `wl-*` tag to a physical node): node id 4
("RU ONLY WL", `84.201.130.217`, serves `wl-tcp-*`) and node id 7
("Selectel", `5.178.85.8`, serves `wl-selec-grpc-*`), both
`usage_coefficient=1.0`. The other 3 real Marzban nodes (Estonia id 3,
Beget id 6, germanyp2 id 8) carry no WL inbound and are excluded by exact
id, never by node-name substring (node id 4's own name literally contains
"WL" -- exactly the kind of accidental match fuzzy matching would produce).
Six stale `wl-selec-tcp-*` `hosts` rows (ids 4451-4453, 4469-4471,
referencing an inbound tag that no longer exists in live config) are
excluded automatically, since `diff_topology()` only ever compares against
live `get_inbounds()` output, not the `hosts` table.
**`diff_topology(observed_tags, observed_nodes)`** is pure/exact-set-only:
`missing_tags` (declared but absent live), `extra_wl_like_tags`
(alert-only: a live tag that looks WL-shaped but isn't on the allowlist --
never auto-included), `missing_node_ids`, `node_field_mismatches`
(role/address/coefficient drift on a declared WL node id). No `wl` substring
search is ever used to decide membership.
**Blocks:** PH6-01/06 -- PH6-01 now consumes this module directly.
**Tests:** `tests/test_wl_topology.py` (11 tests: exact baseline shape,
clean match, missing/extra/renamed tag, stale non-wl-like tag ignored,
missing node, coefficient/role-rename mismatch, node-name-contains-"wl"
never auto-included, real-payload shaping helpers).
**Rollback:** pure data/diff module, no schema, no live enforcement wired
-- reverting is a plain code revert.

## [x] PH0-06 — Token/device/payment execution paths

**Готово:** legacy flow и Stars expire-only behavior зафиксированы.
**Файлы:** `src/routes/sub.py`, `src/routes/lk.py`, `src/device_headers.py`, `src/bot_support.py`, `src/stars.py`.

## [~] PH0-07 — Regression baseline

**Сделано:** есть tests management sessions, Stars durability/refunds, audit events, renew locks/support и single-process concurrency.
**Добавлено PH1-01:** browser stored-XSS E2E под production CSP, JS render-path malicious-value test, admin session fixation/CSRF/expiry/logout/rotation tests и server-side Marzban broker negative tests.
**Осталось:** token-log canary, общий malformed-input corpus вне admin broker, multi-process, Marzban contract, WL fault injection и end-to-end device/subscription revoke tests.

## [x] PH0-08 — Inventory manual payment and separate Filin automation flow

**Сделано:** подтверждено, что «reseller» в исходной формулировке — ручное применение прямой внешней оплаты основным admin, а не отдельный tenant. Dedicated payment/order provenance сейчас отсутствует. Generic HMAC Filin API является другой integration: перечисляет/создаёт/продлевает/удаляет Marzban users с MGBoost SUDO; `add_days` 1..3650, отрицательные expire/data_limit отклоняются, renew сериализуется со Stars только process-local per-username lock.
**Files:** `src/security.py`, `src/routes/internal.py`, `src/server.py`, `tests/test_internal_renew_lock.py`; внешний caller вне этого repo.
**Tests:** read-only contract/permission map; не выгружать raw credentials/customer UUID.
**Migration:** нельзя infer external payment по username prefix/note; backfill только при transaction/admin evidence, иначе provenance `UNKNOWN_LEGACY`.
**Corrective fix (2026-08-27):** независимый read-only аудит нашёл P2 в `_CrossThreadLockCtx.__enter__` (`src/routes/internal.py`) — `result(timeout=10)` не отменял зависший `lock.acquire()`, который мог позже выиграть lock без владельца, кто мог бы его освободить, навсегда блокируя per-username lock (и потенциально Stars apply-loop). Воспроизведено на `main` до фикса отдельным harness'ом. Исправлено вызовом документированного `future.cancel()` по timeout + освобождением lock на его собственном loop в редком случае, когда acquire успел выполниться за мгновение до отмены. 1 новый regression test; race-serialization test не пострадал. Full regression `1116 passed`. Деплой `ed77b11`->`096c8d4`, только restart `mgboost-panel`, схема не менялась.

# Phase 1 — Emergency security

**Status 2026-08-24:** COMPLETE; PH1-01–PH1-08 deployed and passed the final
production regression/security gate. Phase 2 may start only in its own task
order and with the residual/dependency constraints recorded in
`docs/PHASE1_COMPLETION_REPORT.md`.

## [x] PH1-01 — Admin stored XSS и безопасная server session — P0

**Completed:** 2026-08-23; production rollout verified 2026-08-24. **Depends:** none; выполнена первой.
**Files:** `frontend/assets/admin.js`, `frontend/index.html`, `src/config.py`, `src/http_utils.py`, `src/marzban.py`, `src/security.py`, `src/server.py`, `src/routes/admin_session.py`, `src/routes/admin_proxy.py`, `src/routes/panel.py`, `.env.example`, `README.md`, `tests/test_admin_sessions.py`, `tests/test_admin_proxy.py`, `tests/test_admin_frontend_security.py`, `tests/test_admin_browser_e2e.py`.
**Implemented:** все admin API/user-controlled значения проходят через единый escaping `SafeMarkup`/DOM render path; inline event attributes удалены и заменены event delegation; Marzban JWT удалён из browser storage/API responses и хранится только в process-local server session; введены CSPRNG opaque session ID и CSRF token, hashed in-memory lookup, absolute TTL, Secure/HttpOnly/SameSite=Strict cookie, logout/revoke/rotation и fixation-safe login; mutation routes требуют constant-time CSRF check; cross-site-form-compatible login requests отклоняются; browser Marzban access переведён на explicit server-side path/method broker; upstream 401/403 отзывает local session; admin SPA получила strict script CSP, no-store/referrer/frame/nosniff headers. Текущий process-local store соответствует single-worker baseline и не разрешает multi-worker до PH8-02.
**Acceptance evidence:** malicious User-Agent, note, username, node/config name, inbound tag и URI отображаются только как text; production CSP исполняется без inline JS; browser не получает Marzban JWT; legacy Bearer auth отклоняется; logout, expiry, rotation и upstream auth failure инвалидируют session. Production deploy gate подтвердил legacy `/sub`, LK, Stars state, Filin, nginx/systemd/journal и masked identity/config contracts; владелец вручную вошёл в admin и подтвердил отсутствие визуальной регрессии.
**Tests:** `test_admin_sessions.py` (server-only JWT, cookie flags, login guard, fixation, CSRF, expiry, logout, rotation), `test_admin_proxy.py` (allowlist/query/path/auth-failure), `test_admin_frontend_security.py` (sink/source matrix и реальный JS render path), `test_admin_browser_e2e.py` (headless Chromium, malicious API values под production CSP). Full regression: 341 passed, 1 browser test additionally passed in Playwright environment.
**Migration/rollback:** admin asset получил новый cache-buster; SPA посылает `Clear-Site-Data: "storage"` и JS удаляет legacy `mz_token`. После deploy существующие browser JWT sessions потребуют новый login. Rollback допустим только к server-side session implementation и никогда не должен возвращать JWT в JavaScript/localStorage.

## [x] PH1-02 — Marzban SUDO CSPRNG rotation и login rate limit — P1

**Completed and production verified:** 2026-08-24. **Depends:** выполненный PH1-05 external mutation boundary и maintenance coordination. Credential storage/caller cutover code завершён PH1-05; прежний circular dependency PH1-02↔PH1-05 устранён. **Files:** `src/marzban.py`, `src/security.py`, `src/http_utils.py`, `src/routes/admin_session.py`, broker/Marzban env, nginx exact-location config, `tests/test_admin_sessions.py`, `tests/test_marzban_auth.py`.
**Scope:** service credential >=128-bit; correct form encoding; atomic rotation; rate limit; проверить JWT invalidation/lifetime.
**Accept/tests:** старый password/JWT не работают; 429 brute-force; special characters и outage ordering; broker smoke.
**Migration/rollback:** controlled secret delivery; отдельный не логируемый rollback credential.
**Implemented/verified:** MGBoost failed-login limiter использует validated client IP только от loopback nginx, отдельные sliding-window budgets для IP+username и IP spray, hashed username keys, bounded process-local storage, uniform `429` и `Retry-After`. Public direct Marzban login защищён nginx `limit_req` exact-location; broker/direct localhost traffic не ограничивается. Для Marzban 0.8.4 одновременно ротированы CSPRNG SUDO username+password: password-only rotation не отзывала бы старый JWT, а общий JWT signing secret намеренно не менялся, чтобы не отозвать legacy subscription tokens. Старые password и admin JWT возвращают `401`; новый credential принят Marzban/broker. Active и отдельный unused fallback credentials переданы только через root-only `0600` owner files, значения не попадали в git/chat/journal.
**Production acceptance:** public rate gate дал `429`; 6/6 broker reads, admin/LK/Stars/Filin smoke и frozen legacy alias прошли. Exact pre/post aggregate совпал для 25 users и 71 device/HWID rows; subscription fetch errors — 0. UUID, token/URL, expiry, access/config shape, HWID и тарифы не изменились. Во время `docker-compose --force-recreate` оборвалась SSH-сессия после удаления контейнера; fail-safe privileged outage был виден как ожидаемый `503`, container был детерминированно поднят с уже согласованной новой парой, после чего весь gate прошёл без расхождений.
**Tests:** full regression до rotation `380 passed, 1 skipped`; targeted credential form-encoding, limiter/session и broker contracts `37 passed`. `tests/test_marzban_auth.py` подтверждает корректный `application/x-www-form-urlencoded` transport для special characters; limiter tests покрывают trusted-proxy spoofing, upstream-call suppression, expiry/success reset и отсутствие plaintext username/password в limiter state.

## [x] PH1-03 — Minimum permissions для Marzban secrets/data — P1

**Completed:** 2026-08-23. **Depends:** PH0-04 ownership matrix.
**Applied production scope:** MGBoost active `/opt/MGBoost_Panel/.env` и `data/db.sqlite3` уже были `0600 root:root`, `data/` — `0700`; это сохранено. Marzban `/opt/marzban/.env`, live `/var/lib/marzban/db.sqlite3`, `xray_config.json` и три найденные Xray backup copies изменены с `0644` на `0600 root:root`; `/var/lib/marzban` изменён с `0755` на `0700`. Два repository-root placeholder DB (`db.sqlite3`, `panel.db`) подтверждены zero-byte и не являются active data.
**Acceptance evidence:** Marzban container не privileged, но его Python/Xray processes сейчас root и сохраняют доступ к root-owned bind mount; container remained running; Marzban `/api/system` вернул expected unauthenticated `401`, MGBoost admin SPA — `200`; Compose config читает `.env`; все Xray JSON copies парсятся; unrelated `nobody` не читает MGBoost/Marzban env/DB/Xray; временный SQLite online backup открылся как restore copy и прошёл `PRAGMA quick_check=ok`, после теста temp data удалены автоматически.
**Regular backup caveat:** scheduled MGBoost/Marzban DB backup jobs и retained DB archives не обнаружены, поэтому здесь нечего было re-owner/chmod. Их создание, encrypted quarantine и retention/restore gates остаются PH1-06/DL-042 и не переопределяют завершённое ограничение существующих файлов.
**Migration/rollback:** pre-change owners/modes зафиксированы inventory (`0644 root:root` для перечисленных Marzban files, `0755` parent); broad `0777` не применялся. Возврат к world-readable modes запрещён; будущий PH1-04 меняет MGBoost owner только на dedicated UID/GID с теми же или более строгими effective permissions.

## [x] PH1-04 — Dedicated MGBoost service user/systemd hardening — P1

**Completed:** 2026-08-23. **Depends:** PH0-04, PH1-03. **Files:** `mgboost-panel.service`, `README.md`, production unit/ownership.
**Implemented:** dedicated system identity `mgboost:mgboost` (UID/GID 999 on current production, no login/home); `.env` is `0640 root:mgboost`; `/opt/MGBoost_Panel/data` is `0700 mgboost:mgboost`, active files `0600`; source tree remains root-owned/read-only. Unit sets `UMask=0077`, `PYTHONDONTWRITEBYTECODE`, `NoNewPrivileges`, empty capability/ambient sets, `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp/Devices`, bounded `ReadWritePaths=/opt/MGBoost_Panel/data`, kernel/control/proc/namespace/realtime/SUID/personality/memory hardening and only AF_UNIX/INET/INET6.
**Acceptance evidence:** remote `systemd-analyze verify` accepted the unit (only unrelated host snapd warning); service MainPID runs as `mgboost`; `systemd-analyze security` improved from `9.6 UNSAFE` to `2.8 OK`; transient equivalent sandbox confirmed source non-writable/data writable; service-user SQLite `BEGIN IMMEDIATE`/rollback passed; MGBoost SPA returned `200`; service remained active after restart. `nobody` cannot read env/DB.
**Migration/rollback:** previous unit saved root-only as `/etc/systemd/system/mgboost-panel.service.pre-ph1-04`; pre-change ownership was root:root (`0600` env/data files, `0700` data dir). Rollback may restore the previous unit and root ownership only for emergency compatibility, but must retain `0600/0700` and never broaden permissions. Rollback file contains unit configuration, not credentials.

## [x] PH1-05 — Narrow Marzban broker/allowlist — P1

**Completed locally/staged and deployed to production:** 2026-08-24. **Depends:** PH1-01 server-session boundary, PH0-08 caller inventory и обязательный no-user-impact gate DL-043; не зависит от завершённой rotation PH1-02. **Files:** `broker_main.py`, `src/broker_protocol.py`, `src/broker_operations.py`, `src/broker_server.py`, `src/service_marzban.py`, `src/legacy_contract.py`, `src/routes/internal.py`, `src/routes/lk.py`, `src/bot_support.py`, `src/routes/admin.py`, `mgboost-marzban-broker.service`, `mgboost-panel.service`, `broker.env.example`, `.env.example`, `README.md`, `docs/PHASE1_BACKWARD_COMPATIBILITY.md`, `scripts/verify_broker_against_staging.py`, `scripts/verify_legacy_sub_restart_staging.py`, `tests/test_marzban_broker.py`, `tests/test_phase1_legacy_compat.py`.
**Выбранная topology (DL-043):** отдельный localhost broker-service хранит/получает Marzban service SUDO credential; основной MGBoost после cutover не содержит этот credential в environment. Broker не публикуется через nginx. Public Marzban `/sub/{legacy_token}` и `/sub/{legacy_token}/info` остаются direct non-SUDO paths, поэтому broker outage не должен останавливать legacy subscriptions. Browser admin использует только user-entered server-side Marzban session PH1-01, не service credential.
**Implemented Stage A:** exact typed allowlist, HMAC-SHA256/timestamp/nonce/body-hash caller authentication, replay rejection, loopback-only literal bind/URL validation, bounded HTTP concurrency, safe target pseudonym audit и deny unknown operation/field/path. Filin create/renew/delete сохранены.
**Implemented Stage B code/config boundary:** service bot/LK/Stars/internal calls используют broker facade; лишний SUDO lookup из public token-info linking удалён; main startup fail-fast отклоняет наличие `MARZBAN_ADMIN_USER/PASS` в broker mode. Broker запускается отдельным Unix identity с `MGBOOST_SKIP_DOTENV=1`, получает полный environment из `/etc/mgboost/marzban-broker.env` и не имеет permission/read dependency на repository `.env`. Production service credential rotation завершена в PH1-02.
**Current legacy allow:** user get/usage/list, node list/usage, inbound list, exact current create, renewal (`add_days`/`expire`/`data_limit`/`status`), Stars expire-only update и exact current delete. Это transitional compatibility surface, а не будущая entitlement API.
**Future narrow allow после legacy retirement:** lookup, child create, expire, enable/disable, only `inbounds.vless`, traffic/node reads. **Always deny:** raw SUDO/JWT exposure, arbitrary URL/path, unknown fields and untyped destructive payloads. Не менять UUID/proxies/inbounds/config/expiry semantics вне явно выбранной operation.
**Acceptance evidence:** все 10 legacy operations прошли direct-vs-broker response/effect comparison на isolated official Marzban 0.8.4 VLESS staging. HMAC Filin create/renew/delete пройдены через реальный MGBoost HTTP route. Broker/Marzban/MGBoost restart, broker outage, Marzban outage/recovery, pre-effect partial failure, Stars post-effect recovery и rollback-direct contracts пройдены. Production cutover подтвердил HMAC denial без key, отсутствие nginx route, 6/6 read operations, exact сохранённый legacy alias при broker up/down, safe `502` privileged/Filin outage, broker recovery и MGBoost restart. Masked pre/post/restart snapshots для 25 users, UUID/expiry/proxies/inbounds/data_limit/config semantics и 71 device/lock rows совпали; main process имеет 0 Marzban SUDO env keys.
**Tests:** `376 passed, 1 skipped`; `tests/test_marzban_broker.py` покрывает all-10 allowlist, HMAC/replay/deny/outage/restart/partial/Filin/rollback; Stars suite покрывает checkout fail-closed, durable paid retry и exact-expire recovery; reproducible staging scripts перечислены выше. Browser E2E skip относится к отсутствующему Playwright в текущем base environment и был пройден отдельно для PH1-01; PH1-05 frontend не меняет.
**Known preserved legacy behavior:** Filin `data_limit=0` преобразуется старым кодом в JSON `null`; Marzban 0.8.4 при partial update оставляет прежнее значение. PH1-05 сохраняет это буквально. Filin add-days не имеет durable operation ID, поэтому blind retry после unknown response остаётся неоднозначным legacy contract; durable/shared idempotency относится к PH2-03 и не заменяет безопасный Stars recovery.
**Migration/rollback:** production cutover выполнен без DB/user migration и user credential rotation. Root-only rollback bundle содержит pre-cutover code/unit/env и verified SQLite backups. Emergency rollback возвращает PH1-01 code/unit и текущий service credential только main process до его успешного старта, затем останавливает broker; user UUID/token/expiry/HWID не меняются. Подробный pre/post runbook — compatibility report.

## [x] PH1-06 — Stop raw subscription leakage/controlled rotation — P1

**Completed and production verified:** 2026-08-24. **Depends:** PH2-01 for final reissue; backup/restore verification and confirmed rotation/reissue strategy before cleanup. Retention policy itself is fixed by DL-042. **Files:** `src/sensitive.py`, `src/server.py`, `src/database.py`, `src/marzban.py`, `src/routes/sub.py`, `src/routes/lk.py`, `frontend/assets/lk.js`, `scripts/secure_db_backup.py`, `scripts/create_legacy_quarantine.py`, backup systemd units, nginx/journal, `docs/PHASE1_RETENTION_AND_BACKUP.md`.
**Scope:** redact sensitive paths; no new raw-token DB/query/log entries; inventory and classify legacy evidence before cleanup. По DL-043 Phase 1 не меняет legacy user subscription URL/token: controlled user-token rotation/reissue выполняется только staged migration Phase 4. Rotation service/admin credentials остаётся обязательной и не отменяется наличием credential в retained backup/quarantine snapshot.
**Fixed retention (DL-042):** sensitive legacy nginx/application/journal logs — 30 days; ordinary operational logs without credentials — maximum 60 days; regular DB backups — 90 days. Before deleting legacy token evidence, create exactly one encrypted quarantine snapshot and retain it 180 days. Quarantine access is limited to the minimum necessary owner/service identity.
**Accept/tests:** canary bearer absent from app/nginx/journal/analytics and every new backup; retention classification, access-denial, encrypted quarantine restore and expiry/deletion dry-run verified; rotated service/admin credential works and retained copy no longer works. Existing legacy subscription aliases continue работать до Phase 4 revoke gate.
**Migration/cleanup:** canary cohort/support plan; verify backup and isolated restore; confirm Phase 4 user-token rotation/reissue strategy; create and verify one encrypted quarantine snapshot; rotate service/admin credentials; then controlled deletion after each retention boundary. No destructive cleanup or legacy user-token revoke before all prerequisites pass.
**Implemented/deployed:** application request/error logs redact `/sub/{token}` and all query values; LK accepts legacy query bookmarks once, removes the bearer from browser URL state and uses a same-origin header for subsequent API calls; subscription/LK responses set `no-store` and `no-referrer`; browser subscription external links use `noreferrer`. `sub_requests`, `user_devices` and `hysteria_stats` keys use stable `sha256:<hex>` references. Controlled production migration atomically converted 198 raw request rows and 71 raw device rows (Hysteria rows: 0), leaving 0 raw rows, without rotating/revoking Marzban bearers or changing device identity. Daily root-only AES-256 GnuPG encrypted backups perform immediate isolated decrypt/checksum/SQLite quick-check and enforce 90-day controlled retention; timer is enabled. Exactly one encrypted quarantine snapshot passed restore verification and is retained 180 days. Nginx sensitive routes/HTTP redirects log a fixed redacted target without query/Referer/User-Agent; nginx and journald sensitive retention is 30 days.
**Acceptance evidence/tests:** full regression `391 passed, 1 skipped`; `systemd-analyze verify` accepted backup units. Two encrypted DB artifacts (pre/post migration) and the one quarantine artifact passed isolated restore; keys/artifacts are `0600 root:root`. Fake bearer exercised legacy `/sub`, old LK query and new LK header flows; strict post-cutover byte-tail scan found 0 raw matches in nginx access/error/sensitive logs and application journal, 0 raw active DB rows and 0 raw matches in the encrypted artifact. One earlier known bearer match was timestamped before quarantine/nginx cutover and remains classified retained legacy evidence. Final production gate: 6/6 broker reads, frozen legacy alias/config/UUID/expiry, admin/LK/Stars/Filin and nginx/systemd healthy; exact masked snapshot matched for 25 users, 71 device rows and 71 HWID locks with 0 fetch errors/warnings and 0 Marzban mutation operations. UUID, subscription URL/token, HWID, expiry, tariff and functional config changes caused by deployment: 0. User credential rotation/reissue remains Phase 4.
**Discovered drift (2026-08-25), closed by PH1-10:** the "nginx and journald sensitive retention is 30 days" line above described the intended contract, but PH1-09/PH1-10 discovered the `logrotate` package/binary was not installed on production at all -- no scheduled mechanism actually enforced 30-day rotation/retention for nginx logs (sensitive or otherwise) at any point before 2026-08-25, even though the *content* redaction this entry's own evidence verified (0 raw bearer matches) was never affected by that gap. PH1-10 installed and enabled the standard `logrotate.timer` and proved a real forced rotation correctly enforces the sensitive log's `0600`/30-day contract end to end. This entry stays `[x]`: its own stated content-redaction/DB-retention/quarantine criteria were all genuinely met at the time and remain true; the nginx-log-scheduler gap is recorded here for the historical record and fixed by the separate PH1-10 entry rather than by reopening this one. A related, still-open, separately-scoped observation: `journald`'s own `MaxRetentionSec` is also unset on production (default size-based rotation, not a 30-day cap) -- out of PH1-10's nginx-specific scope, noted here for a future task.

## [x] PH1-07 — Patch applicable dependency DoS via staging — P2

**Scope:** Marzban `python-multipart 0.0.7`, MGBoost `aiohttp 3.10.11`, transitive inventory.
**Accept/tests:** patched immutable build; advisory payload/load/soak; Telegram/OpenRouter/proxy integration.
**Migration/rollback:** staging only first; no production in-place package upgrade; previous image digest retained.
**Implemented/staging verified 2026-08-24:** exact Marzban 0.8.4 base digest overlays only hash-pinned `python-multipart 0.0.32`; MGBoost target is a separate hash-locked/root-owned Python 3.10 runtime with `aiohttp 3.14.3`, `aiogram 3.30.0` and `aiohttp-socks 0.12.0`. Local suite `394 passed, 1 skipped`; isolated Python 3.10 suite `393 passed, 2 skipped`; both target locks have 0 known advisories. All-10 broker operations, legacy alias/UUID/config continuity across forced renewal timestamp boundary, MGBoost/broker restart, Filin and 100-login/parser load passed against baseline and target localhost-only VLESS containers. Telegram `getMe` through current SOCKS proxy passed. OpenRouter completion returned the same pre-existing HTTP 403 on old/new runtimes, so no dependency regression exists but external authorization remains residual. Runbook/evidence: `docs/PHASE1_DEPENDENCY_HARDENING.md`.
**Production completed 2026-08-24:** Marzban runs the immutable PH1-07 image over the unchanged 0.8.4 application/schema and MGBoost runs the root-owned isolated venv. An initial full-body digest mismatch triggered the documented rollback before the MGBoost runtime step; investigation proved that existing Marzban output randomizes only Reality `sid` and MGBoost adds a dynamic fake information node. The gate was corrected to exclude only that known non-connection info node and normalize only the value of present `sid`, while retaining UUID/credential, host, port, transport/TLS, all other query fields, fragments, counts, expiry/identity, device and HWID-lock comparisons. The corrected baseline was stable on the rollback image and matched exactly after both deploy steps. Admin/LK, 25/25 legacy subscriptions, Telegram proxy, Stars durable state, authenticated/unauthenticated Filin, broker reads, nginx/systemd and token-safe logging passed. UUID, subscription URL/token, HWID, tariff, expiry and forced client reconfiguration changes: 0; unexpected effective config changes: 0.

## [x] PH1-08 — Remove password from Marzban login notifications — P2

**Scope:** failed-login Telegram/Discord reports. **Accept:** password never enters report/log.
**Tests:** canary password absent everywhere. **Rollback:** never restore password field.
**Implemented/staging verified 2026-08-24:** PH1-08 builds as a narrow layer over the exact PH1-07 image and replaces only the failed-login `report.login` password argument with `🔒`; authentication validation and HTTP semantics are unchanged. The build patch is AST-validated, idempotent and fail-closed on unexpected/duplicate upstream call shapes. Built-image AST and direct report capture prove two redacted login-report calls and 0 plaintext report arguments. With `NOTIFY_LOGIN=true`, a real isolated failed login returned the same 401 and the canary appeared 0 times in report capture, HTTP response and container logs. All-10 broker operations, legacy `/sub` with broker up/down, MGBoost restart, Filin HMAC create/renew/delete, UUID/config continuity and full regressions passed (`401 passed, 1 skipped` locally; `400 passed, 2 skipped` on production Python 3.10). Staging verifier flakiness was corrected: timestamped Marzban `subscription_url` is verified as a preserved old alias rather than an immutable admin field, and only the randomly selected Reality `sid` value is normalized while UUID/proxy/inbound/endpoint/transport semantics stay strict. Runbook: `docs/PHASE1_LOGIN_NOTIFICATION_HARDENING.md`.
**Production completed 2026-08-24:** exact candidate image/AST/report-capture gates passed before an image-only Marzban recreate. Valid admin login remained functional; a real failed-login canary returned 401 with `NOTIFY_LOGIN=true` and appeared 0 times in the report argument, response, container, application journal and nginx logs. The masked pre/post/final state matched for all 25 users/configs, 71 device rows and 71 HWID locks. Admin/LK, Stars durable state, Filin HMAC, broker, backup timer, nginx/systemd and permissions passed. Four broker error-signature messages occurred only at the Marzban recreate timestamp; the following stable 10-minute window contained 0. UUID, subscription URL/token, HWID, tariff, expiry and forced client reconfiguration changes: 0; unexpected effective config changes: 0. Final Phase 1 verdict and residual risks: `docs/PHASE1_COMPLETION_REPORT.md`.

## [x] PH1-09 — Minimum permissions for the MGBoost sensitive nginx log — P1

**Discovered:** 2026-08-25, during the PH4-01 valid-legacy-`/sub` production gate (residual, pre-existing, not caused by PH4-01). **Depends:** none; standalone filesystem/nginx/logrotate hardening, does not reopen PH1-06 (whose own redaction/retention contract was never violated -- the sensitive log's *content* was already correctly redacted at the nginx layer; only its *file mode* was too permissive).
**Scope:** `/var/log/nginx/mgboost-sensitive-access.log` was `0644 root:root` (world-readable) instead of minimum-necessary access. Fix file mode at the source (creation mechanism), not just the current inode, and prove it survives rotation/reload.
**Accept/tests:** actual production contract determined by code, not assumed; minimal mode applied; forced real rotation proves the new inode keeps the secure mode; unrelated local identity denied read; nginx continues writing; redaction contract (no raw `/sub/{token}`, query, Referer, User-Agent) intact; retention (30 days, DL-042) unchanged.
**Root cause:** `log_format mgboost_sensitive` (in `/etc/nginx/conf.d/mgboost-sensitive-log.conf`, predates this task) already redacts every request target to a fixed `<redacted-sensitive-target>` placeholder -- the file never contained a raw bearer. Its `0644` mode came from nginx's own default file-creation umask the one time it was first created; the file had never actually been rotated, so the generic `/etc/logrotate.d/nginx` stanza's own `create 0640 www-data adm` had never been applied to it either (and `adm`-group readability would still not have been minimal).
**Implemented/verified 2026-08-25:** `chmod 600` on the current inode; `/etc/logrotate.d/nginx`'s glob narrowed from `/var/log/nginx/*.log` to an explicit `access.log`/`error.log` list (unrelated nginx logs, unchanged `create 0640 www-data adm`, unchanged 30-day `rotate`); a new dedicated `/etc/logrotate.d/mgboost-sensitive-nginx` stanza added for this one file only, `create 0600 www-data root`, same 30-day `rotate`/`compress`/`delaycompress`/`notifempty`, `postrotate: invoke-rc.d nginx rotate` (USR1 to the nginx master, matching the existing stanza's own mechanism) -- avoiding the double-rotation hazard of two overlapping glob-based stanzas matching the same file. A real forced rotation (the file moved aside, a fresh file created with the new stanza's exact `create` semantics, nginx signaled to reopen) produced a new inode that a real production request (garbage token, no real bearer) wrote to; the file settled at `www-data:root 0600` (nginx's own worker identity ends up owning the actively-written fd on this host, empirically confirmed by two independent real requests -- the `create` line was corrected to match this observed reality rather than an assumed `root:root`). The rotated archive (`.1`) stayed `root:root 0600`. `nobody` (an unrelated local identity) got `Permission denied` reading the live log. Both the live and rotated files contain 0 occurrences of any raw `/sub/{token}`-shaped path -- only the fixed redacted placeholder, exactly matching PH1-06's pre-existing content contract. `nginx -t` passed before and after; `nginx`/`mgboost-panel`/`mgboost-marzban-broker`/`mgboost-child-worker` stayed active throughout; legacy `/sub` invalid-token smoke and admin reachability were unchanged. Runbook: `docs/PHASE1_SENSITIVE_LOG_PERMISSIONS.md`.
**Residual risk (explicitly out of this task's scope):** the `logrotate` package/binary is not installed on this host at all, so no scheduled rotation currently runs for *any* nginx log (this predates this task and is a separate, larger gap than the file-mode issue it was asked to fix -- installing/enabling a rotation daemon for the first time is a distinct, more invasive change than "narrow existing permissions," and was therefore not done here). The logrotate config files added by this task are correct and ready for whenever a rotation mechanism is provisioned (manually, via a future explicitly-scoped task, or if `logrotate` is later installed); the one rotation performed here was executed manually, replicating the configured stanza's exact semantics.

## [x] PH1-10 — Scheduled nginx sensitive-log rotation and retention — P1

**Discovered:** 2026-08-25, as PH1-09's own documented residual (its "forced rotation" was a manual `mv`/`create`/reopen simulation, not a real `logrotate` invocation, because `logrotate` was not installed). **Depends:** PH1-09 (schema/config already correct; this closes the scheduler gap only).
**Scope:** make the already-correct `/etc/logrotate.d/mgboost-sensitive-nginx` stanza actually run on a schedule, using the standard distro mechanism, without inventing a custom rotation daemon.
**Accept/tests:** `logrotate` installed and its systemd timer enabled/active; a real (not simulated) forced `logrotate -f` run rotates correctly; new sensitive-log generation is `0600 www-data:root`; unrelated local identity denied on every generation (active + all archives); nginx keeps writing after reopen; no double-rotation between the generic and sensitive stanzas; 30-day retention (DL-042) unchanged and durably guarded by an automated config test; zero raw bearer/query/Referer/User-Agent in any generation.
**Runtime facts established before any change:** `logrotate` package was genuinely absent (`dpkg-query` found nothing), no `logrotate.timer`/`logrotate.service` unit existed, and `cron` was not installed either (`dpkg -l cron` showed `un`/none) -- there was no alternate scheduler of any kind. PH1-09's "forced rotation" evidence is confirmed, not rewritten, to have been a manual simulation (`mv` + `install -m` + `invoke-rc.d nginx rotate`), matching what PH1-09 itself already disclosed as a residual.
**Implemented/verified 2026-08-25:** `apt-get install logrotate` (Ubuntu 22.04, package `3.19.0-1ubuntu1.1`) -- installation automatically enabled `logrotate.timer` (symlinked into `timers.target.wants`, confirmed `enabled`/`active`, next trigger scheduled, survives reboot via the standard systemd timer unit semantics, no custom unit written). `logrotate -d /etc/logrotate.conf` (dry-run) confirmed both stanzas parse correctly and match their files exactly once each -- no glob overlap, no double-rotation. A real forced run (`logrotate -f /etc/logrotate.conf`, not a dry-run and not a manual simulation) rotated the sensitive log for real: the new generation is `www-data:root 0600` (a new inode, distinct from the pre-rotation one); the superseded generation became `.1` (still `0600`); the PH1-09-era archive advanced to `.2.gz`, correctly compressed under `delaycompress` semantics. A real HTTP request (garbage token, no real bearer) confirmed nginx reopened and wrote to the new inode; `nginx -t` passed; all four services (`nginx`, `mgboost-panel`, `mgboost-marzban-broker`, `mgboost-child-worker`) stayed active. `nobody` (unrelated local identity) got `Permission denied` on every generation, active and archived, including the compressed one. Content leakage scan across all three generations (active, `.1`, decompressed `.2.gz`): 0 raw `/sub/{token}`-shaped paths, 0 Referer/User-Agent mentions, only the fixed redacted placeholder -- unchanged from PH1-09's own content contract. Retention: `daily` + `rotate 30` (unchanged, DL-042) caps the sensitive log at 30 kept generations; `notifempty` means a zero-traffic day simply has nothing to retain, which does not violate a "do not keep sensitive data longer than ~30 days" ceiling. A durable regression test (`tests/test_ph1_10_sensitive_log_retention_config.py`, 6 passed) parses the repository's own versioned `ops/nginx/logrotate.d-*` reference copies and asserts: the sensitive stanza matches only its one file, `rotate 30`/`daily`, `create 0600 www-data root`, a `postrotate` reopen hook, and that the generic stanza's file list is explicit (no glob) and never includes the sensitive log. Full regression: `798 passed, 3 skipped`.
**PH1-06 re-check:** its own stated content-redaction/DB-retention/quarantine criteria were genuinely met and remain `[x]`; the nginx-scheduler gap this task closes is recorded as discovered drift on that entry, not treated as a PH1-06 regression (see the note there). `journald`'s own `MaxRetentionSec` is separately unset (out of this task's nginx-specific scope) and is flagged there for a future task.
**Residual risk:** none specific to nginx sensitive-log rotation. `journald` retention (noted above) remains a separate, unscoped observation.

# Phase 2 — Security foundation

**Status 2026-08-24:** first executable dependency block completed without
starting Phase 3. PH2-02 and PH2-04 are production-complete. PH2-03 durable
replay is deployed, while mutation idempotency remains partial until the
external Filin caller adopts signed v2 operation IDs. PH2-01 contract/schema/
API/migration design is fixed, but implementation remains blocked by PH3 parent
account identity and Phase 4 migration. Consequently PH2-05/06/07 remain open
and are not eligible for completion.

## [x] PH2-01 — Random 256-bit MGBoost opaque subscription tokens — P2

**Depends:** PH1-06, PH3 account identity, Phase 4 migration.
**Never:** TG ID/hex/SHA256(TG_ID)/concatenated SHA. **Scope:** CSPRNG >=256-bit, hash/verifier DB, account binding/version, individual revoke/reissue; old token invalid after rotation; no full token logs.
**Ownership-rebind rule:** ordinary Telegram binding change does not rotate opaque token or child UUID. If primary admin marks suspected compromise, opaque token rotation is mandatory in the same recovery workflow; child UUIDs remain unchanged unless a separate device/credential revoke is explicitly requested.
**Target:** `sub.beykus.fun/{opaque_token}` with route collision contract.
**Accept/tests:** DB leak не даёт URL; entropy/tamper/enumeration/timing; per-token revoke; ordinary ownership rebind preserves token; compromise flow invalidates old opaque token and issues a new one without implicit UUID rotation.
**Migration/rollback:** versioned legacy alias; revoked token никогда не реактивируется.
**Design completed 2026-08-24:** canonical external route is `https://sub.beykus.fun/<43-char-base64url-token>` generated from exactly 32 CSPRNG bytes. Reserved application paths are matched first; the root token route accepts only the exact new format. Local persistence uses a unique SHA-256 verifier (random 256-bit input makes offline brute force infeasible), account FK, version/generation, lifecycle timestamps/status and no raw token. Root resolution performs verifier lookup first and never falls through to the legacy `/sub/{legacy_token}` namespace. Rotation/revoke use account-generation CAS, immutable credential history and an explicit one-time delivery state; raw delivery material may exist only as a short-lived AEAD envelope under a dedicated non-DB key until ACK, never plaintext in DB/backup/log. Full contract, API, schema and migration requirements: `docs/PHASE2_OPAQUE_TOKEN_DESIGN.md`.
**Implementation blocker:** `account_id` and ownership authority do not exist until PH3-01/PH3-04; device-slot resolution needs PH3-02/03; alias bridging/revoke is Phase 4. Do not create an interim token table keyed to Telegram ID or legacy Marzban username, because that would require a second identity migration and could recreate the TG-ID credential flaw.
**Implemented/staging verified 2026-08-25:** additive dormant `src/subscription_credential_schema.py`/`src/subscription_credentials.py` implement the schema/API exactly as designed -- CSPRNG 32-byte token, SHA-256 verifier-only storage, `(account_id, generation)` CAS rotation that atomically revokes the previous `ACTIVE` credential in the same transaction, a partial-unique index enforcing at most one `ACTIVE` credential per account, and immutable terminal state (a schema trigger blocks any further status/timestamp change once `REVOKED`/`EXPIRED`). One deliberate, documented deviation from the design doc's "recommended" AEAD delivery envelope: `prepare()` never persists the raw token anywhere, not even encrypted -- it returns it once, synchronously, inside the authenticated caller's own response (matching the design's own "returns/delivers the raw token" API wording), satisfying the identical "raw token absent from DB/backups/logs" requirement without adding a new symmetric-encryption dependency this project does not otherwise have. The cost is explicit: a `prepare()` response that never reaches its caller cannot be recovered and must be explicitly abandoned (`revoke_reason='ABANDONED_PENDING'`) before a fresh generation can be issued -- the design doc itself allows exactly this ("if delivery ultimately fails, admin issues another generation"). The public resolver engine (`src/opaque_resolver.py`, reached only through the new `GET /<opaque_token>` route added to `src/routes/opaque_sub.py`) resolves the verifier to an `ACTIVE` credential's account, recomputes the parent's current PH3-08 desired state (never a cached value; an `EXPIRED`/`DISABLED` parent is denied before any provisioning), runs PH3-04's `hwid_gate.evaluate()` unconditionally (this new route has no legacy-compatibility burden, so it is not gated by `PH3_04_ENFORCEMENT_MODE`, which remains `OFF` and continues to have zero effect on the legacy path), and then reuses PH3-02's existing `DeviceSlotStore.claim()` and PH3-03's existing `ChildProvisioningStore` outbox/worker pipeline verbatim -- no parallel slot or child-creation code exists. A new minimal typed broker operation, `child.user.subscription.get`, fetches the child's own rendered Marzban subscription body server-side (the same public-endpoint mechanism the legacy resolver already uses, just pointed at a per-device child) and never returns the child's subscription bearer path to the caller. The route is dormant by two independent, redundant gates: `OPAQUE_SUBSCRIPTION_ENABLED` defaults to off (uniform invalid response regardless of DB state), and separately neither `sub.beykus.fun` nor `panel.beykus.fun`'s nginx vhost proxies a root path to the application at all today (verified against the live production nginx config), so the route is unreachable externally either way. A known, deliberate, documented scope limit: the engine resolves *additional* devices/slots for an account that already has at least one child, but does not itself discover/verify a brand-new source template for an account's very first device -- that trust decision belongs to PH4-01's legacy bridge (which verifies a real live legacy Marzban user on every request by design), not to this module. Full contract: `docs/PHASE2_OPAQUE_TOKEN_DESIGN.md`.
**Tests:** `tests/test_subscription_credentials.py` (15 passed: issuance, CAS-rotation-revokes-previous-atomically, idempotent prepare/activate/revoke, stale-CAS rejection, terminal-immutability at both the store and raw-schema level, cross-account rejection, abandoned-pending reissue, zero raw-token leakage in a full DB dump), `tests/test_opaque_resolver.py` (12 passed: typed subscription-fetch broker operation incl. verifier mismatch, invalid/unknown token, known-HWID idempotent retry, missing/malformed HWID, unsupported client, full-slots clear refusal with no eviction, cross-account HWID denial, expired-parent denial even with a valid token, no-silent-create for an account's first device, a second distinct HWID getting its own second child) and `tests/test_opaque_sub_route.py` (5 passed: reserved-route precedence, exact-43-char match vs. the SPA catch-all, default-off uniform response, env-driven flag). Full regression: `737 passed, 3 skipped`. The real isolated Marzban 0.8.4 gate (`scripts/verify_ph2_01_opaque_resolver_staging.py`, same immutable digest as every other PH3-0x gate) passed all 11 checks: a bridged device receives a real child-credential subscription body with the account's shared source UUID absent from it, an idempotent retry converges on the exact same child/slot/generation, a second distinct HWID gets its own second child, missing-HWID/invalid-token/revoked-token are all denied, and zero raw credentials appeared in the MGBoost DB dump.
**Remaining before `[x]`:** the ROADMAP scope for this line historically included "legacy alias bridge" in its own closure bar; per explicit owner sequencing this session (PH2-01 -> PH2-07 -> PH4-01), that bridge is deliberately left to PH4-01 and is not implemented here. Everything else in the original "Remaining before `[x]`" list (schema/API/resolver, one-time issuance/rotation/revoke, uniform-failure public endpoint, migration/crash/concurrency tests) is complete and verified above. `PH2-06` rate/burst/trusted-XFF controls are not yet layered onto this new route (PH2-06 itself remains `[ ]`); until then, this route's only production exposure control is the dormancy described above.
**PH2-01 status re-check after PH4-01 (2026-08-25):** the legacy alias bridge closure criterion is now satisfied by PH4-01 (`[~]`, see below). PH2-01 stayed `[~]`, not `[x]`, because a genuinely separate, real criterion in this same entry's own "Accept/tests" line remained untested: "ordinary ownership rebind preserves token" and "compromise flow invalidates old opaque token and issues a new one" both describe behavior during a Telegram ownership rebind -- and PH2-05 (admin/user session and ownership lifecycle, the only place a rebind can happen) was not started yet. There was no rebind mechanism to test the opaque credential's behavior against.
**PH2-01 closed `[x]` 2026-08-25, after PH2-05:** PH2-05's real isolated Marzban 0.8.4 gate and focused tests now prove exactly the remaining criteria: ordinary rebind leaves the opaque credential verifier/generation byte-identical; compromise rebind denies the old opaque token and issues exactly one new active generation without any implicit child-UUID rotation. No other unclosed PH2-01 acceptance criterion remains. See `ROADMAP.md` PH2-05 and `docs/PHASE2_OWNERSHIP_REBIND.md`.

## [x] PH2-02 — LK device-name XSS и inline handlers — P2

**Depends:** PH1-01 frontend conventions. **Files:** `frontend/assets/lk.js`, `frontend/lk.html`, весь frontend.
**Scope:** `textContent`, opaque dataset IDs, `addEventListener`; no inline onclick.
**Accept/tests:** quotes/entities/tags/backslashes остаются текстом; CSP без unsafe-inline; rename/delete E2E.
**Rollback:** не возвращать unsafe handlers.
**Implemented and staging verified 2026-08-24:** LK/API-controlled username, node, error and device values are now rendered only through DOM construction and `textContent`; device mutations use validated positive integer opaque `data-device-id` values and `addEventListener`, never a device name inside JavaScript/HTML. All LK inline event attributes and HTML-string render sinks were removed. The browser subscription copy page also moved its handler to a same-origin external script and keeps the legacy URL only as HTML-escaped text, not JavaScript source. Static whole-frontend scan found no inline event attributes; the only remaining `innerHTML` is the PH1-01 admin controlled `SafeMarkup` renderer covered by its sanitizer tests.
**Evidence:** malicious quotes/entities/tags/backslashes remain inert text under the production LK CSP; real Chromium rename and delete flows pass with hostile device/API values. Full regression: `404 passed, 2 skipped` in the base environment and `406 passed` with browser dependencies. Deployment runbook and production gate: `docs/PHASE2_LK_XSS_HARDENING.md`.
**Production completed 2026-08-24:** exact deploy commit was pulled and only `mgboost-panel` restarted. LK/admin/assets, authenticated Filin status, durable Stars tables, Telegram bot through the configured proxy, broker/nginx/Marzban health and token-safe journals passed. The post-deploy snapshot exactly matched the pre-state for 25 users/configs, 71 device rows and 71 HWID locks with zero config fetch errors. UUID, legacy subscription token/URL, HWID, expiry, tariff, forced client reconfiguration and unexpected effective config changes: 0.

## [~] PH2-03 — Shared durable Internal HMAC replay protection — P2

**Keep:** HMAC-SHA256, timestamp, nonce, body hash, constant-time. **Add:** atomic shared nonce consume+TTL, idempotency/CAS.
**Accept/tests:** replay блокируется same/other worker и после restart/cache flood.
**Rollback:** fail closed при store outage.
**Implemented/staging verified 2026-08-24:** signed nonce consumption moved from a bounded process-local dict to an additive SQLite table with SHA-256 nonce refs, TTL pruning, row cap and `BEGIN IMMEDIATE`/primary-key CAS. Signature verification happens before storage, store/capacity outage returns `503`, and replay returns `409` across separate DB connections/restart. Legacy v1 signature/payload/response contract is unchanged. Optional signed v2 binds a high-entropy idempotency key into the HMAC; one logical mutation is atomically `pending -> completed`, mismatched reuse and same/other-worker retry are rejected, only request/response/key hashes are stored, and a crash/ACK failure leaves a non-expiring `pending` reconciliation state instead of blind re-execution.
**Tests:** concurrent two-connection consume, restart replay, invalid-signature non-consumption, expiry/capacity, store outage, hashed-at-rest assertions, v1 compatibility, v2 completed/conflict/pending crash-retry and acknowledgement failure. Full regression: `415 passed, 2 skipped` base and `417 passed` with browser dependencies; runbook: `docs/PHASE2_INTERNAL_HMAC.md`.
**Production compatibility rollout 2026-08-24:** encrypted backup and restored-copy additive-schema/CAS gate passed before restart. Real signed v1 status succeeded, the exact nonce was rejected after MGBoost restart, and a fresh nonce succeeded. Active DB quick-check/hash-only assertions, broker/Marzban/admin/LK/Stars/Telegram/support and token-safe logs passed. Masked 25-user/config and 71-device/HWID state matched exactly; user credential/config/expiry/tariff changes: 0. `INTERNAL_API_REQUIRE_V2_MUTATIONS=0` is confirmed.
**Remaining before `[x]`:** update the external Filin mutation caller to generate/reuse stable v2 operation IDs, verify create/renew/delete retry/reconciliation end-to-end, then enable `INTERNAL_API_REQUIRE_V2_MUTATIONS=1`. Until caller adoption, v1 mutations remain intentionally accepted and cannot be safely deduplicated when a caller retries the same logical action with a fresh nonce.

## [x] PH2-04 — Headers/cache/error hardening — P3

**Depends:** PH1-01, PH2-02 перед strict CSP.
**Scope:** no-store, Referrer-Policy, HSTS, CSP, frame protection, uniform invalid subscription, hide unnecessary docs/version.
**Tests:** header/frame/cache/referrer/status/body/timing. **Rollout:** CSP report-only first.
**Implemented/staging verified 2026-08-24:** every application response now inherits no-store (unless an explicit static-asset cache policy exists), no-referrer, nosniff, DENY/frame-ancestors, restrictive Permissions-Policy and default-deny CSP; stdlib/Python patch versions are removed from `Server`. HSTS is applied at the production TLS-terminating nginx boundary. Admin/LK retain their stricter executable CSP. Browser subscription uses a safe baseline plus strict report-only policy until the production gate flips `SUB_BROWSER_CSP_ENFORCE=1`. Invalid upstream 401/403/404 subscriptions receive the same fixed 404 body and minimum response floor; outages receive a generic 502 without upstream details. `/docs`, `/redoc`, `/openapi.json`, `/debug`, `/version` are explicit 404. Non-base64 upstream response headers now use the same allowlist as normal subscriptions and CR/LF/oversized response header values are dropped.
**Tests:** real HTTP dynamic/static/error header matrix, runtime version hiding, uniform invalid status/body/timing, outage redaction, report-only/enforce CSP contract, oversized token, upstream header allowlist, and real Chromium browser-copy execution with zero CSP violations. Full regression: `433 passed, 3 skipped` base and `436 passed` with browser dependencies; runbook: `docs/PHASE2_HTTP_HARDENING.md`.
**Compatibility:** valid legacy `/sub/{token}` body, UUID, filters, response metadata allowlist and client configuration semantics remain unchanged. No token/UUID/HWID/expiry/tariff migration.
**Production completed 2026-08-24:** application report-only deployment passed before strict CSP enforcement. Nginx `server_tokens off` and HSTS were applied idempotently to the global config plus five child sensitive locations after root-only backup and `nginx -t`; active `panel.beykus.fun`/`sub.beykus.fun` externally return HSTS and no nginx version. Strict browser CSP was then enabled and externally verified on a valid legacy URL with no report-only header. Masked 25-user/config and 71-device/HWID state matched exactly; admin/LK/Stars/Filin/broker/Marzban/Telegram/support and token-safe logs passed. UUID, token/URL, HWID, expiry, tariff, forced reconfiguration and unexpected config changes: 0.
**Operational residuals:** MGBoost/broker can report `systemd active` several seconds before their HTTP listeners are ready, so deploy/monitoring must retain a readiness loop (tracked with the availability redesign); legacy `mgboostmsk.ddns.net` did not resolve during the external gate, while its installed nginx block was hardened. Active product hostnames passed.

## [x] PH2-05 — Admin/user session and ownership lifecycle

**Depends:** PH1-01, PH2-01.
**Scope:** logout/revoke/rotation/TTL/scopes, CSRF/Origin, Telegram ownership recovery/rebind, authz after authn.
**Fixed ownership recovery policy (OPD-39/DL-041):** первый rollout — только ручной rebind основным MGBoost admin; self-service recovery/codes отсутствуют. HWID и possession subscription URL не являются достаточным proof. После successful atomic rebind старый Telegram binding немедленно revoked; dual active ownership запрещён.
**Preserve:** тот же parent account, plan, expiry, WL periods, traffic history, device slots, HWIDs, child users и UUID. Обычный rebind не вращает opaque token/UUID; suspected compromise требует одновременной rotation opaque token по PH2-01.
**Audit:** immutable old/new Telegram ID, primary-admin actor, reason, timestamp, result/correlation; IDs не выводятся в общие application/access logs.
**Tests:** IDOR, CSRF, fixation, expiry/replay, multi-TG legacy ownership, non-primary denial, HWID-only/token-possession-only denial, atomic old-binding revoke, data/child/UUID preservation, ordinary no-rotation, compromise mandatory token rotation, partial-failure rollback/reconcile.
**Out of scope:** self-service recovery и recovery codes; возможны только через новое future product decision.
**Audit matrix vs. PH1-01 (2026-08-25):** logout/revoke/rotation/TTL, CSPRNG opaque session, hashed lookup, Secure/HttpOnly/SameSite cookies, constant-time CSRF, fixation-safe login and "authz after authn" (the existing `PrimaryAdminAuthority` sealed-capability pattern, already reused verbatim by `InternalEntitlementStore`/`LegacyBridgeStore`) are all already closed and production-verified by PH1-01 (341+ tests) -- not reimplemented here. `OwnershipRebindStore` requires the exact same sealed capability every other primary-admin-gated store already requires, so any future HTTP route wiring inherits PH1-01's session/CSRF/fixation protections automatically. The only new work in this phase is Telegram ownership recovery/rebind.
**Implemented/staging verified 2026-08-25:** additive dormant `src/ownership_rebind_schema.py`/`src/ownership_rebind.py` (no route imports it). Reuses PH3-01's existing `mgboost_telegram_identities` table verbatim -- its own partial-unique indexes (`ux_mgboost_tg_active_identity`, `ux_mgboost_account_active_owner`) already make "two ACTIVE owners" and "one Telegram ID owning two accounts" structurally impossible, not just conventionally avoided. `process_rebind()`: claim -> atomic identity mutation (re-verify current owner == caller's `expected_old_telegram_id` CAS expectation, else a stale-request conflict; revoke old with `revoke_reason='ownership_rebind:<mode>'`; insert new with `provenance='ADMIN_REBIND'`) -> for `COMPROMISE` only, unconditional PH2-01 credential rotation (`prepare()`+`activate()` CAS, with abandon+reissue on a lost-response retry, never reactivating the old token) -> finish (terminal, schema-immutable). `ORDINARY` never touches `mgboost_subscription_credentials` at all (schema `CHECK` enforces this); neither mode ever touches `mgboost_device_slot_generations`/`mgboost_child_user_intents` -- this is not a device rebind and never calls `src/child_lifecycle.py`. Full contract: `docs/PHASE2_OWNERSHIP_REBIND.md`.
**Tests:** `tests/test_ownership_rebind.py` (16 passed): atomic revoke+activate with no dual owner, ordinary preserves the opaque credential and all PH3-02/03/08 data byte-identical (including the remote child UUID), compromise revokes the old opaque token and issues exactly one new generation while never rotating the child UUID, mandatory rotation with no bypass parameter, lost-response abandon+reissue, retry-after-success creates no `N+2` generation, non-primary/missing-capability denial, stale-old-owner (IDOR-adjacent) rejection, dual-ownership denial, cross-account IDOR rejection, concurrent same-account rebind (exactly one winner), idempotency-key-reuse conflict, schema-level terminal immutability, zero raw credential leakage. Full regression: `787 passed, 3 skipped`. The real isolated Marzban 0.8.4 gate (`scripts/verify_ph2_05_ownership_rebind_staging.py`, same immutable digest as every other PH3-0x/PH2-01/PH4-01 gate) passed all 12 checks across two independent synthetic parents with real PH3-03 children: ordinary rebind left the remote child and opaque credential completely untouched while atomically swapping the Telegram owner; compromise rebind (fresh setup) denied the old opaque token, issued exactly one new active generation, and still left the remote child's UUID unchanged; zero raw credentials in the MGBoost DB dump.
**Production dormant deploy 2026-08-25:** see the cardinality block below. No real production Telegram identity was created, changed or rebound; no synthetic Telegram ID was created in production for evidence -- all destructive evidence is from the isolated gate and test suite only.
**COMPROMISE crash-boundary fault injection (2026-08-25):** a targeted point-in-time review found and fixed a real gap before it could ever reach production: the original `process_rebind()` ran the identity mutation before the PH2-01 credential rotation, each its own separately committed SQLite transaction, so a real crash between them left a durable, insecure resting state -- new Telegram owner active while the old (compromised) opaque credential was still valid -- until a manual retry closed it. Fixed by rotating the credential *first* for `COMPROMISE` (`ORDINARY` is unaffected, it has no credential step). `tests/test_ownership_rebind_compromise_crash.py` (5 passed) proves this with a real crash simulation -- the SQLite connection is closed mid-sequence and a *fresh* `Database()` opened against the same on-disk file inspects durable state -- across three crash points (before any step, between the two steps, after both steps before `finish()`), plus a fault injection directly inside the real `process_rebind()` function. Reverting the fix was confirmed to make the fault-injection test fail (verified by temporarily restoring the old order), proving the test actually catches the regression. Full regression after the fix: `792 passed, 3 skipped`; the real isolated Marzban 0.8.4 gate was re-run and passed all 12 checks unchanged (ownership rebind never calls Marzban, so this could not have affected that evidence, but it was re-confirmed rather than assumed). See `docs/PHASE2_OWNERSHIP_REBIND.md`.

## [x] PH2-06 — Subscription/API abuse controls — P2

**Depends:** trusted proxy + PH2-01.
**Scope:** rate/body limits, malformed/oversized IDs, deadlines, uniform failures, trusted XFF only from nginx.
**Accept/tests:** один client не блокирует/лимитирует других; fuzz/slow/burst/spoofed-XFF.
**Rollback:** versioned, scoped, time-limited relaxation.

**Сделано (2026-08-26):** production topology confirmed (`Internet -> nginx -> MGBoost`, app bound to `127.0.0.1:8001` only, unreachable directly from the Internet). Trusted-XFF client identity was already fully solved by an existing primitive, reused unchanged: `http_utils.client_ip()` trusts `X-Real-IP` only when the actual TCP peer is loopback (nginx overwrites, never appends, that header -- unspoofable by an external client). New `src/subscription_rate_limit.py::SubscriptionRateLimiter` -- single-process in-memory sliding window (same architecture class as the existing `AdminLoginRateLimiter`, no second competing framework), keyed by client IP only (never by raw token/token hash -- a malformed-token flood shares the exact same per-IP budget as any other request from that IP), bounded memory (`_MAX_TRACKED_IPS` eviction, each IP's own bucket capped at `max_requests` even under an indefinite flood since rejected requests are never recorded). Defaults (`60`s window / `30` requests) are a conservative technical implementation detail with no real-client impact -- chosen and documented, not a product decision. Wired as the very first check in both `handle_sub` (legacy) and `handle_opaque_sub` (PH4-04), before any token-length validation, credential resolution or upstream/broker work; a limited request gets a uniform `429`/`Retry-After` response that never depends on token validity (not a token-existence oracle). Restart behavior is explicit: in-memory state, so a process restart resets every client's budget to full (same accepted behavior as the existing admin-login limiter). Body/size/malformed-ID and uniform-failure requirements were largely already satisfied by existing code, verified rather than reinvented: legacy token length is already bounded (`_MAX_LEGACY_TOKEN_LENGTH=4096`) before any upstream call; the opaque route's exact `{43}`-char regex already rejects any wrong-length path before it reaches the resolver; HWID is already regex-bounded (6-180 chars) before any slot-claim work; unsupported HTTP methods already get the stdlib's own uniform `501` with zero route/app work; `handle_sub`/`handle_opaque_sub` already share one uniform invalid-token response helper (`_invalid_subscription_response`), and broker calls already carry their own bounded timeout (`BrokerTransport.timeout`, capped at 30s) -- deadlines "after durable commitment" were already safe, no legacy-fallback path exists once a slot claim has happened (PH4-02's own fail-closed contract, unchanged). New: an explicit socket-level deadline (`_Handler.timeout = 15`) on the single-threaded stdlib server -- bounds how long one slow client's connection can occupy the one request-handling thread before any request line/headers/body are fully read (never fires mid-mutation, since no further socket reads happen once processing starts) -- protects the *current* single-process architecture without pretending to be PH8-01's future concurrency redesign.
**Tests:** `tests/test_subscription_rate_limit.py` (15 passed): normal-refresh not limited, burst hits a controlled limit, a different IP is unaffected by another's burst, window recovery, rejected requests don't grow the bucket/extend the block, bounded memory under many distinct IPs, no raw token/hash ever stored, spoofed XFF from an untrusted peer ignored, XFF from the trusted loopback boundary honored, missing/malformed XFF falls back safely to the peer address, a burst of invalid tokens is rejected by the limiter before any resolver/upstream work, a different IP keeps working during another's burst, the opaque route enforces the same limiter, and the 429 response never contains the request's token anywhere (body or headers). New `tests/conftest.py` resets the shared limiter between tests (many pre-existing unrelated tests already call these same routes). Full regression: `884 passed, 3 skipped` (zero regressions from `869`).

## [x] PH2-07 — No persistent raw upstream token in new resolver

**Depends:** PH2-01, PH1-05, PH3 children.
**Scope:** opaque token -> account/slot -> child config without stored raw legacy token; если нужен recoverable upstream secret, отдельно design encryption/rotation.
**Tests:** DB+source leak, revoke, broker outage. **Migration:** raw legacy exists only in marked bridge with retirement.
**Verified 2026-08-25 -- no new production code needed:** PH2-01's opaque resolver (`src/opaque_resolver.py`, `src/routes/opaque_sub.py`), as already built, already fully satisfies this contract -- confirmed by proof, not by new implementation. Data flow: opaque token verifier (`SubscriptionCredentialStore.resolve`) -> account_id -> PH3-08 fresh desired state -> PH3-04 `hwid_gate.evaluate` -> PH3-02 `DeviceSlotStore.claim` -> PH3-03 `ChildProvisioningStore` -> the new minimal typed `child.user.subscription.get` broker operation, which fetches the *child's own* ephemeral Marzban subscription path entirely inside the broker process and never returns that path to the caller. At no point does this path read, store, forward or log a shared/legacy upstream subscription bearer: `src/opaque_resolver.py` and `src/routes/opaque_sub.py` import no direct `MarzbanClient` (only the typed `ServiceMarzbanClient` broker transport), contain zero calls to `.get_sub(` at all, write to no legacy request/device table (`sub_requests`/`user_devices`/`hysteria_stats`), and contain zero `print`/`logging` calls -- there is structurally nothing in these two modules that could leak a raw value. The account's legacy alias (`mgboost_legacy_account_aliases.legacy_username`) used for the PH3-03 source-contract template is a public identifier, never a bearer/token -- the schema has no raw-token column anywhere (`sub_requests`/`user_devices`/`hysteria_stats`'s own pre-existing `token` columns are PH1-06 SHA-256 references, unrelated to and untouched by this new path). Full contract and evidence: `docs/PHASE2_OPAQUE_TOKEN_DESIGN.md`.
**Tests:** `tests/test_ph2_07_no_persistent_legacy_token.py` (12 passed): static source-scan proof (no `MarzbanClient` import, no `.get_sub(` call, no legacy-table reference, no logging/print call, no schema module adds a raw bearer/token column), a mandatory negative test that fails if the forbidden "child resolution failed -> fetch legacy /sub with the old token" fallback is ever added, a behavioral proof that a full resolve succeeds against a backing store whose only fetchable subscription is the child's own (asserting the source template's own subscription is never fetched), broker-outage fail-closed with no fallback, revoked-credential and expired-parent denial, and one shared code path proven for both an existing and a lazily-created child. Full regression: `749 passed, 3 skipped`. Real Marzban 0.8.4 evidence: the existing PH2-01 gate (`scripts/verify_ph2_01_opaque_resolver_staging.py`) already proves this end to end against a real instance -- its own `child.user.subscription.get` calls are traceable (via the broker's `self.marzban.get_sub(sub_token)` call) to only ever use the *child's* `subscription_url`, never the legacy source template's; no second real-Marzban run was needed to re-demonstrate an already-proven property.

## [!] PH2-08 — Reseller tenant authentication/capabilities — superseded 2026-08-23

**Причина:** владелец уточнил, что отдельного reseller login/tenant нет; external payment применяет только основной MGBoost admin.
**Сохранённые security requirements перенесены:** PH1-01/05 и PH2-05 защищают admin session/broker; PH5-09 проверяет account/catalog/price/idempotency; negative tests запрещают arbitrary username/price/GB/free entitlement и raw bearer export. Shared Filin HMAC не является payment/admin session.

# Phase 3 — Parent account + child devices

## [x] PH3-01 — Parent account/identity/entitlement schema

**Inputs (approved):** six-plan Stars catalog, `RUB-2026-08-23-v1`, OPD-06 device limits and OPD-14 WL pool model. **Entities:** accounts, Telegram identity links, plan versions, subscriptions, entitlements/overrides. Telegram ID не credential. No open product decision blocks schema design.
**Accept:** account независим от Marzban username; `billing_required`, WL quota и device limit вычисляются entitlement engine.
**Tests:** identity uniqueness/IDOR, plan snapshots, multiple identity policy.
**Migration/rollback:** additive schema/backfill preview; legacy data не удалять.
**Implemented/staging verified 2026-08-24:** additive `ph3_01_parent_account_v1` schema introduces account identity independent of Telegram/Marzban, revocable unique Telegram owner links, immutable versioned plan/duration and subscription-term snapshots, account-scoped subscription/entitlement state, expiring typed overrides, explicit payment/mutation provenance and sequential WL-period anchors for future ledger/device work. Device limits are data (`3/6/12`, technical ceiling 99) with a separate `UNLIMITED` mode; WL uses decimal-byte quota plus explicit period days; duration rows accept future 180-day SKUs without schema changes. No legacy runtime path reads these tables and no catalog/account/backfill rows are seeded.
**Production preview:** 25 authoritative Marzban users versus 26 distinct local legacy usernames; 71 device/HWID rows across 24 usernames; 5 Telegram links across 4 usernames, including one multi-link case; two durable Stars events for one username, but zero current six-plan assignments are provable. Automatic backfill is therefore exactly zero. Migration applied twice to an online production DB copy (`first=True`, `second=False`), preserved the exact digest of all 20 legacy tables, left all ten new runtime tables empty, and passed SQLite quick/FK checks. Full regression: `452 passed, 3 skipped`; PH3-01 constraint suite: `19 passed`, including repeated concurrent identity claims and fail-closed incompatible-schema detection. Evidence/runbook: `docs/PHASE3_PARENT_ACCOUNT_SCHEMA.md`.
**Production completed 2026-08-24:** a fresh encrypted root-only backup completed successfully before the exact commit was pulled. Restarting only `mgboost-panel` applied one matching checksum marker; all ten new runtime tables remained empty, with `quick_check=ok` and zero FK violations. Exact masked pre/post state matched for 25 Marzban users/configs, 71 device rows and 71 HWID locks. Legacy admin/LK/subscription resolution, durable Stars ledger, signed Filin, broker, Marzban container, Telegram proxy/support, nginx/systemd and token-safe logs passed. UUID, legacy subscription token/URL, HWID, expiry, tariff, child-user creation, forced reconfiguration and unexpected config changes: 0. PH3-02 was not started.

## [x] PH3-02 — Atomic device slots with generation

**Depends:** PH3-01. **Entities:** BASE/ADDON/INTERNAL slot, stable number, generation, HWID verifier/masked form, desired/observed status, child mapping.
**Capacity policy:** current paid limits строго 3/6/12; future commercial/technical cap 99; INTERNAL configurable/unlimited. Add-on purchase сейчас не запускается.
**Accept:** DB atomically ограничивает entitlement capacity; один HWID -> тот же active slot/generation; не process `RLock`.
**Tests:** два запроса за последний slot, 3/6/12, future cap 99, INTERNAL unlimited, duplicates, multi-worker, slot generation reuse.
**Rollback:** additive; не переиспользовать старый credential generation.
**Implemented/staging verified 2026-08-24:** dormant additive slot/generation schema and repository use account-scoped composite FKs, global active-HWID and per-slot active-generation uniqueness, `BEGIN IMMEDIATE`, entitlement read/count/select/claim in one transaction, monotonic `generation+1` triggers and terminal history. Raw HWID becomes only a dedicated-key HMAC-SHA256 verifier plus HMAC-derived mask; neither raw value nor key is stored/returned. Commercial baselines accept only 3/6/12; INTERNAL supports configurable/unlimited with technical cap 99; commercial upward/add-on capacity remains disabled. A lower entitlement reports conflict/overage and never chooses a device. Focused tests `21 passed`, including two connections, two spawned processes, duplicate convergence, final-slot race, stale release, cross-account isolation, privacy-at-rest, pre-audit forensic classification and generation reactivation denial; full regression `473 passed, 3 skipped` twice.
**Production-copy gate:** exact PH3-02 migration applied twice (`true/false`), preserved the digest of all 30 pre-existing non-marker tables, left parent/slot/generation rows at zero, kept legacy devices/HWID locks 71/71, and passed checksum/quick/FK checks. No legacy runtime module references the new repository. Forensic note and rollout/rollback: `docs/PHASE3_DEVICE_SLOTS.md`.
**Production completed 2026-08-24:** a fresh encrypted backup preceded the exact `1d0d120` pull and `mgboost-panel`-only restart. The migration checksum is present while all parent/slot/generation tables remain empty; legacy `user_devices`/`hwid_lock` remain 71/71 and authoritative. Exact pre/post masked digests match for all 25 Marzban identities and 25/25 fetched legacy configs with zero fetch errors. Admin/LK, invalid-sub uniform response, durable Stars state, signed Filin, localhost broker, Telegram proxy, nginx/systemd, SQLite quick/FK and token-safe journal/access-log gates passed. UUID, legacy subscription URL/token, HWID, expiry, tariff, child users, forced client reconfiguration and unexpected config changes caused by deployment: 0. Rollback remains application-only with no credential change. PH3-03 was not started.

## [x] PH3-03 — Lazy idempotent child Marzban creation

**Depends:** PH1-05, PH3-02. **Scope:** child создаётся при первом занятии slot; отдельный UUID; не создавать 12 заранее.
**Accept:** 2 занятых Family slots = 2 child users; retry/timeout не создаёт duplicate/orphan.
**Tests:** remote-created/local-ACK-failed, collisions, concurrent requests.
**Rollback:** remote reread/reconcile до retry/delete.
**Owner-approved canary identity (DL-045):** `beykus`, `beykusios`, `BeykusLaptop` — три immutable legacy aliases одного будущего parent `INTERNAL_OWNER_PRIMARY`, owner Telegram identity `905302972`, INTERNAL/billing-free/WL-unlimited/devices=10/expiry-unlimited. Девять observations — practical slot candidates, не доказанные physical devices; cross-client dedup запрещён. Остальные перечисленные legacy candidates и anomalies не классифицировать/не создавать.
**Prerequisites implemented/staged and single-canary production verified 2026-08-25:** additive one-parent/many-alias group/rows preserve each exact legacy username and evidence. Account public ID, child username and operation ID are server-derived; an account-owned active slot generation can atomically create one immutable child intent plus one leased durable outbox row. UUID persists only as hash/mask after ACK. Append-only attempt events and idempotent `CREATED`/`EXISTING` convergence cover remote-created/local-ACK-failed without duplicate/orphan. Cross-account alias/generation substitution is rejected by transactions/composite FKs. Only the owner-approved dormant canary rows described below exist in production; no legacy runtime route consumes them.
**Broker prerequisite:** the existing ten legacy operations are unchanged; typed `child.user.ensure` accepts only server-derived operation/child identity, reviewed VLESS-only source alias, exact source-contract hash and expiry. It clones exact VLESS inbound/flow shape while making Marzban generate a fresh UUID, then rereads/verifies. No generic payload, caller UUID/proxies/inbounds/data-limit/status is accepted. DL-046 retires Shadowsocks from the product contract; production sources are now VLESS-only after the verified typed cleanup.
**Tests/gates:** focused cleanup/child/broker/staging-guard tests `43 passed`; final full regression `524 passed, 3 skipped`. Typed credential reread verifies only the VLESS UUID against its hash-only local verifier. The previous real 0.8.4 failure remains historical evidence of the retired metadata incompatibility and is superseded by DL-046, not erased. The isolated typed cleanup gate passed on a stale VLESS+Shadowsocks user after restart into exact 25-VLESS/0-SS topology: one narrow mutation, idempotent `UNCHANGED` retry, unchanged UUID/inbounds/subscription and zero raw leak.
**Production retirement and real child gate completed 2026-08-25:** encrypted restore-verified backup preceded the broker-only deploy. Seven of 25 live users had retired Shadowsocks proxy metadata; `beykusios` passed as canary, then all remaining records were removed one by one with fresh digest and reread. One transient pre-effect HTTP 400 stopped the batch at `BeykusLaptop`; the user was unchanged, two fresh snapshots matched, and an individual retry passed before rollout continued. Final inventory is 25 VLESS / 0 Shadowsocks proxies and 25 VLESS / 0 Shadowsocks inbound; 25/25 legacy subscriptions returned 734 VLESS and zero other entries. UUID/token/URL/VLESS inbound/flow/expiry/status/data-limit/HWID/tariff/config changes: 0. Admin/LK, Stars durable state, signed Filin, broker/nginx/systemd, SQLite and token-safe journal gates passed. The subsequent exact-image isolated Marzban 0.8.4 VLESS-only gate is PASS: source and child each retain the exact approved 25 inbound, flow/unlimited status/expiry/data semantics match, config is equivalent after normalizing only the new credential and server-derived username, one CREATE is followed by idempotent EXISTING, lost local ACK reconciles without a second child, typed ephemeral credential reread/verifier passes, contract drift enters ERROR, Marzban outage returns 503 and no raw UUID/token reaches MGBoost DB/logs.
**First exact dormant manifest executed:** source alias `beykusios`; selected observation `user_devices.id=56` / privacy ref `corr_701f5982b4` (iPhone 17, INCY 2.5.2); slot 1/generation 1; parent public ID `acct_435p4hjeoxeq3bzg4ifkdut4veower4r`; child `mgc_sgg6v7t6he43yytsqmkdczzfpa`; operation `op_lw33pjhqhnvorrgh4p754bnc34`; unlimited expiry. The child remains dormant and does not revoke or redirect any legacy UUID/URL/config; all other observations remain legacy.
**Durable worker production-deployed 2026-08-25:** additive workflow state/events, DB-backed provisioning and reconciliation leases, bounded exponential retry, stale-lease takeover, digest/scope/generation validation and explicit manual review are implemented. Typed read-only `child.user.observe` distinguishes absent/match/mismatch without generic Marzban access. The worker observes before CREATE and after it, so remote-success/lost-ACK converges without duplicate; APPLIED+remote-missing never recreates. Privacy-safe metrics cover pending age/count, retries, unavailable, mismatch, stale leases, manual review and desired/observed divergence. The real isolated Marzban 0.8.4 worker gate passed with exactly one CREATE/remote child through lost ACK, worker/broker restart, stale lease and outage recovery; raw UUID persistence was zero. Focused worker/child/broker regression: `58 passed`; full project regression: `550 passed, 3 skipped`. Production is fail-closed `reconcile_only` with the exact existing operation allowlisted, no panel `.env`/SUDO access and no nginx route. The existing APPLIED canary converged read-only to `IN_SYNC` (`provisioned=0`, retries/errors/divergence=0); there remain exactly one parent, three aliases, one slot/generation, one intent/outbox and one remote child. Pre/post masked user/config and 71/71 legacy device/HWID digests match; UUID/token/URL/HWID/expiry/tariff/config/client changes are 0. Runbook/evidence: `docs/PHASE3_CHILD_WORKER.md`.
**Closed 2026-08-25 (see the Gate A/Gate B evidence below):** the mandatory real isolated Marzban 0.8.4 gate, the production SHADOW-only deployment, an accelerated production SHADOW observation window and a real manual child-config check on a separate physical device have all passed. Multi-slot/concurrent acceptance beyond the single fixed canary and the future real-traffic account-aware resolution path remain intentionally out of scope for PH3-03 itself; they belong to the separately approved Phase 4 bridge, which has not started. No existing client's real subscription was switched. PH3-04 remains off. Contracts/runbooks: `docs/PHASE3_CHILD_PROVISIONING.md`, `docs/PHASE3_CHILD_WORKER.md`, `docs/SHADOWSOCKS_RETIREMENT.md`, `docs/PHASE3_DORMANT_PRODUCTION_CANARY.md`.

**Dual-run SHADOW resolver implemented locally, not staged or deployed (2026-08-25):** a prior session had begun a broker capability split (`broker_main.py`, `src/broker_server.py`) and an empty additive `mgboost_shadow_resolver_bindings`/`mgboost_shadow_resolver_metrics` schema (`src/shadow_resolver_schema.py`) without any resolver logic, route wiring or tests. This session completed the split and added the resolver itself in `src/shadow_resolver.py`: a fail-open background-thread hook in `src/routes/sub.py` that runs strictly after the legacy response is already built, so a shadow failure can never change, delay or replace it. `child.user.observe` still uses the existing `mgboost-main` broker identity (it never returns a raw credential); only the new `child.user.credentials.get` capability moves to a separate `mgboost-sub-resolver` identity with its own `MARZBAN_BROKER_RESOLVER_AUTH_KEY`, matching the capability boundary in this task's brief. A binding only ever exists for a device an explicit, root-only administrative tool opts in (`Database.shadow_resolver_bindings`, mirroring the existing canary-tool pattern) and is scoped to `hwid:`-locked legacy devices only, matching the legacy resolver's own enforcement boundary. Functional comparison clones each raw pre-filter legacy VLESS line, substitutes only the ephemeral child UUID, and asserts every other field (address/port, transport, TLS/Reality/SNI, flow, all other query parameters) is byte-identical — the only tolerated differences are the UUID and the free-text remark/label, matching the documented expected-difference contract; this also performs an INCY-equivalent parser/format round trip without touching the real approved canary device. Metrics are a privacy-safe daily PASS/FAIL aggregate (category, credential success/failure, always-1 legacy-fallback-success, latency) with no raw UUID, token, username, IP or header ever written to it or to a log line.
**Test evidence (local only):** `tests/test_broker_client_policies.py` (6 passed) proves `mgboost-main` loses `child.user.credentials.get`, the new `mgboost-sub-resolver` identity can call only that one operation, an unknown client identity is rejected, and omitting the policy split reproduces the exact pre-PH3-03 single-client broker. `tests/test_shadow_resolver.py` (19 passed) proves the dual-run safety invariants (disabled flag, no binding, disabled binding, `fp:`-key requests, and the public `schedule_shadow_resolution` entrypoint returning in well under 0.2s with the network call happening only on a background thread) and covers the required failure matrix — broker unavailable, resolver-capability-denied misconfiguration, remote child missing, remote contract mismatch, credential verifier mismatch (with the drifted raw UUID proven absent from the whole database dump), stale slot generation, invalid account/slot mapping, resolver timeout, shadow comparison failure on an unparsable line, malformed outbox payload, and a metrics-write failure that still never raises. Full project regression is `575 passed, 3 skipped`.
**Real isolated Marzban 0.8.4 SHADOW resolver gate — PASS (2026-08-25):** the same immutable `gozargah/marzban@sha256:8e422c21997e5d2e3fa231eeff73c0a19193c20fc02fa4958e9368abb9623b8d` digest used by every other PH3-03 gate was run loopback-isolated with a disposable SQLite DB. `scripts/verify_ph3_03_shadow_resolver_staging.py` reproduced the exact server-derived identity already live in production (`source_contract_hash`, `acct_435p4hjeoxeq3bzg4ifkdut4veower4r`, `mgc_sgg6v7t6he43yytsqmkdczzfpa`, `op_lw33pjhqhnvorrgh4p754bnc34`), then drove the real `src/shadow_resolver.py` against a real split broker and real Marzban: `mgboost-main` got a real `403` from `child.user.credentials.get`; `mgboost-sub-resolver` could call only that one operation; the PASS/`MATCH` comparison and all 8 failure-matrix categories (`BROKER_UNAVAILABLE` via a real shutdown, `RESOLVER_CAPABILITY_DENIED`, `REMOTE_CHILD_MISSING` via a real delete, `REMOTE_CONTRACT_MISMATCH` via a real expire drift, `CREDENTIAL_VERIFIER_MISMATCH` via a real UUID drift, `RESOLVER_TIMEOUT` via a real `docker pause`, `SHADOW_COMPARISON_FAILURE`, `MALFORMED_REQUEST`) were each recorded as exactly one metric row with `legacy_fallback_success=1`. No raw UUID/token appeared in captured broker/resolver log output or in the resulting MGBoost DB dump.
**Root-only shadow binding tool implemented (2026-08-25):** `scripts/configure_ph3_03_shadow_binding.py` is the only way a device can ever enter SHADOW scope; it is not imported by startup or any route, targets only the one fixed approved manifest (account/alias/device 56/slot 1-generation 1/child/operation), and verifies account identity, alias ownership, HWID-backed device contract, active slot generation, child-intent ownership, exact `APPLIED` outbox operation and `IN_SYNC` reconciliation before creating anything. `create` is idempotent (`CREATED` then `EXISTING`); a conflicting identity for the same device, a wrong/missing account, a wrong device, a stale generation, a non-`APPLIED` outbox, a non-`IN_SYNC` reconciliation and an unexpected pre-existing binding cardinality all fail closed without creating or updating a row. New bindings are created `enabled=0`; `enable`/`disable` are separate idempotent actions. It prints only row ids/booleans, never raw HWID/UUID/token. `tests/test_shadow_binding_tool.py`: `13 passed`.
**Test evidence (local, pre-production):** full project regression is `588 passed, 3 skipped` (575 from the resolver itself plus 13 for the binding tool).
**Production SHADOW-only deployment completed 2026-08-25:** a fresh encrypted backup (`mgboost-db-20260825T082137Z.tar.gpg`, AES-256, `encrypted_backup_create=PASS`/`encrypted_backup_restore=PASS`) preceded a clean fast-forward pull to `13cdc3b`; `extra_configs.json` was untouched. The panel restart applied only the additive `ph3_03_shadow_resolver_v1` migration — both new tables started empty, all pre-existing cardinality and `PRAGMA quick_check`/`foreign_key_check` were unchanged. A new independent `MARZBAN_BROKER_RESOLVER_AUTH_KEY` (384-bit CSPRNG, matching fingerprint on both sides, correct `0640`/`0600` file modes) was installed in the broker and panel environments; after restarting only the broker and panel, a real production HTTP capability check confirmed `mgboost-main` gets `403` on `child.user.credentials.get`, `mgboost-sub-resolver` gets a verified `200` (its returned UUID matched the stored verifier) and is itself `403` on any other operation such as `legacy.user.get`. `scripts/configure_ph3_03_shadow_binding.py --action create` then produced exactly one `enabled=0` row for the approved canary (account 1, alias `beykusios`, device 56, slot 1/generation 1, `op_lw33pjhqhnvorrgh4p754bnc34`); a repeat call returned `EXISTING`. After enabling that one binding and `SHADOW_RESOLVER_ENABLED=1` with only the panel restarted, three controlled non-HWID production `/sub` requests and two organic HWID requests from the real approved device were observed: the real HTTP response stayed legacy on every request (legacy UUID present, child UUID absent, `Cache-Control: no-store`), and the real canary binding recorded exactly one `PASS`/`MATCH` metrics row (`credential_result=SUCCESS`, `legacy_fallback_success=1`, `request_count=2`, both real requests <=66ms). A full scan of the MGBoost DB dump, panel/broker/worker journals and nginx access/error logs found zero substrings whose SHA-256 verifier matched the child's stored credential verifier. Final state: parent accounts=1, active slot/generation=1, child users=1, legacy Marzban users=25, total Marzban users=26, Shadowsocks=0, shadow bindings=1 (1 enabled), outbox unchanged at 1 `APPLIED`, `user_devices`/`hwid_lock` unchanged at 71/71. Admin/LK/Filin-unsigned/broker-loopback-only/nginx-no-broker-route all passed. Legacy UUID, subscription URL/token, HWID, expiry, tariff and forced client reconfiguration changes caused by this deployment: 0. iPhone/INCY's real subscription profile was never switched.
**Gate A — accelerated production SHADOW observation, PASS (2026-08-25):** an explicitly accelerated (not multi-day soak) ~19-minute window collected 11 SHADOW evaluations against the real approved canary binding: 4 organic (from the real approved device) and 7 clearly-labeled controlled evaluations (direct calls into the same `shadow_resolver._resolve_and_record` code path using the device's already-stored, non-raw `request_key` reference and the real broker/Marzban, spaced ~3 minutes apart, never a tight-loop burst, never mutating `user_devices`). Result: **100% PASS/`MATCH`, 0 FAIL, 0 MISMATCH**, `credential_result=SUCCESS` and `legacy_fallback_success=1` on every one of the 11, aggregated into a single metrics row (`request_count=11`, latency 49-91ms). Cardinality, `PRAGMA quick_check`/`foreign_key_check`, service health, broker loopback-only/no-nginx-route and a full DB/journal/nginx leakage scan (0 matches against the child's stored credential verifier) were all confirmed unchanged before and after. The binding's `enabled` flag has no dedicated audit timestamp in the schema (a known, disclosed gap, not fabricated); the closest available evidence for the earlier `enabled=0`→`1` transition is the `mgboost-panel` restart recorded in the systemd journal at `2026-08-25 11:27:16` local time, immediately following the documented `configure_ph3_03_shadow_binding.py --action enable` step.
**Gate B — real manual child-config check on a separate device, PASS (2026-08-25):** a single real Marzban-rendered child subscription line was exported to a root-only `0600` file on production, retrieved locally over `scp` into `0600` temp files, decoded, and only its first `vless://` line was extracted and rendered into a local PNG QR code using the already-installed system `libqrencode`/Pillow (no network service, no new packages, raw value never printed). The owner scanned it into a **separate temporary INCY profile on a Samsung M21** (the primary iPhone 17/production INCY profile was not touched) and reported: QR import PASS, parser/import PASS, connection PASS, real traffic PASS, egress verification PASS (`2ip.ru` showed the VPN/server IP, not the device's own public IP). No field-by-field visual config comparison was performed or is claimed here beyond what the owner actually reported. All temporary artifacts (`config.txt.b64`, `decoded.txt`, the QR PNG locally; `/root/ph3-03-manual-incy-test-config.txt` on production) were securely deleted and their absence verified after the owner confirmed the import.
**Post-Gate-B production check, PASS (2026-08-25):** cardinality, `PRAGMA quick_check=ok`/`foreign_key_check=0`, the single unchanged `PASS`/`MATCH` metrics row (still `request_count=11` — the manual export/QR flow does not itself invoke the shadow resolver), service health, broker loopback-only/no-nginx-route, legacy `/sub` behavior and a second full leakage scan (0 matches) were all reconfirmed after Gate B with zero drift from the Gate A snapshot.
**Residual risk (explicitly not a blocker):** Gate A used an accelerated ~19-minute/11-evaluation SHADOW window instead of a multi-day soak. This is accepted as a residual risk given the accelerated-timeline mandate, not as an unmet PASS criterion.

**Approved dormant-production gate prepared 2026-08-25:** the owner approved exactly one `INTERNAL_OWNER_PRIMARY` slot-1/generation-1 canary and no wider activation. A fixed-target root-only runner requires the checksum-pinned additive schema, independent slot/telemetry keys, a real upstream-authenticated server-side primary-admin session, loopback authenticated typed broker operations, a fresh restore-verified encrypted backup, exact 25-VLESS/0-SS topology and the unchanged `beykusios`/device-56/legacy-subscription baseline before it can write. It records one durable intent/outbox operation before the remote ensure, supports same-operation lost-ACK reconciliation, persists only UUID verifier/mask/observed state and compares all 25 legacy users/configs plus 71 device/HWID rows. Final local focused regression is `79 passed`; full regression is `530 passed, 3 skipped`.

**Dormant production canary PASS 2026-08-25:** a fresh encrypted artifact passed restore before config or data writes. The dedicated slot-HMAC key is independent of telemetry; the primary actor maps only from a real server-authenticated allowlisted admin session; main still has zero Marzban SUDO keys; broker is HMAC-authenticated on `127.0.0.1:8002` with no nginx route. Production now contains exactly one reviewed parent/Telegram identity/plan/subscription/entitlement mutation/review/alias group, three immutable aliases, one slot/generation, one child intent and one logical outbox operation. Outbox attempt 1 is `APPLIED` with `CREATED`; repeated ensure and final reconciliation return `EXISTING`, with exactly one remote child among 26 users. The child is VLESS-only/active/unlimited, retains exact source contract hash, flow and all approved 25 inbound, has a new UUID distinct from the legacy source and stores only `uuid_d4ae1519` plus its verifier. The selected slot stores only `hwid_46609d7eddbb`; raw child UUID/token leakage across MGBoost DB, application/nginx logs and the two-hour journal window is 0.

The first post-effect comparison intentionally stopped because Marzban 0.8.4 mints a new timestamped `UserResponse.subscription_url` alias on serialization; source inspection proved old aliases remain valid until `sub_revoked_at`, so this volatile admin presentation is not rotation. A second over-strict diagnostic correctly stopped because PH1-06 stores 45 `sha256:` token verifiers, not recoverable bearers. The corrected gate compares those verifier rows plus Marzban `created_at/sub_revoked_at` and functional configs. Final pre/post digests match for 25 legacy identities/configs, 45 token refs, 71 device rows, 71 HWID locks and three Stars tariffs. Actual MGBoost legacy `/sub` returned the same source UUID in 37 functional VLESS links with `no-store`; signed Filin returned 200/unsigned 401, admin/LK returned 200, broker/nginx/systemd/SQLite passed. Legacy UUID/token/URL/HWID/expiry/tariff/forced reconfiguration/unexpected config changes: 0. No resolver switch, revoke, PH3-04 or other account/device/child mutation occurred.

## [x] PH3-04 — HWID fail-closed compatibility gate

**Depends:** PH3-02/03, PH3-07 telemetry, implementation readiness of fixed admin-only ownership recovery in PH2-05. Product policy OPD-39 закрыта.
**Policy:** нет supported HWID -> config не выдавать; unknown+free -> assign; full -> clear refusal; known -> same slot/generation. HWID остаётся practical, не cryptographic identity.
**Accept/tests:** compatibility list опубликован; каждый client/version, missing/spoofed/copied HWID, reinstall/device rebind; HWID не принимается как proof Telegram ownership.
**Rollback:** staged feature flag только в migration window; не unlimited silent bypass.
**Implemented/staging verified 2026-08-25 (DL-047 accelerated conservative allowlist):** `src/compat_registry.py` is a git-tracked, schema-validated, exact-match-only `(client, version, platform)` allowlist (no fuzzy/substring matching, no raw identifiers); only tuples with fresh organic-live PH3-07 evidence are `SUPPORTED`, everything else -- including `UNKNOWN` -- is treated as not compatible. `src/hwid_gate.py` is a dormant deterministic policy layer that accepts only an already-resolved `account_id` plus request-derived client/HWID signals (no caller-suppliable slot id, generation, child identity or Telegram proof) and reuses the existing PH3-02 `DeviceSlotStore.claim` verbatim -- no new provisioning path, no new schema. PH2-05 itself has not started and has no ownership-recovery route to misuse; tests prove zero coupling to `mgboost_telegram_identities`/`mgboost_accounts` mutation on every decision path. Neither module is imported by `src/routes/sub.py` or any other legacy route; `PH3_04_ENFORCEMENT_MODE` defaults to `OFF` and currently has no runtime effect. Full compatibility matrix, evidence and design: `docs/PHASE3_HWID_GATE.md`.
**Tests:** `tests/test_compat_registry.py` (14 passed) and `tests/test_hwid_gate.py` (29 passed) cover exact/unknown/spoofed compatibility, missing/malformed HWID denial, known-HWID idempotency, exact 3/6/12 and INTERNAL-unlimited capacity, full-capacity refusal without eviction, same-HWID and different-HWID concurrency, cross-account HWID deny, copied-HWID same-account practical-identity limitation, no caller-suppliable slot/generation, stale-generation non-reactivation, reinstall with free/full slots and zero ownership/child/outbox mutation on every path. Full regression: `631 passed, 3 skipped`. An isolated gate against a `.backup`-consistent copy of the live production DB (never the live file, securely deleted after) reran all of the above directly against the real schema and the real approved canary account's entitlement row with `ALL_PASS`, including proof that every pre-existing production row stayed byte-identical.
**Production completed 2026-08-25:** a verified encrypted backup (`encrypted_backup_create=PASS`/`encrypted_backup_restore=PASS`) preceded a clean fast-forward pull to `d6b86d1`; `extra_configs.json` was untouched. No new schema was needed (the gate reuses PH3-02 tables verbatim), so only `mgboost-panel` was restarted, reaching HTTP-ready in one check. `PRAGMA quick_check=ok`, zero FK violations, and every cardinality figure (parent accounts=1, aliases=3, active generation=1, child intents=1, outbox=1 `APPLIED`, shadow bindings=1 with 1 enabled, `user_devices`/`hwid_lock`=71/71, Marzban 26 total/26 VLESS/0 Shadowsocks) matched the pre-deploy snapshot exactly. Legacy `/sub` returned the same uniform `404` for an invalid token; admin/LK/signed-boundary Filin (`403` unsigned) all passed; broker remained loopback-only on `127.0.0.1:8002` with no nginx route; zero errors in the post-restart journal; zero raw HWID/token/bearer patterns in a targeted log scan. `PH3_04_ENFORCEMENT_MODE` remains `OFF` in production and no route imports the gate, so real legacy subscription responses, UUID, token/URL, HWID, expiry, tariff and client configuration are all unchanged: 0.
**Residual/out of scope:** the compatibility registry's own live evidence is only ~1 day old for several tuples (single-observation entries are explicitly caveated in `docs/PHASE3_HWID_GATE.md`); actual fail-closed activation (`ENFORCE`/`CANARY`) and wiring the gate into any real resolver remain a separate, explicitly-approved future migration step and are not part of this closure. PH3-05 device revoke/rebind, PH3-08 and Phase 4 are unaffected and unstarted.

## [x] PH3-05 — Real child revoke/disable/free/rebind

**Depends:** PH3-03, broker. **Scope:** revoke/free инвалидирует Marzban/Xray UUID; rebind увеличивает generation; old cached config не работает.
**Deletion policy (DL-019/038):** немедленно revoke/disable, сохранить tombstone/history 180 дней; physical delete разрешён только после retention expiry, successful reconciliation и проверки отсутствия живых references. Immutable audit history сохраняется по своей policy.
**Accept/tests:** direct old UUID отклонён на всех nodes; offline-node reconcile; rebind race; delete до 180 дней запрещён; delete после 180 дней не выполняется при live reference/reconcile error.
**Rollback:** новый generation, никогда восстановление leaked UUID.
**Implemented/staging verified 2026-08-25:** additive dormant `src/child_lifecycle.py`/`src/child_lifecycle_schema.py` implement durable REVOKE/FREE/REBIND as a hash-idempotent state machine mirroring PH3-03's outbox exactly (immutable identity, append-only attempt events, atomic lease claim). The hard ordering guarantee is structural: `apply_free` refuses unless the matching REVOKE lifecycle operation is `APPLIED`, and `process_rebind` always revokes-and-verifies the old child before `DeviceSlotStore.rebind()` (a new, narrow, single-transaction release+claim on the exact same slot) ever runs. A new typed `child.user.revoke` broker operation accepts only server-derived `{operation_id, child_username, uuid_verifier}` -- no caller-supplied UUID/proxies/inbounds/arbitrary patch -- disables the Marzban user and rotates its VLESS UUID in the same mutation, then rereads and verifies before returning `REVOKED`/idempotent `ALREADY_REVOKED`/`ALREADY_ABSENT`; a verifier mismatch fails closed. Rebind hands off new-child creation to the *existing* unmodified PH3-03 `child_provisioning`/outbox/worker pipeline -- no parallel provisioning path. Cross-account isolation, no caller-suppliable slot/generation, and zero coupling to `mgboost_telegram_identities`/`mgboost_accounts` are all covered by tests. DL-019/038's 180-day tombstone retention is implemented as a pure injectable-clock eligibility check; no physical-DELETE path exists (matching every other PH3-0x table's permanent-tombstone precedent). Full contract: `docs/PHASE3_CHILD_LIFECYCLE.md`.
**Tests:** `tests/test_child_lifecycle.py` (27 passed) and `tests/test_child_lifecycle_retention_and_broker.py` (15 passed) cover normal/duplicate/lost-ACK/already-revoked/remote-missing/mismatch/outage/retry-exhaustion revoke, ordering-guaranteed free, generation-incrementing/idempotent/lost-ACK/cross-account rebind, concurrency (simultaneous revoke, simultaneous rebind, stale lease reclaim), retention eligibility and typed-broker payload rejection. Full regression: `673 passed, 3 skipped`. The real isolated Marzban 0.8.4 gate (`scripts/verify_ph3_05_lifecycle_staging.py`, same immutable digest as every other PH3-0x gate) passed all 18 checks against a real disposable Marzban instance: real disable+UUID-rotation confirmed by authoritative reread, idempotent duplicate revoke with zero re-rotation, free gated on confirmed revoke, rebind producing exactly one new remote child via the unmodified PH3-03 pipeline while the old child stayed disabled throughout, a real outage failing closed, and zero raw credentials in the MGBoost DB dump.
**Pre-canary broker authorization binding preflight (2026-08-25):** before any production mutation, the typed `child.user.revoke` broker operation's authorization model was reviewed. Verdict: PASS. The broker itself is intentionally stateless (no MGBoost SQLite access, matching the PH1-05 isolation boundary) and only self-verifies `operation_id == derive_lifecycle_operation_id(child_username, "REVOKE")` plus a live remote-state check -- exactly like `child.user.ensure`/`observe` already do. The real account/slot/generation/child binding lives one layer up: `process_revoke`/`process_rebind` never construct a revoke payload from free-form input, only from `ChildLifecycleStore.claim()`'s DB-verified, non-terminal row. `tests/test_child_lifecycle_authorization_binding.py` (6 passed) proves this: an operation_id that was never prepared cannot be claimed; a different account can never even prepare an operation against another account's child intent; a terminal `APPLIED` operation can never be reclaimed; and a stale/superseded generation's independent revoke attempt converges safely to the broker's own idempotent no-op path without touching the live generation. No code change was required. Full regression at this point: `679 passed, 3 skipped`.
**Owner-approved production canary gate completed 2026-08-25:** device capacity on the existing reviewed `INTERNAL_OWNER_PRIMARY` account (`account_id=1`, `device_limit=10`, only 1 of 10 slots previously used) allowed a throwaway canary without any entitlement change. Three throwaway children were created and fully exercised end-to-end through the real production broker/Marzban/worker, all on slot 2 (server-allocated, never hardcoded), while slot 1/generation 1 -- the existing PH3-03/04 dormant canary (`mgc_sgg6v7t6he43yytsqmkdczzfpa`) and its enabled shadow binding -- were read-verified unchanged at every step and never targeted by any prepare/claim/mutation call. No `user_devices`/`hwid_lock` row was created (the PH3-02+ architecture never touches those legacy tables at all; they stayed 71/71 throughout).
Sequence and real evidence: (1) slot 2/generation 1 child created via the unmodified PH3-03 pipeline (`CREATED`, VLESS-only, exact 25 approved inbound, `xtls-rprx-vision`, active, unlimited); (2) `REVOKE` -- real Marzban reread showed `active -> disabled` and a rotated VLESS UUID; an independent duplicate revoke returned the identical operation, could not reclaim the `APPLIED` lease, and performed zero re-rotation; (3) `FREE` -- refused before the confirmed revoke would have been possible, succeeded immediately after, slot desired/observed state became `FREE`, the old generation became `RELEASED` with an `end_reason`, and a duplicate free was idempotent; (4) a second throwaway child was created on the now-free slot 2 (generation 2, exercising the ordinary claim path); (5) `REBIND` on that generation -- old child revoked-and-verified first (`active -> disabled`, UUID rotated), then `generation 2 -> 3` in one atomic transaction (never `+2`), handed off to the unmodified PH3-03 outbox/worker pipeline which created exactly one new remote child (idempotent `EXISTING` confirmed on immediate repeat); a duplicate rebind request returned the identical operation and could not reclaim it -- no generation 4 was ever created; (6) functional check -- the new (generation 3) credential was retrievable and verifier-matched only through the resolver-only `child.user.credentials.get` capability, while the exact same typed reread of the just-revoked generation-2 credential was denied (`entitlement drift`), proving the old credential is dead through the same approved ephemeral path rather than a bare local flag; a live raw VLESS/Xray protocol handshake was not additionally attempted, matching the evidentiary bar already used by every other PH3-0x gate; (7) final cleanup -- generation 3 was itself revoked and freed through the same standard path, per DL-019/038 leaving no unaccounted-for active test credential; all three generations (1/2/3) on slot 2 remain `RELEASED` tombstones, all corresponding Marzban users remain `disabled` (never physically deleted), and slot 2 itself ended `FREE`.
Security/isolation evidence: a wrong-account (`account_id=999999`) revoke attempt against the throwaway child intent was rejected before any DB row was created; the already-`APPLIED` first revoke operation could not be reclaimed by a fresh worker; the existing canary's child intent (`id=1`) was independently read-verified `ACTIVE`/`ACTIVE` throughout and was never the target of any prepare/claim/mutation call in this gate. Final masked cardinality: parent accounts=1 (unchanged), aliases=3 (unchanged), slots=2 (+1, the throwaway canary's own stable slot), device-slot-generation history=4 rows (slot 1/gen 1 `ACTIVE` unchanged; slot 2/gens 1-3 all `RELEASED` tombstones), child intents=4 (+3, all throwaway ones `REVOKED`), outbox=4 (+3), lifecycle operations=5 (`APPLIED`: REVOKE, FREE, REBIND, REVOKE, FREE) with 10 attempt events, shadow bindings=1/enabled=1 (unchanged), `user_devices`/`hwid_lock`=71/71 (unchanged), Marzban total users=25 legacy + 4 `mgc_` (1 original canary `active`, 3 throwaway `disabled`)=29, VLESS=29/Shadowsocks=0. A valid legacy `/sub` fetch before and after the whole gate returned the same `200`/`no-store`/`active` legacy identity with the legacy UUID present in a functional VLESS config; UUID/token/HWID/expiry/tariff changes caused by this gate: 0. A full DB/journal/nginx privacy scan against all 4 stored child UUID verifiers found zero raw-credential matches anywhere. `PRAGMA quick_check=ok`, zero FK violations; all services stayed healthy throughout; broker remained loopback-only with no nginx route.

## [x] PH3-06 — Internal/god entitlements без username hardcode

**Depends:** PH3-01. **Fixed policy:** OPD-15/DL-032 — versioned internal plans плюс explicit per-account overrides с обязательными expiry/reason; `billing_required=false`, WL unlimited, configurable/unlimited devices. Только primary MGBoost admin может выдать unlimited.
**Never hardcode:** `beykus*`, `megochel*`, `german`, `pensioner`, `client_buy_9`.
**Accept/tests:** special access только plan/entitlement; ordinary/non-primary admin не получает или не выдаёт unlimited flags; override expiry возвращает вычисление к plan/AUTO; source scan hardcodes.
**Migration:** internal accounts — canary cohort и тоже child users.
**Implemented/staging verified 2026-08-24:** checksum-pinned additive review/revision schema plus dormant primary-admin-only repository creates a versioned internal plan and reviewed parent account atomically, rejects ambiguous Telegram ownership before binding, and evaluates configurable/unlimited devices (technical cap 99), billing-free and unlimited-WL semantics without username rules. Every override requires reason, expiry (maximum 90 days), immutable idempotent mutation evidence and returns to plan/AUTO at expiry. No legacy route imports this service; an unset `PRIMARY_MGBOOST_ADMIN_ACTOR_ID` fails every write closed. Focused tests: `8 passed`; full regression `495 passed, 3 skipped`. A disposable online production DB copy applied the migration twice (`true/false`), preserved all 34 pre-existing table digests, left review/revision/account/slot/generation rows at zero, retained 71/71 legacy device/HWID rows and passed quick/FK checks. Read-only candidate evidence and exclusions are recorded in `docs/PHASE3_INTERNAL_ENTITLEMENTS.md`; zero production accounts are automatically provisioned.
**Production completed 2026-08-24:** verified encrypted backup preceded exact commit `eb6dd37`; only `mgboost-panel` restarted and the additive migration marker/quick/FK checks passed. Production leaves primary-admin actor unset, so every dormant provisioning/override write fails closed. Accounts, plans, internal reviews/revisions, slots and generations remain 0. Exact masked pre/post state matches for 25 Marzban users/configs and 71 legacy device/HWID rows. Admin/LK, invalid subscription, Stars durable state, signed Filin, broker, Telegram proxy, nginx/systemd and token-safe journal/access logs passed. UUID, legacy token/URL, HWID binding, expiry, tariff, parent account, slot/generation, child user, forced reconfiguration and unexpected config changes: 0.
**PH3-03 prerequisite amendment, not production deployed:** writes now require a sealed capability derived from an authenticated server-side allowlisted admin login; a caller/frontend actor string cannot authorize. DL-045 fixes stable actor `owner:mgboost-primary:v1` and Telegram identity `905302972`, but both production config values remain intentionally unset until the canary gate. Reviewed creation now supports immutable one-parent/many-alias evidence instead of one review username being the only mapping.

## [x] PH3-07 — Privacy-safe HWID/client compatibility telemetry

**Depends:** PH1-06. **Scope:** aggregate client/version/HWID-present без raw token/full HWID; retention/access control.
**Accept:** supported/unsupported client share известна до fail-closed. **Tests:** redaction canary, aggregation, retention.
**Rollout:** observe-only -> decision -> staged enforce.
**Implemented/staging verified 2026-08-24:** additive daily subject/rollup schema records only bounded client/version/platform, three compatibility categories, monotonic counts and a subscription-scoped HMAC-SHA256 correlation verifier under a dedicated non-DB key. No username, raw bearer, UUID, IP, full HWID/UA/header or device name enters telemetry. Independent short-timeout `BEGIN IMMEDIATE` writes are fail-open, bounded by daily row caps and protected across workers; detailed pseudonyms retain 30 days and identifier-free rollups 60 days, with opportunistic plus daily systemd cleanup. There is no HTTP endpoint. Focused tests `42 passed`; full regression `487 passed, 3 skipped`. A fresh production DB copy applied the migration idempotently (`true/false`), preserved all 32 pre-existing non-marker table digests, kept legacy device/HWID 71/71 and parent/slot/generation 0/0/0, passed quick/FK checks and persisted zero raw canaries. Contract/runbook: `docs/PHASE3_COMPATIBILITY_TELEMETRY.md`.
**Production completed 2026-08-24:** verified encrypted backup preceded the exact implementation pull; a dedicated 64-character CSPRNG key was added to the protected service environment without disclosure, and only `mgboost-panel` restarted. The hardened daily cleanup timer is enabled. Its first immediate start raced the `Type=simple` application migration and safely exposed a missing-table startup assumption; main remained healthy, no user flow changed, and `d69ee39` made pre-schema/rollback cleanup a tested no-op before the timer gate passed. Exact masked pre/post digests match for 25/25 Marzban identities/configs and 71/71 legacy device/HWID rows; parent/slot/generation remain 0/0/0. Valid legacy `/sub` returned 200 with functional config, and admin/LK/Stars/Filin/broker/Telegram/nginx/systemd/SQLite/token-safe logs passed. UUID, legacy token/URL, HWID binding, expiry, tariff, parent account, slot/generation, child user, forced reconfiguration and unexpected config changes: 0.
**Initial compatibility evidence:** the first eight-minute live window has only six supported requests (five organic and one controlled historical-header replay): Happ 2.7.0/Windows and 3.26.3/Android, Incy 2.5.2/iOS, v2rayTun 2.4.7/iOS and 5.25.81/Android. Its 100% supported rate with zero missing/malformed is not an enforcement decision. The pre-existing deduplicated history contains 204 observations: 115 supported (56.37%) and 89 missing (43.63%), with no malformed candidate. Missing-HWID families include Streisand, HiddifyNext, v2rayN, Throne and Exclave plus unknown clients. Historical rows are not request-rate telemetry and include known gate/tool traffic. PH3-04 therefore remains blocked on a representative live observation window and client compatibility/recovery plan. Missing/malformed HWID remains permissive. PH3-03 was not started.
**Latest observe-only snapshot after PH3-09 gate:** 9 live requests: 8 supported (88.89%) and one missing HWID (11.11%, HiddifyNext 2.5.7/Windows). Supported observations remain Happ 2.7.0/Windows and 3.26.3/Android, Incy 2.5.2/iOS, v2rayTun 2.4.7/iOS and 5.25.81/Android. The sample is still too small and biased for fail-closed; PH3-04 remains blocked.

## [x] PH3-08 — Parent expiry/status -> all children

**Depends:** PH3-01/03, outbox interface. **Scope:** idempotent child synchronization; expiry change не сбрасывает WL period.
**Accept/tests:** 1/3/6/12 children converge; partial failure visible; concurrent Stars/admin updates.
**Rollback:** reconcile from durable desired state.
**Implemented/staging verified 2026-08-25:** additive dormant `src/parent_sync.py`/`src/parent_sync_schema.py` compute a canonical parent desired state (`ACTIVE`/`DISABLED`/`EXPIRED`/`UNLIMITED`, a pure function of only real `mgboost_accounts.status`/`mgboost_subscriptions.status`/`current_expiry`) into PH3-01's existing but previously-unused `mgboost_entitlement_state(revision)` table, then fans it out through a durable per-child outbox mirroring PH3-03/05's exact prepare/claim/acknowledge/lease pattern. Reversible suspend, not PH3-05 revoke: the new typed `child.user.state.sync` broker operation only ever PUTs `{status}` or `{status,expire}` -- never `proxies` -- and asserts the UUID it rereads after the mutation is unchanged (a hard `RuntimeError` STOP otherwise); an `EXPIRED`/`DISABLED` parent disables current children without touching `expire`, and a renewed/re-enabled parent reactivates the *same* generation/child/UUID with no new provisioning. Only current, non-terminal generations participate (`slot_generation.status='ACTIVE' AND desired_state!='REVOKED'`, structural, so a renewal can never resurrect a PH3-05-revoked device). Stale-operation protection is revision-stamped: every sync op is derived from `(child_username, parent_revision)`, and `claim()` re-checks the stamped revision against the live one immediately before dispatch, marking a superseded op `SUPERSEDED` without ever calling the broker -- proven for both a stale ENABLE surviving a DISABLE and a stale DISABLE surviving a renewal. `aggregate_state()` reports `IN_SYNC`/`PENDING`/`PARTIAL`/`MANUAL_REVIEW` across an account's current children. Full contract, and a genuine cross-module bug this work found and fixed (PH3-05's revoke idempotency check used to treat any `status=disabled` child, including a PH3-08-suspended one, as already-revoked and skip re-mutation -- fixed by keying that short-circuit off the UUID verifier instead of bare status): `docs/PHASE3_PARENT_SYNC.md`.
**Tests:** `tests/test_parent_sync.py` (26 passed) covers pure policy (active/exact-boundary/expired/disabled/unlimited/pending), revision discipline, end-to-end active/expire/renew cycles with UUID-stability assertions, already-in-sync idempotency, multi-child partial convergence + aggregate state, cross-account isolation, revoked-generation exclusion, the PH3-05-revoke-after-PH3-08-suspend fix, rebind's new generation converging to current (not stale) parent state, both stale-enable and stale-disable race protections, and broker-level verifier/remote-missing/malformed-id/no-expire-on-disable checks. Full regression: `704 passed, 3 skipped`. The real isolated Marzban 0.8.4 gate (`scripts/verify_ph3_08_parent_sync_staging.py`, same immutable digest as every other PH3-0x gate) created a synthetic parent with 3 real children and passed all 16 checks: active-with-correct-expiry, expired-disables-without-rotation, one child PH3-05-revoked then a renewal that reactivates only the other two with unchanged UUIDs/generations/zero new provisioning while the revoked child never resurrects, a stale enable never dispatched, worker-restart/lost-ACK convergence, a Marzban outage failing closed, and zero raw credentials in the MGBoost DB dump. This run also confirmed real Marzban 0.8.4 derives effective status from `expire` vs. its own wall clock regardless of the requested `status` field -- `child_target_for` already always uses the parent's real wall-clock-relative `current_expiry`, so this is a non-issue for this module, but is recorded as an integration note for any future caller.
**Production dormant deploy 2026-08-25:** additive schema only, `mgboost-panel` restarted; `PRAGMA quick_check=ok`, 0 FK violations, all pre-existing cardinality unchanged, existing `account_id=1`/slot 1/shadow binding read-verified unchanged. A read-only dry-run correctly computed account 1's desired state (`UNLIMITED`) and targeted only its one live child, excluding all 3 terminal slot-2 tombstones -- proving `IN_SYNC`-equivalence without any dispatch.
**Owner-approved production reversible-transition canary completed 2026-08-25:** the owner explicitly authorized creating exactly one throwaway production parent for this closing gate, since `account_id=1` cannot itself be safely cycled through ACTIVE->EXPIRED->RENEWED without touching its real PH3-03 child. `mgboost-marzban-broker` was restarted (the only remaining minimal-necessary restart -- it had been running pre-PH3-08 code and returned HTTP 404 for the new operation on the first attempt, which failed closed exactly as designed rather than silently succeeding).
Throwaway parent `account_id=2` was created entirely through the real `InternalEntitlementStore.create_internal_plan`/`create_reviewed_account` repository methods -- no raw SQL, no fabricated payment: `payment_channel=NOT_APPLICABLE`, `mutation_source=INTERNAL`, `operation=INTERNAL_ACCOUNT_CREATE`, identical provenance enums to the existing `account_id=1`, `ownership_evidence=ABSENT` (no Telegram identity created). Its subscription was created directly with a finite `current_expiry` (`legacy_expiry` parameter, already supported by the existing method -- no schema change needed), giving an honest finite ACTIVE parent. One slot was claimed through the unmodified PH3-02 allocator (server-picked `slot_number=1`) and one child through the unmodified PH3-03 pipeline; because `CHILD_WORKER_ALLOWED_OPERATION_IDS` scopes the always-on worker to only the pre-existing canary operation, the same claim/dispatch/acknowledge sequence the worker itself runs was driven manually against the real broker/Marzban, exactly as every prior PH3-0x production canary did. `InternalEntitlementStore` has no exposed method to mutate an existing subscription's status/expiry (PH3-08 deliberately implements no billing/renewal engine), so each of the two entitlement transitions below used the same low-level mechanism already proven and accepted as evidence in the real isolated Marzban 0.8.4 gate (a direct `UPDATE` of only `mgboost_subscriptions.current_expiry`), paired every time with an honest `NOT_APPLICABLE`/`ADMIN` audit row in `mgboost_entitlement_mutations` (existing enums only) so the change stays inside the project's own PH3-09 provenance model; all PH3-08 business logic (revision bump, per-child sync, broker dispatch, UUID-stability enforcement) ran entirely through the real `ParentSyncStore`/broker code, untouched by that raw SQL.
Sequence and real evidence, masked child `mgc_hvc24kxaysfstgxcz7vef5lmvq`: (1) child created `active`, `entitlement_state.revision=1`/`desired_status=ACTIVE`; the very first `run_account_sync_cycle` call converged to `ALREADY_IN_SYNC` -- PH3-03 had already initialized the child to match the ACTIVE parent, proving that integration without PH3-08 needing to mutate anything; UUID verifier baseline recorded (masked). (2) `current_expiry` moved into the past -> `revision=2`/`EXPIRED` -> real `child.user.state.sync` dispatch -> remote `status=disabled`, `expire` untouched, **UUID verifier identical to the ACTIVE baseline** (suspend, not revoke) -- `desired_state`/`observed_state=DISABLED`, same generation, same child intent. (3) an idempotent re-run with no parent change made zero further remote calls (`claim()` found the op already `APPLIED` and stopped before ever touching the broker) -- child/UUID/generation unchanged. (4) `current_expiry` moved back into the future -> `revision=3`/`ACTIVE` -> real dispatch -> remote `status=active` with the new expiry, **UUID verifier still identical to the original ACTIVE baseline** -- same `child_username`, same child intent, same generation, zero new outbox rows, zero PH3-05 lifecycle operations: a full real production `ACTIVE(UUID A) -> EXPIRED(UUID A) -> RENEWED(UUID A)` cycle. (5) functional check: the renewed credential was retrievable only via the resolver-only `child.user.credentials.get` capability with a matching verifier; `mgboost-main` was confirmed still denied that operation. (6) regression proof for the cross-module fix: the child was suspended a second time (`revision=4`/`EXPIRED`, same UUID again), then a real PH3-05 `REVOKE` was run against it -- the remote UUID verifier **did** change this time (confirmed via authoritative reread), proving the fix: a real revoke against a merely-PH3-08-suspended child still actually invalidates the credential rather than short-circuiting as `ALREADY_REVOKED`. (7) cleanup: `FREE` succeeded (ordering-gated on the confirmed revoke, matching every other PH3-05 gate), leaving a `RELEASED` tombstone and a `FREE` slot -- no physical delete; the throwaway parent was then set `DISABLED` (existing enum, own honest `NOT_APPLICABLE`/`ADMIN` audit row, no Telegram identity, no account deletion) and has zero remaining live generations.
Isolation evidence throughout: `account_id=1` was read-verified byte-identical at every checkpoint (`status=ACTIVE`, subscription `UNLIMITED`/`current_expiry=NULL`, child `desired_state`/`observed_state=ACTIVE`, UUID verifier unchanged, shadow binding `enabled=1`); its one `mgboost_parent_sync_operations` row from the prior dry-run stayed `PENDING` throughout this entire canary -- never claimed, never dispatched, zero broker calls ever made against account 1's child. All 3 slot-2 PH3-05 tombstones (`RELEASED`, generations 1-3) were read-verified unchanged and were never selected by any `enqueue_current_children` call (structural exclusion, not convention). Final masked cardinality delta, entirely attributable to the one throwaway parent: accounts 1->2, aliases 3->4, slots 2->3, generations 4->5 (the new one `RELEASED`), child intents 4->5 (the new one `REVOKED`), outbox 4->5, lifecycle operations 5->7 (+REVOKE/+FREE), PH3-08 sync operations 1->5 (+4, one per real revision transition), `mgboost_entitlement_mutations` +5 (create/expire/renew/resuspend/shutdown, all `NOT_APPLICABLE`/`ADMIN`/`INTERNAL`). Shadow bindings, `user_devices`/`hwid_lock`, and every pre-existing row are unchanged. A DB-wide scan for raw-VLESS-UUID-shaped values found zero matches inside any PH3-01/03/05/08 table (the 46 UUID-shaped matches found elsewhere in the DB are pre-existing, unrelated legacy `sub_requests.device_id`/`fingerprint` client-supplied request-correlation values, not PH3-08 credentials); zero raw UUID appeared in the panel/broker/worker journals. `PRAGMA quick_check=ok`, 0 FK violations, all services healthy throughout. A valid-token legacy `/sub` fetch was not independently reproduced this session (the raw production token was not available to this session and is never derived/stored by this task) -- treated as non-blocking per structural isolation: PH3-08 is imported by no route, `/sub`'s own code path is untouched, and prior sessions already hold valid `/sub` evidence pre-PH3-08.
**Verdict: `[x]`.** Every completion criterion in the brief is met: dormant implementation, focused/regression/isolated-Marzban evidence, dormant deploy, read-only dry-run, and now a real owner-authorized production `ACTIVE->EXPIRED->RENEWED` transition with UUID stability proven at every step, terminal-generation exclusion proven, cross-account isolation proven, and the PH3-05-revoke-after-PH3-08-suspend fix proven live.

## [x] PH3-09 — Account/payment/mutation provenance model

**Depends:** PH3-01, PH0-08.
**Model:** account ownership/source остаётся `DIRECT` или `INTERNAL`; payment channel отдельно хранит `TELEGRAM_STARS`, `EXTERNAL_PAYMENT`, `ADMIN_GRANT`; mutation source включает `MANUAL_PAYMENT`. VPN account/credentials принадлежат end user, MGBoost остаётся authority children/UUID/devices/enforcement.
**Constraints:** payment record связан с account/entitlement/admin actor, но не меняет ownership; never infer channel from username/prefix.
**Accept:** WL/device limits всегда принадлежат end subscription независимо от payment channel; child creation идёт через один slot engine.
**Tests:** Stars/external/admin provenance, cross-account IDOR, duplicate reference, direct renewal without account replacement.
**Migration/rollback:** channel backfill только по evidence; иначе `UNKNOWN_LEGACY`, не выдумывать external payment.
**Implemented/staging verified 2026-08-24:** additive immutable payment records and account-scoped payment↔mutation links complement the PH3-01 mutation ledger. The typed repository requires explicit account source/channel/mutation source, validates channel/source pairs, stores hash-only idempotency identities plus canonical request hashes, returns the original record for an identical retry, and rejects changed payloads, duplicate external refs and cross-account payment/subscription links. It has no username/note input and performs no inference. No legacy Stars/Filin/admin flow imports it; it cannot change entitlement/expiry or call Marzban. Focused tests: `9 passed`; full regression with PH3-06: `504 passed, 3 skipped`. A fresh production DB copy applied the migration twice (`true/false`), preserved all 36 previous table digests, left both provenance tables empty, kept account/review/slot/generation 0 and legacy device/HWID 71/71, and passed quick/FK checks. Contract and remaining outbox/broker work: `docs/PHASE3_PROVENANCE.md`.
**Production completed 2026-08-24:** encrypted backup preceded exact commit `08397f3`; only `mgboost-panel` restarted. Migration marker/quick/FK passed and payment/link/account/slot rows remain zero. Exact masked state matches for 25 Marzban users/configs and 71 legacy device/HWID rows. Admin/LK, invalid subscription, signed Filin, broker, Telegram proxy, nginx/systemd and token-safe journal/access logs passed. UUID, legacy token/URL, HWID binding, expiry, tariff, account, slot, child, forced reconfiguration and unexpected config changes: 0. PH3-03 was not activated.

# Phase 4 — Legacy migration

## [x] PH4-01 — Legacy subscription alias bridge

**Depends:** PH2-01/07, PH3-01–05.
**Flow:** legacy account -> supported HWID -> find/assign slot -> lazy child -> child config; migrated device не получает shared UUID.
**Accept/tests:** valid/invalid/revoked legacy; missing HWID/full slots/repeat request.
**Rollback:** shared legacy credential остаётся до explicit revoke; no duplicate children.
**Owner-approved dependency waiver 2026-08-25:** PH2-07 is `[x]`; PH2-01's own only remaining criterion was this exact legacy alias bridge (circular by construction) -- the owner explicitly confirmed this is no longer a blocker for starting PH4-01.
**Implemented/staging verified 2026-08-25:** additive dormant `src/legacy_bridge_schema.py`/`src/legacy_bridge.py` add one explicit, root-only, per-account staged-rollout gate (`mgboost_legacy_bridge_bindings`, mirroring PH3-03's shadow-resolver-binding pattern exactly) -- no account is ever bridged without a matching `enabled=1` row created ahead of time by the primary admin; production ships with zero such rows. `src/opaque_resolver.py` was refactored (no behavior change, full PH2-01 regression still green) to expose a shared `resolve_account_device()` tail; PH4-01's new `src/legacy_bridge_resolver.py` calls it after its own, entirely independent, deterministic legacy-username resolution (`LegacyBridgeStore.resolve_account_for_legacy_username`, exact match against an already-reviewed immutable alias AND an explicit enabled binding -- never inferred from username shape, HWID, Telegram ID or possession). This guarantees PH2-01 and PH4-01 can never drift into two different security postures for the identical downstream decision (PH3-08 parent state -> PH3-04 HWID gate -> PH3-02 slot -> PH3-03 lazy child -> typed subscription fetch). `src/routes/sub.py` gained one minimal hook (`_try_legacy_bridge`, called only when the new `LEGACY_BRIDGE_ENABLED` flag -- default off -- is true) right before the existing legacy response is built; with the flag off, byte-identical to pre-PH4-01 behavior (proven, not assumed). Per-device, not per-user: every deny decision (missing/malformed HWID, unsupported client, full slots, cross-account HWID, expired/disabled parent) happens strictly before any durable slot claim, so those cases fall through to the *exact unmodified* legacy response -- the device keeps working normally, never denied outright. Only once a slot has actually been durably claimed (an ALLOW decision) does a later failure fail closed (`502`, never the legacy shared credential) -- codified as `is_fall_through_outcome()`, a pure classification with no ambiguity between the two cases. Full contract: `docs/PHASE4_LEGACY_BRIDGE.md`.
**Known, documented scope limit (shared with PH2-01):** the bridge only adds *additional* devices to an account that already has at least one child, provisioned through the existing PH3-03 pipeline ahead of the bridge going live for that account -- it does not itself discover/verify a brand-new source template for an account's very first device. This is a deliberate non-invention of a second, redundant "find the legacy user and validate its shape" mechanism, not a missing feature.
**Tests:** `tests/test_legacy_bridge.py` (8 passed: no-binding/disabled-binding non-resolution, enabled binding resolves deterministically, enable/disable toggle, duplicate-binding conflict, cross-account alias rejection, primary-admin-capability requirement, throwaway-account isolation from a real account's resolution), `tests/test_legacy_bridge_resolver.py` (11 passed: unmapped/disabled fall-through, bridged OK, per-device full-capacity/missing-HWID fall-through -- never a hard deny, expired-parent fall-through, provisioning failure *after* a durable slot claim is fail-closed not fall-through, idempotent repeat, second device gets its own child, shared legacy UUID absent from the bridged body, zero raw-HWID leakage), `tests/test_legacy_bridge_route.py` (3 passed: flag-off byte-identical legacy response, flag-on-no-binding still byte-identical, flag-on-bridged-account response contains no shared legacy UUID). Full regression: `771 passed, 3 skipped`. The real isolated Marzban 0.8.4 gate (`scripts/verify_ph4_01_legacy_bridge_staging.py`, same immutable digest as every other PH3-0x/PH2-01 gate) passed all 12 checks: legacy remains authoritative before any binding exists, an explicit binding plus a supported HWID bridges to a real PH3-03 child whose body omits the shared legacy UUID while the legacy remote user itself stays untouched/active throughout, a repeat converges on the same child, a second distinct HWID gets its own second child, missing-HWID/full-capacity/unmapped-username all fall through to the unmodified legacy response, and zero raw credentials appeared in the MGBoost DB dump.
**Production dormant deploy 2026-08-25:** see the cardinality/evidence block below. `LEGACY_BRIDGE_ENABLED` remains unset (off) in production; zero `mgboost_legacy_bridge_bindings` rows exist -- no real account/device was ever bridged.
**Valid legacy `/sub` proof completed 2026-08-25:** the raw legacy subscription token was obtained through an already-existing authorized runtime mechanism -- the production broker's own typed `legacy.user.get` read (the same admin capability every other legacy operation in this project already uses; no new endpoint, no new broker allowlist entry, no log/backup/quarantine extraction). A single root-only, 0600, immediately-deleted-after-use script read the legacy user's `subscription_url` transiently in-process, made one real `GET https://sub.beykus.fun/sub/{token}` request through the normal production nginx path, and printed only derived/masked evidence -- the raw token and raw UUID were never written to any file, stdout or log by this session. Result: `HTTP 200`, `Cache-Control: no-store`, `Referrer-Policy: no-referrer`, `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`; legacy `status=active`, `expire=null` (unlimited), identical to the immediately-preceding masked pre-snapshot; 33 VLESS configs (25 own inbounds plus existing per-user/extra entries), 0 Shadowsocks; the shared legacy UUID verifier appeared in the response body and matched the pre-snapshot verifier exactly; a second, unrelated UUID verifier present in the body was cross-checked against all 5 `mgboost_child_user_intents` verifiers and matched none (no child credential mixed in); an immediate second admin read confirmed the remote UUID verifier was still identical post-request. Bridge-dormant proof: `LEGACY_BRIDGE_ENABLED=False`, `OPAQUE_SUBSCRIPTION_ENABLED=False`, `PH3_04_ENFORCEMENT_MODE=OFF` and zero `mgboost_legacy_bridge_bindings` rows, all unchanged before/after; full masked cardinality (accounts=2, aliases=4, slots=3, generations=5, child_intents=5, telegram_identities=1) unchanged; account 1's Telegram owner and account 2's `DISABLED` status unchanged; `PRAGMA quick_check=ok`, 0 FK violations; all three services stayed active throughout. Leakage: the application journal and nginx `error.log` contain no trace of the request; nginx's pre-existing `mgboost-sensitive-access.log` (a location-specific access log that has always captured every `/sub/` request's URI, unrelated to and not introduced by PH4-01) necessarily also recorded this one request's URI like every real subscriber's -- its current file permissions were found to be world-readable (`644`) rather than root-only, a **pre-existing condition predating this session**, noted as a residual hardening item, not fixed here (out of this gate's scope). The temporary read script was deleted immediately after use and its absence verified.
**Verdict: `[x]`.** Every remaining ROADMAP acceptance criterion is now satisfied: implementation, focused/full regression, real isolated Marzban 0.8.4 destructive evidence, dormant production deploy with zero real bridge activation, and now a genuine, safely-obtained valid production legacy `/sub` pre/post proof. No migration, no bridge activation and no UUID/token change occurred.

## [x] PH4-02 — Durable migration state machine

**Depends:** PH4-01. **States:** `LEGACY`, `MIGRATING`, `MIGRATED`, `LEGACY_REVOKE_PENDING`, `LEGACY_REVOKED`, `ERROR_RECONCILE`.
**Accept/tests:** durable/idempotent transitions; duplicate/crash boundary tests.
**Rollback:** после revoke backward transition запрещён; recovery выдаёт new credential.
**Implemented 2026-08-25:** additive dormant `src/migration_lifecycle_schema.py`/`src/migration_lifecycle.py`. One durable row per (account_id, hwid_verifier) migration lineage in `mgboost_migration_bindings` (append-only `mgboost_migration_binding_events`) -- `LEGACY` is the implicit absence of a row (mirrors PH4-01's own "no binding = fall through" pattern); a row is created only once `resolve_legacy_bridge()` has already returned a non-fall-through outcome, i.e. only after PH3-02's `hwid_gate.evaluate()` has durably claimed a slot. Stores only non-secret identity: account_id, legacy_alias_id, hwid_verifier (the same keyed HMAC PH3-02 already uses), slot_generation_id/child_intent_id (references, never a raw child UUID) -- never a raw legacy token, opaque token, child UUID or HWID. Explicit transition allowlist (`_ALLOWED_TRANSITIONS` dict) plus an optimistic-concurrency `revision` CAS on every mutation (a stale writer/request is rejected, never silently overwritten) plus a DB trigger making `LEGACY_REVOKED` terminal independent of application logic. `process_migration_bridge_request()` in `src/migration_lifecycle.py` is a thin wrapper around the unmodified `legacy_bridge_resolver.resolve_legacy_bridge()` -- no second resolver was written; it adds only the durable lifecycle record on top. A downstream failure after a durable MIGRATING commitment never falls back to the shared legacy credential (`is_fall_through_outcome()` reused verbatim from PH4-01); an unambiguous recoverable failure (`PROVISIONING_PENDING`/`PROVISIONING_UNAVAILABLE`) stays `MIGRATING` and retries; a genuinely ambiguous single-signal failure (`INTERNAL_ERROR`) goes to `ERROR_RECONCILE` instead of a blind retry. `reconcile_binding()` compares the durable desired state against the authoritative `mgboost_device_slot_generations`/`mgboost_child_user_intents` rows (never a single signal) and safely classifies: already-applied/lost-ACK -> `MIGRATED`; not yet provisioned -> retry `MIGRATING`; anchored slot generation no longer `ACTIVE` (superseded by a PH3-05 rebind) -> stays `ERROR_RECONCILE` for manual review, never blindly reassigned. `MIGRATED -> LEGACY_REVOKE_PENDING -> LEGACY_REVOKED` is implemented and admin-capability-gated (`PrimaryAdminAuthority`, same sealed-capability pattern as PH2-05/PH4-01) but no production code path invokes it -- dormant, isolated-test/gate-only, per this task's explicit boundary (PH4-06 owns the real production legacy revoke).
**Tests:** `tests/test_migration_lifecycle.py` (22 passed): idempotent prepare/one-lineage-per-device, idempotency-key-reuse conflict, illegal-transition rejection, stale-revision rejection, `LEGACY_REVOKED` terminal immutability (both at the store layer and via the DB trigger directly), primary-admin-capability requirement for revoke-pending, real crash-boundary fault injection (connection close + fresh `Database()` reopen, same on-disk file, mirroring `tests/test_ownership_rebind_compromise_crash.py`'s proven methodology) at "binding created before slot recorded" and "slot recorded before child recorded", duplicate-operation-id convergence to one lineage, ambiguous-failure/lost-ACK reconciliation converging to `MIGRATED` without a second child, stale-slot-generation reconciliation staying `ERROR_RECONCILE`, two concurrent requests for the same device converging on one lineage/one child, two devices on one account getting independent lineages, fail-closed-not-fallback after a durable commitment, zero binding created on any fall-through outcome (unmapped username, missing HWID), full end-to-end `MIGRATING -> MIGRATED` with an immutable ordered event trail, idempotent repeat after `MIGRATED`, the full `MIGRATED -> LEGACY_REVOKE_PENDING -> LEGACY_REVOKED` lifecycle with post-revoke child continuity, cross-account HWID isolation, zero raw-HWID storage. Relevant PH2-01/PH2-05/PH3-02/03/05/08/09/PH4-01 suites plus full regression: `820 passed, 3 skipped` (up from 771 pre-PH4-02, zero regressions).
**Real isolated Marzban 0.8.4 gate 2026-08-25:** `scripts/verify_ph4_02_migration_lifecycle_staging.py`, same immutable digest as every other PH3-0x/PH4-01 gate, `--network host` (this image binds uvicorn to loopback-only without TLS, so bridged Docker networking cannot reach it -- host networking is required for isolated-loopback gates on this image). All 23 checks PASS across two independent synthetic accounts: (A) `LEGACY -> MIGRATING -> MIGRATED` via `process_migration_bridge_request` with a lazy PH3-03 child, working config, absent shared legacy UUID in the migrated body, legacy remote user untouched/active throughout, idempotent repeat with no duplicate lineage, and a real crash/lost-ACK convergence proof (subscription fetch raised mid-attempt, durable state inspected via a freshly reopened `Database()` against the same on-disk file confirmed `MIGRATING`, retry converged to `MIGRATED` with exactly one child, no duplicate); (B) on a separate disposable account, `MIGRATED -> LEGACY_REVOKE_PENDING -> LEGACY_REVOKED` with a REAL revoke of the synthetic legacy Marzban user (disabled + UUID rotated), a rollback attempt explicitly refused after `LEGACY_REVOKED`, and the migrated child continuing to resolve correctly afterward. Zero raw credentials appeared in the MGBoost DB dump.
**Production dormant deploy 2026-08-25:** additive-schema-only (`mgboost_migration_bindings`/`mgboost_migration_binding_events`), no route/worker wires `process_migration_bridge_request` into any live path (same dormancy discipline as PH4-01 -- reachable only via direct library/script call). Encrypted backup + restore verification passed; fast-forward-only pull; minimal service restart; `PRAGMA quick_check=ok`; 0 FK violations. Post-deploy: 0 `mgboost_migration_bindings` rows, 0 `mgboost_migration_binding_events` rows; `LEGACY_BRIDGE_ENABLED=False`, `OPAQUE_SUBSCRIPTION_ENABLED=False`, `PH3_04_ENFORCEMENT_MODE=OFF` all unchanged; pre/post masked cardinality (accounts/aliases/slots/generations/child_intents/telegram_identities/bridge_bindings) identical; all services stayed active. Zero real migrations, zero real legacy revokes, zero UUID changes.
**Verdict: `[x]`.** State machine, transitions, crash/concurrency/reconciliation matrix, PH4-01 integration (no second resolver), terminal revoke semantics (isolated-proven), audit trail, focused/full regression, real isolated gate, and dormant production deploy are all complete. No production migration was performed -- that is PH4-03.

## [x] PH4-03 — Internal canary migration (CLOSED 2026-08-26: mass migration completed, real ACTIVE cohort fully technically migrated)

**Depends:** PH3-06/09, PH4-01/02.
**Cohort order:** internal users -> несколько DIRECT/Stars subscriptions -> несколько DIRECT/external-payment subscriptions -> mass migration. Не считать internal-only canary достаточным.
**Accept:** representative clients прошли migrate/device rebind/revoke и admin-only Telegram ownership rebind; account identity, payment provenance и manual renewal flow сохраняются; metrics/support runbook готовы.
**Tests:** real client matrix, cached/direct UUID, Stars/external channel and renewal assertions; Telegram rebind preserves account/devices/history and ordinary flow preserves token/UUID, while compromise flow rotates opaque token. **Rollback:** pause cohort, preserve evidence/new child state.

**Revised product decision (2026-08-26, later than the reopening below):** mass migration is now explicitly a *process that runs inside* the PH4-05 14-day grace-campaign window, not a prerequisite that had to finish before that window could open. All 19 real ACTIVE legacy users (17 grace-cohort accounts) now have their grace clock running; as each registers in Telegram during the window, `bind_telegram_after_registration()` links ownership and the account becomes eligible for the same bridge-enable/genesis-child migration sequence already proven three times (accounts 1, 3, 4) -- not yet executed for the other 14 accounts (see PH4-05's own entry for the exact Day-0 counts). **This does not close PH4-03.** Closing `[x]` still requires either mass migration actually completing (every real ACTIVE account either migrated or an explicitly recorded exception -- expired/excluded/manual-review) or the owner explicitly accepting a smaller final scope with recorded reasons -- starting the clock is not itself completion, and this reopening note is deliberately not being quietly closed out just because a clock now exists. PH4-06 (the actual legacy revoke) stays blocked on real migration/exception completion, not on the clock alone, per DL-023's own Rollback discipline the owner reaffirmed this session.

**Mass migration completed, CLOSED `[x]` (2026-08-26, same day, after a read-only human-review decision packet and explicit owner device-policy/ownership decisions):** a read-only production audit first produced a per-account decision packet for the 4 accounts whose device/ownership state was genuinely ambiguous (device counts above the D3 default, one ambiguous Telegram-ownership case). The owner reviewed it and returned four explicit decisions, applied via existing capability-gated, audited code paths (no raw SQL mutation, nothing bypassing domain logic):

- Account 8 (`client_buy_1`): two real people legitimately sharing one subscription. `2105984481` set as the sole Telegram `OWNER` via a new, deliberately narrow `resolve_ambiguous_telegram_ownership()` (`src/legacy_grace_registration.py`) -- the one explicit exception to "ambiguous ownership is never auto-resolved," gated by the same primary-admin capability as every other consequential PH3/PH4 action, requires the chosen id to already have real `tg_users` evidence (never invents ownership), and is proven by test to make the non-chosen id (`1130407008`) permanently unable to hijack the account through the ordinary automatic bind path (`CONFLICT`, not a silent rebind). Both historical `tg_users` rows preserved, neither deleted. Device policy: D8.
- Account 10 (`German`): D6, using a new `acknowledge_observed_overage=True` parameter on `ensure_legacy_paid_compat_entitlement()` -- the raw observed-device evidence (7) is frozen/immutable and intentionally never edited, so this is a distinct, explicitly-evidenced admin acknowledgment that the reviewed limit is correct despite it, not a change to the evidence itself.
- Account 11 (`Pensioner`, the owner's parents): device-limit exempt. Investigated the existing entitlement architecture first, per instruction, and found `device_limit_mode='UNLIMITED'` already exists as a generic `mgboost_plan_versions` concept but was hard-blocked for any `DIRECT` account by two separate checks in `device_slots.py`. Relaxed exactly one of them (a `DIRECT` plan may now carry `UNLIMITED` mode, but *only* via an immutable, capability-gated plan_version an admin explicitly created -- never self-service, never a catalog/global policy change) and added a new `device_limit_exempt=True` path to `legacy_paid_compat.py` that creates a `LEGACY_PAID_COMPAT_V1_UNLIMITED` plan variant reusing the exact same migration-only-compat contract as every `Dn` variant. No hardcoded username/account id/magic device-count constant anywhere in the implementation -- it is a generic, reusable exemption mechanism.
- Account 13 (`client_buy_7`): D4 (`PAID_BASELINE_LIMITS` extended `{3,6,12}` -> `{3,4,6,8,12}`, anticipated by this exact module's own prior documentation, never a catalog change).

**Mass-migration execution:** a new, reusable, idempotent, capability-gated primitive (`src/legacy_grace_migration.py::migrate_bootstrapped_account()`) packages the exact genesis-child-on-slot-1-then-bridge-enable sequence already proven three times in production (accounts 1, 3, 4) -- fails closed if an account has no entitlement yet, never invents a second resolver, and is proven by test that a real device (not the synthetic genesis placeholder) migrates transparently through the unmodified PH4-01/02 resolver once the bridge is enabled. Run in 4 production batches (3, 3, 4, then the 4 device-policy accounts last, exactly the order requested): `[5,6,7]` -> `[9,12,14]` -> `[15,16,17,18]` -> `[8,10,11,13]`. Every batch verified before the next: `quick_check=ok`, 0 FK violations, all genesis child intents `ACTIVE`, zero duplicate `child_username`, all 4 services active, legacy `/sub` unaffected (bogus-token `404`, readiness `200`), zero raw username/token/UUID/HWID in the journal.

**Final production accounting:** 17/17 real ACTIVE parent accounts technically migrated (genesis child `ACTIVE` + bridge `enabled=1`) -- covering all 19 real ACTIVE legacy Marzban usernames. `mgboost_migration_bindings`: `MIGRATED=9` (unchanged, the real per-device lineages from accounts 1/3/4's own real customer devices that had already connected), `MIGRATING=0`, `ERROR_RECONCILE=0`. Accounts 1/3/4 completely untouched throughout (`quick_check` cardinality, migration state, active device count all identical before/after). **Important, honest nuance for the Day-0/Day-N report:** each of the 14 newly-migrated accounts' genesis child occupies 1 `ACTIVE` device-slot-generation by design (the same synthetic-placeholder pattern accounts 1/3/4 used) -- this is *not* a real customer device and has no `mgboost_migration_bindings` row; a real per-device migration entry appears only once that customer's own client organically reconnects through the now-enabled bridge, exactly as designed (no simulated/forced connection was made on any of the 14 real accounts' behalf). 13 of 19 real users remain `WAITING_FOR_REGISTRATION` (unregistered in Telegram) and 4 are now Telegram-`BOUND` (1, 3, 4 pre-existing + account 8's newly-resolved ownership) -- Telegram onboarding continues as the ongoing campaign inside the still-running grace window, and is explicitly not a blocker for this phase's own closure.

**Verdict: `[x]`.** Every real ACTIVE legacy account is either already fully migrated with real customer devices (1, 3, 4) or has completed the technical migration infrastructure step and is only waiting on the customer's own device to naturally reconnect (the other 14) -- there is no remaining unmigrated, unaccounted-for real ACTIVE account and no open manual-review blocking closure. Telegram registration completion is explicitly out of scope for this phase's closure criterion (it is PH4-05's own ongoing campaign metric, tracked daily via `scripts/ph4_05_daily_cohort_report.py`). PH4-06 (the actual shared-legacy-credential revoke) remains its own separate, unstarted, gated phase.

**Сделано:**
- Real internal-cohort canary on production account 1 (`beykusios`): controlled migrate/revoke/free on a synthetic device slot, legacy user and the account's real live device untouched throughout (see `AGENT_HANDOFF.md` history).
- Reviewed DIRECT enrollment foundation (additive, dormant, no route wiring): `src/direct_enrollment_schema.py` (`mgboost_direct_enrollment_intents`, `mgboost_direct_account_reviews` -- separate from and never touching PH3-06's INTERNAL-only `mgboost_internal_account_reviews`) and `src/direct_enrollment.py` (`DirectEnrollmentStore`). `AccountStore.create_account('DIRECT')` is the only account-creation path used; a durable pre-account-creation intent row makes retries after a crash converge on one account instead of allocating an orphan. Ambiguous ownership fails closed (nothing written); one legacy username can never bind to two accounts (checked in-application and backstopped by the existing DB `UNIQUE` constraint). `TELEGRAM_STARS`: only `paid`/`plan_committed`/`applied` `stars_invoices` become a canonical `mgboost_payment_records` row via the existing `ProvenanceStore`; refused/refunded/manual-review invoices and payer/ownership mismatches are rejected; duplicate invoice recording is idempotent. Minimal admin-only `record_external_payment()` primitive covers `payment_channel='EXTERNAL_PAYMENT'`/`mutation_source='MANUAL_PAYMENT'` as a PH5-09 prerequisite only (PH5-09 itself is not implemented). One orchestration flow (`process_direct_stars_enrollment`) converges to exactly one account/alias/payment across a simulated crash/retry. 16 focused tests (`tests/test_direct_enrollment.py`) plus full regression (`842 passed, 3 skipped`, zero regressions).
- Owner decision (2026-08-26): every real legacy paying user historically paid the owner directly, never Stars, before any canonical ledger existed. Added `mgboost_owner_attested_legacy_payments` (sibling additive table -- `mgboost_payment_records`' CHECK constraints are already checksum-locked by the deployed PH3-09 migration and are never edited in place), recording this fact with no invented amount/date/reference, distinct from a real new `EXTERNAL_PAYMENT` with known details. `enroll_direct_account()` now cross-checks the existing bot subscription-link flow's `tg_users` table (`bot_support.py`, unchanged, no second linking mechanism): a username the bot already linked to more than one distinct Telegram ID is ambiguous, and an asserted Telegram ID contradicting the single bot-recorded one is a conflicting mapping -- both fail closed. 9 more focused tests, full regression clean (`851 passed, 3 skipped`).
- **Real DIRECT/EXTERNAL_PAYMENT cohort enrolled on production** (2 real paying legacy customers, read-only-discovered and owner-authorized: `cohort-2 account #3` account id 3, `cohort-2 account #4` account id 4). Each got: one reviewed DIRECT account via `AccountStore.create_account('DIRECT')`, one reviewed legacy alias (`EVIDENCE_PROVEN`), Telegram owner linked by reusing their existing single unambiguous `tg_users` bot mapping (no new linking mechanism, no duplicate), and one `mgboost_owner_attested_legacy_payments` row per the owner's decision above. Zero invented amount/date/reference. Verified before mutation: current legacy status/expiry/device/HWID evidence unchanged from the read-only discovery session, no conflicting Telegram mapping, no pre-existing DIRECT/alias/payment state. Verified after: `quick_check=ok`, 0 FK violations, real legacy Marzban users completely untouched (still `active`, same `expire`), all 4 services stayed active, `mgboost_legacy_bridge_bindings` unchanged (still only account 1) -- proving these enrollments are additive/dormant with zero effect on live legacy traffic.
- the excluded ambiguous-ownership legacy account was not used anywhere in this work, per explicit instruction.
- **TELEGRAM_STARS production cohort: owner-approved exception, `N/A`.** Zero real successful Stars purchases existed in production at PH4-03 time -- the only two historical `stars_invoices` rows are `refunded` test canaries for the excluded ambiguous-ownership legacy account and are not counted as payment. No artificial Stars purchase was created and no real user was asked to buy Stars for testing. The Stars code path itself remains fully covered by focused tests (paid/refunded/manual-review/payer-mismatch/duplicate-invoice, `tests/test_direct_enrollment.py`) and is unaffected by this exception. The first real successful Stars purchase after launch requires its own real canary gate before any wider Stars rollout -- this is a requirement, not yet satisfied.

- **Migration-only legacy paid compatibility entitlement (owner decision 2026-08-26), closing the prior blocker:** `src/legacy_paid_compat.py::ensure_legacy_paid_compat_entitlement()` -- NOT a commercial catalog entry, no reconstructed historical tariff/price. Historical default device limit for legacy paid subscriptions is `3`, never inferred from current device/HWID/registration counts; an individually owner-approved increase is `3 + approved_extra_device_slots`, requiring explicit evidence. Reuses the existing `mgboost_plan_versions`/`mgboost_subscriptions` tables (no new schema) and the existing commercial `PAID_BASELINE_LIMITS={3,6,12}` capacity contract in `DeviceSlotStore` unchanged. Variants are named/reused by exact resulting limit (`LEGACY_PAID_COMPAT_V1_D3`/`_D4`/`_D6`, only created as actually needed -- `_D3` is the only one in production use so far). Exact legacy expiry preserved (never extended/shortened/rounded); `wl_mode='UNLIMITED'` with no quota bytes (legacy paid users never had a WL quota); a terminal legacy state (`DISABLED`/`EXPIRED`) is preserved as-is, never promoted to a fresh paid period; occupied-device evidence exceeding the derived limit fails closed. Guards: requires an already-reviewed DIRECT account and existing owner-attested legacy payment evidence; idempotent retry; conflicting subscription rejected; one live subscription per account (existing partial-unique-index contract). 18 focused tests (`tests/test_legacy_paid_compat.py`, including a full reviewed-enrollment -> attestation -> compat entitlement -> PH4-02 migration -> child integration test) plus full regression (`869 passed, 3 skipped`, zero regressions). Deployed additive (no schema change, pure application code), backup+restore-verified beforehand.
- **Real DIRECT/EXTERNAL_PAYMENT device-limit evidence re-verified fresh before assignment:** both cohort-2 accounts still `active` in Marzban with unchanged expiry, still exactly one unambiguous Telegram mapping each, no evidence anywhere (notes, tickets, audit log, node filters, per-user configs) of an individually approved device-limit increase for either -- both assigned the historical default `LEGACY_PAID_COMPAT_V1_D3` (matches their own observed ~2-device usage with headroom, so D3 does not constrain their current real usage).
- **Real DIRECT/EXTERNAL_PAYMENT migration canary PASS on both accounts, in order (A then B):** for each account, an initial genesis child was first provisioned directly through the existing PH3-03 pipeline against the real production broker (the resolver never invents an account's first child; this bootstrap step happened entirely before any bridge binding existed, so the real customer's own device was never exposed to a missing-subscription/missing-source gap), then the bridge binding was created+enabled, then a synthetic canary device was migrated end-to-end through the unmodified `process_migration_bridge_request` (`LEGACY -> MIGRATING -> MIGRATED`, real new child, real working subscription body, no shared legacy UUID), then PH3-05 REVOKE (real disable, confirmed `REVOKED`), then a same-device retry confirmed no resurrection and no legacy fallback (`PROVISIONING_PENDING`, not `OK`, not a fall-through outcome), then PH3-05 FREE (slot returned to `FREE`). On account A, an additional PH3-05 REBIND proof was run on the same already-freed canary slot (zero real-device impact): generation incremented by exactly one, the new credential resolved and worked end-to-end, the old child stayed `REVOKED` throughout. Verified before and after every step: exact legacy expiry/status/account identity/ownership/payment-attestation evidence unchanged, `mgboost_payment_records` stayed empty (no invented payment), the real legacy Marzban user stayed `active` with its original `expire` and was never modified, `quick_check=ok`, 0 FK violations, all 4 services stayed active throughout. Both real customers' actual daily-use devices were never touched -- only synthetic canary HWIDs on the accounts' spare device slots were migrated/revoked/rebound.
- **Telegram ownership rebind:** relies on the existing focused integration tests (`tests/test_ph4_03_migration_cohort_integration.py`) and PH2-05's own real production mechanism proof (see PH2-05's closure) -- per owner instruction, no real customer's live Telegram identity was mutated solely to check this box.
- **Metrics/support runbook:** `docs/PHASE4_MIGRATION_SUPPORT_RUNBOOK.md` -- how to read an account's migration state/lineage, a compat entitlement's `Dn`/expiry/WL semantics, ownership/payment provenance, how to recognize and react to `ERROR_RECONCILE`, and what to do (and never do) when a canary/migration attempt fails. No secrets/PII; every example is by account id, never a real username/Telegram ID.

**Original verdict (stated `[x]`, 2026-08-26 earlier same day):** Internal cohort PASS; two real DIRECT/`EXTERNAL_PAYMENT` legacy customers reviewed-enrolled, given a migration-only compatibility entitlement (no invented tariff), and fully migrated/revoked (plus one rebind proof) on production with account identity, payment provenance, and legacy expiry/conditions preserved throughout and zero impact on either customer's real device. `TELEGRAM_STARS` cohort is a documented, owner-approved `N/A` exception (zero real purchases ever existed) with its own code path fully test-proven and a standing requirement that the first real future purchase gets its own canary before wider rollout. Telegram ownership rebind relies on existing tests/PH2-05 production evidence rather than an unnecessary live mutation. Metrics/support runbook exists. PH4-08 (full legacy-subscription-preservation flow, renewal UI) and PH5-09 (manual-payment-driven renewal) remain their own future phases, both still `[ ]` -- this phase only needed and built the minimal migration-compatibility prerequisite. **This canary evidence itself is not retracted -- the internal-cohort proof and both real DIRECT/`EXTERNAL_PAYMENT` migrations genuinely happened and genuinely passed.**

**REOPENED (2026-08-26, later same day): the phase's own "mass migration" cohort step was never reached, and the original closure did not say so explicitly.** This phase's own contract (line above: "Cohort order: internal -> несколько DIRECT/Stars -> несколько DIRECT/external-payment -> mass migration. Не считать internal-only canary достаточным.") names four cohort stages ending in mass migration. Only stages 1 and 3 (internal x1, DIRECT/`EXTERNAL_PAYMENT` x2) were ever executed; stage 2 (DIRECT/Stars) is a documented `N/A` exception (zero real Stars purchases exist), and mass migration was never started, attempted, or explicitly deferred to a named future phase in the original closing verdict -- it was simply not mentioned. A read-only production audit (no mutation) found:

- **44** total Marzban users. Excluding 18 `mgc_*` child accounts (already-migrated children, not legacy users), 1 explicit PH3-08 throwaway test canary, and 1 generic `testing` user (22 excluded, all previously-known test/system artifacts) leaves **24 real legacy Marzban users** (19 currently `active`, 5 `expired`/lapsed) -- two distinct real naming cohorts: 10 undecorated usernames (the earliest/original users, one of which -- 3 aliases -- is account 1, the owner's own account) and 14 `client*`-prefixed usernames with human-readable `note` fields (real names/labels, organic signup dates spread Feb-Jul 2026) -- this second group was initially miscategorized as synthetic test data during the audit's first pass and was corrected after inspecting `note`/`created_at` evidence; it is real.
- Of these 24 real users, only **5** are represented in `mgboost_legacy_account_aliases` (account 1's 3 aliases + account 3's + account 4's 1 each) -- i.e. only **3 real parent accounts** exist at all (`mgboost_accounts=4`, but account 2 is not a real legacy user -- its sole alias is the exact `ph308_thro*` throwaway PH3-08 canary template, correctly `DISABLED`).
- All 3 real accounts have an `enabled=1` `mgboost_legacy_bridge_bindings` row and a `MIGRATED` `mgboost_migration_bindings` lineage (account 1: 2, account 3: 4, account 4: 3 -- the latter two counts include now-`REVOKED` historical canary/rebind-proof devices from the original PH4-03 session, not all still live). Currently `ACTIVE` device-slot generations across these 3 accounts: 7 (2+2+3).
- **19 real legacy usernames have zero parent-account/alias/migration representation at all.** Of those: **13 active, real (named-note or original-scheme) users have no Telegram-bot linkage whatsoever** (`tg_users` empty for their username) -- PH4-03's own enrollment code requires evidence-based ownership (`enroll_direct_account`, PH2-05's "possession is not ownership" rule) and cannot safely enroll them as-is; **5 are `expired`/lapsed with no Telegram linkage** (lower urgency -- not currently relying on the legacy URL for a working subscription); **1 active user has an *ambiguous* multi-Telegram mapping** (more than one distinct Telegram id already linked to that username) -- this is a distinct case from, not the same as, the one already-known excluded ambiguous-ownership account referenced elsewhere in this history, and needs its own explicit owner review before any enrollment.
- **Zero of the remaining 19 meet the exact "single unambiguous Telegram mapping" bar PH4-03 originally used to select accounts 3/4** -- every user who could be enrolled *for free* via the existing bot-linkage evidence has already been enrolled. This is the honest reason mass migration stalled: not neglect, but exhaustion of the cheap-evidence pool, which the original closing verdict should have stated explicitly and did not.

**Why this counts as a premature closure:** the phase's own written accept contract requires the mass cohort; the closing verdict enumerated what *was* done and what was explicitly deferred (PH4-08, PH5-09) but never named "mass migration" as a deferred/blocked item, creating a false impression that only optional follow-on work remained. It should have read `[~]` with an explicit mass-cohort blocker, not `[x]`.

**Concrete mass-migration plan (proposed, not yet started -- requires owner authorization per batch, same discipline as every prior real cohort):**

1. **Evidence collection for the 13 active, unlinked real users.** Two non-exclusive paths, both already fully built and tested, zero new code: (a) the existing bot `/waiting_link` flow -- ask each customer (support message, using their existing legacy URL, which still works) to link Telegram once, the same organic mechanism accounts 3/4's evidence ultimately traced back to; (b) `ownership_provenance='OWNER_APPROVED'` in `direct_enrollment.py` (already supported, unused so far) -- for a user the owner personally knows (the `note` fields like "Vladislav and Valera", "Maria_From_Anna" suggest direct personal relationships), the owner attests ownership directly with a recorded reason/evidence reference, exactly like `record_owner_attested_legacy_payment` already does for payment.
2. **Resolve the 1 ambiguous-Telegram case** with an explicit owner decision (which linked Telegram id is the real owner) before enrollment -- never auto-picked.
3. **Decide the 5 expired/lapsed users' fate explicitly** (migrate anyway vs. formally excluded/deferred as dormant) -- do not silently drop them from the count without a decision either way.
4. **Batch-enroll and migrate in reviewed sub-cohorts** (e.g. 3-5 accounts per batch, not all 13-19 at once), reusing the exact already-proven-safe sequence from accounts 3/4 unchanged: `enroll_direct_account` -> `ensure_legacy_paid_compat_entitlement` (`LEGACY_PAID_COMPAT_V1_D3` default unless evidenced otherwise) -> genesis child bootstrap on the account's own slot *before* any bridge binding exists -> bridge binding -> synthetic-canary-device migrate/revoke/free proof -> only then treat the account as migrated. Fresh pre/post state re-verification per account, per this project's own established discipline.
5. **Extend the existing support runbook** (`docs/PHASE4_MIGRATION_SUPPORT_RUNBOOK.md`) with a literal per-account mass-cohort checklist (by account id only, never raw username) so "mass" has a visible, trackable finish line.
6. **Only after the real active pool is fully covered** (or explicitly, individually excluded with a recorded reason) should PH4-03 be marked `[x]` again, and only then should PH4-05 consider starting a real grace period for the full graduated cohort -- starting grace before mass migration completes would begin a real 14-day countdown for customers who do not yet even have a working migrated path, which is exactly the risk this reopening exists to prevent.

No code changes are required to execute this plan -- every primitive it uses (`direct_enrollment.py`, `legacy_paid_compat.py`, the PH4-01/02 bridge/migration machinery) already exists and is already tested; this is a data/ownership-evidence and rollout-sequencing gap, not an engineering gap.

## [x] PH4-04 — New opaque URL rollout

**Depends:** canary, PH2-01. **Scope:** Telegram/LK/admin показывают новый URL; no query/log leak; secure one-time presentation/reissue.
**Accept:** новые accounts не зависят от legacy URL. **Tests:** full journey/rotation/log canary.
**Rollback:** pause issuance; issued token state сохраняется.

**Сделано (2026-08-26):** crash-safe issuance/rotation orchestration (`src/subscription_credential_issuance.py::issue_or_reissue_credential`) sequences PH2-01's already-dormant `SubscriptionCredentialStore.prepare()/activate()` around a caller-supplied `deliver_fn`: abandon any stale `PENDING_DELIVERY` (unrecoverable) -> `prepare()` a fresh generation (old stays `ACTIVE`) -> deliver -> only if delivery did not raise, `activate()` (atomically flips new->`ACTIVE`, old->`REVOKED`). A failed/lost delivery always leaves exactly the old credential `ACTIVE`, never two actives, and a retry converges. New `SubscriptionCredentialStore.abandon_pending()` makes this safe without inventing a second store. Three presentation surfaces, each within its own already-existing auth boundary (no boundary weakened): **Admin** -- `src/routes/subscription_credentials_admin.py`, `GET/POST /admin/accounts/{id}/subscription-credential(/issue)`, requires `require_admin_auth` (session+CSRF) AND the server-derived primary-admin capability (the same PH3-06/PH4-01 boundary, wired into a live route for the first time); raw token returned exactly once, never in the status endpoint, never logged. **Telegram** -- hidden `/newsub` command in `bot_support.py`, private-chat-only (`F.chat.type == ChatType.PRIVATE`), requires the canonical Telegram OWNER link (`accounts.get_account_for_telegram` -- PROVEN ownership, never mere possession of an old link); not a visible keyboard button (avoids confusing the vast majority of still-legacy-only users). **LK** -- `GET/POST /lk/api/opaque-subscription(/issue)`, gated by the exact same `_require_mgmt_session` boundary every other destructive LK device action already requires (never the bare legacy subscription token alone, per PH2-05's "possession is not ownership" rule). nginx: a new regex `location ~ "^/[A-Za-z0-9_-]{43}$"` added to `sub.beykus.fun` (every reserved prefix location -- `/sub/`, `/lk/`, `/assets/`, `/internal/`, `/sub-admin*` -- wins over it under nginx's own matching rules regardless of order, so none of them can ever be shadowed), reusing the exact same redacted `mgboost_sensitive` access log, security headers and trusted-`X-Real-IP` handling as the existing `/sub/` location; config was root-owned-backed-up and `nginx -t`-verified before reload; legacy `/sub/`, `/lk/` and the new route all verified reachable/correct after reload with zero raw-token leakage in the sensitive log. `OPAQUE_SUBSCRIPTION_ENABLED` remains the actual production gate (still the sole dormancy control, matching `/sub/`'s own `LEGACY_BRIDGE_ENABLED` precedent) -- the route is now reachable end-to-end but inert until explicitly enabled for a canary. 33 new focused tests (issuance orchestration crash/retry, admin route auth/CSRF/IDOR/one-time-token, bot private-chat/ownership/lost-delivery, LK mgmt-session-vs-bare-token), full regression `911 passed, 3 skipped`, zero regressions.
**New-account "first device" limitation -- investigated, not a new blocker:** confirmed the broker's real `child.user.ensure` operation cryptographically re-derives and re-verifies the claimed `source_contract_hash` against the *live* config of a real, already-existing Marzban `source_username` (`broker_operations.py`) -- there is no way to bootstrap a child from a purely synthetic/non-existent template without weakening that anti-tamper check, and every account in this system's current schema already requires exactly one real backing legacy Marzban username (`mgboost_legacy_account_aliases.legacy_username`, `NOT NULL UNIQUE`) by design -- a zero-Marzban-footprint "brand new signup" is not a flow this system supports today (that is PH5 catalog/billing territory, not yet built). PH4-01/02 migration already requires and performs this exact same one-time real-legacy-user-derived genesis-child seeding step per account before its first real device resolves; PH4-04's own acceptance ("new accounts don't depend on legacy URL") is about the *end user* never needing to fetch/use the old `/sub/{legacy_token}` URL again once their opaque credential exists -- already true structurally, proven by the existing `test_second_new_hwid_gets_its_own_second_child`/this session's production canary below, with zero code change needed.
**Real production canary PASS (owner's own account 1, 2026-08-26):** encrypted backup+restore-verified beforehand (production state had changed since the prior backup); root-owned `.env`/nginx config backups taken before each mutation. Issued a real credential via the admin route, then externally fetched `https://sub.beykus.fun/<token>` over the real Internet path with a real supported-client shape (`happ/2.7.0/windows`, already in the existing `compat_registry` allowlist): a brand-new synthetic canary device on a genuinely free slot (never slot 1/2, the account's two real live devices, confirmed untouched throughout and after) got a real working VLESS child config with **zero** occurrence of the account's real legacy shared UUID; the same device repeated the fetch successfully; a second distinct canary device got its own separate child. Rotation: a new generation was issued, the old token immediately started returning the exact same uniform `404` as a syntactically-valid-but-unknown token (bounded-timing floor and uniform body confirmed identical), the new token worked immediately, and the underlying child/UUID were untouched by the rotation (only the external credential changed) -- proving "rotation is not a device revoke". The PH2-06 rate limiter fired for real over the public Internet path (a `429`/`Retry-After` appeared partway through a same-IP burst, confirmed via nginx + the app, not just in unit tests). Full leakage scan after the canary: zero 43-character opaque-token-shaped strings anywhere in the nginx access/error logs or the application journal; the DB and its audit-event table (`mgboost_subscription_credential_events`) contain only 64-hex-character `token_hash` values and plain reason/actor text, never a raw token. Canary devices were revoked+freed afterward (permanent tombstones, matching this project's own retention convention); the account's two real devices and its real legacy Marzban user (`status`/`expire`) were read-verified unchanged before, during and after. `quick_check=ok`, 0 FK violations, all 4 services stayed active throughout. `OPAQUE_SUBSCRIPTION_ENABLED` is left permanently `1` (graduated from canary to deployed, matching `LEGACY_BRIDGE_ENABLED`'s own PH4-03 precedent) -- the owner's account now has one real, working, rotated opaque credential; no other account has been issued one yet.
**Original verdict (2026-08-26): `[x]`.** Every accept item was satisfied at the time: Telegram/LK/admin each present/reissue safely within their own real, unweakened auth boundary; the opaque route is production-exposed with no query/log leak; one-time presentation/reissue is proven crash-safe; a full new-account "first device" journey needs no legacy URL/token; rotation works end-to-end for real; the production canary passed in full including a real rate-limit trigger and a clean leakage scan. **This backend/resolver canary genuinely passed and is not being retracted or rewritten** -- the finding below is a separate, later, post-closure user-journey regression, not evidence the original canary was false.

**Post-closure regression found by owner manual testing (2026-08-26), reopened `[~]`:** the owner personally exercised the real production `/newsub` flow end-to-end (not just the supported-VPN-client path the canary covered) and found two real UX/safety gaps the canary's scope never exercised:
1. Opening the received opaque URL in an ordinary browser (Chrome/Firefox/Telegram in-app browser) returned the uniform invalid `Subscription not found` response instead of the existing friendly legacy browser landing page -- a real regression relative to `/sub/{token}`'s long-standing behavior for the exact same class of request.
2. A bare repeat of `/newsub` while a credential was already `ACTIVE` implied a repeat command could itself be read as consent to rotate -- rotation is destructive (the old URL stops working immediately) and needs an explicit, distinct confirmation step, not just a second use of the same command.

**Fix (this session):** (1) `src/routes/sub.py`'s existing browser-vs-VPN-client detection (`is_browser_request`) and its exact landing-page renderer (`send_browser_landing`, reusing `frontend/browser_page.html` verbatim -- no second parallel browser UX) are now shared with `src/routes/opaque_sub.py` via a new `_try_browser_landing` gate that runs strictly *after* the token resolves to an `ACTIVE` credential with a live, non-`EXPIRED`/`DISABLED` parent; any invalid/unknown/revoked/expired-parent token still gets the exact same uniform invalid response regardless of `User-Agent` (no validity oracle introduced for browsers), and the check is read-only -- zero slot/child/device mutation from a browser hit, proven by a mutation-count test. (2) `/newsub` (`src/bot_support.py`) no longer auto-rotates on a bare repeat: no `ACTIVE` credential -> issues directly as before (normal initial issuance); an `ACTIVE` credential exists -> shows an info message with a single "🔄 Перевыпустить ссылку" button, which leads to a distinct destructive-action confirmation message with explicit "✅ Перевыпустить"/"❌ Отмена" buttons -- only the final confirm triggers rotation. Every callback step re-verifies the canonical Telegram OWNER (`message`/`callback.from_user.id`, never account-id-by-possession), is private-chat-only, never holds a raw token between steps (created only after confirmed rotation), and a fast double-tap on confirm is serialized by a small in-memory per-account guard (never double-rotates). (3) LK/Admin consistency audit: both `src/routes/lk.py::handle_lk_opaque_subscription_issue` and `src/routes/subscription_credentials_admin.py::handle_subscription_credential_issue` had the exact same silent-rotation gap (a single POST always issued/rotated, `ACTIVE`-or-not) -- both now require an explicit `{"confirm": true}` in the request body when an `ACTIVE` credential already exists (a bare request without it returns `409 requires_confirmation` and performs no mutation), while a fresh account with no `ACTIVE` credential still issues in one call. No UI redesign; no frontend currently calls either endpoint yet, so this is a backend-contract-only fix. 22 new/updated focused tests (browser landing legality/mutation-safety/headers/uniform-invalid preservation, Telegram initial-issue/offer/confirm/cancel/confirmed-reissue/double-tap/non-owner/group-chat, LK/admin confirm-required/explicit-confirm-rotates), full regression `931 passed, 3 skipped`, zero regressions.

**Production re-verification PASS (2026-08-26):** encrypted backup+restore verified (`encrypted_backup_create=PASS`, `encrypted_backup_restore=PASS`) before deploy; `origin/main` fast-forwarded onto production, `mgboost-panel` restarted, all 4 services (`mgboost-panel`, `mgboost-marzban-broker`, `mgboost-child-worker`, `nginx`) active, readiness `200`, `quick_check=ok`, 0 FK violations. Real journey on the owner's own account (account 1, the exact account/credential exposed during the owner's manual test that found this regression): confirmed one `ACTIVE` credential (generation 5) existed; a bare `/newsub`-equivalent repeat left it fully unchanged and delivered no token; the confirm-button click alone still left it unchanged; the explicit final confirm rotated it for real (generation 5 -> 6, old row flipped to `REVOKED`, device-slot-generation/child-intent table row counts and child-intent id set proven byte-for-byte unchanged by the rotation itself); the new generation-6 URL was fetched for real over the public Internet (`https://sub.beykus.fun/<token>`) with a real browser `User-Agent` and got the fixed landing page (`200`, no `Subscription not found`, `Cache-Control: no-store`/`Referrer-Policy: no-referrer`/`X-Frame-Options: DENY`/CSP all present), with device-slot/child-intent counts proven unchanged before vs. after that browser visit. The old (owner-exposed) generation-5 token's raw value was intentionally never captured, re-typed or re-fetched at any point this session, per the standing security instruction -- its invalidation is proven by its DB row's `REVOKED` status, not by re-issuing an HTTP request with it. Supported-VPN-client fetch on the same route is unmodified by this fix (the new browser check returns before ever reaching the resolver) and stays proven by the 39 already-passing focused resolver/browser-landing tests rather than a second live production mutation, per explicit instruction to avoid unrelated repeat risk. Leakage scan: the currently-live generation-6 credential produced zero application-journal or nginx-log entries after the deploy restart (confirmed empty for the entire post-restart window) and the DB stores only its hash, matching PH2-01's unchanged contract; nginx's ordinary/sensitive/rotated logs contained zero opaque-token-shaped strings from this session's testing (the handful of 43-character matches found in the journal predate this session's restart, are already-`REVOKED` generations 1-4 from earlier ad hoc/canary testing, and are dead credentials with no live value -- out of scope for this corrective fix to remediate retroactively, noted here for visibility only). Transient verification script and the one-time root-only token file used to drive the real browser fetch were both shredded immediately after use; no raw token, UUID or HWID was ever printed to this session's output. Full regression: `931 passed, 3 skipped`.

**Verdict: `[x]`.** The post-closure browser-landing and implied-consent-rotation regressions are fixed, tested (22 focused tests) and re-verified for real on production end-to-end, including the mandated controlled rotation of the owner's manually-exposed credential without ever re-exposing a raw secret. The original 2026-08-26 backend/resolver canary above remains valid and is not retracted.

## [x] PH4-05 — Approve and implement legacy grace period

**Depends:** telemetry (PH3-07, closed). **Fixed policy:** OPD-09/DL-023 — grace period 14 дней.
**Accept:** per-account/cohort start/end, communications, support and metrics. **Tests:** exact UTC boundary at 14 days, inactive clients.
**Rollback:** extension только explicit/audited; revoked UUID не reopen.

**Revised product decision (2026-08-26, supersedes the "blocked until mass migration" note below):** the owner decided grace-period membership must never wait on Telegram registration or on migration having already happened -- the clock is about an already-real legacy subscription continuing to work unchanged, and the 14-day window itself is used *as* the mass-migration campaign (register in Telegram during the window -> account gets migrated -> new opaque URL), not as a reward for having migrated first. This directly resolves the blocker paragraph immediately below (kept for history, not deleted) by changing what "cohort readiness" means: readiness is now "this is a real ACTIVE legacy user," not "this account is already migrated."

**Superseded blocker note (2026-08-26 earlier the same day, kept for history):** starting a real grace period for any account requires that account to already be migrated. A production audit found only 3 real accounts have ever been migrated, out of 24 real legacy Marzban users (19 active) — the mass-migration cohort PH4-03 itself requires was never executed. PH4-05's own `start()` mechanism is account-scoped and technically works for any already-migrated account regardless of cohort size, but starting grace for only 3 accounts while ~19 real users remain entirely unmigrated is not what this phase's "per-account/cohort" accept criterion or DL-023's own intent describe as a rollout — it would start a real irreversible-feeling countdown for a tiny fraction of the real user base while the rest have no working alternative URL yet. No real `start()` should be authorized until PH4-03's reopened mass-cohort gap is resolved or the owner explicitly decides otherwise.

**Production mass-cohort launch DONE (2026-08-26, later the same day):** a single, shared-UTC-boundary grace cohort (`cohort_ref='PH4-05-MASS-COHORT-2026-08-26'`) was started for real production: `cohort_start_at=1787742505` (2026-08-26 14:08:25 MSK), `cohort_end_at=1788952105` (2026-09-09 14:08:25 MSK, exactly `+1209600`s). 17 real parent accounts, all sharing the exact same `started_at`/`original_end_at` (verified via `SELECT DISTINCT started_at, original_end_at ... -> exactly one pair`): the 3 already-migrated accounts (1, 3, 4) plus 14 newly-bootstrapped accounts (`ownership_evidence='ABSENT'`, zero Telegram claim made at bootstrap -- see `src/legacy_grace_registration.py::bootstrap_grace_subject`). This covers all 19 real ACTIVE legacy Marzban usernames (5 already aliased under accounts 1/3/4 + 14 newly aliased). Excluded, exactly as decided: 5 `EXPIRED` real users (a future renewal/policy path, not this campaign), the PH3-08 synthetic test canary, generic test users, and `mgc_*` child accounts. The 1 known ambiguous-Telegram-ownership real user (account 8) was included in the cohort (grace applies) but its Telegram ownership was never guessed/assigned -- `telegram_status='AMBIGUOUS'`, `action='MANUAL_REVIEW'`, zero identity mutation. 4 of the 14 new accounts (account ids 8, 10, 11, 13) have more than the default `D3` observed active devices (4, 7, 8, 8) -- their account/alias/grace membership was still created (grace never depends on entitlement), but `ensure_legacy_paid_compat_entitlement` correctly failed closed (`DeviceOverageConflict`, no invented device limit) and these accounts have **no subscription/entitlement yet** -- migration for them requires an explicit owner-approved `approved_extra_device_slots` decision first, exactly matching this project's own "never infer an increased device limit" rule from PH4-03. Day-0 report: 3 `OK_MIGRATED`, 13 `WAITING_FOR_REGISTRATION`, 1 `MANUAL_REVIEW`, 0 `RECONCILE_REQUIRED`/`COMPATIBILITY_BLOCK`. Pre/post: encrypted backup+restore verified PASS, `quick_check=ok`, 0 FK violations both before and after, all pre-existing table cardinalities unchanged except the additive new rows, all 4 services stayed active throughout, legacy `/sub` (bogus-token 404 and readiness 200) confirmed unaffected, zero raw legacy/opaque token/username printed anywhere in this session's output or the application journal, zero Telegram messages sent, zero `LEGACY_REVOKE_PENDING`/`LEGACY_REVOKED` transitions (PH4-06 untouched).

**Reversible/dormant part DONE (2026-08-26), real clock NOT started for any
account — explicit owner decision gate, per instruction.** Additive durable
schema `mgboost_legacy_grace_periods`/`_events`
(`src/legacy_grace_schema.py`): one row per account ever
(`UNIQUE(account_id)`), fixed-14-day window enforced by a schema `CHECK`
tying `original_end_at = started_at + 1209600` (not just application code),
`current_end_at` gated by a monotonic-forward-only DB trigger (never
shrinks/resets), identity columns and the full event log immutable
(no-update/no-delete triggers), mirroring PH4-02's own
`LEGACY_REVOKED`-can-never-transition-again precedent. `LegacyGraceStore`
(`db.legacy_grace`, `src/legacy_grace.py`): `start()`/`extend()` both
require the same sealed `PrimaryAdminAuthority` capability every other
PH3-06/PH4-01..04 consequential action already requires; `start()` is
idempotent per account (same idempotency key -> same row) but a genuinely
new start attempt after one already exists fails closed
(`GraceAlreadyStarted`, never a reset); `extend()` requires a strictly later
`new_end_at` (never a no-op/shrink, `GraceTransitionError` otherwise) plus a
CAS `expected_revision` and always writes an immutable, reason+evidence-ref
audited `EXTENDED` event — there is no silent extension anywhere in this
code. Pure boundary helpers (`grace_active`/`seconds_remaining`/`day_index`)
implement the exact tested rule: `now < current_end_at` is still within
grace, `now == current_end_at` is already expired.

New privacy-safe grace-activity telemetry
(`mgboost_legacy_grace_activity_daily`, `src/legacy_grace_activity*.py`,
mirrors PH3-07's own isolated-short-timeout-connection discipline): daily
per-account/per-channel (`LEGACY`/`OPAQUE`) request counters only — never a
raw token, full subscription URL, UUID, full HWID, cookie/auth value or
bearer path, 60-day retention (cleanup script:
`scripts/cleanup_ph4_05_grace_activity_telemetry.py`, not yet on a systemd
timer). Wired as a fail-open, response-blind observation hook into the
already-live `routes/sub.py::handle_sub` (legacy path, once
`legacy_bridge.resolve_account_for_legacy_username` resolves a real
account) and the dormant `routes/opaque_sub.py::handle_opaque_sub` (real
resolve path) — exactly the same pattern and exception-swallowing
discipline as the already-deployed PH3-07 `_observe_compatibility_fail_open`
hook; a write failure here can never change, delay or deny a subscription
response (proven by `tests/test_legacy_grace_route_hooks.py`). This is the
one part of this session's change that touches the **already-live**
legacy `/sub` request path (an extra read + a fire-and-forget write per
real request), not a purely dormant module — called out explicitly for the
owner's separate deploy sign-off, distinct from the schema-only pieces.

`src/legacy_grace_observability.py::account_grace_snapshot()` assembles the
full per-account visibility set (grace day/remaining time, PH4-02 migration
state counts, active vs. migrated devices, last legacy/opaque activity,
24h/72h request counts, resolver/reconciliation/revoke-rebind event counts
from PH4-02's own existing audit trail, `inactive_since_grace_start`) by
composing already-existing tables wherever one exists rather than
duplicating them — no mutation anywhere in this module.
`scripts/ph4_05_grace_eligibility_report.py` is a read-only dry-run CLI
(table/json/csv) producing exactly the requested
account/migration-state/active-devices/last-activity/compatibility/
blockers/`START_GRACE`|`HOLD` decision table, validated end-to-end against
a synthetic local DB (real production dry-run requires a downloaded DB
copy and was not run this session — no SSH/production access was used).
Draft (unsent) Telegram/LK/support-ticket communications:
`docs/PHASE4_GRACE_PERIOD_COMMS_DRAFT.md`. Full runbook:
`docs/PHASE4_GRACE_PERIOD_RUNBOOK.md`. 38 new focused tests across 5 files
covering exact `<`/`==`/`>` boundary semantics, restart/persistence,
duplicate start, explicit audited extension (incl. stale-revision/no-shrink
rejection), inactive-client detection and route-hook fail-open behavior;
full regression `969 passed, 3 skipped` (zero regressions from `931`).

**Explicitly NOT done, per instruction:** no real account has a
`mgboost_legacy_grace_periods` row (dry-run report against real production
accounts, and the schema/route-hook code's own production deploy, both
await the owner's next decision); no communication was sent to any real
user; PH4-06 (the actual revoke) remains its own separate, unbuilt phase.

## [ ] PH4-06 — Revoke shared legacy URL/UUID after grace

**Depends:** PH4-05 and migration/exception review.
**Scope:** global legacy revoke/UUID rotation, verify main/remote Xray; не отзывать после первого migrated device.
**Accept/tests:** old URL/config/direct host fail, child works, offline-node reconcile.
**Rollback:** issue new child; no restoration of shared leaked UUID.

## [ ] PH4-07 — Migration observability/support/cleanup

**Depends:** Phase 4. **Scope:** cohorts/failures/orphans/stale aliases/pending/error, support tools, cleanup under DL-042 retention and any entity-specific retention.
**Accept:** no unexplained pending/error. **Tests:** reconciliation/support drill.
**Rollback:** destructive cleanup only after verified backup/restore, confirmed rotation/reissue strategy, verified quarantine snapshot where legacy token evidence is involved, and applicable retention expiry.

## [ ] PH4-08 — Preserve legacy manual/external-payment subscriptions

**Depends:** PH0-08, PH3-09, PH4-01/02, authoritative payment/admin evidence.
**Preserve:** expiry, plan/conditions, device evidence, legacy Marzban username, Telegram/end-user mapping и renewal/issuance history; external-payment metadata сохраняется только если доказана.
**Flow:** legacy direct subscription -> same parent MGBoost account -> lazy child users. Manual renewal после migration меняет тот же parent, синхронизирует children, сохраняет slots/HWID/current WL period и UUID без revoke причины.
**Accept:** отсутствие metadata не превращается в выдуманный payment channel; ambiguous provenance получает `UNKNOWN_LEGACY`; historical references retained.
**Tests:** full snapshot before/after, retry/idempotency, renew during migration, missing TG mapping, multiple legacy devices, rollback bridge.
**Rollback:** не удалять legacy mapping/username/history до verified revoke and retention expiry.

# Phase 5 — Tariffs and billing

## [x] PH5-01 — Versioned six-plan catalog

**Depends:** PH3-01. **Scope:** stable plan codes/versions, approved prices/durations/devices/WL; independent Stars and `RUB-2026-08-23-v1` channel-price tables. Current free-form `stars_tariffs` requires migration.
**Accept/tests:** exact 12 plan-duration combinations in each applicable price channel; RUB values exactly match DL-040; invoice/payment snapshots unchanged by later catalog edits.
**Migration:** current 199/349 rows map only via explicit production plan; canary archived/retained by decision.

**Implemented and production-deployed (2026-08-26):** new dormant/additive
schema (`src/plan_catalog_schema.py`, own checksum-pinned migration
`ph5_01_plan_catalog_v1`, requires the exact PH3-01 parent checksum before
applying, matching PH3-06's own precedent) adds `mgboost_price_catalog_versions`
(one row per channel/catalog-version, immutable identity fields, at most one
`ACTIVE` version per channel via a partial unique index) and
`mgboost_plan_prices` (immutable, FK-bound to a specific plan-version+duration,
`UNIQUE(catalog_version_id,plan_version_id,duration_id)`). `src/plan_catalog.py`
defines the exact owner-approved catalog data (nothing invented: prices/
durations/device-limits/WL quotas copied verbatim from this file's own
"Approved product catalog" table and DL-040) and `seed_plan_catalog()`
idempotently creates the six `mgboost_plan_versions` rows (`BASIC`,
`BASIC_PLUS`, `BASIC_PRO`, `WL`, `EXTENDED`, `FAMILY`; `COMMERCIAL`,
`billing_required=1`, device limits 3/6/12, WL `NONE` for the three Base
tiers and `LIMITED`/100-150 decimal GB/30-day period for WL/Расширенный/
Семейный) with 30/60-day durations, then both channels' active price
catalogs (`TELEGRAM_STARS` = new `STARS-2026-08-26-v1`, `RUB` =
`RUB-2026-08-23-v1` per DL-040) -- exactly 12 SKUs and 12 price rows per
channel, 24 total. Seeding runs via a new explicit idempotent script
(`scripts/seed_ph5_01_plan_catalog.py`), NOT automatically at `Database`
startup -- same dormant-until-explicitly-seeded discipline PH3-01/PH3-06
used for their own schemas. Nothing in the legacy/Stars/LK/bot purchase
paths reads this catalog yet; the live `stars_tariffs` table and the actual
199⭐/349⭐ current-production-tariff -> catalog mapping decision noted in
this entry's own "Migration" line are explicitly deferred to the purchase-
flow phase that reads this catalog for real (PH5-04/05), not part of this
slice's scope (schema/catalog data only, per PH5-01's own "Depends: PH3-01"
boundary -- no purchase/entitlement wiring dependency yet exists to cut
over).
**Tests:** 9 new focused tests (`tests/test_plan_catalog_schema.py`,
`tests/test_plan_catalog.py`) covering migration idempotency, exact
PH3-01-parent-checksum requirement, price/catalog-version immutability
(`UPDATE`/`DELETE` both rejected), one-active-catalog-version-per-channel,
positive-integer-amount validation, exact device/WL terms per plan, exact
12-SKU/24-price seeding matching the approved tables verbatim, full
seed-then-reseed idempotency (zero duplicate plan-version rows, zero
newly-created prices on the second run), and the seed script's own `main()`
end-to-end. Full regression `1039 passed` (via the already-installed
`/tmp/mgboost-wave-a-browser-venv` Playwright/Chromium environment, all
browser suites included, zero skips).
**Production deploy:** fresh encrypted backup create/restore PASS
immediately before deploy; preflight invariants recorded (`quick_check=ok`,
0 FK violations, accounts=18, grace=17, `mgboost_plan_versions`=7 (pre-
existing `LEGACY_PAID_COMPAT_V1_*`/internal variants),
`mgboost_plan_prices` table absent, `LEGACY_REVOKED=0`). Fast-forward pull
`f4a250e` -> `6414a59`, `mgboost-panel` restart only (schema self-applies
on `Database` construction, additive-only). Post-deploy: same invariants
unchanged, new schema present, `mgboost-panel`/`mgboost-marzban-broker`/
`mgboost-child-worker`/`nginx` all active, static ES modules/CSS `200`
correct MIME, `/admin/accounts`/`/admin/dashboard` still `401`
unauthenticated, legacy `/sub` bogus-token still `404`. Catalog explicitly
seeded in production via the new script: 6 plan codes, 24 prices created,
re-run immediately after confirmed fully idempotent (0 newly-created,
`quick_check=ok`, 0 FK violations, accounts/grace still 18/17 unchanged).
`plan_versions` count went 7 -> 13 (exactly the 6 new commercial rows), no
existing row touched (immutability triggers make that structurally
impossible, not just observed).

## [x] PH5-02 — 30/60-day entitlement and WL-period semantics

**Depends:** PH5-01, PH6 period interface. **Policy:** 60d = two sequential 30d WL periods; Non-WL unlimited. По OPD-40/DL-044 повторная покупка того же plan — renewal с формулой `max(current_expiry, now) + purchased_duration`; накопленный срок создаёт последовательные 30-day WL periods, а не объединённый base quota.
**Accept:** purchase создаёт expiry/schedule без сброса WL на plain expiry admin action; active subscription продлевается от current expiry, expired — от текущего момента; каждая успешно оплаченная покупка добавляет duration ровно один раз.
**Tests:** boundary, second/следующие periods стартуют ровно один раз со fresh base quota; active/expired formula; repeated equal durations; timezone semantics explicit.
**Rollback:** immutable scheduled periods/invoice snapshot preserved.

**Implemented and production-deployed (2026-08-26):** "PH6 period interface"
in this entry's own `Depends` line is not a wait for Phase 6 code to exist
first -- `PH6-02 Immutable WL periods` itself `Depends: PH5-02`, so that
would be a dependency cycle. It is the contract PH6-02 will later consume:
sequential, UTC-epoch-second-aligned WL period rows in the already-existing
PH3-01 `mgboost_wl_periods` table. `src/subscription_renewal.py` is that
contract's producer plus the DL-044 renewal formula itself.
`compute_new_expiry()` implements `max(current_expiry, now) +
purchased_duration` as one formula with no separate active/expired branch
(`max` degenerates to `now` when `current_expiry` is `None` or already
past -- exactly "expired -- от текущего момента", and equals
`current_expiry` when it's still in the future -- exactly "active --
продлевается от current expiry"). `schedule_wl_period_windows()` splits the
purchased duration into sequential, contiguous, non-overlapping
`wl_period_days`-long windows (60 days / 30-day period -> exactly two
windows, second starts exactly where the first ends); a `wl_mode='NONE'`
plan schedules zero periods (Non-WL is unlimited, nothing to track).
`SubscriptionRenewalStore.apply_same_plan_purchase()` composes both inside
one transaction: idempotent per `idempotency_key` (reuses
`mgboost_entitlement_mutations.idempotency_key_hash` uniqueness, replays the
prior result rather than re-applying on a repeat call), same-plan-only
(`PlanMismatch` if the account's current live plan differs -- a different
plan is upgrade/downgrade policy, PH5-06, not stacking), refuses to ever
touch an admin-granted `UNLIMITED` subscription
(`UnlimitedSubscriptionConflict`), validates the plan/duration actually
exist in the PH5-01 catalog (`UnknownPlan`/`RenewalError`, never invents
one), and writes an immutable `mgboost_subscription_terms` snapshot per
purchase. A new additive migration
(`src/wl_period_lifecycle_schema.py`, `ph5_02_wl_period_lifecycle_v1`,
requires the exact PH3-01 parent checksum) closes a real PH3-01 gap:
`mgboost_wl_periods` had no immutability triggers at all (unlike
`mgboost_plan_versions`/`mgboost_subscription_terms`, which already did) --
now its identity/quota fields (`account_id`/`subscription_id`/
`subscription_term_id`/`sequence_no`/`starts_at`/`ends_at`/`quota_mode`/
`base_quota_bytes`/`created_at`) are guarded against `UPDATE`/`DELETE`;
`status` alone stays mutable for Phase 6's own future
`PLANNED`->`ACTIVE`->`CLOSED` runtime transitions, not built here. Not
wired to any live purchase flow yet -- PH5-05 (Stars) and PH5-09 (manual
external payment) are the future callers, each responsible for its own
payment/actor verification before calling this engine.
**Tests:** 15 new focused tests (`tests/test_wl_period_lifecycle_schema.py`,
`tests/test_subscription_renewal.py`): migration idempotency/parent-
checksum requirement, WL-period identity immutability (`status` alone still
transitions), the exact DL-044 boundary (`current_expiry == now` counts as
already-ended, matching this project's existing grace-period convention),
first purchase, 60-day-creates-exactly-two-contiguous-periods, Non-WL
creates zero periods, repeated-equal-duration purchases stack and periods
keep incrementing (never restart at sequence 1), idempotency-key replay,
different-plan refusal, unlimited-subscription refusal, unknown-plan/
unknown-duration rejection, and pure-UTC-epoch-seconds arithmetic (no
calendar/timezone semantics). Full regression via the already-installed
`/tmp/mgboost-wave-a-browser-venv` Playwright/Chromium venv: `1054 passed`
(all browser suites included, zero skips).
**Production deploy:** fresh encrypted backup create/restore PASS
immediately before deploy; preflight invariants recorded (`quick_check=ok`,
0 FK violations, accounts=18, grace=17, `mgboost_subscriptions`=18
(pre-existing `LEGACY_PAID_COMPAT_V1_*` rows), `mgboost_wl_periods`=0,
`LEGACY_REVOKED=0`). Fast-forward pull `6414a59` -> `7820443`,
`mgboost-panel` restart only (new triggers self-apply on `Database`
construction, additive-only, no existing row touched -- `mgboost_wl_periods`
was and still is empty in production). Post-deploy: same invariants
unchanged, new migration row present, all 4 services active, unauthenticated
`/admin/accounts` still `401`, legacy `/sub` bogus-token still `404`.

**Next Phase 5 slice explicitly NOT started this session:** PH5-03
(Versioned WL package catalog) is next by number, but its own `Depends`
line ("PH5-01/02 и entitlement ledger") and its own Accept/Tests
("base-first consumption", "unused-only refund", "freeze/resume") are not
satisfiable without a real measured WL consumption number -- and zero of
PH6-01..04's actual usage-tracking/collector/pool infrastructure exists yet
(all still `[ ]`). This is a real, non-circular blocking dependency (unlike
PH5-02's own "PH6 period interface" naming, which was a forward-interface-
contract, not a real wait) -- inventing a fake consumption number to make
PH5-03's rollover/refund logic "work" would be exactly the kind of
fabricated data this project's own discipline forbids, and building the
real Phase 6 usage-tracking machinery is explicitly out of this session's
scope. PH5-04 depends on PH5-03; PH5-05/06/08 depend on PH5-04; PH5-09's own
test list ("manual package eligibility/refund") is also entangled with
PH5-03 despite its `Depends` line not naming it explicitly. No further PH5
slice was judged safely startable this session without either skipping this
dependency or starting Phase 6, both explicitly out of scope.

## [x] PH5-03 — Versioned WL package catalog

**Depends:** PH5-01/02 and entitlement ledger. **Fixed policy:** OPD-02/03/04/12/13/32 and DL-025–027 — rollover/freeze, Base rejection, base-first consumption and unused-only refund. **Approved Stars:** +50/79, +100/149, +250/349, +500/599. **Approved RUB v1:** +50/139, +100/249, +250/579, +500/999. Purchase/use only on WL-enabled plans.
**Accept:** invoice snapshot, eligibility, rollover bucket, adjustment/audit durable; period reset не удаляет remainder; expiry/Base transition freezes bucket; unused-only refund atomically revokes it.
**Tests:** all packages, base-first consumption, multiple period transitions, freeze/resume, stale callback, Base rejection, zero-consumption refund, partial-consumption refund denial, duplicate payment.
**Rollback:** stop sales; paid grant follows recorded product version.
**Unblocked 2026-08-27:** this task's own "base-first consumption"/rollover/
freeze semantics need a real measured WL consumption number to build
against -- PH6-03 (durable ledger) and PH6-04 (shared parent pool sum) are
now both closed and production-verified, so that real consumption data
exists. No longer blocked by missing Phase 6 infrastructure; still needs its
own fresh scoping session (package purchase/refund/rollover ledger design,
package logic is explicitly out of PH6-04's own scope).

**Implemented and production-deployed (2026-08-27):** additive
`ph5_03_wl_package_catalog_v1` reuses PH5-01's immutable channel catalog
versions and PH3-09 payment/mutation provenance. It adds immutable versioned
package products/prices and parent-account package grants/refund evidence;
the explicit seed is dormant and no route/UI/worker invokes it. Consumption
is a pure derived read over PH6-03 samples: each canonical period spends its
base quota first, then allocates excess to active package buckets by DL-053
FIFO. Expiry/lapse/Base reads as frozen; no bucket order or remainder is
rewritten. A zero-derived-consumption refund appends durable evidence and
atomically changes the bucket only from `ACTIVE` to `REVOKED`.
Production gate: encrypted backup create/restore PASS; pre/post
`quick_check=ok`, 0 FK violations, accounts/subscriptions/periods unchanged
at 18/18/0. Explicit dormant catalog seed created exactly 4 products and 8
prices, then re-run created 0; grants/refunds remain 0. Only `mgboost-panel`
restarted; all four services active, unauthenticated admin remains `401` and
legacy bogus `/sub` remains `404`. No route/UI/worker invokes grant/refund,
and no enforcement/config/inbound/UUID/expiry behavior changed.

## [x] PH5-04 — Deterministic entitlement engine

**Depends:** PH3-01, PH5-01–03. **Inputs:** plan, packages, admin adjustments/overrides, slot add-ons, period.
**Accept:** one function returns effective expiry/device/WL plus explanation; no username hardcode.
**Tests:** combinations, deductions, unlimited/internal, expired plans.
**Migration:** calculation version pinned to migrated account.

**Implemented and production-deployed (2026-08-27):**
`src/entitlement_engine.py` exposes the sole public path,
`db.entitlements.calculate(account_id=..., now=...)` (also
`calculate_effective_entitlement`). It takes an explicit UTC snapshot time
and opens one SQLite read transaction under the existing DB lock; it performs
no write, period-status advance, network request or Marzban call. The result
is structurally deterministic and versioned as `ph5-04-entitlement-v1`.
It composes, rather than duplicates, the existing canonical models:
immutable pinned `mgboost_plan_versions` and subscription state for real
plan/version/expiry; PH6-04 `compute_parent_wl_pool()` for actual current
period consumption from the PH6-03 WL-node ledger; and PH5-03
`WLPackageStore.package_state()` for its already-approved base-first,
DL-053 FIFO, rollover and freeze/resume bucket remainder. Package SKU/product
version/catalog version are returned from the immutable grant snapshot.

Commercial limits remain exactly 3/6/12 from the real plan. INTERNAL is an
explicit plan model with configurable `LIMITED` or `UNLIMITED` device terms,
never a username rule. Active canonical overrides are returned with durable
ids/mutation/reason/expiry and applied in the existing `(starts_at,id)` order
to effective access/device configuration. Billing and package eligibility are
always facts of the real active commercial plan: a Base `FORCE_ENABLED`
override can expose its effective access state but never makes Base billable
as WL or package-eligible. A stale/expired override is absent and returns to
`AUTO`. The current canonical period is selected without mutating its
`PLANNED/ACTIVE/CLOSED` lifecycle; expired/lapsed entitlement exposes frozen
package history but no fabricated active WL remainder. Decimal units stay
`1 GB = 1_000_000_000 bytes`.

PH5-07 has no durable add-on state, so the contract explicitly returns
`slot_addon_state='NONE'` and `additional_slots=0`. PH6-08 has no durable
adjustment ledger, so it explicitly returns `adjustment_state='NONE'` and
`adjustment_bytes=0`; an existing quota override remains visible as a
configuration component but does not retroactively rewrite an immutable
period/package allocation. No schema migration was required.

**Tests:** new `tests/test_entitlement_engine.py` covers all six commercial
plans; exact 3/6/12 limits; WL/non-WL; 30/60-day periods; base
`<`/`=`/`>` consumption; absent/one/multiple FIFO packages; expiry freeze and
same-WL-plan resume; active/expired overrides; Base `FORCE_ENABLED`; both
INTERNAL device modes; pinned catalog snapshots; stable JSON result for the
same input; no known username literals; and no DB/period-status mutation.
Focused related suite: `98 passed`. Complete regression in four terminal-safe
groups: `1161 passed, 3 skipped` (the three pre-existing environment-dependent
browser skips). Production preflight/verification: `quick_check=ok`, 0 FK
violations; subscriptions/WL periods/package grants/package refunds stayed
`18/0/0/0`; calculation across all 18 accounts returned only
`ph5-04-entitlement-v1`, zero current WL periods and zero package buckets.
Only `mgboost-panel` restarted. No purchase, Stars worker, enforcement,
subscription/expiry/UUID/config/inbound/user-access mutation was introduced.

## [x] PH5-05 — Stars purchase + renewal

**Depends:** PH5-01/04; сохранить текущие payer/currency/amount/CAS/refund/reconcile strengths.
**Scope:** distinguish purchase/renewal, product version, outbox entitlement and child expiry sync. Повторная покупка того же plan всегда renewal; покупка другого plan проходит PH5-06 и не использует stacking.
**Accept/tests:** atomic/idempotent apply; repeated и concurrent successful payments каждого добавляют срок ровно один раз; duplicate callback не даёт double grant; crash/retry восстанавливает единственный apply; mismatches manual-review.
**Migration:** old invoices остаются expire-only snapshots, не переинтерпретируются.

**Implemented and production-deployed (2026-08-27, commit `0d2e354`):**
`src/stars_purchase.py` + additive migration `ph5_05_stars_purchase_v1`
(`src/stars_purchase_schema.py`, requires exact PH3-01/PH5-01/PH3-08
checksums). New durable evidence chain: `mgboost_stars_payment_evidence`
(captured Telegram charge, immutable, no update/delete), `mgboost_stars_
purchase_applications` (one entitlement mutation per invoice, immutable),
`mgboost_stars_purchase_sync_jobs` (PENDING/SYNCED/MANUAL_REVIEW child
expiry sync, reusing the PH3-08 outbox rather than an inline call). Legacy
`stars_invoices` rows are untouched: new columns are additive
(`invoice_kind` defaults `'LEGACY_EXPIRE'`), and
`trg_stars_legacy_invoice_kind_immutable` blocks any legacy row from ever
being reinterpreted as `'CANONICAL_PLAN'`.

**Independent production-verification session (2026-08-27), closing this
entry after the implementing session hit a rate limit before final
docs/commit:** local/origin/production `HEAD` all confirmed `0d2e354`
(fast-forward already applied, no dirty state beyond the pre-existing
untracked `extra_configs.json`). Real production `data/db.sqlite3`
(not the empty repo-root `panel.db` stub) checked directly over SSH:
`quick_check=ok`, 0 FK violations; `mgboost_schema_migrations` carries
`ph5_05_stars_purchase_v1` with checksum
`9ab3bbfda297641a00e087ec76c8efc20315117ce8979de270d35f6fb8c0f724`,
byte-for-byte identical to the checksum the current `src/stars_purchase_
schema.py` computes locally. All three new tables, all `stars_invoices`/
`mgboost_entitlement_state.desired_expire` columns, and all 6 immutability
triggers present and correct. Cardinalities: accounts=18, subscriptions=18,
WL periods=0, package grants=0, package refunds=0 (all unchanged from
PH5-04's own baseline); legacy `stars_invoices` still exactly 2 rows, both
`invoice_kind='LEGACY_EXPIRE'` (zero reinterpreted); the 3 new canonical
PH5-05 tables (payment evidence/applications/sync jobs) are all **0 rows**
-- no purchase flow is wired to any live route yet, so no fictitious
payment/application/grant record exists. All 4 services
(`mgboost-panel`/`mgboost-marzban-broker`/`mgboost-child-worker`/`nginx`)
active; `mgboost-panel` has been running without a single error/exception
in its journal since the last restart (`2026-08-26 22:15:23`, over an hour
uptime at verification time). Unauthenticated `/admin/accounts` and
`/admin/dashboard` still `401`, bogus legacy `/sub/<token>` still `404`.
Targeted regression: `tests/test_stars_purchase.py` + `tests/test_bot_
support_stars.py` = `56 passed`. Full non-browser regression re-run against
the exact checkpoint commit (code identical to what was deployed, so this
was a confirmation run, not a code-change trigger): `1163 passed, 15
deselected` (browser suite deselected since nothing changed since the
last recorded browser-inclusive full run). No real Stars invoice/payment
callback was initiated at any point in this verification.

## [ ] PH5-06 — Upgrade/downgrade engine

**Depends:** PH5-01/04 and admin ticket workflow. **Fixed policy:** OPD-07/08/17/31 and DL-021/022/033 — self-service только upgrade; surcharge = prorated source/target price difference за remaining current period, округлённая вверх до integer Stars. Downgrade только support ticket с preview/reason/audit и explicit выбором лишних devices. Plan purchase/renewal определяется реальным plan и не очищает действующий admin override.
**Accept:** система не выбирает device сама; old/new plan, surcharge или ticket relation audited.
**Tests:** upgrade price boundaries/rounding/replay, 12->3 ticket, WL->Base ticket, concurrent payment. **Rollback:** audited compensating entitlement.

## [ ] PH5-07 — Additional device slot product

**Status:** deferred by DL-017; не входит в текущий product rollout. Пока effective device limit строго равен plan baseline 3/6/12.
**Current dependency:** none — задача deferred и не входит в текущий rollout. **Перед будущей реактивацией:** закрыть deferred OPD-05 и завершить PH3; OPD-06/DL-017 уже фиксируют maximum 99 и повторного решения не требуют. **Fixed:** slot и WL quota независимы; slot даёт lazy child и использует parent pool.
**Accept:** approved price/duration/max и revoke semantics. **Tests:** baseline+addons/max/duplicate/downgrade.
**Rollback:** stop sale; existing version honored.

## [ ] PH5-08 — Billing UX/API/changelog contract

**Depends:** PH5-01–07. **Scope:** одинаковое explanation в bot/admin/LK, plan version, periods/packages/slots, purchase vs renewal, API versioning.
**Tests:** snapshot/API/UI/localization. **Rollback:** compatible versioned endpoint.

## [x] PH5-09 — Manual external-payment record and entitlement application

**Depends:** PH3-09, DL-034–036/040, admin session/audit. RUB catalog data blocker закрыт.
**Actor/channel:** только основной MGBoost admin; account source DIRECT, payment channel `EXTERNAL_PAYMENT`, mutation source `MANUAL_PAYMENT`.
**Store:** immutable plan/version/fixed-price-table version snapshot, exact amount, currency, payment method, external reference/comment, admin actor, timestamp, target account, result/idempotency/reconciliation. Telegram Stars не обязательны.
**Lifecycle:** pending record можно исправить до apply с audit before/after; после apply исходная запись immutable, исправление только append-only compensating operation с reason/reference.
**Accept:** первый rollout принимает только RUB; frontend не inject arbitrary account/plan/price/days/GB; versioned RUB catalog и exact amount проверяются server-side; duplicate reference/action не double-grant. Никаких balance, wholesale, margin или reseller settlement entities.
**Tests:** account IDOR, non-RUB rejection, stale price version, amount/price/plan tamper, pending edit/apply race, applied-record edit denial, compensating operation, negative days/GB, duplicate/replay/reference collision, manual package eligibility/refund, partial remote failure.
**Migration/rollback:** historical facts импортируются только по evidence; unknown amount/channel остаётся unknown, entitlement correction после apply только через compensation.

**Implemented (2026-08-27), implementation-complete / pending independent
review; NOT deployed to production:** additive migration
`ph5_09_manual_payment_v1` (`src/manual_payment_schema.py`, requires exact
PH3-01/PH5-01/PH3-09/PH5-03 checksums) with four tables:
`mgboost_manual_payment_records` (full durable payment contract incl.
snapshots pinned to concrete catalog/plan/duration/price rows,
`expected=recorded` amount CHECK, `currency='RUB'` locked server-side,
UNIQUE external_reference and idempotency-key hash, lifecycle/status plus
review/cancel fields), `mgboost_manual_payment_edits` (append-only
before/after audit for pending field edits, review resolution and
cancellation), `mgboost_manual_payment_applications` (immutable one-per-
record link to the entitlement mutation) and `mgboost_manual_payment_sync_jobs`
(expiry hand-off; not created for packages). Triggers refuse UPDATE of an
APPLIED/CANCELLED record and any UPDATE/DELETE of the audit/application
rows. `ManualPaymentStore` (`src/manual_payment.py`) validates account/
catalog/RUB channel/exact fixed price/duration/admin capability
server-side at create AND at apply; the pinned snapshot is re-validated
against its own referenced rows so a retired or superseded catalog version
remains the contractual price and nothing is ever repriced from a current
table. Pending correction is the only sanctioned modification path; a
compensating-operation engine does not exist anywhere yet, so none was
built -- applied facts simply have no edit/cancel path at all. Wrong
amounts are rejected outright (fail-closed, no ambiguous parking state).
Package purchases ride the existing PH5-03 grant/refund machinery
unchanged via a canonical CONFIRMED EXTERNAL_PAYMENT row in
`mgboost_payment_records`; plan renewals follow the PH5-05 precedent
(this module's own tables carry the evidence chain). Dormant by design:
no admin route/UI/bot wiring, no scheduler. Tests:
`tests/test_manual_payment_ph509.py` (33 checks covering the full matrix
above minus renewal/concurrency items that belong to PH5-10).

**Independently reviewed and production-deployed (2026-08-27):** review of
checkpoint `af1effe` against `a5c846b` confirmed no second entitlement/
renewal/usage/child-sync engine was created (reuses PH5-02/03/04, PH3-08/09
verbatim), the `amount_minor` naming matches the pre-existing repo-wide
convention (whole RUB units, e.g. `169` = 169 ₽ — same as PH5-01's own
`RUB_PRICES`/`mgboost_plan_prices.amount`, not kopecks; PH3-09's provenance
field is named identically and already carries the same convention), and
applied-record immutability without a compensating-operation engine matches
this entry's own explicit v1 scoping. One genuine product ambiguity was
found and resolved by the owner: permanent `external_reference` uniqueness
even after `CANCELLED` was undocumented by any prior DL; **DL-054** now
records the owner's decision (reference stays reserved forever; the
existing table-wide `UNIQUE(external_reference)` is confirmed correct,
equivalent to the codebase's established `UNIQUE(payment_channel,
external_reference)` precedent since this module's channel is invariant).
No code fix was required. Targeted `47 passed`; full non-browser regression
`1210 passed, 16 deselected` (deselection is pre-existing environment-gated
browser cases, not new). Production preflight/deploy: fresh encrypted
backup create/restore `PASS`; all four parent-migration checksums
(`ph3_01_parent_account_v1`, `ph5_01_plan_catalog_v1`, `ph3_09_provenance_v1`,
`ph5_03_wl_package_catalog_v1`) verified byte-identical between local code
and production before deploy; fast-forward `a5c846b` -> `5cbee5c`
(`mgboost-panel` restart only); migration `ph5_09_manual_payment_v1`
self-applied, checksum `e3d453176428cffd73243096fc857b7c89933a4a1ad908cad18cbae151ac7223`
verified identical to what the current source computes; all 4 new tables
and 6 immutability triggers present; `quick_check=ok`, 0 FK violations;
cardinalities unchanged `accounts=18/subscriptions=18/wl_periods=0/
package_grants=0/package_refunds=0`; all 4 new manual-payment tables `0`
rows each (no real manual payment was created); legacy `stars_invoices`
unchanged at `2` rows; all four services active; `mgboost-panel` journal
since restart shows zero errors/tracebacks; safe HTTP smoke unchanged
(`/admin/accounts`/`/admin/dashboard` `401`, bogus legacy `/sub` `404`). No
admin route/UI/bot wiring exists; no real manual or Stars payment was
created or mutated. Final HEAD: local/origin/production all `5cbee5c`.

## [x] PH5-10 — Manual external-payment renewal of the same parent account

**Depends:** PH3-08/09, PH5-04/09, outbox.
**Scope:** admin-confirmed external payment того же plan продлевает existing parent по `max(current_expiry, now) + purchased_duration`, синхронизирует active child expiry, сохраняет slots/HWIDs/current WL period и UUID без revoke причины. Другой plan направляется в PH5-06.
**Accept:** retry cannot create new parent/root user or double-add days; admin actor/payment/reference/channel recorded.
**Tests:** repeated same-duration purchase, concurrent Stars/admin/manual renew, duplicate callback/reference, crash/retry, 12 children, remote partial failure; каждый unique successful payment применяется ровно один раз.
**Rollback:** durable target and reconciliation/compensation; never subtract guessed days or restore stale child state.

**Implemented (2026-08-27) as the application half of the same slice,
implementation-complete / pending independent review; NOT deployed:**
`apply_record()` on a PLAN_PRODUCT record reuses PH5-02's canonical
renewal (`payment_channel='EXTERNAL_PAYMENT'`,
`mutation_source='MANUAL_PAYMENT'`, actor PRIMARY_ADMIN, per-record
durable idempotency key) which updates only the existing subscription row
-- a different real plan fails closed to MANUAL_REVIEW (PH5-06 territory),
admin-granted UNLIMITED can never be overwritten. Every unique successful
payment therefore applies exactly once to the SAME parent account/root
identity: device slots, HWID verifiers/masks, child UUID verifiers and WL
period history are structurally untouched by this path (proven by
byte-equal before/after snapshots in tests), while new WL periods append
contiguously on the UTC-hour-aligned anchor exactly like PH5-05. Crash
between the local commit and this module's bookkeeping converges on retry
through the engines' own keys without a second term; replays after later
independent renewals verify without rolling anything back. Child expiry
sync enqueues the durable PH3-08 hand-off (`run_account_sync_cycle`,
never an inline Marzban loop); partial remote failures stay recoverable
(PENDING/lease) and converge without ever subtracting guessed days.
Concurrent Stars + manual, and multiple simultaneous manual payments,
stack each exactly once under SQLite serialization with CAS backstop.
Verified result conformance against PH5-04 `calculate` after every
scenario. Tests: `tests/test_manual_renewal_ph510.py` (14 checks: 30/60d
stacking, expired/far-future anchors, replay-after-later-renewal,
concurrent Stars+manual and 4-way manual, 0/1/3/12-child topology matrix,
partial-failure recovery, restart/backoff durability, WL-period history).

**Independently reviewed and production-deployed (2026-08-27):** all four
crash boundaries (pre-renewal, renewal-committed/pre-bookkeeping,
bookkeeping-committed/pre-sync-hand-off, partial child sync) traced through
the code and proven to converge without ever adding duration twice, via
deterministic per-record idempotency keys at every layer (renewal engine,
application bookkeeping row, sync job). Replay after a later independent
renewal correctly uses `effective_expiry >= renewal.new_expiry` instead of
exact equality, matching the DL-044/PH5-05 precedent. See PH5-09's own
entry above for the shared review verdict, DL-054 and full production
evidence (same deploy, same commit).

## [~] PH5-11 — First commercial STANDARD signup (self-service DIRECT account + system-owned provisioning template)

**Depends:** PH5-01/02/04/05, PH3-01/02/03/04/08, PH2-01.
**Scope:** первый полноценный commercial signup/purchase flow: Telegram →
«Купить VPN» → BASIC/BASIC_PLUS/BASIC_PRO → 30/60 дней → canonical Stars
invoice → confirmed payment → self-service DIRECT account → subscription →
opaque credential → first device → child Marzban user → рабочий STANDARD
config. Повторная покупка того же plan — существующая PH5-02 renewal
semantics; другой plan — контролируемый отказ (PH5-06 не начат).
**Implemented locally (2026-08-27); independent review (2026-08-28) —
implementation-level defects found and closed with regression tests (see
the review note under PH5-12 below), but the review is APPROVED WITH FIXES
for those defects ONLY. Deploy is BLOCKED pending an owner decision on the
`tpl-<public_id>` per-account provisioning-template architecture: two
independent reviews of this same diff reached opposite conclusions on
whether it is technically required or an avoidable per-customer cost, and
neither is authorized to decide unilaterally (see the PH5-12 review note).
Local checkout has no production/SSH access in this session, so production
HEAD/`OPAQUE_SUBSCRIPTION_ENABLED`/live-topology could NOT be independently
re-verified here; NOT pushed, NOT deployed, canary NOT started.**

- **Additive checksum-pinned migration `ph5_11_commercial_signup_v1`**
  (requires PH3-01 + PH5-05 exact checksums): `mgboost_provisioning_templates`
  (per-account infrastructure template anchor: deterministic
  `tpl-<public_id>` username + pinned exact source-contract hash),
  `mgboost_signup_template_jobs` (durable PENDING/READY/MANUAL_REVIEW retry
  job), and the fill-once trigger `trg_stars_signup_account_fill_once` for
  `CANONICAL_SIGNUP` invoice rows (the nullable `stars_invoices.account_id`
  becomes the binding anchor; any second binding is schema-refused).
- **No account/entitlement before confirmed payment**: the only durable
  pre-payment state is the invoice row itself (kind `CANONICAL_SIGNUP`,
  `account_id` NULL). At `capture_paid` — strictly after money moved — the
  bound signup factory resolves-or-creates exactly ONE DIRECT account for
  the payer (single transaction, fill-once, `PROVEN` Telegram owner link
  `DIRECT_BIND`, PRIMARY alias = template username, review row in the
  existing `mgboost_direct_account_reviews`). Concurrent capture races and
  duplicate charge deliveries converge to one account / one evidence row /
  one application (thread-race tested).
- **Plan gate**: `SELLABLE_STANDARD_PLAN_CODES = (BASIC, BASIC_PLUS,
  BASIC_PRO)` enforced server-side at `create_invoice`,
  `validate_invoice_for_checkout` and `capture_paid` for BOTH purchase
  kinds — WL/EXTENDED/FAMILY are structurally unpurchasable this rollout
  (create raises `PlanNotSellable`; checkout/capture fail closed). The
  renewal menu serves only the sellable subset of the active immutable
  catalog; callback data carries only plan_code+duration — every
  price/name/device count is re-resolved server-side.
- **System-owned provisioning template** (owner-approved direction): the
  account's source-of-contract is an infrastructure Marzban user
  (`tpl-<public_id>`, flow `xtls-rprx-vision` — evidenced against live
  production children 2026-08-27, inbounds == the STANDARD delivery
  profile membership, expire=0, data_limit=None, note-marked
  infrastructure). The anti-tamper source-contract verification is
  preserved verbatim: the worker job re-reads live state through the
  existing broker `legacy.user.get`/`legacy.user.create` ops, computes
  `source_contract_hash`, and pins it once; any remote drift is a
  STOP-class `MANUAL_REVIEW` (`template_contract_drift`), never a silent
  re-pin. The customer never receives the template's UUID or subscription
  URL; every occupied slot provisions its own child user with its own
  Marzban-minted UUID (existing `validate_created_child` guarantees child
  UUID != source UUID).
- **First-device bootstrap**: `resolve_account_device` gains exactly one
  new authority — when an account has zero prior child intents, the pinned
  ACTIVE template row supplies the source contract hash (legacy accounts
  without a template keep the exact prior behavior:
  `PROVISIONING_UNAVAILABLE`). Broker-outage during provisioning stays
  fail-closed and recoverable; the paid entitlement is durable
  independent of the template.
- **Credential delivery**: after a successful CREATE-apply the worker
  delivers the initial opaque credential once (deliver-then-activate
  sequencing; lost delivery leaves a recoverable `PENDING_DELIVERY` and
  converges on retry), never rotates an existing ACTIVE credential
  (pointing at `/newsub` instead), and is gated by
  `OPAQUE_SUBSCRIPTION_ENABLED` (with an admin alert while it is off).
  Renewal purchases keep the plain renewal text.
- **Tests:** `tests/test_commercial_signup.py` (30): exact six-SKU matrix,
  WL/EXTENDED/FAMILY rejection (new + existing paths), personal checkout,
  single-account guarantee under 8-thread capture races, crash durability
  across a real process restart (fresh `Database()` on the same file),
  same-plan renewal / different-plan refusal, template
  happy-path/idempotency/drift/outage/corrupted-membership, first-device
  bootstrap without any legacy dependency, per-slot child/UUID isolation,
  credential create/lost-delivery/no-rotation, and the bot buy UX
  (3 plans → 30/60 → confirm → invoice; tampered callback rejected).
- **Known v1 boundary (documented, feeds a future slice):** a delivery
  profile change applies to templates/children created afterwards; already
  provisioned children keep their pinned membership (admin recovery path:
  Rebind provisions the successor from the current profile). Propagating
  membership changes onto existing children is PH6-adjacent remote-mutation
  territory and deliberately not built here.

## [~] PH5-12 — Operational delivery routing (plan → delivery profile → host membership)

**Depends:** PH0-05, PH3-01, PH6-01.
**Scope:** host membership is operational routing configuration, NOT tariff
data: replacing Germany/Estonia/etc. never requires a new plan version or a
repurchase. STANDARD/WL delivery composition becomes explicit admin-managed
state with a hard backend guarantee that the STANDARD profile can never
contain an exact WL host.
**Implemented locally (2026-08-27); independent review (2026-08-28) —
implementation-level defects APPROVED WITH FIXES (see the review note at
the end of this section), but deploy is BLOCKED pending an owner decision
on the `tpl-<public_id>` architecture question, which two independent
reviews of this diff could not resolve in agreement.**

- **Additive checksum-pinned migration `ph5_12_delivery_routing_v1`:**
  `mgboost_delivery_profiles` (CAS `row_version`),
  `mgboost_delivery_profile_hosts` (immutable membership rows; UPDATE is
  refused outright, DELETE is refused unless a prior `HOST_REMOVED` audit
  event exists in the same transaction), `mgboost_plan_delivery_profiles`
  (BASIC/BASIC_PLUS/BASIC_PRO → STANDARD seeded via
  `scripts/seed_delivery_routing.py`, idempotent + audited), and the
  append-only `mgboost_delivery_profile_events` ledger (no-update/no-delete
  triggers, UNIQUE idempotency-key replay) — the entitlement-mutations
  ledger requires a NOT NULL account_id, which a routing mutation does not
  have, so this is the same-discipline sibling ledger.
- **Backend safety policy (the UI is never the authority):** classification
  is exact `PH0-05` allowlist membership (`wl_topology.WL_INBOUND_TAGS`)
  only; adding an exact-WL tag is structurally rejected
  (`WLHostRejected`); a wl-shaped tag absent from the exact allowlist is
  unverified drift and is likewise rejected fail-closed
  (`WLLikeHostRejected`) — stale/unknown rows are never auto-classified as
  usable WL or STANDARD by name; an addition requires the tag to exist in a
  FRESH live topology observation that passed `require_topology_ok()`
  (the route records the assertion per mutation, the PH6-06 pattern).
  Every mutation: session+CSRF (`require_admin_auth`) + sealed primary-admin
  capability + mandatory reason + idempotency key + route-level
  `expected_row_version` CAS (stale writer gets 409, never a silent
  overwrite).
- **STANDARD cannot physically receive WL — three exact layers:** (1) the
  profile guard refuses WL tags; (2) template creation re-checks the
  membership against the exact allowlist and refuses to pin
  (`wl_tag_in_standard_profile` → MANUAL_REVIEW) even if storage is
  corrupted; (3) the resolver's render-boundary backstop marks a freshly
  ensured child carrying an exact WL inbound as a permanent
  `WL_INBOUND_IN_STANDARD_CHILD` ERROR (new terminal
  `ChildProvisioningStore.fail_permanent` — a poison state must never
  retry), so no body containing WL can ever be served. Beyond these, the
  existing broker contract re-verifies child membership on every credential
  reread/subscription fetch (drift ⇒ fail-closed, never a partial body).
  **Honest limitation:** Marzban subscription lines carry human remarks,
  not inbound tags (verified live 2026-08-27: 32 real lines, remarks are
  display names), so a line-level WL filter is impossible without
  substring matching, which is forbidden — the fail-safe therefore lives at
  the exact-membership layers above, where a corrupted child fails closed
  instead of rendering at all.
- **Admin UX:** new «Роутинг хостов» page (`frontend/assets/admin/
  routing.js`, ES module, CSP-safe): live host inventory from the same
  broker identity (tag + exact classification + membership state),
  add/remove with mandatory-reason confirm dialog + one idempotency key per
  dialog + CAS expectation, WL/wl-shaped rows render a permanently
  disabled action with the server's own refusal reason, plan→profile map
  and the recent immutable audit events. Routes: `GET
  /admin/routing/hosts`, `POST /admin/routing/hosts/{add,remove}`.
- **Tests:** `tests/test_delivery_routing.py` (15): exact-vs-fuzzy
  classification, WL/wl-like/unknown rejections, add/remove roundtrip with
  audit + replay, store-level serialization, corrupted-storage template
  guard, and the route authz matrix (401/403/CSRF/400 WL refusal/409 stale
  CAS/roundtrip).
- **Independent review (2026-08-28) of both PH5-11 and PH5-12 against
  `f228b46..b22e5f8`: verdict APPROVED WITH FIXES for the implementation
  defects below; deploy BLOCKED on the open `tpl-<public_id>` architecture
  question (see the last item).** Real defects found and closed (not
  hypothetical, each with a regression test):
  - **P0** — the Telegram bot layer (`src/bot_support.py::on_pre_checkout`/
    `on_successful_payment`) special-cased only `invoice_kind ==
    "CANONICAL_PLAN"`; a real `CANONICAL_SIGNUP` payment either failed at
    pre-checkout (the placeholder `signup-<tg_id>` "Marzban username" isn't
    a real user) or, if accepted, would have been captured through the
    legacy `mark_invoice_paid` path instead of `capture_paid`, never
    creating an account. The entire commercial signup purchase was
    non-functional end-to-end despite 35 green store-level tests, because
    none of them drove the real dispatcher handlers. Fixed by routing both
    `CANONICAL_PLAN` and `CANONICAL_SIGNUP` through the same canonical
    path; proven with new tests exercising the real handlers directly.
  - **P1** — `CommercialSignupStore.ensure_signup_account` called
    `link_telegram_owner` AFTER releasing the shared process lock; two
    different signup invoices for the same brand-new Telegram payer could
    race into two independently-created orphan accounts. Fixed by keeping
    the owner-link call inside the same locked section as the account
    commit; deterministic repro test
    (`test_owner_link_lock_scope_prevents_orphan_account_race`) forces the
    exact interleaving.
  - **P1** — `scripts/seed_delivery_routing.py` originally shipped a
    hardcoded 13-tag `VERIFIED_STANDARD_BASELINE` tuple ("STANDARD is these
    tags because that's what live topology looked like on 2026-08-27") —
    exactly the eternal-constant anti-pattern the design explicitly forbids.
    Rewritten to derive the baseline from a fresh live topology observation
    every run (`classify_inbound_tag(tag) == "STANDARD"`), fail-closed on
    topology mismatch; regression test includes a brand-new host tag absent
    from the old hardcoded list.
  - **P2** — a failed initial opaque-credential delivery (`_deliver_signup_
    credential`) logged an error but told neither the customer nor the
    admin; the customer had no way to know `/newsub` exists. Added an admin
    alert mirroring the existing `OPAQUE_SUBSCRIPTION_ENABLED=off` pattern.
  - **P3** — `test_first_rollout_purchase_gate_rejects_non_standard_plans`
    passed vacuously (a `plan=` kwarg typo raised `TypeError` before the
    real gate ever ran). Fixed to call with the correct kwarg and assert
    the specific `PlanNotSellable` exception.
  - A minor `DeliveryRoutingStore._replay` read was moved inside the store's
    lock (was reading `self._conn` before acquiring it).
  - **`tpl-<public_id>` architecture verdict: UNRESOLVED — two independent
    reviews of this same diff disagree, and per the review brief neither
    is authorized to decide unilaterally; this is an owner decision.**
    Reading A (technically necessary / "Variant A"): the pre-existing
    (pre-PH5-11) `ChildProvisioningStore.prepare_child_ensure` contract
    hard-requires `source_alias_id` to belong to the SAME `account_id`
    being provisioned (`child_provisioning.py`, the alias lookup is scoped
    `WHERE id=? AND account_id=?`) — with that interface unmodified, a
    single shared template is impossible, so per-account is what the
    current code forces. Reading B (avoidable per-customer cost / "Variant
    B"): that scoping check exists to stop cross-tenant cloning of
    DIFFERENTIATED legacy content (e.g. account A must never clone account
    B's real legacy alias, which might carry WL inbounds) — a system-owned
    STANDARD template carries no differentiated, account-specific content
    at all (every commercial account is entitled to the identical STANDARD
    membership), so sharing it doesn't reintroduce the risk that check
    defends against; the 1:1 requirement is then a consequence of reusing
    the existing per-account alias table rather than adding a small
    system-scoped source path (the same shape as the already-existing
    `system_actor` parameter in `delivery_routing.py`), not a security
    necessity. Reading B is reinforced by a confirmed-by-code fact: the
    account-closure path (`src/account_consolidation.py::close_account`,
    exercised for real by DL-057) has no awareness of
    `mgboost_provisioning_templates` at all, so every closed/absorbed
    commercial account's per-account `tpl-<public_id>` Marzban user is left
    permanently active with no cleanup/reversal policy — a real, not
    hypothetical, operational-debt surface that scales with customer count.
    Both readings agree on everything checkable by code: template UUID/URL
    never reach the customer, `derive_template_username` never accepts an
    arbitrary username, remote drift never silently re-pins, and template
    create-retry converges via get→create→reread. **Recommendation: do not
    treat this as settled either way; put both readings to the owner before
    deploying this slice.**
  - Full regression after fixes: 1396 passed, 0 failed. `git diff --check`
    clean; touched JS/Python compile clean.

# Phase 6 — WL quota

## [x] PH6-01 — Runtime topology allowlist/assertions

**Сделано (2026-08-26, production-deployed):** `src/wl_topology_guard.py` +
`src/wl_topology_guard_schema.py` (additive migration
`ph6_01_wl_topology_guard_v1`). `WLTopologyGuardStore.run_assertion()`
diffs a live observation against PH0-05's exact baseline and records one
immutable append-only row per check in the new
`mgboost_wl_topology_assertions` table (config version, ok/mismatch,
missing/extra tags, missing nodes, field mismatches, timestamp) --
no UPDATE/DELETE ever, mirroring the project's established audit-log
pattern. `require_topology_ok()` is the fail-closed gate a future PH6-06
destructive enforcement action must consult (raises `TopologyMismatchError`
both on an actual mismatch and on "no assertion has ever run" -- never
assumes OK by default). `fetch_live_topology_observation()` wraps the
already-existing read-only `MarzbanClient.get_nodes`/`get_inbounds` calls,
zero new Marzban API surface, zero mutation.
**Not yet wired to any live enforcement path** -- PH6-06 (which does not
exist yet) is the future consumer; running an assertion today is an
on-demand library call, not a scheduled job.
**Accept:** confirmed -- a mismatch is durably recorded and
`require_topology_ok()` blocks (raises) on it; a clean assertion allows.
**Tests:** `tests/test_wl_topology_guard.py` (8 tests: ok/mismatch
recording, append-only enforcement, fail-closed with no prior assertion,
fail-closed after a mismatch, live-payload fetch helper, a real non-WL
node present live never satisfying the WL-node requirement).
**Rollback:** additive-only schema (new table, no existing table touched);
revert is a plain code+migration revert, no data ever depended on for
correctness elsewhere.

## [x] PH6-02 — Immutable WL periods

**Depends:** PH5-02; units/anchor закрыты DL-016/DL-020.
**Fields:** UTC-hour-aligned start/end, decimal quota bytes (GB x 1,000,000,000), source/reason, closed/successor; ADMIN_RESET closes and creates, never rewrites consumed. Subscription expiry хранится отдельно.
**Tests:** two periods for 60d, overlap/gap, UTC/partial-hour/reset.
**Migration:** explicit initial period; no guessed historical usage.

**Gap audit against PH5-02's existing engine (2026-08-26, before doing any
work):** decimal-GB units and full immutability (identity fields + no-delete
triggers) were already correct and already deployed (PH5-02/its own
`wl_period_lifecycle_schema`) -- not rebuilt. Two real gaps against this
entry's own Accept/Fields text: (1) `schedule_wl_period_windows`'s anchor
was exact-second, not UTC-hour-aligned as DL-020 requires; (2) there was no
ADMIN_RESET close+successor mechanism at all. Both closed, in the existing
engine, without a second period engine:
- **UTC-hour alignment:** `subscription_renewal.align_to_utc_hour()` floors
  a timestamp down to the current UTC hour; used only for the WL-period
  anchor passed into the unchanged, still-pure `schedule_wl_period_windows`
  -- the subscription's own `current_expiry`/DL-044 anchor stays
  exact-second, per DL-020's own "subscription expiry хранится отдельно."
  Floor (not ceil) was chosen because every plan duration is a whole
  multiple of 86400s (itself a multiple of 3600s), which makes flooring
  each purchase's own anchor independently always reproduce exact
  contiguity with the previous purchase's own floored period boundary --
  verified both by proof and by
  `test_repeated_equal_duration_purchases_stack_and_periods_keep_incrementing`.
- **ADMIN_RESET close+successor:** new additive
  `src/wl_period_admin_reset_schema.py` (migration
  `ph6_02_wl_period_admin_reset_v1`, requires the exact PH3-01 checksum,
  same pattern as PH5-02's own lifecycle schema) adds
  `mgboost_wl_period_resets` (append-only audit: closed period id, successor
  period id, reason, actor). `src/wl_period_admin_reset.py`'s
  `WLPeriodAdminResetStore.reset_period()` requires the same sealed
  `PrimaryAdminAuthority` capability every other PH3-06+ consequential
  action requires; closes the old period (`status='CLOSED'`, the one
  field PH5-02's own migration docstring already left mutable for exactly
  this), creates a successor with the same account/subscription/term/
  quota, starting at the UTC-hour-floored reset time and ending at the
  **original period's own `ends_at`** (never extends the schedule), refuses
  a period that isn't `PLANNED`/`ACTIVE`, refuses a period already reset,
  refuses a reset at/after the period's own end. "Never rewrites consumed"
  holds by construction, not just by discipline: there is no `consumed`
  column on `mgboost_wl_periods` (that ledger is PH6-03, keyed by period
  id) -- a closed period keeps its own id and any future ledger rows keyed
  to it untouched; only a brand-new period id starts counting.
- Decimal-GB (`GB_DECIMAL = 10**9` in `src/plan_catalog.py`, already
  verified against the PH5-01 catalog) and source/reason for ordinary
  (non-reset) period creation (already fully recoverable via the existing
  `mgboost_wl_periods.subscription_term_id -> mgboost_subscription_terms.
  mutation_id -> mgboost_entitlement_mutations` join, which already carries
  `payment_channel`/`mutation_source`/`actor_type`/`actor_ref`/`reason`)
  needed no new column -- adding one would have duplicated existing
  provenance, not filled a gap.
**Production-deployed 2026-08-26** together with PH0-05/PH6-01 in the same
commit/deploy (`a223f80`); `mgboost_wl_period_resets`=0 rows post-deploy
(dormant, no purchase flow calls `apply_same_plan_purchase` live yet, and
nobody has ever called `reset_period` -- both are already true for
`mgboost_wl_periods` itself since PH5-02's own deploy).
**Tests:** `tests/test_wl_period_admin_reset.py` (6 tests: close+successor
end-to-end incl. UTC-hour-aligned successor start and unchanged `ends_at`,
double-reset refused, already-closed-period refused, capability required,
reset-at-or-after-end refused, append-only audit rows) +
`tests/test_subscription_renewal.py` (2 new: `align_to_utc_hour` boundary
table, a genuine partial-hour purchase producing an hour-floored period
start while the subscription's own anchor stays exact) + 2 existing
PH5-02 integration tests updated for the now-correct hour-floored absolute
values (their own relative/contiguity assertions were already
alignment-agnostic and needed no change).

## [x] PH6-03 — Durable monotonic usage ledger/collector

**Depends:** PH6-01/02. **Require:** unique period/child/node/sample-hour, idempotency, non-decreasing usage, cursor/snapshot, one leader or CAS/shared lock, retry/reconcile.
**Caveat:** Marzban UTC-hour aggregation requires own snapshot/delta or UTC-hour alignment.
**Tests:** duplicate/out-of-order/node reset/two collectors/clock skew/delay.
**Rollback:** pause, retain ledger/cursor, replay idempotently.

**Gap-audit first (2026-08-27), real production Marzban semantics, not assumed:**
read the live Marzban 0.8.4 source over SSH (`app/jobs/record_usages.py`,
`app/db/crud.py`, `app/db/models.py`) before writing any schema. Findings:
Marzban's own scheduler already reads each node's xray-core stats with
`reset=True` (atomically zeroing the in-process counter on every read) and
accumulates the delta into a durable per-(user,node,UTC-hour)
`NodeUserUsage.used_traffic` row plus the user's own cumulative
`used_traffic` column -- so `GET /api/user/{username}/usage?start=&end=`
(`crud.get_user_usages`) is always an *interval sum*, never itself
negative, and a node restart causes only bounded *under*-counting (missed
polls), never a visible decrease. The one real decrease vector is
`POST /api/user/{username}/reset` (admin-triggered only; children never
have `data_limit_reset_strategy` set to anything but `no_reset` --
`child_contract.build_child_payload` always sends `data_limit=None`): it
cascade-deletes (`cascade="all, delete-orphan"` on `User.node_usages`)
*every* historical `NodeUserUsage` row for that user, so a query spanning
through a reset can genuinely report less than an already-ledgered window.
This is why the collector's cursor is a *last observed cumulative total*,
detecting a reset as `cursor_after < cursor_before` rather than ever
computing (and durably recording) a negative delta -- the narrow residual
risk (a reset landing in the still-unconsumed window between two polls
loses that window's real traffic, bounded by the poll interval) is a real,
documented, irreducible limitation of Marzban's own reset semantics, not a
correctness bug; every detected reset is durably recorded
(`mgboost_wl_usage_sample_events.reset_detected`) for operator visibility,
never silently absorbed.

**Attribution, without a new broker surface:** a "child" is a currently-live
`mgboost_child_user_intents` row (`observed_state='ACTIVE'`); its already-
stored `child_username` (PH3-03) is used only transiently, in-memory, to
call the read-only usage endpoint -- never persisted into any PH6-03 table
or logged. The existing `legacy.user.usage` broker operation's
`validate_username()` (`[A-Za-z0-9_.@-]{1,128}`) already accepts every real
`mgc_*` child username, so `ServiceMarzbanClient.get_user_usage()` -- the
same read-only broker path every other usage caller in this codebase
already uses -- worked with zero new broker code. Every WL-period boundary
is exactly UTC-hour aligned (DL-020, `subscription_renewal.
align_to_utc_hour`), so a single UTC-hour sample bucket can never straddle
two periods -- `wl_period_id` attribution (nullable, resolved at collection
time; still `None` everywhere today since no purchase flow creates periods
yet) is therefore unambiguous whenever a period exists.

**Design:** additive migration `ph6_03_wl_usage_ledger_v1`
(`src/wl_usage_ledger_schema.py`), requiring the exact PH3-01/PH3-03-
prerequisite/PH5-02 checksums (three-parent dependency, same pattern
`parent_sync_schema.py`/PH3-08 already uses). `mgboost_wl_usage_cursors`
holds the last observed cumulative per-(child,node) total (mutable by
design -- a reset legitimately decreases it, that's the detection signal).
`mgboost_wl_usage_samples` is a DB-enforced *monotonic non-decreasing*
per-(child,node,UTC-hour) `bytes_delta` accumulator -- a trigger rejects
any UPDATE that would lower it, mirroring the exact extension-only
`mgboost_legacy_grace_periods.current_end_at` precedent, so "never rewrites
consumed" holds at the schema layer, not just by discipline.
`mgboost_wl_usage_sample_events` is fully immutable/append-only, keyed
`UNIQUE(child_intent_id, node_id, cursor_before)` -- this *is* the
idempotency key: replaying an unconsumed cursor state (a crash between the
Marzban read and the commit that would have advanced it, or two collectors
racing on the same stale read) can only ever insert that event once, and a
poll that observed zero new traffic naturally no-ops through the exact
same path (`cursor_after==cursor_before` is just another already-seen
transition). `mgboost_wl_usage_collector_lease` is a single-row (`id=1`)
CAS lease mirroring the PH3-03 `mgboost_outbox` lease shape exactly --
the one-leader mechanism; any number of processes/hosts may race to claim
it, only one wins per window, everyone else's cycle is a no-op skip.
`src/wl_usage_ledger.py`'s `run_collection_cycle()` reuses PH6-01's
`require_topology_ok()` (fails closed if the WL node/tag allowlist isn't
freshly confirmed) and PH6-02/PH5-02's `align_to_utc_hour()` instead of
duplicating either; per-child/per-node Marzban read failures are isolated
(counted, never abort the whole cycle). Storage is plain integer bytes
throughout (decimal, matching `GB_DECIMAL=10**9` elsewhere) -- no unit
conversion happens in the ledger at all. No raw username/UUID/HWID/token
is ever written to any PH6-03 table or printed by
`scripts/run_wl_usage_collector.py` (aggregate JSON summary only: counts,
outcome, safe error *class* names).

Observe/accounting-only: never mutates Marzban, never touches
`mgboost_wl_periods`/subscriptions/entitlements/inbounds, never disables or
resets anyone, never wired to a scheduler -- dormant/on-demand, matching
the PH6-01/02 precedent. PH6-04/06/09 and everything gated on this ledger
remain untouched.

**Tests:** 34 new focused tests (`tests/test_wl_usage_ledger_schema.py` 11,
`tests/test_wl_usage_ledger.py` 23) covering every named scenario: first/
second sample accumulation, negative-value rejection, cross-node/cross-hour
isolation, duplicate stale-cursor-read idempotency ("two collectors"
simulated at the exact CAS boundary), reset decrease detected and never
subtracted, direct-decrease trigger rejection, out-of-order/clock-skew
delayed samples treated as a safe reset, delayed-sample hour bucketing by
processing time, collector-lease exclusivity/expiry-reclaim/release-
ownership, `wl_period_id` attribution (none/covering-window/end-exclusive),
full `run_collection_cycle` (topology-gate fail-closed, live-children-only,
second-cycle-delta-only, second-collector-skips-while-leased, per-child
error isolation, period attribution, lease released after completion,
no child_username/username column anywhere in the ledger). Full regression
via `/tmp/mgboost-wave-a-browser-venv`: **`1115 passed, 0 skipped`** (up
from `1081 passed`, all browser suites included, zero regressions).

**Production-deployed 2026-08-27** (fast-forward `d11005d` -> `ed77b11`,
`mgboost-panel` restart only; additive schema self-applies on `Database()`
construction, zero existing table touched). Fresh encrypted backup create/
restore PASS immediately before deploy; preflight/post-deploy invariants
identical (`quick_check=ok`, 0 FK violations, accounts=18, grace=17,
subscriptions=18, `mgboost_wl_periods`=0, 42 child intents/31
`observed_state='ACTIVE'`), all 4 services active, unauthenticated
`/admin/accounts`/`/admin/dashboard` still `401`, legacy `/sub` bogus-token
still `404`. **Real production observe-only verification, not a dry run:**
a fresh live topology assertion (`fetch_live_topology_observation` +
`wl_topology_guard.run_assertion`, read-only) confirmed `ok=True` against
the current `2026-08-26-v1` config version, then
`python3 -m scripts.run_wl_usage_collector` was run twice, 5 seconds apart,
against the real production DB and real broker: first cycle -- 31 live
children, 62 samples (both WL nodes x 31 children), 0 errors, 0 resets,
real observed totals node 4 (RU ONLY WL) ~64.6MB / node 7 (Selectel)
~5.35GB across 10 children with nonzero traffic; second cycle -- same 31
children, same 62 sample rows (no new UTC-hour buckets), only the 10
children with genuine new traffic produced new (idempotent) event rows
(62->72), byte totals increased by exactly the real observed deltas, the
other 52 (child,node) pairs correctly no-op'd through the identical
duplicate-detection path used for crash/retry safety. `quick_check=ok` and
0 FK violations held after both runs. Collector lease released
(`lease_owner=NULL`, `last_run_outcome='OK'`) after each run.

## [x] PH6-04 — Default shared parent WL pool: accounting/read model only, production-deployed 2026-08-27

**Depends:** PH6-02/03 + children. **Policy:** sum child usage on two WL nodes; at quota disable all children. Family использует один общий 150 GB parent pool; optional advanced per-device allocation идёт через PH6-05, purchased traffic увеличивает этот же parent pool.
**Accept:** 60+20+10 = 90/100; at 100 disable exactly once.
**Tests:** concurrent children, slot changes, unlimited/non-WL. **Rollback:** derive desired from ledger, no consumed edit.

**Done (accounting/read model scope only -- disable-at-quota is PH6-06, explicitly not built here):**
`src/wl_parent_pool.py`, new module, no new schema. `compute_parent_wl_pool()` is
a pure `SUM(bytes_delta)` over the already-durable, already-deduplicated PH6-03
`mgboost_wl_usage_samples` ledger, grouped by `(account_id, wl_period_id)` and
filtered to the exact PH0-05 `WL_NODE_IDS` allowlist -- one accounting path,
zero new tables, exactly matching Rollback's "derive desired from ledger, no
consumed edit". Parent-pool semantics needed no new "family" concept: WL quota
already belongs to `mgboost_wl_periods.account_id`, and every child of that
account contributes regardless of its *current* `observed_state`, so a
revoked/rebound generation's already-ledgered current-period traffic is never
lost (the ledger tables are immutable/append-only by PH6-03's own schema).
`resolve_current_parent_wl_pool()` is the time-aware entrypoint (Non-WL/
UNLIMITED-WL/between-periods/never-purchased accounts all correctly resolve
to `None`, not a fabricated zero).

**Real gap found and closed while building this (not scope creep -- PH6-04's
own pool literally cannot resolve "the current period" without it):**
`wl_period_lifecycle_schema.py`'s own docstring already named the
`PLANNED -> ACTIVE -> CLOSED` state machine but explicitly deferred building
it ("Phase 6's own future runtime concern"); nothing ever actually promoted
a period past `PLANNED`, so PH6-03's own `resolve_active_wl_period` (already
deployed, `status='ACTIVE'` filter) could never attribute a live purchase's
usage to any period. `WLUsageLedgerStore.sync_wl_period_statuses()` (new
method, `src/wl_usage_ledger.py`) is the mechanical, purely time-driven
completion of that already-declared machine -- never a new policy decision:
a period becomes `ACTIVE` the instant its own `starts_at` arrives and
`CLOSED` the instant its own `ends_at` passes, close-before-activate in one
atomic transaction so a period whose window already fully elapsed (a long
collector gap) closes directly without blocking a later sequential period,
and a `CLOSED` period (including one closed early by ADMIN_RESET) is never
revived. Wired into `run_collection_cycle` immediately before its existing
`resolve_active_wl_period` call -- the exact same resolver PH6-03 already
used, never a second one. Zero new tables; only `mgboost_wl_periods.status`,
the one column PH5-02's own immutability trigger deliberately left mutable
for this.

24 new focused tests (`tests/test_wl_usage_ledger.py` +7 for
`sync_wl_period_statuses`: PLANNED->ACTIVE at `starts_at`, ACTIVE->CLOSED at
`ends_at`, a fully-elapsed PLANNED period closing directly, the exact
contiguous two-period boundary in one atomic call, a CLOSED period never
revived, idempotent repeated calls, cross-account isolation;
`tests/test_wl_parent_pool.py` 17: one parent/several children summed, quota
exceeded reported with zero enforcement side effect, one child through both
WL nodes, duplicate ledger observations never double-counted, a revoked
generation keeping its already-consumed current-period traffic, the WL
period boundary never leaking usage, 30d/60d sequential periods never
merging quota, unknown/cross-account period rejected, a Non-WL account and a
never-purchased account both resolving to `None`, the real PLANNED->ACTIVE
gap closed end-to-end through a genuine `run_collection_cycle` call,
concurrency/restart/idempotent recomputation, and zero raw-identifier
leakage / zero Marzban or config mutation of any kind). Full regression:
**`1140 passed, 0 skipped`** (up from `1116`).

**Production deploy (2026-08-27):** application-code-only, no schema
migration (PH6-04 needed none). Fresh encrypted backup create/restore PASS;
preflight/post-deploy invariants identical (`quick_check=ok`, 0 FK
violations, accounts=18, grace=17, subscriptions=18, `mgboost_wl_periods`=0
-- unchanged, since no real purchase flow calls `apply_same_plan_purchase`
live yet), all 4 services active, unauthenticated `/admin/accounts`/
`/admin/dashboard` still `401`, legacy `/sub` bogus-token still `404`.
Real production observe-only verification: a fresh `run_wl_usage_collector`
run (now internally calling `sync_wl_period_statuses` per live child, a
no-op against the real 0-row `mgboost_wl_periods` table) reproduced the same
outcome shape PH6-03's own prior real run already proved (0 errors, 0
resets, idempotent second-cycle no-op), confirming the wired-in sync adds no
observable behavior change for real production data today. `resolve_current_
parent_wl_pool()` against every one of the 18 real production accounts
returned `None` for all of them (zero real WL periods exist yet -- expected,
not a bug) with zero Marzban calls and zero row mutation beyond the
already-existing collector's own ledger writes. Not wired to any admin
route/UI/scheduler -- dormant/on-demand, matching the PH6-01/02/03
precedent; PH6-06 (disable-at-quota enforcement) remains unstarted and
explicitly out of this task's own scope.

## [ ] PH6-05 — Optional manual per-device allocation

**Depends:** PH6-04. **Policy:** consumed monotonic; move only unspent; allocation >= consumed; device cap disables its child only.
**Transition policy (OPD-18):** shared/manual можно переключать в current period; перераспределяется только unspent remainder.
**Tests:** allocated 60/consumed 50 cannot shrink to 10; concurrency/shared-mode transition, repeated transition cannot refund consumption.

## [x] PH6-06 — Exact inbound-only state machine — P2

**Depends:** PH1-05, PH6-01, children.
**States:** `ACTIVE -> DISABLE_PENDING -> DISABLED`; `DISABLED -> ENABLE_PENDING -> ACTIVE`; mismatch `ERROR_RECONCILE`. Local DB holds desired.
**Remote:** reread user, change only `inbounds.vless`, exact WL set, never proxies/UUID/expire/data_limit, then verify.
**Accept/tests:** WL blocked on cached/direct hosts, Non-WL works, offline node/retry/stale object/idempotency.
**Rollback:** compensating desired transition, never blind full-object restore.

**Implemented and fully tested locally (2026-08-27), pending independent
review + production deploy.** `src/wl_enforcement.py` (+ additive checksum-
pinned `src/wl_enforcement_schema.py`, migration `ph6_06_wl_enforcement_v1`)
is the correct machine and the exact remote mutation semantics -- dormant,
on-demand only (`python -m scripts.run_wl_quota_enforcement`), nothing
scheduled, no route/UI/bot wiring, matching the PH6-01..04 precedent.
Per-account machine row (`mgboost_wl_enforcement_states`: epoch monotonic by
trigger, state, last_direction, decision source/period) plus durable per-
(epoch, child) ops (`mgboost_wl_enforcement_ops`, lease/attempts/events
mirroring `mgboost_parent_sync_operations`) -- `UNIQUE(account, epoch,
child)`; every desired-direction change opens a fresh epoch and the
claim-time guard re-checks the stamped epoch against the LIVE row, so a
superseded disable/enable can never be dispatched (anti-stale both ways).
Decisions come ONLY from PH6-04's `resolve_current_parent_wl_pool()`:
LIMITED+exceeded -> EXCLUDED, LIMITED+not-exceeded -> INCLUDED, `None` /
UNLIMITED -> structurally abstain (no state row is even created for
Non-WL/UNLIMITED accounts). Late arrivals (rebind successors / devices
joining mid-suspension) are picked up as missing children of the live epoch
or a re-opened epoch. Remote mutation is ONE new narrow broker op
`child.user.wl.set` (allowlist guard test updated): reread -> identity +
UUID-verifier fail-closed -> target vless set derived from live state plus
the static PH0-05 allowlist only (EXCLUDED = observed − WL tags, refuses to
produce an empty remainder; INCLUDED = (observed − WL) ∪ baseline, where
the baseline is the child's own frozen pre-disable list intersected the
allowlist) -> minimal partial update `{"inbounds": {"vless": target}}` ->
reread/verify membership == target with UUID/status/expire byte-stable.
Convergence is decided from LIVE state (replay settles `ALREADY_IN_SYNC`
with zero writes -- exactly-once by observation, per the a68e265 lesson):
first observation freezes the manifest first-writer-wins, so a crash after
the remote mutation replays against the SAME recorded target and ACKs
exactly once. Full cycle gates on a FRESH PH6-01 assertion +
`require_topology_ok()` BEFORE any decision is opened (unknown/mismatch/
unreachable topology => zero transitions); Marzban/broker failures map to
bounded RETRY (cap 8, then permanent ERROR => account `ERROR_RECONCILE`,
never blind mutation out of error -- recovery is verification-only);
finalization flips terminal state only when every epoch op is APPLIED AND a
fresh independent reread of each touched child matches its frozen target.
`ERROR_RECONCILE` entry points: op exhaustion, REMOTE_MISSING (never
auto-create), NO_BASELINE_FOR_INCLUDE (fail closed), WOULD_REMOVE_ALL_
INBOUNDS (fail closed), live-reread drift. Slot-paused children receive
enforcement uniformly (status stays PH7-05's business); revoked children
are structurally excluded by the same ACTIVE-generation join PH3-08 uses.
**Known limitation (feeds PH6-07/09, documented not hidden):** Marzban
stores our vless target as a persistent `excluded_inbounds` list computed
against the LIVE xray config, so a NEWLY-ADDED WL inbound after a disable
would be auto-included for suspended users until the next enforcement
pass; a fresh topology assertion with changed tags plus PH6-07 periodic
reconciliation own this drift (a new inbound also shows up in
`extra_wl_like_tags` alert evidence).
**Tests:** `tests/test_wl_enforcement.py` -- 31 focused tests covering the
exact brief: exactly-once disable across repeated cycles (byte-exact
snapshots: only inbounds moves), exactly-once restore on reset/new period,
three-epoch flip-flop with all-distinct op ids, superseded stale epoch +
direction-tampered claim guard, topology never-checked/fresh-mismatch/
unreachable/stale-version all blocking with zero transitions, UNLIMITED and
plan-less accounts structurally untouched, partial-offline sibling
isolation to ERROR_RECONCILE, Marzban outage retry→recover without double
mutation, attempt cap, restart between commit and mutation, restart after
remote success before ACK (single mutation, manifest frozen), revoke
exclusion, slot-pause uniformity, rebind-shaped late arrival, absent child
REMOTE_MISSING, remove-all refusal, include-without-baseline refusal,
post-convergence manual drift deliberately NOT auto-repaired (PH6-07
boundary), broker wire negatives (foreign baseline tag, verifier mismatch,
EXCLUDED-with-baseline), schema trigger guards.
**Rollback:** additive-only schema + revert = plain code+migration revert;
`Rollback:` line honored by design (compensating desired transitions are
new epochs; never a blind full-object restore).

**Independent review (2026-08-27, against `14bdbcf..5dabafb`): APPROVED WITH
FIXES, one P0 found and corrected before deploy.** `apply_decision`'s
late-arrival path (a new/rebound child appearing while the account was
already mid-transition, `*_PENDING`/`ERROR_RECONCILE`, same direction as
before) unconditionally opened a FRESH epoch even though the current epoch
still had a genuinely unsettled sibling op (`PENDING`/`RETRY`/expired
`IN_FLIGHT`). Bumping the epoch there made that sibling op permanently
unreachable: `claim()`'s epoch-supersede guard refuses to dispatch an
op whose epoch no longer matches the live state, and `finalize_account`
only ever inspects the CURRENT epoch's ops — so the account could
terminal-flip to `DISABLED` (or back to `ACTIVE`) while a sibling child's
own WL inbound mutation had never actually been applied or verified
remotely, silently masking a partial success as a full one. Reproduced
deterministically with a new regression test before the fix, then fixed to
match the behavior the module's own docstring already promised ("truly
missing children get minted into it" — the SAME epoch, no bump) for the
same-direction/non-terminal case; the epoch bump stays reserved for a
genuine direction flip and for late arrivals discovered after the account
already reached a terminal state (safe there by construction: every op of
a terminal epoch is already `APPLIED`, so nothing outstanding can be
orphaned). New regression test:
`test_late_arrival_mid_transition_never_orphans_a_pending_sibling_op`.
Also removed dead/unreachable code left after a `return` in
`_derive_freeze_and_dispatch` (cosmetic, never executed). No other P0/P1
found across the crash/retry, exact-inbound-mutation, and topology-gate
hotspots; `resolve_current_parent_wl_pool()` is NOT actually called (the
cycle re-derives the identical 3-line sequence inline via
`compute_parent_wl_pool` instead) — behaviorally identical, a P3
duplication worth collapsing later, not a second accounting path.
Verified: targeted suite 32 passed (was 31 + 1 new), full regression 1334
passed (was 1333 + 1 new) via the Playwright venv, `git diff --check`
clean. Production-deployed application-code-only; see the deploy evidence
entry for exact HEAD/service/backup facts.

## [x] PH6-07 — Production WL enforcement runtime: scheduler / reconciliation / drift / backlog — implemented locally 2026-08-28, checkpoint only (NO push, NO deploy)

**Depends:** PH6-06. **Scope narrowed by PH6-06's independent review
(2026-08-27):** PH6-06 already built the durable local-transaction
desired+event writes, the per-op outbox with lease/attempts/manifest-freeze,
and the observe→freeze→dispatch→verify worker loop with bounded retry —
PH6-07 wraps the EXISTING `run_wl_enforcement_cycle`/`WLEnforcementStore`
(the same epoch/op/lease/manifest mechanics), it does NOT reimplement them;
there is no second enforcement engine and no second outbox.

**Done (local checkpoint `ph6-07-wl-runtime` off `4c9d832`):**
- **Scheduler/worker lifecycle** — `mgboost-wl-enforcement.timer` +
  `mgboost-wl-enforcement.service` (hardened oneshot, telemetry-cleanup unit
  shape): one cycle = one bounded invocation of the new orchestrator
  `run_wl_reconciliation_cycle` (`src/wl_reconciliation.py`), shared by the
  timer (`--trigger SCHEDULED`) and manual runs (`--trigger MANUAL`) through
  the single existing entry point `scripts/run_wl_quota_enforcement.py`.
  Overlap forbidden via a non-blocking `flock` cycle lock (concurrent
  timer/manual invocation → `SKIPPED_BUSY`, never queued); crash/restart-safe
  (lock dies with the process; pending work is durable op rows);
  `TimeoutStartSec=900`; clean exit codes; no secrets in argv/logs; pause of
  the timer loses nothing. **Cadence is a technical configurable default
  (15 min) — NOT a product SLA, no maximum-overshoot claim; PH6-09 owns
  overshoot/outage policy.**
- **Periodic reconciliation** — fresh PH6-01 topology assertion per cycle
  (fail-closed blocks the WHOLE cycle before any observation); canonical
  PH6-04 pool via `resolve_current_parent_wl_pool` (the inline 3-line
  duplication in the engine cycle was removed — identical semantics, PH6-06
  suite green); the existing decision/dispatch/finalize pass; then the
  post-terminal drift scan.
- **Post-terminal drift detection + safe repair** (`scan_terminal_drift`) —
  already-terminal accounts are re-observed each cycle (read-only
  `legacy.user.get` + local UUID-verifier check). Exact classification only:
  `WL_PRESENT_WHILE_EXCLUDED` / `WL_MISSING_WHILE_INCLUDED` → repair through
  the EXISTING machinery (`WLEnforcementStore.open_repair_epoch` mints a
  fresh same-direction epoch over ONLY the drifted children; convergence via
  the engine's own `drive_account_ops` — claim guard, manifest freeze,
  exactly-once by observation); `WL_UNEXPECTED_WHILE_INCLUDED` /
  `NON_WL_MEMBERSHIP_LOST` / `REMOTE_MISSING` / `UUID_MISMATCH` /
  `REMOTE_UNREADABLE` → `ERROR_RECONCILE`, ZERO mutation, never auto-create,
  never guess; any flagged finding suppresses repair for that account this
  cycle. Repair requires the fresh canonical decision to still prove the
  frozen direction — no invented entitlements. The documented
  newly-added-WL-inbound gap is closed for operator-APPROVED versioned
  baseline updates (unknown tags still fail closed — PH6-01 contract kept).
- **Backlog/observability** — additive checksum-pinned migration
  `ph6_07_wl_reconciliation_v1`: append-only `mgboost_wl_reconciliation_cycles`
  (heartbeat: outcome/topology/engine summary/drift counters/last error class)
  + `mgboost_wl_reconciliation_drift` (one row per REAL finding) +
  `backlog_snapshot()` identifier-free operator read model. No telemetry DB;
  no UUID/HWID/token/username anywhere.
- **Legacy UNLIMITED / STANDARD invariants intact** — no-signal accounts stay
  structurally invisible (no rows/ops/scan actions); the P0 abstain contract
  held; only LIMITED accounts with a real ACTIVE canonical WL period
  participate.

**Tests:** new suite `tests/test_wl_reconciliation.py` (18 tests): steady-state
zero-write rereads, manual WL re-add repair (exactly once, non-WL byte-stable),
entitled restore / no-entitlement refusal, remote-missing/UUID-mismatch/
non-WL-loss flagging with zero mutations, partial child outage isolation,
newly-added approved WL inbound cleanup, unknown wl-like tag fail-closed
whole-cycle block, legacy-UNLIMITED no-op, crash after repair-epoch before
dispatch, expired-lease reclaim, duplicate-trigger lock, cycles+backlog read
model. Targeted regression (PH6-01..06, P0 hotfix, broker, provisioning):
190 passed. Full regression: see AGENT_HANDOFF.
**Rollback:** `systemctl disable --now mgboost-wl-enforcement.timer` stops the
runtime with zero durable loss; no blind rollback when remote succeeded.
**Deploy acceptance expectation:** on the current production shape (0 ACTIVE
`mgboost_wl_periods`, enforcement tables empty) the post-deploy steady state
must be: timer active, cycles `OK`, remote WL mutations = 0, drift rows = 0 —
until a LIMITED WL canary is deliberately created.
**Independent review (2026-08-28) fixed two real defects before deploy:**
the systemd unit was missing `EnvironmentFile` (broker auth would have
failed every cycle) and `scan_terminal_drift` had a TOCTOU window where a
stale `pool`/`desired` read before per-child network calls could open a
repair epoch against an already-changed entitlement. Both fixed minimally
with regression tests (`tests/test_wl_reconciliation.py`, now 20 tests);
everything else (epoch/CAS guard, baseline-intersection safety, child
observation path, flock/PrivateTmp, UNLIMITED/STANDARD abstain) verified
unchanged by independent code reading and test reproduction. See
`AGENT_HANDOFF.md` for the full verdict and production deploy evidence.

## [ ] PH6-08 — Effective quota/adjustment ledger

**Depends:** PH5-03/04; package lifecycle decisions закрыты. **Breakdown:** base+purchased rollover+admin grant-deduction=effective; consumed/remaining; append-only package buckets/compensations. Base quota расходуется первой. Package remainder переносится через periods, не обнуляется reset, а при expiry/non-WL plan замораживается до возврата WL entitlement.
**Tests:** example 100+50+20-10=160; base-first, multi-period carry, freeze/resume, unused refund/stack; 83 consumed vs 60 effective -> exceeded.
**Rollback:** compensating entry only.

## [x] PH6-09 — Overshoot/outage fail-safe — implemented locally 2026-08-28, checkpoint only (NO push, NO deploy)

**Depends:** PH6-03/07. **Scope:** cadence/headroom, bounded overshoot, DB/Marzban/node outage, fail closed for activate/restore, never global disable on uncertain topology.
**Tests:** each outage/recovery. **Accept:** bound and alerts documented.

**Implemented locally (2026-08-28, checkpoint off `d6afae1`; status stays
checkpoint-only until independent review + owner-authorized deploy).**
PH6-09 does NOT add a second enforcement engine — the existing chain
collector → ledger → parent pool → PH6-06 machine → PH6-07
reconciliation/scheduler stays canonical; PH6-09 adds the fail-safe policy
around it:

- **Collector/enforcement runtime chain (blocker closed):** PH6-03's
  collector had NO scheduler (production-verified: last real run was
  2026-08-26 while enforcement fired every 15 min against a 2-day-old
  ledger). New `mgboost-wl-usage-collector.{service,timer}` run the
  EXISTING canonical `run_collection_cycle` every 10 min (same hardened
  oneshot + `EnvironmentFile` shape as the PH6-07 unit; PH6-03's own
  single-leader CAS lease makes overlap a no-op). No parallel collector.
- **Freshness contract** (`src/wl_freshness.py`): `usage_freshness()`
  reads the collector lease row (`last_run_completed_at` +
  `last_run_outcome`); `fresh = outcome=='OK' AND age <=
  USAGE_FRESHNESS_MAX_AGE_SECONDS` (technical default 1800 s = 3x the
  collector cadence — NOT an SLA). Never-ran / ERROR / PARTIAL are all
  UNKNOWN → not fresh. Topology freshness was already per-cycle (fresh
  PH6-01 assertion, fail-closed); entitlement freshness was already
  re-derived per cycle + immediately before every repair epoch.
- **Two governing invariants:**
  1. *Uncertainty cannot increase WL access* — every access-INCREASING
     action (DISABLED→ACTIVE restore; DL-059 newly-approved WL auto-add)
     requires fresh usage + fresh topology + fresh entitlement; otherwise
     fail closed, 0 mutation, observably counted
     (`accounts_skipped_stale_usage` / `access_increase_blocked`).
  2. *Uncertainty alone cannot mass-disable already-active users* — the
     ledger is monotonic, so stale telemetry can only UNDER-count and can
     never manufacture a fresh `exceeded` proof; EXCLUDED (access-
     decreasing) decisions are deliberately NOT freshness-gated, and a
     collector/node outage never becomes an outage of all WL clients.
- **Overshoot model (demonstrated, not promised):** observed overshoot =
  traffic accumulated between the last trustworthy usage observation and
  successful disable convergence. Demonstrated detection window =
  collector cadence (10 min) + enforcement cadence (15 min) + bounded
  retry (cap 8 × 60 s backoff, per-op, pre-existing). Byte overshoot =
  link rate × window — a temporal bound only, never a byte guarantee.
  Exposed as `overshoot_bounds` in `backlog_snapshot()`.
- **Headroom: none implemented — exact quota threshold kept.** Headroom is
  not needed for correctness (disable converges within the demonstrated
  window after the observed crossing); shrinking the user's purchased GB
  would be a product decision and was NOT taken — options deferred to the
  owner (see PH6-09 final report).
- **DL-059 (owner decision):** ACTIVE LIMITED + newly-approved exact WL
  inbound auto-add through the EXISTING PH6-07 drift path; proven scoping
  via the append-only `mgboost_wl_topology_versions` registry
  (`ph6_09_wl_topology_versions_v1`): a child gains ONLY
  `tags_added_since(<its frozen manifest version>)`; unknown version →
  nothing; unknown/wl-like tags still fail-closed the whole cycle (PH6-01
  unchanged); symmetric DISABLED removal kept; manifests now durably
  record their `topology_version`.
- **Outage matrix** (documented in `docs/PHASE6_09_WL_FAIL_SAFE.md`):
  DB outage → cycle-level bounded failure, durable ops resume idempotently
  (pre-existing epoch/lease machinery, unchanged); broker/Marzban outage →
  RETRY with cap 8 → ERROR_RECONCILE, no retry storm (bounded by timer
  cadence + `next_attempt_at`), frozen manifest replays to
  ALREADY_IN_SYNC after recovery (no duplicate remote effect); WL node /
  collector outage → staleness freezes access-increases only, ZERO is
  distinguished from UNKNOWN (missing child observation is UNKNOWN, never
  counted as 0 traffic); collector stale + already-ACTIVE user → left
  ACTIVE, never mass-disabled.
- **Observability** (identifier-free, extends the PH6-07 read model only):
  `collector_freshness` + `overshoot_bounds` in `backlog_snapshot()`;
  per-cycle `ph6_09` block (usage freshness snapshot,
  `accounts_skipped_stale_usage`, `access_increase_blocked`) inside the
  cycle's `summary_json`; no new UUID/HWID/token/username anywhere.

**Tests:** new suite `tests/test_wl_ph6_09_fail_safe.py` (13 tests, RED
first — 12 failed before implementation): freshness contract (never-ran/
too-old/ERROR/PARTIAL all not fresh), stale restore blocked + counted,
two consecutive outage/recovery cycles then exactly-once restore,
stale-cannot-fabricate-exhaustion (already-ACTIVE survives untouched),
DL-059 auto-add (only the new tag, byte-identical otherwise, replay zero
writes), auto-add blocked while stale, pre-arrived approved tag
legitimate (no ERROR_RECONCILE), symmetric suspended-child removal,
unknown-tag whole-cycle block kept, version registry unknown-version → ∅,
collector units shape, cadence bounds consistency, and a real
`run_collection_cycle` → enforcement chain through the snapshot read
model. Targeted WL suites + P0 hotfix green; full regression see
AGENT_HANDOFF.

**Rollback:** disable `mgboost-wl-usage-collector.timer` + the usual
code revert; the freshness gate then freezes INCLUDED decisions (fail
closed) — it can never cause a mutation by being rolled back.

**Product decisions deliberately NOT taken here (owner STOP items):**
commercial overshoot budget, any headroom that reduces purchased GB,
outage SLA numbers, and the maximum stale-telemetry window as a product
guarantee — the implemented 1800 s freshness bound is a technical
fail-safe, not any of those.

## [ ] PH6-10 — Subscription UX after exhaustion

**Depends:** PH6-06. **Fixed policy:** OPD-11/DL-028 — один informational placeholder, например `🔒 WL исчерпан • сброс <date>`; no `0.0.0.0` enforcement.
**Accept/tests:** placeholder безопасно parses во всех supported clients и объясняет reset; реальные WL hosts не дают доступ, block остаётся Marzban/Xray.
**Rollback:** remove decoration without enabling WL.

## [!] PH6-11 — Reseller-wide WL/package isolation — superseded 2026-08-23

**Причина:** отдельного reseller account/shared pool нет. External payment — канал прямой end-customer subscription.
**Сохранённое правило:** quota/packages/device slots всегда принадлежат конкретному parent account и не зависят от payment channel; это покрывают PH6-02–08. Manual package grant проходит PH5-09 с теми же eligibility/idempotency rules.

# Phase 7 — Admin controls

## [x] PH7-01 — Expiry operations and child sync

**Depends:** PH3-08/outbox. **Ops:** +7/+30/+60, -N, exact date, end now; no WL reset.
**Accept/tests:** preview/reason, all children converge, timezone/concurrent Stars/12 children.
**Rollback:** audited compensating expiry, no raw DB edit.

**Implemented locally (2026-08-27, checkpoint commit pending independent
review + production deploy; status stays `[ ]` until that deploy):** the two
new primary-admin routes (`POST /admin/accounts/{id}/expiry/preview` and
`/adjust`) write exclusively through the new durable
`SubscriptionAdminOpsStore` writer: +N reuses the documented DL-044 anchor
(`max(current_expiry, now)`, so an expired subscription resumes from now), -N
subtracts from the finite expiry, SET_EXACT takes a bounded UTC-epoch second,
END_NOW pins the term to now — one optimistic-CAS update of ONLY
`mgboost_subscriptions.current_expiry` per operation (concurrent Stars/manual
renewals are detected via `row_version`, never silently overwritten), and an
immutable actor/reason/before-after evidence row in the EXISTING PH3-09/PH7-08
ledger (`ADMIN_EXPIRY_ADJUSTMENT`, mutation_source=ADMIN). WL periods, terms
and packages are untouched ("no WL reset"); child convergence rides the
existing PH3-08 `run_account_sync_cycle` (any expiry change bumps the
desired-state revision through `refresh_desired_state`). Admin-granted
UNLIMITED subscriptions and plan-less UNKNOWN_LEGACY subscriptions are
refused. No raw SQL-edit expiry path exists anywhere in the UI or routes.
Idempotent replays return the original audited result with
`already_applied=true`; genuinely new keys are separate compensable
adjustments (the roadmap's "audited compensating expiry" is exactly a
reverse-direction adjustment through the same writer). Tests:
`tests/test_admin_operational_admin.py` covers anchors active/expired,
reduce-into-past child disabling then resume-from-now, END_NOW exactness
with byte-identical WL-period rows, validation matrix, UNLIMITED refusal,
replay/no-duplicate-evidence, compounding grants, sibling-convergence and a
12-children FAMILY-account convergence run.

**Independent review (2026-08-27) verdict: APPROVED, no code defects.**
Read the full `f7ea7f4..dec28f5` diff and the primitives it builds on
(`subscription_admin_ops.py`, `parent_sync.py`, `admin_expiry.py`) line by
line, independently re-derived (not trusted from the self-report) that: the
CAS on `row_version` genuinely rejects a lost update against a concurrent
Stars/manual renewal or a second admin mutation (both paths use the same
`row_version`-guarded UPDATE, confirmed by reading `subscription_renewal.py`
too); WL periods/terms/packages are never touched by this store (only
`current_expiry` is written); the new `ADMIN_EXPIRY_ADJUSTMENT` operation
renders in the existing Audit timeline via the unmodified generic
allow-list projector (no second audit framework); idempotency-key replay
returns the original result without double-applying. One genuine product
ambiguity was found in the companion PH7-05 slice (Rebind-after-Disable,
see PH7-05 below) — resolved by the owner as **DL-056**, no code change
needed. Independently re-ran the full suite after a confirmed `/tmp`
scratch-quota exhaustion (same known class as prior sessions; cleaned only
hour-stale anonymous `/tmp/tmp*` dirs): **1266 passed, 0 failed, 0
skipped**, matching the implementer's own count exactly.

**Production rollout (2026-08-27), following this review's own approval
gates:** fresh encrypted backup create/restore `PASS`
(`scripts/secure_db_backup.py`, default `/var/backups/mgboost`); preflight
confirmed HEAD `f7ea7f4`, `quick_check=ok`, 0 FK violations, cardinalities
`accounts=18/subscriptions=18/manual_payments=0/child_lifecycle_ops=20/
ownership_rebind_ops=0` (all pre-existing), `SLOT_*`/`ADMIN_EXPIRY_
ADJUSTMENT` rows `=0`, all 4 services active; pushed reviewed HEAD
(`78588bc`, the checkpoint plus this review's DL-056/verdict documentation
-- no code changes) to `origin/main`; `git pull --ff-only` on production to
`78588bc`; `systemctl restart mgboost-panel` only (confirmed no schema/
migration diff); post-deploy: `quick_check=ok`, 0 FK violations, every
cardinality above byte-identical, all 4 services active, zero errors/
tracebacks in the `mgboost-panel` journal since restart. Safe HTTP smoke via
the app's real `LISTEN_PORT=8001` (not nginx's `panel.beykus.fun` default
location, which proxies unrelated paths to Marzban's own port 8000 --
the documented gotcha from earlier sessions, re-confirmed, not a new
incident): unauthenticated `/admin/accounts`/`/admin/dashboard` and the two
NEW mutation routes (`POST .../expiry/preview`, `POST .../devices/1/disable`)
all `401`; bogus legacy `/sub` token `404`; all 7 admin JS modules incl. the
new `expiry_ops.js` and `/admin` index `200`. Read-only direct-call
verification (`Database()` + `admin_read_models.account_detail` +
`admin_audit_timeline.account_timeline`, no HTTP session forged, matching
this project's established read-only-verification precedent) against 5 real
production accounts ran without exception and confirmed zero `mgc_`/
`sha256:`/`hmac-sha256:`/`Bearer `/`uuid_verifier`/`hwid_verifier` markers in
any timeline. **No real expiry adjustment, device disable/enable, revoke,
free, rebind or any other mutation was created at any point** -- every
production touch this session was read-only until the reviewed code deploy
itself, and the deploy made zero data-row changes (cardinalities identical
before/after, `SLOT_*`/`ADMIN_EXPIRY_ADJUSTMENT` rows stayed `0`). Final
HEAD: local/origin/production all `78588bc`.

## [ ] PH7-02 — WL quota breakdown

**Depends:** PH6-08. **Display:** base, purchased, grants, deductions, effective, consumed, remaining, period, desired/observed.
**Tests:** 100+50+20-10=160, consumed 83, remaining 77; UI/API/calculator same units.

## [ ] PH7-03 — Administrative GB adjustments

**Depends:** PH6-08. **Ops:** +N/-N, grant package, cancel adjustment, baseline, remove extras, force new period. Never reduce consumed.
**Accept:** 83/60 shows exceeded and disable flow. **Tests:** concurrency/validation/payment link/audit.
**Rollback:** compensating adjustment.

## [ ] PH7-04 — Immutable WL reset

**Depends:** PH6-02. **Scope:** close old `ADMIN_RESET`, create successor consumed=0, preserve ledger.
**Tests:** collector boundary, duplicate request, pending disable. **Correction:** another explicit period action.

## [~] PH7-05 — Device slot administration

**Depends:** Phase 3. **Display:** slot/type, masked HWID/UUID, name, child, dates, traffic/WL, status, desired/observed.
**Ops:** unbind/disable/enable/revoke/free/rebind/add/remove/restore baseline.
**Accept/tests:** old UUID fails all nodes; permissions/stale UUID/partial failure/12 slots.

**Wave B slice (Revoke/Free/Rebind) production-deployed (2026-08-27) after
independent review; still `[~]` because Disable/Enable/add/remove/restore
baseline remain unbuilt, not because the deployed slice itself is broken:**
Revoke / Free / Rebind wired as three distinct authenticated primary-admin
routes (`src/routes/admin_devices.py`; DL-049 granularity preserved) over the
unchanged PH3-05 durable primitives (`prepare_*` + `process_*` +
`DeviceSlotStore.release/rebind`), each with deterministic per-target
idempotency keys so double-clicks/retries converge; remote broker failures
leave a durably scheduled RETRY instead of an orphaned lease; Free refuses
without an APPLIED REVOKE (server-side hard ordering); Rebind additionally
requires explicit `confirm:true` + a new-device HWID and creates the successor
generation (new child provisioning via the existing PH3-03 outbox). Per-slot
action availability is derived server-side from lifecycle tables into
`account_detail.devices[].actions` (`src/admin_read_models.py`) and rendered
as buttons in Devices; confirmed a UI-only hint -- mutation routes
independently re-validate lifecycle state server-side, never trust it.
**Disable/Enable remain explicitly unavailable:** independently re-confirmed
during review (grepped every writer in the schema/lifecycle modules) that no
standalone slot-disable backend primitive exists anywhere, so nothing was
offered rather than inventing a lifecycle; add/remove slots & restore-baseline
stay `[ ]` (PH5-07/PH7-06 territory). Generic delete does not exist (asserted
by test over `_ROUTES`). Confirmation UX: preview consequences list,
mandatory reason, typed acknowledgement checkbox (Rebind adds an extra
compromise acknowledgement).

**Disable/Enable implemented locally (2026-08-27, same pending-review
checkpoint as PH7-01 above):** the previously missing standalone backend
primitive is `src/device_slot_admin.py::DeviceSlotAdminStore` -- the only
writer of the schema-blessed `mgboost_device_slots.desired_state='DISABLED'`
value (already present in the PH3-02 CHECK since the beginning). Convergence
decision comes from the slot's LIVE state read inside the store transaction
(no hash-replay shortcut: Disable -> Enable -> Disable on the same generation
performs a real second pause; this refuses the false-convergence defect class
found in the a68e265 review by design). The pause narrows ONLY its own slot's
child target inside `parent_sync.enqueue_current_children` (a structural
join-level override like the REVOKED exclusion), commits flag+evidence+forced
PH3-08 revision bump in ONE transaction, so no later renewal/expiry/parent
transition can resurrect a paused device, while Enable restores the SAME
generation/child/UUID narrowed by the parent state. Guards scope to the
current generation/intent, not the slot's lifetime. Remote effect stays the
typed `child.user.state.sync`; observed_state aligns only on IN_SYNC, and a
`POST .../devices/{n}/sync` retry route converges crash-window toggles
without inventing mutations. Evidence lands in the existing entitlement-
mutations ledger (`SLOT_DISABLE`/`SLOT_ENABLE` incl. actor/reason/before-
after) and renders in the deployed Audit timeline. Capacity accounting keeps
counting a paused slot (`active_count` unchanged); Free after Revoke works on
a paused slot via a widened release CAS (`ACTIVE|DISABLED`); Rebind consumes
the pause and starts its successor enabled. Mandatory reason + confirm and
UI wiring follow DL-055. Remaining unbuilt for PH7-05: add/remove slots &
restore-baseline (explicitly PH5-07/PH7-06 territory).

**Independent review of the Disable/Enable slice (2026-08-27) verdict:
APPROVED, no code defects.** Read `device_slot_admin.py`,
`admin_devices.py`'s new pause routes, `parent_sync.py`'s per-slot override
and `admin_read_models.py`'s availability projection line by line, plus every
reachable `mgboost_device_slots.desired_state` value against the schema
CHECK, and independently confirmed: convergence is decided from live
in-transaction state only (never a hash-replay shortcut), so Disable ->
Enable -> Disable performs a real second remote disable, matching the
regression test; the CAS guard (`row_version` + `current_generation`) makes
every guard generation-scoped, never slot-lifetime-scoped, closing the
a68e265 P0 class; capacity accounting genuinely keeps counting a paused slot;
Free-after-Revoke's widened CAS (`ACTIVE|DISABLED`) is correct; the per-slot
override in `enqueue_current_children` narrows only the paused child's own
target, verified empirically by
`test_pause_survives_expiry_adjustment_and_later_sync_cycles` (sibling
receives the new expiry, paused child's remote `expire` stays byte-identical
across a real later extension). One genuine product ambiguity found and
escalated rather than silently resolved: `DeviceSlotStore.rebind()` does not
check `desired_state` before unconditionally setting it back to `ACTIVE`, so
Rebind on a currently-DISABLED slot silently drops the pause -- a real
behavior (confirmed reachable and exercised by
`test_rebind_after_disable_successor_starts_enabled_and_stale_enable_refused`)
that no DL/ADMIN-UX text had explicitly ruled on. Put to the owner directly;
resolved as **DL-056** (keep GLM's behavior, no code change). No security,
IDOR, CSRF or secret-leak issue found in the new routes (`disable`/`enable`/
`sync` all POST, all through `require_admin_auth`+`require_primary_capability`
except read-only `sync`'s primary-capability-gated retry, mandatory
reason+confirm per DL-055, no raw HWID/UUID/bearer in evidence JSON --
verified by `test_new_mutations_surface_in_existing_timeline_without_secrets`).
DL-056 resolves the one product ambiguity found (Rebind-after-Disable
consumes the pause; kept as-is, no code change). **Disable/Enable are now
production-deployed** as part of this same review's rollout (see PH7-01's
"Production rollout" entry above for the full evidence -- one push/deploy
covered both families at HEAD `78588bc`); still `[~]` because add/remove
slots & restore-baseline remain unbuilt (PH5-07/PH7-06 territory), not
because Disable/Enable themselves are unverified.

**Independent review (2026-08-27) found and fixed one P0 and one related P1/P2,
both in the same root cause, before deploy:** `_existing_slot_op`
(`src/routes/admin_devices.py`) matched the latest lifecycle op of a kind by
`slot_number` alone, independent of which generation/intent it was recorded
against, while the underlying `ChildLifecycleStore._prepare` primitive was
already correctly scoped by `old_child_intent_id`. Consequences: (1) REVOKE
on a slot's current generation, issued after an earlier generation on the same
slot had already been revoked, matched the OLD generation's APPLIED REVOKE row
and returned `converged: true`/HTTP 200 without ever touching the current
(possibly compromised) generation -- a false confirmation that an active
device had been revoked; (2) REBIND permanently refused any second rebind of
the same slot after the first one ever completed, with no recovery path other
than a direct DB write, contradicting DL-049's "replacement flow" scoping
(nothing limits Rebind to once per slot). Fixed by scoping `_existing_slot_op`
to the current intent's `old_child_intent_id`, matching the primitive's own
idempotency scope; true concurrent double-click safety is unaffected (it was
always provided by `_prepare`'s own idempotency-key-hash dedup, covered by the
pre-existing `test_repeated_rebind_request_is_idempotent_exactly_one_x_plus_1`
in `tests/test_child_lifecycle.py`). Regression added: a legitimate second
REBIND of the same slot succeeds and creates a real third generation; a
REVOKE issued after a REBIND targets and actually revokes the *current*
generation instead of false-converging on the stale one.
Separately, `src/admin_audit_timeline.py` had 7 of its 8 SQL sections with no
exception guard (only manual payments had one), so a single anomalous
evidence row would raise out of `account_timeline()` -- called unconditionally
by `account_detail()` -- and take down the whole account detail page, not
just the Audit tab (P2, fixed with a shared `_rows()` helper; regression
added simulating one corrupted source).
Targeted coverage in `tests/test_admin_operational_admin.py` (24 checks after
the review's additions).

## [ ] PH7-06 — Explicit conflict resolution on limit reduction

**Depends:** PH7-05 and ticket workflow. **Fixed policy:** OPD-07/08 and DL-021/022 — downgrade только через ticket; active 5 -> limit 3 требует явного выбора, no silent automatic choice.
**Tests:** conflict/no conflict/concurrent slot/state changed before confirm.
**Rollback:** explicit new generation only.

## [ ] PH7-07 — WL override AUTO/FORCE_ENABLED/FORCE_DISABLED

**Depends:** PH6-06. **Fixed policy:** OPD-17/36 and DL-033/037 — affects desired only, never history; every FORCE override has mandatory expiry/reason, duration at most 90 days, and atomically returns to AUTO after expiry. FORCE enabled for internal/compensation, disabled independent of remainder. Purchase/renewal eligibility always follows actual plan; purchase/renewal neither clears override nor lets FORCE_ENABLED buy WL packages on a non-WL plan.
**Authority:** unlimited grants only by primary MGBoost admin.
**Tests:** each override with exhausted/available/unlimited, reject >90 days, expiry-to-AUTO, renewal during override, Base+FORCE_ENABLED package rejection, non-primary unlimited denial. **Rollback:** audited return AUTO.

## [~] PH7-08 — Immutable administrative audit trail

**Depends:** PH1-01/PH2-05 actor model. **Fields:** who/when/account/operation/old/new/reason/source/order/correlation.
**Scope:** GB/reset/device/slot/expiry/plan/override/token/migration/revoke/Telegram ownership rebind; rebind stores old/new Telegram ID, primary-admin actor, reason and timestamp; no raw secrets.
**Tests:** complete before/after for success/failure/partial/reconcile, redaction; rejected and successful rebind actor/target assertions; Telegram IDs absent from unrelated logs/exports.
**Migration:** preserve current `audit_log` as legacy evidence.

**Read-side aggregate production-deployed (2026-08-27) after independent
review; still `[~]` because the write-side emit-coverage goal is not this
slice's scope:** `src/admin_audit_timeline.py::account_timeline()` is a pure
read-only join over the already-existing immutable evidence tables (entitlement
mutations, canonical payment records, manual payments + edits/applications +
sync state, child lifecycle operations, migration binding events, grace events,
credential events, ownership-rebind events incl. old/new Telegram IDs) with a
scrubbing layer that structurally excludes token/verifier/HWID/idempotency-key
material and bounds every value; rendered by the new account `Audit` tab
(`timeline.js`). No second audit framework or writer was created — this is the
presentation aggregation the section above scoped, while the complete
write-side emit-coverage goal stays `[ ]` until the remaining operation kinds exist.
As of the 2026-08-27 local checkpoint the two NEW admin mutation families --
expiry ops (PH7-01) and slot pause (PH7-05 Disable/Enable) -- emit durable
actor/reason/before-after/result evidence through this SAME existing ledger
and render in this timeline without any second framework; what still keeps
PH7-11 `[ ]` is the unified emit point covering every future kind (WL
override/reset, refunds/compensations).
Independent review confirmed: not a naive deny-list-over-`**dict` (every SQL
query is an explicit column allow-list; the JSON-flatten path additionally
type-filters to bounded scalars); no live secret (raw bearer token, HWID,
UUID verifier, idempotency key) reaches a real timeline entry against actual
production data (verified read-only against 5 real accounts post-deploy, see
"Production verification" below). One real robustness defect found and fixed
before deploy: 7 of 8 sections had no exception guard, so a single anomalous
row anywhere would have taken down the whole account-detail page, not just
the Audit tab -- every section now degrades independently.

**Independent review (2026-08-27) of the two new write-side families
confirmed:** `ADMIN_EXPIRY_ADJUSTMENT`/`SLOT_DISABLE`/`SLOT_ENABLE` all land
in the unmodified `mgboost_entitlement_mutations` ledger (free-text
`operation` column, no allow-list to fall through), carry actor_ref/reason/
before_json/after_json with only bounded scalars, and are rendered by the
existing generic timeline projector with zero code changes to
`admin_audit_timeline.py` -- confirmed by reading the projector's query (no
operation-kind filtering) and by
`test_new_mutations_surface_in_existing_timeline_without_secrets`. No second
audit framework introduced. **Both families' write-side evidence is now
production-deployed** (HEAD `78588bc`, 2026-08-27; see PH7-01's "Production
rollout" entry); still `[~]` because the unified emit point covering every
future operation kind (PH7-11) remains unbuilt.

## [ ] PH7-09 — Safe plan/entitlement admin

**Depends:** PH5-04/06 and admin ticket workflow. **Fixed policies:** approved OPD/DL decisions referenced by PH5-04/06 and PH7-06/07; no open plan/entitlement-transition decision remains for this task. **Scope:** preview effective change/conflicts/schedule/reasons/confirmations; no raw counters.
**Tests:** plan matrix, invalid transition, concurrent payment. **Rollback:** compensation with snapshot.

## [x] PH7-10 — Manual external-payment admin UI

**Depends:** PH3-09, PH5-09; только основной admin.
**Account UI:** payment channel, plan, expiry, WL/devices; first rollout фиксирует RUB и позволяет выбрать только versioned fixed RUB price, exact amount/method/reference/comment с preview entitlement change. Pending можно исправить до apply; applied record read-only, correction создаётся отдельной compensation action.
**No separate reseller UI/login.** Raw subscription bearer, UUID и full HWID не нужны для payment operation.
**Controls:** backend проверяет admin session, target account и fixed catalog; confirmation/reason mandatory.
**Tests:** account IDOR, masked fields, non-RUB/manipulated plan/price/days/GB, pending edit/apply race, applied edit denial, duplicate reference and confirmation.

**Production-deployed (2026-08-27) after independent review (no fix needed --
this endpoint group reviewed clean, 3 non-blocking P3 notes only, see below):**
authenticated primary-admin HTTP surface
(`src/routes/admin_payments.py` + `admin_support.py`, registered in
`src/server.py`) over the already-deployed PH5-09/10 `ManualPaymentStore`
verbatim -- no payment logic in routes: `GET /admin/manual-payment-catalog`
(server-provided RUB products only), `POST /admin/accounts/{id}/manual-payments
/preview|create-flow` (preview computes same-plan purchasability + exact price
+ DL-044 expiry estimate via PH5-04/PH5-02 read paths; other-plan targets are
shown as explicitly blocked with reason `PLAN_SWITCH_REQUIRES_PH5_06`,
UNLIMITED admin grants blocked, WL packages eligibility-gated by the real plan),
plus per-record edit (min-8-char reason → append-only before/after),
cancel, resolve-review, apply and sync-retry endpoints. Apply drives child
convergence through the existing PH3-08 `run_account_sync_cycle` exactly like
PH5-05's Stars driver (`pending_sync_jobs`+`record_sync_result` mapping,
worker_id `admin-manual-ph5-09`) and returns old/new expiry + PH5-04
entitlement summary; every mutation requires session+CSRF AND the sealed
primary-admin capability; bodies bounded; IDOR-safe 404s; double-submit
converges via the store's durable idempotency key; DL-054 forever-reserved
references enforced (409 reuse). Vanilla-JS UI (`frontend/assets/admin/
payments.js` + Payments tab in `accounts.js`, no framework): server-catalog
product grid with purchasable=false explanations, server preview panel,
bounded create form minting one idempotency key per wizard, pending card with
Edit/Cancel/Apply dialogs (apply shows immutable warning + result panel with
old→new expiry, entitlement, child-sync state), cancelled-reference warning;
payment rows show sync badge for applied records. Blockers honest: non-
purchasable products render disabled with reasons instead of silently going
to MANUAL_REVIEW. Targeted tests: `tests/test_admin_operational_admin.py`
(22 checks incl. exact DL-040 price table, manipulated price/duration/product/
account rejection, duplicate-submit convergence, edit/cancel/apply/immutability,
drift→MANUAL_REVIEW→resolve, package grant, DL-054 reuse 409, auth matrix,
no raw secrets in list/detail/timeline payloads). Independent review confirmed:
no second billing/payment engine (routes are auth/validation/orchestration
only over the existing PH5-09/10/PH5-02/03 stores); child-sync mapping in the
apply route (`_drive_child_sync_once`) is a byte-for-byte structural copy of
the already production-reviewed PH5-05 `stars.py::_sync_canonical_purchase_
children` state mapping (`SYNCED` only on `aggregate_state=="IN_SYNC"`, never
on PENDING/PARTIAL/0-children); DL-054 reuse enforced at the store layer with
no route-level bypass; same-plan vs. other-plan correctly gated by
`PLAN_SWITCH_REQUIRES_PH5_06`, never silently reinterpreted as a tariff
change. Three non-blocking P3 notes (not fixed, tracked for the future
PH7-11/compensation slice): `handle_manual_payment_sync`'s docstring implies
it can retry a `MANUAL_REVIEW` job, but `pending_sync_jobs()` only ever
returns `PENDING` (same inherited property as the already-deployed PH5-05
driver, not a new defect); the sync-retry route has no UI button yet
(backend-only in this slice); one route-level test accepts either
`SYNCED`/`PENDING` as passing rather than forcing deterministic convergence.
Deploy is application-code-only (no schema migration).

## [ ] PH7-11 — Immutable manual-payment mutation audit

**Depends:** PH7-08, PH5-09.
**Sources:** `SYSTEM`, `DIRECT_PURCHASE`, `MANUAL_PAYMENT`, `ADMIN`, `MIGRATION`, `PACKAGE`, `INTERNAL`.
**Fields:** admin actor, end account, operation, old/new, reason, amount/currency/method/reference, timestamp, result, idempotency and retry/reconciliation state.
**Scope:** manual create/link, renew, plan, package, expiry, refund/correction and failed/denied attempts.
**Accept/tests:** every mutation/reconciliation emits correlated append-only events; raw security credentials excluded; audit editable only through compensating event, never reseller API.
**Note (2026-08-27):** the mutation events themselves already exist durably
via the deployed stores (`mgboost_manual_payment_edits/_applications`,
`mgboost_entitlement_mutations`+links, lifecycle/rebind/credential/grace event
tables) and are surfaced read-only by the new `Audit` tab aggregate — but a
correlated *unified write-side* audit framework (one emit point covering every
mutation kind incl. future refund/correction ops) is NOT built here; PH7-11
stays `[ ]`.

## Admin panel redesign — owner-approved architecture/UX decisions

A read-only audit (2026-08-26, HEAD `8b73843`) of the current admin
frontend/backend found it is still entirely Marzban-username-centric with
zero UI for the parent-account/migration/grace/opaque-credential/device-slot
domain, even though that backend is fully implemented and live. The full
audit findings, target navigation, read-model plan and implementation waves
are recorded in `docs/ADMIN_PANEL_REDESIGN.md`. Five owner decisions from
that session are recorded as `DL-048`..`DL-052` (technical-identifier depth,
PH7-05 Wave B mutation granularity, legacy Users cutover, Dashboard
priority, frontend technology direction). **This subsection and
`docs/ADMIN_PANEL_REDESIGN.md` are design/architecture only — no PH7-01..11
status above changes to `[~]`/`[x]`; implementation progress is tracked by
the separate Wave tasks below.**
The approved implementation order is Wave A (foundation + current-state
read-only UI, can start immediately) → Wave B (PH7-01/05/08 safe mutations)
→ Wave C (PH5-backed) → Wave D (PH6-backed); see
`docs/ADMIN_PANEL_REDESIGN.md` §6 for exact scope per wave.

## [~] PH7-12 — Wave A account-centric admin foundation

**Depends:** PH2–PH4 read models and DL-048..052. **Scope:** approved Wave A
from `docs/ADMIN_PANEL_REDESIGN.md` §6; it does not start PH4-06 and does not
add Wave B mutations.

**Implemented in the first completed slice (2026-08-26):** new authenticated
read-only `GET /admin/accounts`, `/admin/accounts/{id}`,
`/admin/migration-grace`, `/admin/dashboard` presentation APIs;
`AccountSummary`/`AccountDetail`/device/subscription/dashboard aggregation;
Migration/Grace classification wraps the canonical
`account_grace_snapshot()` + `classify_action()` directly. New vanilla-JS
ES modules (`frontend/assets/admin/core.js`, `admin/accounts.js`) provide the
primary Accounts list, Account tabs (Overview/Subscription/Devices/Telegram-
Ownership/Migration-Grace/Technical), standalone Migration/Grace view and
DL-051 Dashboard. Technical identifiers are rendered only in Technical;
normal device rows expose masked identifiers and explicitly separate active
slots/parent readiness from real migration lineages. The production-proven
opaque credential issue/reissue route is surfaced with reason, explicit
rotation confirmation and one-time URL display. Legacy Users moved out of
top-level customer navigation to `System / Technical / Marzban Raw Users`;
Tickets/Nodes/Extra configs/Stars/Settings remain reachable and responsive.

**Evidence:** focused account/read-route/frontend/security regression
`24 passed, 1 skipped` before the temporary browser runtime was installed;
the real headless-Chromium CSP/XSS/Technical-visibility/480px-responsive gate
then passed, and the final full project regression with all browser suites is
`1023 passed` (zero skips).
Fresh production DB-copy compatibility gate assembled and JSON-serialized all
18 accounts/details, reported active grace, `quick_check=ok`, 0 FK failures
and unchanged account cardinality. Fresh read-only cohort report: 17 members,
actions `OK_MIGRATED=8`/`WAITING_FOR_REGISTRATION=9`, Telegram
`BOUND=4`/`UNREGISTERED=13`, active slots 27, real migrated lineages 15.

**Production deployed:** encrypted backup create/restore PASS; production
fast-forwarded `8b73843` -> `e5e2e21`, only `mgboost-panel` restarted. All four
services active; new page/modules/CSS return `200` with correct MIME; new API
routes return `401` without a session; `quick_check=ok`, 0 FK failures,
accounts/grace rows unchanged at 18/17, `LEGACY_REVOKED=0`. The first localhost
curl landed in the short restart window (`connection refused`); the immediate
repeat and every later public/localhost gate passed after the listener was up.

**Corrective UX slice (2026-08-26, owner manual walkthrough follow-up):**
after the first Wave A deploy above, an owner manual walkthrough found
several remaining rough edges: English labels leaking into an otherwise
Russian UI, the Marzban per-user `note` (the human-readable name owners
actually recognize) not shown anywhere, no way to filter internal/service
test accounts out of the Accounts list, the Telegram/real-device migration
summary using an inconsistent denominator, "active devices" not
distinguishing a proven genesis/bootstrap placeholder from a real customer
device, and the Technical tab rendering raw identifiers as one unlabeled
string. This slice fixed all of these read-only/presentation issues:
- `humanLabel()` (`frontend/assets/admin/core.js`) is a single reusable
  enum->Russian-label map; all remaining account-centric English labels
  (tabs, navigation, badges, Stars status) now render through it or a
  direct Russian string.
- The Marzban `note` field is fetched read-only with the admin's own
  session JWT and surfaced as `display_identity`
  (`src/admin_read_models.py::_display_identity`) — explicitly
  **presentation-only**: it is never written back, never used for
  account/alias/ownership linkage, and Accounts search matches note,
  every alias, and public ID. A `presentation_metadata_available` flag
  and a visible amber notice make a Marzban-fetch failure fail open to
  the canonical alias/public-ID label instead of failing the page.
- `_is_technical_account()` hides only accounts that are simultaneously
  `account_source=INTERNAL`, have **no** grace-cohort row, and have a
  reviewed-account evidence record with `ownership_evidence=ABSENT` and a
  structured `purpose` field — no username/account-id text ever
  participates, and any real grace-cohort member is always shown
  regardless of source. An explicit "Показывать служебные аккаунты"
  toggle overrides the default hide, both in Accounts and Migration/Grace.
- `Migration/Grace` and the Dashboard now report `parent_ready`, the
  Telegram cohort breakdown (denominator = the actual grace cohort size,
  not `classify_action()`'s output), `accounts_with_real_lineage` /
  `accounts_without_real_lineage`, an absolute `total_real_lineages`
  count, and `active_slots` as clearly separate numbers — `active_slots`
  is explicitly labeled as possibly including genesis/bootstrap.
- `src/legacy_grace_migration.py::is_genesis_hwid_verifier()` is exact,
  keyed (`hmac.compare_digest` over the same `privacy_safe_hwid()` HMAC
  every real device verifier uses) proof that a slot's HWID verifier is
  the canonical synthetic genesis value — never inferred from an absent
  migration lineage. Devices now shows a "Служебный bootstrap" badge only
  for a slot that passes this exact check.
- Grace progress gained a fixed-boundary Day X/14, elapsed/remaining
  percent and exact end timestamp (`_grace_progress()`), shown as a
  progress bar in both the account detail and the Dashboard.
- The Technical tab now renders each raw identifier
  (`slot_generation_id`, `child_intent_id`, HWID/credential verifier,
  outbox id/operation/state, generation state) as its own labeled,
  individually-copyable field, with historical (non-`ACTIVE`) generations
  collapsed behind `<details>` and only the current generation open by
  default.
- Fixed one real bug the browser gate caught: `metadataWarning()`
  returned a raw `''` instead of `` html`` `` when no warning applied,
  violating this project's own SafeMarkup discipline — corrected before
  any production deploy.

Evidence: full regression `1029 passed, 0 skipped` (was `1026 passed, 3
skipped` — the 3 prior skips were only the browser suites, unavailable
until Playwright/Chromium was installed into a temporary venv). Production
preflight immediately before this deploy: `quick_check=ok`, 0 FK
violations, accounts=18, grace rows=17 (`PH4-05-MASS-COHORT-2026-08-26`,
unchanged start/end), `LEGACY_REVOKED=0`, all 4 services active. Fresh
encrypted backup create/restore PASS taken immediately before deploy.
Deployed `955f255` -> `76daec6`, fast-forward pull + `mgboost-panel`
restart only; post-deploy re-verification: same DB invariants unchanged,
static ES modules/CSS `200` with correct MIME, new API routes still `401`
unauthenticated. Verified via unauthenticated API/static checks, DB-level
invariants and the dedicated CSP/XSS/search/technical-visibility browser
gate (against realistic fixtures matching the exact new response shapes)
— **not** yet via an interactive authenticated click-through, since this
session had no Marzban admin login credentials and did not attempt to
obtain or guess any; that specific check is the owner's own next step (see
`AGENT_HANDOFF.md`).

**Devices read-only client-evidence addendum (2026-08-26, small follow-up,
does not close PH7-12):** owner asked, before final Wave A sign-off, to see
the actual device/VPN client in Devices, not just slot/technical state.
Added `known_client_devices` to `account_detail()`
(`src/admin_read_models.py`) sourced from the already-existing, continuously
-updated `user_devices` table (populated by `Database.check_device_access`
on every real legacy `/sub/{token}` hit -- the same path every currently
-migrated account's real traffic still runs through): device name (the
admin's own renamed `display_name` if set, else the reported model),
humanized OS/platform, humanized VPN client name (`Happ`/`v2rayTun`/`INCY`
casing for the three the owner named explicitly; anything else shown
exactly as captured, never guessed) + version, last activity. Only active
(`is_active=1`) rows are surfaced. Shown in Devices as its own card block,
explicitly separate from the existing slot/state table -- the two use
different, non-comparable technical identifiers (device-slot HWID verifier
vs. legacy `user_devices.request_key`) with no shared key to prove a 1:1
match, so this session deliberately did not fabricate a per-slot pairing.
A genesis/bootstrap placeholder slot never sends a real HTTP request, so it
structurally can never appear in this list (not a filter, an inherent
property of where the data comes from) -- keeping genesis/bootstrap and
real customer devices explicitly distinct, as asked. No raw HWID/UUID/
request-key exposed; read-only, no PH7-05 mutation started. 1 new focused
test (`tests/test_admin_account_read_models.py`); full regression `1039
passed` (browser suites included). Deployed together with PH5-01 below in
the same `6414a59` production deploy -- see that entry for backup/restart/
invariant evidence, unchanged here.

**Remaining before Wave A `[x]`:** (1) an owner-performed (or
owner-authorized, credentialed) interactive authenticated
Dashboard/Accounts/Devices/Migration/Technical/mobile-viewport
click-through, confirming the semantics read the same live as they do in
the fixture-driven browser gate; (2) finish splitting the legacy
monolithic screen code out of `admin.js` into per-domain ES modules, then
an equivalent-functionality review of the preserved legacy screens. This
is a completed, production-deployed slice, not a claim that all of Wave A
is finished.

**Operational-admin completion slice (2026-08-27, pending independent
review; NOT deployed):** account detail extended with the authoritative
PH5-04 `entitlement` block (`Overview` renders status/expiry/plan/device/WL
consumption/packages/overrides read-only), recent canonical payments,
legacy Stars invoice summary and the unified timeline; Dashboard gained
actionable operator queues (`queues`: manual payments PENDING / MANUAL_REVIEW
/ sync-pending + child-sync pending count + Stars manual-review count, each
row deep-linking to the Payments tab; no fabricated monitoring state — every
queue reads an existing durable store). Credential issue/reissue flow kept as-is inside Subscription with its rotation warning. Telegram
ownership rebind form added to the Telegram tab per OPD-39/DL-041. New
frontend ES modules: `modals.js` (reusable two-step consequence dialogs),
`payments.js`, `device_ops.js`, `timeline.js`; monolithic `admin.js`
split-out remains the open Wave A item above.

## [x] PH7-13 — Account consolidation (merge/supersession)

**Depends:** PH3-01 (account schema), PH4-01 (legacy bridge), PH3-05 (child lifecycle), PH4-03 (legacy-compat entitlement). **Scope:** merge two already-independent parent accounts that are the same real person into one canonical survivor, without ever mutating or physically moving either account's existing history.
**Trigger:** owner-approved consolidation `MegochelPC` (account 5) + `MegochelAndroid` (account 6) -> `Megochel`, per DL-057.
**Why no existing primitive covers this:** `mgboost_legacy_alias_groups` is a 1:1 `account_id PRIMARY KEY` table -- a multi-alias group is only ever assembled once, at bootstrap (`legacy_grace_migration.migrate_bootstrapped_account`); `mgboost_legacy_account_aliases.legacy_username` is globally `UNIQUE` and the table is fully immutable (no `UPDATE`, no `DELETE`), so an existing alias can never be moved to a different account. Every other account-scoped table (`mgboost_subscriptions`, `mgboost_device_slot_generations`, `mgboost_child_user_intents`, `mgboost_entitlement_mutations`, `mgboost_legacy_bridge_bindings`, `mgboost_legacy_grace_periods`, `mgboost_wl_usage_samples`) treats `account_id` as part of an immutable identity, enforced by triggers or by construction. Reassigning history across accounts is therefore structurally impossible; a new, purely additive supersession layer was required.
**Implemented/staging verified 2026-08-27:** new checksum-pinned `src/account_consolidation_schema.py` (`ph7_13_account_consolidation_v1`, depends on the exact PH3-01 schema): `mgboost_account_merges` (`absorbed_account_id UNIQUE`, `survivor_account_id`, `status IN ('ACTIVE','REVERSED')`, `decision_ref`, immutable identity columns via trigger, no-delete) + `mgboost_account_merge_events` (append-only `CREATED`/`REVERSED` log, mirroring the existing `mgboost_legacy_bridge_bindings`/`_binding_events` precedent) + `mgboost_account_display_names` (append-only, `ux_..._active_display_name` partial-unique index enforcing at most one active label per account, modeled on `mgboost_telegram_identities`'s revoke-and-reinsert pattern -- unrelated to any legacy alias).
`src/account_consolidation.py` (plain functions taking `db`, mirroring `legacy_paid_compat.py`'s cross-store orchestration style, not a single-table store class): `resolve_account_id()` is the one shared canonicalizer every resolver calls; `create_merge()` requires the absorbed account already `CLOSED` and the survivor `ACTIVE`, and permanently forbids self-merge and any chain/cycle via a strict bipartition (neither id may ever have played the other role anywhere in the table, even across a later reversal) -- replay of an identical pair is idempotent, a different survivor for an already-merged absorbed account is a hard `MergeConflict`; `reverse_merge()` never deletes the merge row, only appends a `REVERSED` event and CAS-flips `status` -- idempotent, and deliberately does NOT reopen the account or resurrect its revoked child/generation (PH3-05's own rollback policy: a new generation, never a restored leaked UUID); `close_account()` fails closed on an active Telegram OWNER identity, a non-terminal child intent, or an ACTIVE device slot generation, then cancels any live subscription (with immutable `mgboost_entitlement_mutations` evidence) *before* flipping the account to `CLOSED` -- `ProvenanceStore.record_mutation()` itself hard-refuses evidence for an already-CLOSED account, so the ordering is load-bearing, not stylistic; `reopen_account()` is the reversal counterpart, refused while an `ACTIVE` merge still points at the account; `set_display_name()` is a purely cosmetic, owner-set label, refused on a non-`ACTIVE` account.
`legacy_paid_compat.increase_device_limit()`: `ensure_legacy_paid_compat_entitlement()` only ever bootstraps a brand-new entitlement (idempotent replay on an exact match, hard `SubscriptionConflict` on any existing different plan) -- it has no path to raise an already-live subscription's device limit, and `subscription_renewal.apply_purchase()` requires a billed commercial plan and explicitly refuses a plan change (`PlanMismatch`, "PH5-06 upgrade/downgrade policy, not implemented"). The new function is the one canonical way to change `current_plan_version_id` (device limit only) on an *already-provisioned* `LEGACY_PAID_COMPAT_V1_D{n}` subscription in place -- single-field optimistic-CAS `UPDATE`, mirroring `subscription_admin_ops.py`'s surgical-adjustment discipline; never touches expiry/status/WL semantics; never creates a second subscription row; refuses any billed/commercial or non-legacy-compat plan outright, and refuses a decrease (a distinct, not-yet-implemented decision).
**Resolver-coverage audit (not limited to the three obviously-named paths):** `legacy_bridge.py::resolve_account_for_legacy_username()` (the sole username->account resolver behind the dormant `/sub` bridge and `migration_lifecycle.py`'s lineage recorder) now canonicalizes through an `ACTIVE` merge before returning -- a real device reconnecting on the absorbed legacy username lands on the survivor's slot pool, never on the closed account, with the alias/binding rows themselves completely untouched. A genuine, previously-unflagged gap was found and fixed: `legacy_grace_registration.bind_telegram_after_registration()`/`resolve_ambiguous_telegram_ownership()` resolved an alias's raw `account_id` and called `AccountStore.link_telegram_owner()` directly, which raises `AccountSchemaError` ("account not found or closed") for a CLOSED absorbed account -- an exception these functions did not catch (only `IdentityConflict` was handled), so a real customer typing the absorbed username into the bot would have hit an unhandled error instead of the correct `ALREADY_BOUND`/`CONFLICT` outcome against the survivor. Both now canonicalize via `resolve_account_id()` first. `subscription_admin_ops.py` (PH7-01 expiry ops) had no account-status check at all -- `preview()`/`apply_adjustment()` now explicitly refuse a CLOSED account's subscription. Verified already-safe without any change, by inspection: `direct_enrollment.py`'s pre-account-creation alias-conflict guard (blocks re-enrolling an absorbed username under a new account regardless of status); `device_slots.py::_entitlement_capacity()` (hard-requires `account_status='ACTIVE'`, so any device claim/rebind on a CLOSED account already fails `EntitlementUnavailable`); `manual_payment.py`/`entitlement_engine.py` (already check `status!='CLOSED'`); `device_slot_admin.py` Disable/Enable (operates only on an `ACTIVE` generation, none of which survive a correctly-sequenced close); Stars/WL purchase paths (resolve strictly via `telegram_id -> account`, and the absorbed account here never had a Telegram identity to resolve through).
`admin_read_models.py`/`accounts.js`: `display_name` (when set) now outranks the existing per-alias `display_note`/`primary_alias`/`public_id` fallback chain everywhere an account's human-facing title is shown, without changing behavior for any account that has none; `account_detail()` gained a new `consolidation` block (`absorbed_into`/`absorbs`) surfacing PH7-13 merge state on both sides.
**Tests:** 34 new focused tests in `tests/test_account_consolidation.py` -- schema/checksum/trigger immutability; `resolve_account_id()` passthrough and canonicalization; the legacy-bridge and Telegram-grace-registration resolver fixes end to end; the PH7-01 CLOSED-account guard; display-name fallback/idempotent-set/change/non-ACTIVE-refusal, visible through `account_detail()`/`account_summaries()`; close preconditions (active owner / non-terminal child / active generation, each independently); the canonical genesis-child Revoke->Free sequence before a successful close; merge apply replay, append-only reversal + reopen, both idempotent; self-merge, absorbed-not-closed, survivor-not-active, conflicting-survivor and both chain/cycle directions all rejected with the correct typed error; concurrent `create_merge()` (8 real threads) converging to exactly one row; the exact optimistic-CAS clause proven directly against a stale `row_version` for both the merge and subscription tables; the exact D3->D6 transition (same subscription id, unchanged expiry/status/WL, exactly one live subscription, idempotent replay, decrease refused, non-legacy-compat/commercial plan refused, missing-evidence refused); survivor's Telegram identity/opaque credential/subscription id byte-for-byte unchanged across the whole sequence; absorbed account's pre-existing alias/child-intent/device-generation row counts unchanged (only new evidence rows are added, which is the correct, expected behavior -- not a literal frozen total); a completely unrelated third account provably untouched. Full regression: `1298 passed, 4 skipped` (was `1264 passed, 4 skipped` -- zero regressions).
**Independent re-verification of `9edd42e`** (a separately-authored, already-implemented device-migration-status GLM bugfix, checked in this same session before building on top of it): `classify_action()`'s `sum(migration_state.values()) > 0` branch (after the `ERROR_RECONCILE` guard) correctly yields `OK_MIGRATED` for any real `mgboost_migration_bindings` lineage regardless of Telegram status, and the renamed `WAITING_FIRST_DEVICE` fallback (no longer named after Telegram) fires only on zero lineage -- Telegram ownership never gates or is gated by technical migration status. Not modified; both mandatory regression cases (`tests/test_admin_account_read_models.py`) reconfirmed passing.
**Production completed 2026-08-27:** fresh encrypted backup (`mgboost-db-20260827T114917Z.tar.gpg`, `--verify` PASS) preceded a fast-forward deploy of `d5ed3b7` (`9b38c91..d5ed3b7`, includes `9edd42e` and this PH7-13 work in one restart), `mgboost-panel` restarted (no other service needed: it is the only process loading the admin/bot code paths this work touches), `ph7_13_account_consolidation_v1` applied automatically on startup, `quick_check=ok`/`foreign_key_check`=0 rows. GLM migration-status fix independently re-verified against real accounts via `scripts/ph4_05_daily_cohort_report.py`: account 6 (`BOUND`, real lineage after an organic device migration that happened between analysis and rollout) -> `OK_MIGRATED`; account 5 (`UNREGISTERED`, zero lineage) -> `WAITING_FIRST_DEVICE`. A fresh read-only re-check of accounts 5/6 immediately before mutating found one new fact versus the original analysis -- account 6's real Android device had organically migrated onto a second real slot/child (`mgc_efwxdfyhmimnyb3dh37gaj3tl4`) in the interim -- which the merge plan already required zero changes for and none were made.
Executed via a new, reviewed, hardcoded-target script (`scripts/dl057_megochel_consolidation.py`, iterated twice in production after two real bugs it exposed in itself -- a 15-character `idempotency_key` one byte under the 16-minimum, and a preflight that mis-required the genesis child to still be "non-terminal" and so couldn't resume after the already-applied `REVOKE` -- both fixed and redeployed before the ordering-guaranteed re-run completed cleanly; the underlying primitives' own idempotency made every retry safe, including the real Marzban `REVOKE` never re-rotating): `REVOKE` (`lc_twdq6bl3hi2scoy2rq2kpumuj4`, `APPLIED`, real Marzban genesis child `mgc_2uipd27le766vdd7lt4kjpj5pi` disabled + UUID rotated) -> `FREE` (`APPLIED`, slot 1/generation 1 -> `RELEASED`, slot -> `FREE`) -> `close_account(5)` (subscription id 5 -> `CANCELLED`, account 5 -> `CLOSED`) -> `create_merge(absorbed=5, survivor=6)` (`mgboost_account_merges` id 1, `ACTIVE`, `decision_ref='DL-057-megochel-consolidation-2026-08-27'`) -> `set_display_name(6, 'Megochel')` -> `increase_device_limit(6, +3)` (subscription id 6 unchanged, plan -> `LEGACY_PAID_COMPAT_V1_D6`, `device_limit=6`, `wl_mode` still `UNLIMITED`, `current_expiry` still `NULL`).
Post-mutation evidence, all read-only against the live DB: account 5 `CLOSED`/subscription `CANCELLED`/generation 19 `RELEASED`/slot `FREE`/zero `ACTIVE` generations anywhere for account 5; exactly one `mgboost_account_merges` row (`5->6`, `ACTIVE`) with exactly one `CREATED` event; account 6 unchanged `ACTIVE` status, unchanged Telegram identity row (id 6, `telegram_id=1623120036`, still not revoked), unchanged opaque credential (id 9, same `generation=1`/`row_version=2`/`last_used_at`), exactly one live subscription (id 6, same row, now `LEGACY_PAID_COMPAT_V1_D6`), new `display_name='Megochel'`; both `mgboost_legacy_account_aliases` rows (`MegochelPC`->5, `MegochelAndroid`->6) byte-for-byte unchanged; `db.legacy_bridge.resolve_account_for_legacy_username('MegochelPC')` and `resolve_account_id(db, 5)` both now return `6`; both real legacy Marzban users (`MegochelPC` id 4, `MegochelAndroid` id 5) confirmed `active` with traffic still accruing normally, completely untouched by this operation; unrelated account 2 (`DISABLED`, pre-existing PH3-08 canary) and the other 16 `ACTIVE` accounts unchanged (18 total, as before); all 5 services active, zero errors/tracebacks/5xx in `mgboost-panel` logs across the whole operation; `admin_read_models.account_detail()`/`account_summaries()` confirmed showing `display_name='Megochel'` for account 6 and the new `consolidation` block correctly cross-referencing both sides. Local/origin/production all at `d5ed3b7`.

# Phase 8 — Hardening and scale

## [ ] PH8-01 — Production-safe bounded HTTP concurrency — P2

**Depends:** PH2-03, PH3-02, PH6-03, PH8-02.
**Scope:** replace single-thread HTTPServer, deadlines/body limits/graceful shutdown/backpressure. Нельзя просто включить workers поверх process-local state.
**Accept/tests:** slow upstream/request не блокирует canary; slowloris/load/soak/graceful restart.

## [ ] PH8-02 — Shared concurrency/datastore readiness

**Scope:** nonce/rate/session/device capacity/outbox/per-account locks/collector ownership; решить SQLite vs other DB на фактах.
**Accept/tests:** correctness across processes/restart/lock timeout/duplicate delivery.
**Migration/rollback:** dual flow только с reconciliation и verified backup.

## [ ] PH8-03 — Reproducible dependencies/images — P3

**Scope:** controlled Marzban upgrade, image digest, lock/hashes, SBOM, secret scan, reproducible build, fail placeholder secrets.
**Tests:** clean build/lock/staging contract. **Rollback:** previous immutable image and DB-compatible release.

## [ ] PH8-04 — Monitoring/alerting

**Metrics:** auth/rate failures, collector lag, monotonicity, outbox age, desired/observed mismatch, node usage stop, migration errors. No raw token/HWID/UUID/password.
**Accept/tests:** actionable runbooks + synthetic alert/redaction drills.

## [ ] PH8-05 — Backup/restore/disaster drills

**Scope:** encrypted minimum-access backups, regular DB-backup retention 90 days, consistent account/ledger/outbox/audit restore, post-leak rotation; legacy token-evidence quarantine is a separate single encrypted snapshot retained 180 days under DL-042.
**Accept/tests:** isolated restore, measured RPO/RTO, no raw bearer in new backup/output, quarantine access limited to minimum owner/service identity, controlled expiry deletion; credentials present in retained backup are independently rotated.

## [ ] PH8-06 — OpenRouter/support privacy

**Scope:** minimal/redacted context, bounded args, SQL user filter before LIMIT, retention/provider policy, no extra operational IDs.
**Tests:** prompt injection, negative/huge LIMIT, cross-user, outbound capture/provider outage.

## [ ] PH8-07 — Continuous security regression suite

**Scope:** XSS/auth/IDOR/CSRF/bearer canary/replay/rates/permissions/dependencies/child revoke/WL cached-direct/log redaction.
**Accept:** CI/deploy gate; waiver only dated Decision Log/security reason.

## [ ] PH8-08 — Operational runbooks

**Scope:** deploy/rollback, credential rotation, migration, failed payment, orphan child, WL reconcile/topology, logs, outage/incidents.
**Accept/tests:** second operator performs staging/tabletop drill without source archaeology.

## [ ] PH8-09 — Manual-payment reconciliation and monitoring

**Depends:** PH3-09, PH5-09/10.
**Scope:** reconcile external payment record/outbox with account entitlement and child expiry; alert orphan payment reference, duplicate/replay, failed apply and mutation backlog.
**Accept/tests:** DB/Marzban failure boundaries, duplicate admin submission, correction/refund dispute; account/devices/traffic history preserved.
**Runbook:** подтверждение перевода, mistaken target, failed apply, duplicate reference, manual correction/refund and reconciliation.

# Cross-phase dependencies

    PH1 emergency security
      -> PH2 token/session/broker foundation
      -> PH3 parent account + child devices
      -> PH4 legacy bridge/canary
      -> PH5 entitlements/billing
      -> PH6 WL ledger/enforcement
      -> PH7 admin controls
      -> PH8 scale

Hard blockers:

- PH1-01 blocks admin expansion.
- PH1-06 + PH2-01/07 block new subscription rollout.
- PH2-05 and PH3-04 implementation/security tests block ownership recovery/rebind activation; OPD-39/DL-041 now fix the product policy and are not blockers.
- PH3-09 + PH5-09/10 block structured manual/external-payment rollout; reseller self-service не существует.
- PH3-02/03/05 block real per-device revoke/allocation.
- PH0-05/PH6-01 closed 2026-08-26; inbound removal is no longer blocked by an unversioned topology.
- PH6-06/07 block WL enforcement/sales (PH6-02/03/04 closed 2026-08-26/27; the durable ledger and shared-pool sum PH5-03's rollover/refund semantics need now exist and are production-verified -- PH5-03 itself is no longer blocked by missing Phase 6 infrastructure).
- PH5-01/03/04/05 and payment reconciliation block package sales; OPD-02/03/04/12/13/32 policies are already closed and are not blockers.
- PH4-05/06 plus successful migration verification block final legacy revoke; OPD-09 already fixes the grace period at 14 days and is not a blocker.
- PH2-03/PH3-02 block multi-worker; PH6-03's own single-leader CAS lease (`mgboost_wl_usage_collector_lease`) is already safe for multiple collector processes/hosts, so it is no longer part of this blocker.

Resolved or deferred product gates — not current blockers:

- OPD-05 is DEFERRED together with PH5-07; extra-slot sales are outside current rollout. If reactivated, price/duration bundles must be decided then.
- OPD-06 is CLOSED: current paid limits are 3/6/12 and future maximum is 99; PH5-07 does not await further OPD-06 finalization.
- OPD-07/08 are CLOSED: self-service downgrade is absent and ticket/admin requires explicit device selection.
- OPD-31 is CLOSED: upgrade pricing no longer blocks design but still requires calculation/rounding tests.
- Package lifecycle/accounting/refund OPDs are CLOSED; implementation dependencies remain PH5/PH6 tasks above.
- Reseller-only OPD-20–30 are SUPERSEDED; they create no dependency. OPD-33–35 and RUB catalog v1 are CLOSED and no longer block PH5-09.
- OPD-39 is CLOSED: first-rollout Telegram ownership recovery is primary-admin-only; self-service recovery/codes remain outside scope and create no current dependency.

Resolved operational gate — not a current decision blocker:

- DL-042 fixes PH1-06 log/backup/quarantine retention and cleanup prerequisites. Remaining PH1-06 blockers are implementation and verification work, not an outstanding retention decision.

# Open Product Decisions

Status semantics: `CLOSED` — решение принято; `SUPERSEDED` — сохранено только как история и не создаёт dependency; `DEFERRED` — не входит в текущий rollout и становится blocker только при явной реактивации соответствующей функции; `PARTIAL` — часть policy утверждена, но остался блокирующий вопрос. Пункт без status не выбран. «Рекомендация» — предложение, не утверждённое решение.

## OPD-01 — Decimal GB или GiB — CLOSED 2026-08-23

- A: 1 GB = 1,000,000,000 bytes — понятнее продуктово. **Рекомендация.**
- B: 1 GiB = 1,073,741,824 bytes — привычнее части технических UI, даёт на 7.4% больше.
- **Выбрано пользователем:** A, decimal GB. Все quota/package/UI значения GB переводятся в bytes через множитель 1,000,000,000.

## OPD-02 — Lifetime купленного WL package — CLOSED 2026-08-23

- A: до конца текущего 30-day WL period — проще и прозрачно. **Рекомендация.**
- B: отдельные 30 дней от покупки — сложные overlapping buckets.
- C: пока не израсходован, но не дольше subscription — долгие liabilities.
- **Выбрано пользователем:** account-level package remainder переносится через WL periods; при expiry/lapse или переходе на plan без WL он замораживается, не расходуется и снова становится доступен после renewal/возврата на WL-enabled plan.

## OPD-03 — Сгорает ли remainder package — CLOSED 2026-08-23

- A: сгорает вместе с period. **Рекомендация при OPD-02A.**
- B: переносится в следующий period — нужен carry bucket/expiry.
- **Выбрано пользователем:** B с расширением — remainder переносится не только в один, а в последующие WL periods.

## OPD-04 — Rollover дополнительного traffic — CLOSED 2026-08-23

- A: rollover запрещён.
- B: только один следующий period, oldest-first. **Рекомендация, если rollover нужен.**
- C: бессрочно до конца subscription.
- **Выбрано пользователем:** multi-period rollover до использования; package bucket не обнуляется при смене 30-day WL period. Subscription-lifecycle boundary остаётся в OPD-02.

## OPD-05 — Цена дополнительных device slots — DEFERRED 2026-08-23

- Не утверждена. Перед PH5-07 предложить 2–4 price/duration bundles с unit economics.
- Нельзя использовать тестовую цену как default.
- **Отложено пользователем:** покупка дополнительных slots не входит в текущий rollout; действуют только тарифные limits 3/6/12. Это не блокирует Phase 3–6.

## OPD-06 — Максимальный общий device limit — CLOSED 2026-08-23

- A: 12 включая add-ons — проще support/capacity. **Предварительная рекомендация.**
- B: 20, как current admin validator; это не делает лимит утверждённым.
- C: более высокий cap только internal.
- D: предложено пользователем — архитектурный максимум 99.
- **Выбрано пользователем:** будущий commercial/technical cap равен 99; INTERNAL сохраняет configurable/unlimited entitlement из DL-011. В текущем product scope покупка дополнительных slots не запускается, effective paid limit строго равен тарифу 3/6/12; PH5-07 отложена.

## OPD-07 — Downgrade timing — CLOSED 2026-08-23

- A: в конце оплаченного period — меньше разрушений. **Рекомендация.**
- B: немедленно без proration.
- C: немедленно с credit/proration — сложнее Stars accounting.
- D: self-service downgrade отсутствует; downgrade возможен только через support ticket.
- **Выбрано пользователем:** D. Автоматический downgrade/proration не реализуется; операторское изменение проходит через ticket, preview, reason и audit.

## OPD-08 — 12 active devices при downgrade до 3 — CLOSED 2026-08-23

- A: user/admin выбирает 9 отключаемых до apply. **Рекомендация.**
- B: downgrade scheduled до выбора.
- C: automatic LRU — риск отключить важный device, только отдельным решением.
- **Выбрано пользователем:** A. В support ticket пользователь/оператор явно выбирает отключаемые devices до применения downgrade; автоматического выбора нет.

## OPD-09 — Legacy migration grace period — CLOSED 2026-08-23

- A: 7 дней — быстрее закрывает leak, больше support.
- B: 14 дней — баланс. **Рекомендация.**
- C: 30 дней — мягче, но дольше живёт shared credential.
- **Выбрано пользователем:** B, 14 дней.

## OPD-10 — Child user delete policy — CLOSED 2026-08-23

- A: revoke/disable + tombstone, физическое удаление после retention. **Рекомендация.**
- B: немедленно delete — хуже audit/reconcile.
- C: никогда не delete — растёт Marzban DB.
- **Выбрано пользователем:** A. Credential отзывается немедленно, tombstone/history сохраняются; срок физического удаления определяется operational retention policy.

## OPD-11 — UX после WL exhaustion — CLOSED 2026-08-23

- A: полностью скрыть WL.
- B: один informational placeholder с reset date. **Рекомендация.**
- C: показать disabled entries; зависит от clients.
- **Выбрано пользователем:** B. Resolver показывает один безопасный informational placeholder со статусом и датой reset; enforcement остаётся в Marzban/Xray.

## OPD-12 — WL package на Base как implicit upgrade — CLOSED 2026-08-23

- A: запрещён, сначала explicit upgrade. **Рекомендация и текущая граница.**
- B: package временно включает WL — новый продукт с отдельной ценой/сроком.
- **Выбрано пользователем:** A. Покупка/использование package доступна только при WL-enabled plan; сохранённый remainder на Base заморожен.

## OPD-13 — Refund/cancellation WL package — CLOSED 2026-08-23

- A: refund только если package не использован, adjustment отзывается atomically. **Рекомендация.**
- B: proportional unused refund — сложнее accounting.
- C: non-refundable после grant — требуется явное предупреждение.
- **Выбрано пользователем:** A. Полный refund возможен только для package bucket с нулевым consumption; частичный refund не выполняется.

## OPD-14 — Family pool/sub-pools — CLOSED 2026-08-23

- A: один общий 150 GB pool. **Рекомендация/default.**
- B: optional manual per-device allocations.
- C: member sub-pools — требует member identities/roles, не только devices.
- **Выбрано пользователем:** один общий 150 GB parent pool по умолчанию, с optional advanced allocation по devices и возможностью докупать WL traffic в тот же parent pool. Отдельные member identities/sub-pools сейчас не вводятся.

## OPD-15 — Internal/god account policy — CLOSED 2026-08-23

- A: internal plan versions + explicit override с expiry/reason. **Рекомендация.**
- B: только per-account overrides.
- **Выбрано пользователем:** A. Используются versioned internal plans и explicit per-account overrides с обязательными expiry/reason. Unlimited может выдавать только основной MGBoost admin; second approval сейчас не требуется.

## OPD-16 — WL period anchor/timezone — CLOSED 2026-08-23

- A: rolling 30x24h от activation UTC — точный срок, сложнее partial-hour usage.
- B: UTC-hour aligned — технически проще ledger. **Рекомендация.**
- C: calendar dates в timezone пользователя — понятнее UX, сложнее DST/timezones.
- **Выбрано пользователем:** B, UTC-hour aligned WL accounting periods. Subscription expiry хранится отдельно; конкретное правило округления/показа границы должно быть одинаковым в schema, UI и tests.

## OPD-17 — Tariff behavior при admin override — CLOSED 2026-08-23

- A: override имеет expiry, затем AUTO. **Рекомендация.**
- B: бессрочно до отмены.
- **Выбрано пользователем:** A. Каждый override имеет обязательный expiry и затем автоматически возвращается в AUTO. Billing/package eligibility всегда определяется реальным тарифом; purchase/renewal не очищает действующий override и не превращает FORCE_ENABLED в скрытый WL entitlement.

## OPD-18 — Shared/manual allocation transitions — CLOSED 2026-08-23

- A: manual распределяет current unspent; обратно объединяется только unspent. **Рекомендация.**
- B: режим меняется только с нового period — проще, менее гибко.
- **Выбрано пользователем:** A. Переключение возможно в current period, но consumed остаётся монотонным и перераспределяется только unspent remainder.

## OPD-19 — Catalog/final price для external payment — CLOSED 2026-08-23

- A: reseller получает явный allowlist plan/package и сам задаёт конечную цену в разрешённых bounds. **Рекомендация для гибкости при server-side bounds.**
- B: allowlist есть, но конечная цена фиксирована MGBoost.
- C: отдельный сокращённый reseller catalog с собственными SKU.
- **Выбрано пользователем:** B по смыслу manual payment. MGBoost определяет catalog и fixed final price; admin/payer не передаёт arbitrary plan/price. Реализация currency/price tables зафиксирована OPD-33/DL-034.

## OPD-20 — Prepaid inventory или оплата каждой activation — CLOSED 2026-08-23

- A: reseller заранее покупает inventory/credits.
- B: начисление за каждую activation/renewal. **Рекомендация для простого immutable order ledger без reservation inventory.**
- C: гибрид по reseller contract.
- **Выбрано пользователем:** отдельного reseller/inventory нет. End user сначала платит MGBoost внешним способом, затем основной admin подтверждает payment record и применяет renewal к тому же account.

## OPD-21 — Нужен ли reseller balance — CLOSED 2026-08-23

- A: prepaid balance с atomic reserve/capture/release.
- B: balance отсутствует, каждый order оплачивается отдельно.
- C: постоплатный credit limit/debt.
- **Рекомендация:** B для первого rollout; A только при доказанной операционной необходимости.
- **Выбрано пользователем:** B; reseller balance не существует.

## OPD-22 — Закупочная цена и margin — SUPERSEDED 2026-08-23

- A: fixed wholesale price per versioned SKU. **Рекомендация.**
- B: процент от retail/final price.
- C: индивидуальные reseller tiers/contracts.
- Нельзя вычислять историческую margin задним числом без transaction evidence.
- **Причина:** отдельного reseller/wholesale/margin нет; пользователь платит непосредственно MGBoost.

## OPD-23 — Reseller WL package pricing и получатель денег — SUPERSEDED 2026-08-23

- A: отдельная fixed wholesale package table; reseller определяет final price в approved bounds. **Рекомендация.**
- B: reseller платит direct retail price и margin отсутствует.
- C: revenue share.
- Отдельно определить, кто получает customer payment и кто несёт refund.
- **Причина:** отдельного reseller нет. External package payment получает MGBoost; fixed external price/currency определяется OPD-33, refund выполняет admin по DL-027.

## OPD-24 — Кто оплачивает дополнительные device slots — SUPERSEDED 2026-08-23

- A: reseller покупает add-on у MGBoost и выставляет свою final price.
- B: end customer платит MGBoost напрямую.
- C: доступны оба канала с одним entitlement ledger. **Рекомендация при наличии mixed billing.**
- Цена direct и reseller пока не утверждена.
- **Причина:** add-on slots отложены, reseller channel отсутствует; будущую цену решает OPD-05.

## OPD-25 — Кто инициирует manual-payment refund/cancellation — CLOSED 2026-08-23

- A: reseller в пределах своих orders/capability, MGBoost выполняет/reconciles. **Рекомендация.**
- B: только MGBoost admin.
- C: end customer может запросить, reseller/admin подтверждает.
- Нужны правила уже consumed package/used days/device add-on.
- **Выбрано пользователем:** только основной MGBoost admin выполняет mutation/refund; end user может обратиться через ticket, но не применяет refund сам.

## OPD-26 — Reseller debt/block и новые/существующие entitlements — SUPERSEDED 2026-08-23

- A: блокировать только новые mutations, существующие работают до expiry. **Предварительная рекомендация.**
- B: freeze renewals/packages, но не network access.
- C: при debt отключать существующих customers — высокий риск, только явное решение.
- Этот вопрос связан, но не заменяет lifecycle policy OPD-29.
- **Причина:** reseller identity/balance/debt отсутствуют.

## OPD-27 — Переход reseller customer на DIRECT billing — SUPERSEDED 2026-08-23

- A: разрешить transfer того же parent account с сохранением devices/traffic/history и новым entitlement source segment. **Рекомендация.**
- B: разрешить только после expiry.
- C: запретить transfer.
- Нужно определить settlement/refund старого reseller и доступ к историческим данным после transfer.
- **Причина:** external-payment customer уже является DIRECT account; меняется только payment channel.

## OPD-28 — Shared reseller WL pool как отдельный продукт — SUPERSEDED 2026-08-23

- A: не предлагать; quota всегда per end subscription. **Рекомендация для initial rollout.**
- B: optional purchased reseller pool с явным allocation ledger.
- C: dynamic shared pool без allocations — высокий риск cross-customer starvation.
- Ни один existing reseller не получает shared pool автоматически.
- **Причина:** reseller account отсутствует; quota всегда per end account.

## OPD-29 — Что происходит с customers при disable/expiry/compromise/delete reseller — SUPERSEDED 2026-08-23

- A: существующие subscriptions работают до expiry; reseller mutations блокируются. **Рекомендация.**
- B: subscriptions замораживаются.
- C: customers переводятся под DIRECT MGBoost с сохранением account/history.
- D: иная policy по событию.
- Credential compromise всегда немедленно отзывает reseller access, но не определяет customer entitlement policy автоматически.
- **Причина:** reseller credentials/lifecycle отсутствуют; admin credential incident покрывается Phase 1 security.

## OPD-30 — Reseller capability profile — SUPERSEDED 2026-08-23

- A: минимальный preset create/renew/read aggregate; остальные capabilities выдаются отдельно. **Рекомендация.**
- B: разные versioned role presets.
- C: полностью индивидуальный capability set.
- Нужно отдельно утвердить create, renew, plan, packages, slot add/remove, traffic, device/HWID view, unbind, reset, force override, manual expiry, delete и token rotation. Owner-level права по default запрещены.
- **Причина:** manual payment применяет только основной MGBoost admin; отдельного login/capability profile нет.

## OPD-31 — Формула доплаты при upgrade — CLOSED 2026-08-23

- A: prorated difference по оставшимся дням текущего period — справедливо, но требует точного daily credit/rounding.
- B: полная разница цен source и target для выбранной длительности независимо от оставшихся дней — проще, но невыгодно при позднем upgrade.
- C: начать новый полный 30/60-day target period и вычесть рассчитанную стоимость неиспользованного остатка current plan — прозрачно как новый purchase, но сложнее credit/refund ledger.
- **Выбрано пользователем:** A; prorated price difference за оставшуюся часть current period, integer Stars всегда округляются вверх в пользу VPN provider. Self-service только upgrade; downgrade через ticket.

## OPD-32 — Порядок списания base quota и rollover packages — CLOSED 2026-08-23

- A: сначала расходовать текущий period base quota, затем package buckets. **Рекомендация:** сохраняет оплаченный rollover и не даёт сгореть base quota неиспользованной.
- B: сначала package traffic, затем base quota — проще отдельный package usage report, но пользователь может потерять period base quota.
- C: списывать buckets по earliest-expiry/oldest-first — требует явного expiry каждого bucket.
- **Выбрано пользователем:** A. Current-period base quota расходуется первой; package buckets начинают расходоваться только после неё.

## OPD-33 — Fixed external-payment currency/price table — CLOSED 2026-08-23

- A: отдельная versioned fixed price table по поддерживаемым currencies; recorded amount должен совпасть с выбранной price version. **Рекомендация.**
- B: одна fixed base-currency table; для другого способа/currency admin записывает conversion amount/rate/reference.
- C: рассчитывать external price из Stars по текущему exchange rate — создаёт нестабильность и источник курса.
- **Выбрано пользователем:** A. Для каждой поддерживаемой currency используется отдельная versioned fixed price table; recorded amount обязан точно соответствовать выбранной price version. Catalog и entitlement фиксированы MGBoost, arbitrary admin price запрещён. Первый RUB catalog утверждён DL-040.

## OPD-34 — Supported currencies для external payment — CLOSED 2026-08-23

- A: на первом rollout только RUB; остальные currencies позже отдельными versioned tables. **Рекомендация:** минимальный scope и однозначная сверка банковского перевода.
- B: сразу утвердить явный allowlist нескольких currencies, для каждой своя fixed table.
- C: принимать любую ISO currency — несовместимо с exact fixed-price validation и не рекомендуется.
- **Выбрано пользователем:** A. Первый rollout принимает только RUB. Новая currency требует отдельной явно утверждённой versioned fixed price table; произвольные currencies отклоняются.

## OPD-35 — Источник fixed RUB/external price values — CLOSED 2026-08-23

- A: использовать те же числовые значения, что Stars, но в RUB — просто, однако экономически произвольно.
- B: владелец отдельно утверждает полную fixed retail table в RUB/каждой supported currency. **Рекомендация:** цена не зависит от Telegram и полностью контролируется MGBoost.
- C: при публикации каждой catalog version пересчитывать Stars через утверждённый fixed multiplier; multiplier и результат сохранять в version snapshot.
- **Выбрано пользователем:** B. Владелец отдельно утверждает полную fixed RUB retail table; Stars values и exchange-rate formula не являются источником RUB price. Значения первой версии утверждены DL-040.

## OPD-36 — Максимальный срок admin override — CLOSED 2026-08-23

- A: не более 30 дней — минимальный риск забытого override, чаще нужна ручная пролонгация.
- B: не более 90 дней — компромисс контроля и операционной нагрузки. **Рекомендация.**
- C: любая точная будущая дата — гибко, но позволяет фактически бессрочный override.
- **Выбрано пользователем:** B. Один admin override действует не более 90 дней, имеет exact expiry/reason и затем возвращается в AUTO.

## OPD-37 — Retention child-user tombstone перед physical delete — CLOSED 2026-08-23

- A: 30 дней после revoke — быстрее очищает Marzban, короче recovery/audit window.
- B: 90 дней после revoke, затем delete только после successful reconciliation и отсутствия живых references. **Рекомендация.**
- C: 180 дней — больше recovery history, дольше хранение identifiers.
- D: никогда физически не удалять — максимальная история, неограниченный рост и privacy burden.
- **Выбрано пользователем:** C. Child tombstone хранится 180 дней после revoke. Physical delete после срока разрешён только при successful reconciliation и отсутствии живых references; audit trail имеет отдельную retention policy.

## OPD-38 — Исправление ошибочной manual payment — CLOSED 2026-08-23

- A: pending record можно исправить до apply; после применения только append-only compensating operation с reason/reference. **Рекомендация.**
- B: даже pending record immutable: cancel и создать новый; строже audit, больше операционных действий.
- C: разрешить редактировать уже применённый record — проще UI, но ломает воспроизводимость entitlement/audit и не рекомендуется.
- **Выбрано пользователем:** A. Pending record редактируется до apply с audit before/after. После успешного apply исходная запись immutable; исправление выполняется отдельной append-only compensating operation с reason/reference.

## OPD-39 — Telegram ownership recovery/rebind — CLOSED 2026-08-23

- A: первый rollout — ручной rebind только через основного MGBoost admin; self-service отсутствует.
- B: self-service recovery после дополнительной Telegram/account verification.
- C: заранее выданные recovery codes.
- **Выбрано пользователем:** A. После successful rebind старый Telegram binding немедленно revoked. Parent account, plan, expiry, WL periods, traffic history, slots, HWIDs, child users и UUID сохраняются. HWID или possession subscription URL недостаточны как proof ownership. Rebind получает immutable audit с old/new Telegram ID, admin actor, reason и timestamp. При suspected compromise admin одновременно вращает opaque subscription token; при обычной смене Telegram token и UUID автоматически не меняются. Self-service recovery/codes не входят в rollout и требуют отдельного будущего решения.

## OPD-40 — Subscription stacking одного plan — CLOSED 2026-08-24

- A: повторная покупка того же plan является renewal и атомарно добавляет duration к `max(current_expiry, now)`; одинаковые durations можно покупать многократно.
- B: новая покупка заменяет current expiry на duration от момента оплаты — теряется уже оплаченный остаток.
- C: stacking запрещён до окончания текущей subscription — проще ledger, но блокирует легальное раннее продление.
- **Выбрано пользователем:** A. Каждая unique successful payment добавляет срок ровно один раз; duplicate callback/retry не добавляет повторно. Накопленный срок не объединяет WL base quota: создаются последовательные 30-day WL periods. Отдельные WL packages продолжают rollover по OPD-02–04/DL-025. Покупка другого plan не является stacking и проходит upgrade/downgrade policy PH5-06.

# Decision Log

Каждая запись содержит дату, вопрос, варианты, выбранное, кто выбрал, почему и связи.

## DL-001 — Roadmap/changelog governance

- **Дата:** 2026-08-23.
- **Вопрос/варианты:** deferred docs или same-change docs.
- **Выбрано:** roadmap/changelog обновляются вместе с кодом и отражают `main`.
- **Кто:** пользователь.
- **Почему:** исключить расхождение реализации и документации.
- **Связано:** `ROADMAP.md`, `CHANGELOG.md`, Definition of Done.

## DL-002 — Новая subscription credential

- **Дата:** 2026-08-23.
- **Варианты:** TG-derived hash, concatenated keyed SHA, opaque random.
- **Выбрано:** random opaque >=256-bit; hash/verifier storage, individual rotation/revoke.
- **Кто:** пользователь.
- **Почему:** unguessability и TG ID не credential.
- **Связано:** PH1-06, PH2-01/07, Phase 4.

## DL-003 — Account/device model

- **Дата:** 2026-08-23.
- **Варианты:** shared Marzban UUID + local HWIDs; child user/UUID per occupied slot.
- **Выбрано:** parent -> slots -> lazy child users.
- **Кто:** пользователь.
- **Почему:** per-device revoke/analytics/WL allocation.
- **Связано:** Phase 3/4, `src/database.py`, `src/routes/sub.py`.

## DL-004 — HWID operational policy

- **Дата:** 2026-08-23.
- **Варианты:** permissive fingerprint; fail-closed after telemetry.
- **Выбрано:** fail-closed после compatibility/recovery readiness; без обещания hardware proof.
- **Кто:** пользователь.
- **Почему:** иначе device cap обходится.
- **Связано:** PH3-04/07.

## DL-005 — Six approved tariffs

- **Дата:** 2026-08-23.
- **Варианты:** arbitrary admin rows; versioned approved catalog.
- **Выбрано:** таблица Approved product catalog.
- **Кто:** пользователь.
- **Почему:** утверждённая продуктовая сетка.
- **Связано:** Phase 5.

## DL-006 — 60-day WL semantics

- **Дата:** 2026-08-23.
- **Варианты:** единый 200/300 GB pool; два 30-day periods.
- **Выбрано:** два последовательных periods с fresh baseline.
- **Кто:** пользователь.
- **Почему:** quota обновляется каждые 30 дней.
- **Связано:** PH5-02, PH6-02.

## DL-007 — WL packages/eligibility

- **Дата:** 2026-08-23.
- **Варианты:** unrestricted; WL plans only.
- **Выбрано:** +50/79, +100/149, +250/349, +500/599 Stars; только WL/Расширенный/Семейный.
- **Кто:** пользователь.
- **Почему:** package не является implicit WL upgrade.
- **Связано:** PH5-03/PH6-08; lifetime/rollover остаются OPD.

## DL-008 — Shared pool/manual allocation

- **Дата:** 2026-08-23.
- **Варианты:** mandatory per-device; shared default + optional manual.
- **Выбрано:** общий parent pool default; consumed monotonic; move only unspent.
- **Кто:** пользователь.
- **Почему:** простой default, безопасная redistribution.
- **Связано:** PH6-04/05.

## DL-009 — WL enforcement

- **Дата:** 2026-08-23.
- **Варианты:** hide/0.0.0.0; exact server-side inbound removal.
- **Выбрано:** partial update только `inbounds.vless`, exact 12 tags, reread/verify/reconcile.
- **Кто:** пользователь; технически подтверждено аудитом Marzban 0.8.4.
- **Почему:** cached UUID/direct host должен перестать работать.
- **Связано:** PH6-01/06/07.

## DL-010 — Immutable usage/admin adjustments

- **Дата:** 2026-08-23.
- **Варианты:** overwrite counters; append-only adjustments/new periods.
- **Выбрано:** consumed не уменьшается; reset закрывает period; admin actions audited.
- **Кто:** пользователь.
- **Почему:** воспроизводимость и защита от возврата spent quota.
- **Связано:** PH6-02/08, Phase 7.

## DL-011 — Internal accounts

- **Дата:** 2026-08-23.
- **Варианты:** username hardcodes; entitlements/overrides.
- **Выбрано:** entitlement model, internal cohort migrates first.
- **Кто:** пользователь.
- **Почему:** auditability и единая architecture.
- **Связано:** PH3-06, PH4-03.

## DL-012 — Additional slots

- **Дата:** 2026-08-23.
- **Варианты:** slot увеличивает quota; independent resources.
- **Выбрано:** slot добавляет child capacity, но использует тот же pool.
- **Кто:** пользователь.
- **Почему:** devices и traffic продаются независимо.
- **Связано:** PH5-07; цена/max открыты.

## DL-013 — Subscription source and reseller ownership boundary — SUPERSEDED 2026-08-23

- **Дата:** 2026-08-23.
- **Варианты:** infer reseller из username; explicit source/association.
- **Выбрано:** source `DIRECT | RESELLER | INTERNAL | future`, stable account->reseller->entitlement relation; end user owns VPN account, reseller issues/funds entitlement, MGBoost controls credentials/children/enforcement.
- **Кто:** пользователь.
- **Почему:** tenant isolation, migration fidelity и отсутствие credential ownership у reseller.
- **Связано:** PH0-08, PH2-08, PH3-09, PH4-08.
- **Причина supersede:** термин «reseller» был ошибочно понят как отдельный third-party tenant. Пользователь уточнил, что это прямой клиент MGBoost, оплативший переводом/иным внешним способом; актуальное решение зафиксировано в DL-029.

## DL-014 — Reseller device/WL isolation — SUPERSEDED 2026-08-23

- **Дата:** 2026-08-23.
- **Варианты:** reseller-wide resources; per-customer entitlement.
- **Выбрано:** device limit и WL quota принадлежат конкретной end subscription; children создаются только через общий slot engine; shared reseller pool не существует без будущего решения.
- **Кто:** пользователь.
- **Почему:** один customer не должен расходовать quota другого или обходить device limits.
- **Связано:** PH3-02/03/09, PH6-11, OPD-28.
- **Причина supersede:** отдельного reseller account/shared pool нет. Device limit и WL quota и без специальной reseller-модели принадлежат одному DIRECT end account; см. DL-029.

## DL-015 — Reseller mutation provenance — SUPERSEDED 2026-08-23

- **Дата:** 2026-08-23.
- **Варианты:** generic adjustment; typed source/actor/order audit.
- **Выбрано:** источники `SYSTEM`, `DIRECT_PURCHASE`, `RESELLER`, `ADMIN`, `MIGRATION`, `PACKAGE`, `INTERNAL`; каждая reseller mutation содержит actor, account, old/new, reason, order, result и reconciliation.
- **Кто:** пользователь.
- **Почему:** финансовая и security воспроизводимость.
- **Связано:** PH5-09/10, PH7-08/11.
- **Причина supersede:** источником ручного применения внешней оплаты должен быть `MANUAL_PAYMENT`, а не несуществующий actor `RESELLER`; актуальная provenance model зафиксирована в DL-029/030.

## DL-016 — WL units

- **Дата:** 2026-08-23.
- **Варианты:** decimal GB; binary GiB.
- **Выбрано:** decimal GB, 1 GB = 1,000,000,000 bytes.
- **Кто:** пользователь.
- **Почему:** тарифы и packages продаются как GB и должны быть понятны без скрытой binary semantics.
- **Связано:** OPD-01, PH6-02/03/08, UI/API/tests.

## DL-017 — Device cap and add-on rollout scope

- **Дата:** 2026-08-23.
- **Варианты:** 12, 20, internal-only high cap, предложенный architecture cap 99.
- **Выбрано:** текущий commercial rollout строго использует plan limits 3/6/12; покупка дополнительных slots отложена. Future commercial/technical maximum 99; INTERNAL остаётся configurable/unlimited.
- **Кто:** пользователь.
- **Почему:** сначала запустить предсказуемую plan/device model без add-on sales.
- **Связано:** OPD-05/06, PH3-02, PH5-07.

## DL-018 — Family WL pool

- **Дата:** 2026-08-23.
- **Варианты:** только shared pool; optional device allocation; member sub-pools.
- **Выбрано:** один общий 150 GB parent pool default, optional advanced distribution по devices и покупка traffic в тот же pool; member identities/sub-pools сейчас не вводятся.
- **Кто:** пользователь.
- **Почему:** общий простой baseline с дополнительным контролем без новой member architecture.
- **Связано:** OPD-14, PH6-04/05/08.

## DL-019 — Child deletion lifecycle

- **Дата:** 2026-08-23.
- **Варианты:** revoke+disable+tombstone затем delete; immediate delete; never delete.
- **Выбрано:** немедленный credential revoke/disable, tombstone/history, physical delete после retention; точный срок 180 дней утверждён DL-038.
- **Кто:** пользователь.
- **Почему:** сразу прекращает доступ, сохраняя audit/reconciliation.
- **Связано:** OPD-10, PH3-05, PH7-05/08.

## DL-020 — WL period anchor

- **Дата:** 2026-08-23.
- **Варианты:** rolling activation UTC; UTC-hour aligned; user calendar timezone.
- **Выбрано:** UTC-hour-aligned quota periods; subscription expiry хранится отдельно.
- **Кто:** пользователь.
- **Почему:** соответствует hourly aggregation Marzban и уменьшает boundary ambiguity.
- **Связано:** OPD-16, PH5-02, PH6-02/03.

## DL-021 — Upgrade and downgrade channel

- **Дата:** 2026-08-23.
- **Варианты:** automatic downgrade timing/proration; support-only downgrade; paid self-service upgrade.
- **Выбрано:** self-service upgrade использует prorated price difference за оставшуюся часть current period; integer Stars округляются вверх в пользу provider. Downgrade только через support ticket, без automatic downgrade flow.
- **Кто:** пользователь.
- **Почему:** upgrades доступны пользователю, destructive entitlement/device reductions требуют оператора.
- **Связано:** OPD-07/08/31, PH5-06, PH7-06/09.

## DL-022 — Downgrade device conflict

- **Дата:** 2026-08-23.
- **Варианты:** explicit selection; scheduled until free slots; automatic LRU.
- **Выбрано:** в ticket пользователь/оператор выбирает отключаемые devices до downgrade; system не выбирает автоматически.
- **Кто:** пользователь.
- **Почему:** destructive revoke должен быть явным.
- **Связано:** OPD-08, PH5-06, PH7-06.

## DL-023 — Legacy grace period

- **Дата:** 2026-08-23.
- **Варианты:** 7, 14 или 30 дней.
- **Выбрано:** 14 дней.
- **Кто:** пользователь.
- **Почему:** баланс между security rotation и client/support compatibility.
- **Связано:** OPD-09, PH4-05/06.

## DL-024 — Shared/manual transition

- **Дата:** 2026-08-23.
- **Варианты:** current-period unspent redistribution; next-period only.
- **Выбрано:** current-period transition разрешён, перераспределяется только unspent remainder; consumed монотонен.
- **Кто:** пользователь.
- **Почему:** гибкость без возврата использованного traffic.
- **Связано:** OPD-18, PH6-05.

## DL-025 — Package rollover across WL periods

- **Дата:** 2026-08-23.
- **Варианты:** expire each period; one-period carry; multi-period carry.
- **Выбрано:** purchased package remainder переносится через последующие WL periods и не обнуляется period reset; при subscription lapse/non-WL plan замораживается и возобновляется при WL entitlement.
- **Кто:** пользователь.
- **Почему:** докупленный traffic сохраняет ценность до использования.
- **Связано:** OPD-02/03/04/32, PH5-03, PH6-08.

## DL-026 — Package consumption priority

- **Дата:** 2026-08-23.
- **Варианты:** base first; package first; earliest-expiry.
- **Выбрано:** сначала current-period base quota, затем rollover package buckets.
- **Кто:** пользователь.
- **Почему:** временная base quota не пропадает, а оплаченный rollover сохраняется.
- **Связано:** OPD-32, PH6-03/08.

## DL-027 — Package eligibility and refund

- **Дата:** 2026-08-23.
- **Варианты:** Base implicit WL или explicit upgrade; unused/proportional/no refund.
- **Выбрано:** package доступен только WL-enabled plans; на Base remainder frozen. Refund только если package consumption равен нулю, без proportional refund.
- **Кто:** пользователь.
- **Почему:** package не создаёт скрытый plan, а refund остаётся однозначным и auditable.
- **Связано:** OPD-12/13, PH5-03, PH6-08.

## DL-028 — WL exhaustion subscription UX

- **Дата:** 2026-08-23.
- **Варианты:** hide; informational placeholder; disabled entries.
- **Выбрано:** один placeholder со статусом и reset date.
- **Кто:** пользователь.
- **Почему:** пользователь видит причину, а clients не получают набор неработающих WL entries.
- **Связано:** OPD-11, PH6-10.

## DL-029 — Manual/external payment semantics

- **Дата:** 2026-08-23.
- **Варианты:** отдельный `RESELLER` account source; только payment channel; одновременно explicit payment channel и mutation source.
- **Выбрано:** account остаётся `DIRECT`; платёж получает `payment_channel=EXTERNAL_PAYMENT`, а применившая entitlement операция — `mutation_source=MANUAL_PAYMENT`. Пользователь платит непосредственно MGBoost вне Telegram Stars, после чего основной admin применяет покупку/продление к тому же parent account.
- **Кто:** пользователь.
- **Почему:** канал оплаты и источник изменения нужно различать, но создавать несуществующего reseller tenant нельзя.
- **Связано:** PH0-08, PH3-09, PH4-08, PH5-09/10, PH7-10/11.

## DL-030 — Manual payment evidence and authority

- **Дата:** 2026-08-23.
- **Варианты:** минимальная заметка; structured payment evidence; отдельная reseller identity/balance.
- **Выбрано:** хранить amount, currency, payment method, external reference/comment, admin actor, timestamp, target account, result и idempotency identity; применять entitlement может только основной MGBoost admin.
- **Кто:** пользователь.
- **Почему:** ручная оплата должна быть воспроизводима и защищена от повторного применения без reseller login, balance, wholesale или margin model.
- **Связано:** PH1-02/07, PH2-05, PH5-09/10, PH7-08/10/11, PH8-09.

## DL-031 — Catalog authority for manual payments

- **Дата:** 2026-08-23.
- **Варианты:** arbitrary admin price; фиксированный MGBoost catalog/final price; reseller-defined final price.
- **Выбрано:** MGBoost определяет фиксированный catalog и конечную цену; frontend/admin не может произвольно подменить plan, entitlement или price. Архитектура versioned currency tables уточнена DL-034.
- **Кто:** пользователь.
- **Почему:** исключить бесплатные/заниженные entitlements и неоднозначную финансовую историю.
- **Связано:** OPD-19/33, PH5-01/02/09, security tests.

## DL-032 — Internal plans and unlimited authority

- **Дата:** 2026-08-23.
- **Варианты:** versioned internal plans + reasoned expiring overrides; только ad hoc per-account overrides; unlimited выдаёт primary admin/любой capability admin/two-person approval.
- **Выбрано:** versioned internal plans плюс explicit per-account overrides с обязательными expiry/reason; unlimited может выдавать только основной MGBoost admin, без second approval на текущем этапе.
- **Кто:** пользователь.
- **Почему:** единая auditable entitlement model без username hardcode и без лишнего approval workflow.
- **Связано:** OPD-15, PH3-06, PH4-03, PH5-04, PH7-07/08.

## DL-033 — Admin override lifecycle and billing boundary

- **Дата:** 2026-08-23.
- **Варианты:** expiring override -> AUTO; indefinite override; purchase/renewal clears override; FORCE changes billing eligibility.
- **Выбрано:** каждый override имеет expiry и затем автоматически возвращается в AUTO. Purchase/renewal не очищает действующий override; billing и package eligibility всегда следуют реальному plan, поэтому FORCE_ENABLED не даёт Base-плану права покупать WL package.
- **Кто:** пользователь.
- **Почему:** временная операционная компенсация не должна незаметно менять тариф или становиться бессрочной.
- **Связано:** OPD-12/17, PH5-04/06, PH7-07/08/09.

## DL-034 — Versioned external-payment price tables

- **Дата:** 2026-08-23.
- **Варианты:** отдельная fixed table для каждой currency; base currency + recorded conversion; derive from Stars exchange rate.
- **Выбрано:** отдельная versioned fixed price table для каждой поддерживаемой currency; recorded amount обязан точно совпадать с выбранной price version, arbitrary admin price запрещён.
- **Кто:** пользователь.
- **Почему:** детерминированная проверка оплаты, воспроизводимый audit и отсутствие зависимости от плавающего курса Stars.
- **Связано:** OPD-19/33, PH5-01/09, PH7-10/11; initial RUB values утверждены DL-040.

## DL-035 — Initial external-payment currency scope

- **Дата:** 2026-08-23.
- **Варианты:** RUB only; explicit multi-currency allowlist; arbitrary ISO currencies.
- **Выбрано:** первый rollout принимает только RUB. Новая currency требует отдельной утверждённой versioned fixed price table.
- **Кто:** пользователь.
- **Почему:** минимальный безопасный scope и точная server-side проверка переводов.
- **Связано:** OPD-34, PH5-09, PH7-10/11.

## DL-036 — RUB catalog price authority

- **Дата:** 2026-08-23.
- **Варианты:** численно копировать Stars; отдельная owner-approved RUB table; versioned Stars multiplier.
- **Выбрано:** владелец отдельно утверждает fixed RUB retail table; Stars price и exchange rate не вычисляют RUB amount.
- **Кто:** пользователь.
- **Почему:** MGBoost напрямую контролирует внешнюю retail price без зависимости от Telegram economics.
- **Связано:** OPD-35, PH5-01/09; конкретные RUB values утверждены DL-040.

## DL-037 — Maximum admin override duration

- **Дата:** 2026-08-23.
- **Варианты:** 30 дней; 90 дней; любая future date.
- **Выбрано:** максимум 90 дней для одного override, с exact expiry/reason и возвратом в AUTO.
- **Кто:** пользователь.
- **Почему:** ограничивает забытые overrides при приемлемой операционной нагрузке.
- **Связано:** OPD-17/36, PH3-06, PH7-07/08/09.

## DL-038 — Child tombstone retention

- **Дата:** 2026-08-23.
- **Варианты:** 30; 90; 180 дней; never delete.
- **Выбрано:** 180 дней после revoke; physical delete только после successful reconciliation и отсутствия живых references.
- **Кто:** пользователь.
- **Почему:** расширенное recovery/audit window важнее более ранней очистки identifiers.
- **Связано:** OPD-10/37, DL-019, PH3-05, PH7-05/08, migration cleanup.

## DL-039 — Manual-payment correction lifecycle

- **Дата:** 2026-08-23.
- **Варианты:** pending editable/applied compensation-only; cancel+recreate even pending; edit applied record.
- **Выбрано:** pending record можно исправить до apply с audit before/after; applied record immutable, исправление только append-only compensating operation с reason/reference.
- **Кто:** пользователь.
- **Почему:** удобная подготовка платежа без потери воспроизводимости уже применённого entitlement.
- **Связано:** OPD-25/38, PH5-09/10, PH7-08/10/11, PH8-09.

## DL-040 — External-payment RUB catalog v1

- **Дата:** 2026-08-23.
- **Вопрос:** конкретные owner-approved RUB retail prices для первого external-payment catalog.
- **Выбрано:** immutable catalog version `RUB-2026-08-23-v1`: Базовый 169/279 ₽; Базовый Плюс 239/339 ₽; Базовый Про 279/399 ₽; WL 349/579 ₽; Расширенный 399/679 ₽; Семейный 499/749 ₽ для 30/60 дней соответственно; packages +50 GB 139 ₽, +100 GB 249 ₽, +250 GB 579 ₽, +500 GB 999 ₽.
- **Кто:** пользователь.
- **Почему:** это отдельная fixed RUB retail table, не вычисляемая из Stars или exchange rate.
- **Связано:** OPD-33–35, PH5-01/03/09, PH7-10/11. Будущая смена цен создаёт новую version, не изменяет snapshots старых payments.

## DL-041 — First-rollout Telegram ownership recovery

- **Дата:** 2026-08-23.
- **Варианты:** primary-admin-only manual rebind; verified self-service; recovery codes.
- **Выбрано:** только основной MGBoost admin выполняет rebind. Старый Telegram binding atomically revoked после success; parent/entitlements/WL/history/devices/children/UUID сохраняются. HWID и subscription URL possession не доказывают ownership. Обычный rebind не вращает token/UUID; suspected compromise обязательно вращает opaque subscription token. Self-service/codes вне rollout.
- **Кто:** пользователь.
- **Почему:** минимальный first-rollout attack surface при сохранении account continuity и явной реакции на compromise.
- **Связано:** OPD-39, PH2-01/05, PH3-04, PH4-03, PH7-08. Audit содержит old/new Telegram ID, primary-admin actor, reason, timestamp и result/correlation.

## DL-042 — PH1-06 operational retention, quarantine and cleanup

- **Дата:** 2026-08-23.
- **Вопрос:** сроки хранения legacy logs/backups с credential evidence и prerequisites безопасного cleanup.
- **Выбрано:** sensitive legacy nginx/application/journal logs — 30 дней; обычные operational logs без credentials — максимум 60 дней; регулярные DB backups — 90 дней. Перед cleanup legacy token evidence создаётся один encrypted quarantine snapshot со сроком 180 дней и доступом только минимально необходимому owner/service identity. После retention выполняется controlled deletion.
- **Обязательные условия:** наличие credential в retained backup/quarantine не отменяет rotation; после PH1-06 новые logs и backups не содержат raw subscription bearer/token; cleanup начинается только после проверки backup/restore и подтверждённой rotation/reissue strategy.
- **Кто:** пользователь.
- **Почему:** сохранить ограниченное incident evidence, прекратить длительное bearer exposure и не уничтожить единственную recoverable копию до проверки восстановления.
- **Связано:** PH0-04, PH1-03/06, PH4-07, PH8-05, incident/rotation/cleanup runbooks.

## DL-043 — PH1-05 localhost broker and Phase 1 no-user-impact boundary

- **Дата:** 2026-08-23.
- **Вопрос:** где хранить Marzban service SUDO credential и допустима ли user/account migration в emergency security rollout.
- **Выбрано:** отдельный localhost broker-service; основной MGBoost после cutover не содержит Marzban SUDO credential в environment. До production rollout обязателен backward-compatibility review и regression gate. Phase 1 не меняет тариф, parent/child model, user UUID, legacy subscription URL/token, config semantics, expiry или HWID/device binding и не включает fail-closed HWID.
- **Кто:** пользователь.
- **Почему:** уменьшить credential blast radius, не связывая emergency security с рискованной migration существующих VPN clients.
- **Execution evidence:** `docs/PHASE1_BACKWARD_COMPATIBILITY.md`; production baseline/cutover 25 users/25 UUID, 71 active device/lock rows; `tests/test_phase1_legacy_compat.py`, `tests/test_marzban_broker.py` и два reproducible staging scripts. На 2026-08-24 all-10 direct-vs-broker staging, production read-only operations, outage/recovery/restart, Filin HMAC и exact legacy `/sub` contract gates пройдены; PH1-05 развернут с verdict SAFE TO DEPLOY FOR EXISTING USERS.
- **Связано:** PH1-01/02/05/06, PH2-04, Phase 3/4, Filin HMAC integration, deployment/rollback runbooks.

## DL-044 — Same-plan subscription stacking and renewal

- **Дата:** 2026-08-24.
- **Вопрос:** как повторная покупка того же plan влияет на expiry и последовательные WL periods.
- **Варианты:** additive renewal от `max(current_expiry, now)`; replacement expiry от момента оплаты; запрет stacking до окончания subscription.
- **Выбрано:** повторная покупка того же plan — renewal по формуле `max(current_expiry, now) + purchased_duration`. Одинаковые durations разрешено покупать несколько раз; каждая unique successful payment применяется атомарно и ровно один раз. Накопленный срок создаёт последовательные 30-day WL base periods, не единый quota pool. Package rollover не меняется. Другой plan обрабатывается только как upgrade/downgrade.
- **Кто:** пользователь.
- **Почему:** сохранить уже оплаченный срок, разрешить ранние и повторные продления и не нарушить утверждённую 30-day WL-period модель.
- **Связано:** OPD-40, PH5-02/05/06/10, PH6-02/08; обязательные tests — repeated payment, concurrent payment, duplicate callback и crash/retry.

## DL-045 — Primary internal owner aliases and privileged actor boundary

- **Дата:** 2026-08-25.
- **Вопрос:** считать ли `beykus`, `beykusios`, `BeykusLaptop` разными accounts и как авторизовать stable primary actor для первого child canary.
- **Варианты:** три parent accounts; один parent с разрушительным alias merge; один parent с immutable one-to-many alias evidence. Для actor: доверять caller `actor_id`; либо mint server capability после authenticated allowlisted admin session.
- **Выбрано:** один parent mapping `INTERNAL_OWNER_PRIMARY` с тремя отдельными immutable aliases; Telegram identity `905302972`; INTERNAL, `billing_required=false`, WL unlimited, device capacity 10, expiry unlimited. Девять observed HWID rows — practical slot candidates, не physical-device proof и не подлежат automatic cross-client dedup. Stable actor `owner:mgboost-primary:v1` может выполнять privileged mutation только через server-derived capability после authenticated primary-admin authorization; Telegram ID остаётся identity link, не credential.
- **Canary:** только `beykusios` legacy `user_devices.id=56`, privacy ref `corr_701f5982b4` (iPhone 17 / INCY 2.5.2 / iOS) -> future slot 1/generation 1; все остальные aliases/devices продолжают legacy runtime. До доказательства child flow old shared UUID и legacy URL не revoke/redirect. Остальные названные candidates и две anomalies не создавать/классифицировать автоматически.
- **Кто:** пользователь.
- **Почему:** сохранить ownership/history/reconciliation для всех legacy имён, ограничить blast radius canary и исключить frontend actor spoofing.
- **Связано:** PH3-01/02/03/06/09, PH4-01/02/03, `src/admin_authority.py`, `src/internal_entitlements.py`, `src/child_provisioning_schema.py`, `docs/PHASE3_CHILD_PROVISIONING.md`.

## DL-046 — Shadowsocks retired; MGBoost is VLESS-only

- **Дата:** 2026-08-25.
- **Вопрос:** считать ли неработающие Shadowsocks proxy records частью compatibility contract и создавать ли ради них topology/upgrade.
- **Варианты:** сохранить metadata и искать Marzban API/upgrade; создать Shadowsocks inbound; считать functional VLESS access authoritative и удалить retired metadata.
- **Выбрано:** Shadowsocks полностью выведен из MGBoost. Все existing users очищаются от legacy Shadowsocks proxy metadata через staged typed mutation; новые parent/child accounts строго VLESS-only. Functional compatibility определяется UUID/flow/exact VLESS inbound/config/expiry/status/data-limit contract. Отсутствие Shadowsocks у child не regression. Shadowsocks topology автоматически не создаётся; Marzban upgrade ради Shadowsocks не выполняется.
- **Кто:** пользователь.
- **Почему:** production имеет 0 Shadowsocks inbound, а 25/25 реальных legacy subscriptions уже содержат только VLESS; dormant metadata блокировала безопасный Marzban 0.8.4 child create, не давая пользователю работающего доступа.
- **Связано:** PH3-03/04, `src/shadowsocks_retirement.py`, `scripts/retire_shadowsocks_metadata.py`, `docs/SHADOWSOCKS_RETIREMENT.md`.

## DL-047 — PH3-04 accelerated conservative compatibility allowlist

- **Дата:** 2026-08-25.
- **Вопрос:** PH3-07 ранее блокировал PH3-04 требованием представительного статистического окна наблюдения (ROADMAP: "the sample is still too small and biased for fail-closed"); владелец теперь ограничен по срокам и явно просит не ждать многодневного окна.
- **Варианты:** (a) продолжать ждать длительное статистическое окно; (b) включить fail-closed для всех клиентов сразу, объявив `UNKNOWN` эквивалентом `SUPPORTED`; (c) не ждать репрезентативной выборки, но и не расширять доверие — принять **только** exact `(client, version, platform)` tuples с положительным organic-live evidence как `SUPPORTED`, оставить всё остальное (включая `UNKNOWN`) недоверенным, отложить фактическую активацию fail-closed до отдельного staged migration window.
- **Выбрано:** (c). Это не отменяет прежнюю честную оценку "sample мал и biased" — она остаётся исторически верной для целей статистической репрезентативности. Новая стратегия просто не требует репрезентативности: конкретная supported-запись не является утверждением "весь этот client reprezentative", а лишь утверждением "у нас есть подтверждённое живое свидетельство именно для этой tuple". `docs/PHASE3_HWID_GATE.md` содержит versioned registry и его источники.
- **Кто:** пользователь (ограничение по срокам), реализация — исполнитель.
- **Почему:** нужен реальный прогресс к PH3-04 без ожидания multi-day soak, но без ослабления security bar — только exact-evidence allowlist, никакого fuzzy match, никакого "unknown значит поддерживается".
- **Связано:** PH3-04/07, `src/compat_registry.py`, `src/hwid_gate.py`, `docs/PHASE3_HWID_GATE.md`.

## DL-048 — Admin panel technical-identifier depth

- **Дата:** 2026-08-26.
- **Вопрос:** насколько глубоко внутренние technical identifiers (raw `mgc_*` child id, child intent id, generation id, outbox id, UUID, full/internal HWID) должны быть видны в новой account-centric admin panel по умолчанию.
- **Варианты:** (a) только под отдельной вкладкой Technical; (b) показывать частично прямо в Devices; (c) полный технический режим по умолчанию, как сейчас.
- **Выбрано:** (a). Все внутренние technical identifiers скрыты по умолчанию и доступны только на вкладке `Account → Technical`. Обычные `Overview`/`Devices` показывают только operational concepts (`Slot 2`, `ACTIVE`, `REVOKED`, `MIGRATED`, `Child ACTIVE`, `desired/observed`). Masked HWID/UUID разрешены вне Technical там, где реально полезны для troubleshooting; полная/raw identity — только Technical. Будущий global Developer/Advanced mode возможен, но не входит в initial rollout.
- **Кто:** пользователь.
- **Почему:** текущий backend полностью account-centric, но UI не должен заставлять обычного администратора думать в терминах internal implementation id ради ежедневной операционной работы.
- **Связано:** PH7-05/08, `docs/ADMIN_PANEL_REDESIGN.md` (ADMIN-UX-01).

## DL-049 — PH7-05 Wave B device mutation granularity

- **Дата:** 2026-08-26.
- **Вопрос:** какие device-mutation операции (Disable/Enable, Revoke/Free, Rebind) должны появиться в первой волне мутаций новой admin panel (Wave B), и как их группировать в UI.
- **Варианты:** (a) все три группы сразу, как четыре различимые операции; (b) только Disable/Enable в Wave B, Revoke/Free/Rebind отложить; (c) одна универсальная "Delete device" операция вместо отдельных.
- **Выбрано:** (a). Wave B включает все три группы как **четыре отдельные** операции: Disable/Enable (обратимая пауза, без обязательного reason), Revoke (terminal revoke текущей credential generation, старый UUID/credential никогда не воскресает), Free (отдельная последующая операция, освобождает slot после Revoke), Rebind (compromise/replacement flow: старая generation terminal revoked, создаётся новая generation, повышенный риск). Revoke/Free/Rebind обязательно требуют preview, reason, explicit confirmation и immutable/auditable event; confirmation для Rebind — самый жёсткий из всех. Не объединять в одну абстрактную кнопку Delete.
- **Кто:** пользователь.
- **Почему:** разные операции имеют разную обратимость и разный blast radius; единая кнопка скрывает от администратора реальные последствия действия.
- **Связано:** PH7-05, `src/device_slots.py`, `src/ownership_rebind.py`, `docs/ADMIN_PANEL_REDESIGN.md` (ADMIN-UX-02).

## DL-050 — Legacy Marzban-username Users screen cutover

- **Дата:** 2026-08-26.
- **Вопрос:** как поступить с текущим top-level Marzban-username-centric экраном Users при вводе новой account-centric навигации.
- **Варианты:** (a) держать рядом с новым Accounts до полного покрытия функционала, затем убрать; (b) сразу убрать с top-level навигации в System/Technical, не удаляя функционал; (c) удалить совсем.
- **Выбрано:** (b). Legacy Users сразу перемещается под `System / Technical → Marzban → Raw Users` (точное имя может быть адаптировано), не остаётся на top-level рядом с Accounts. Ничего не удаляется — это compatibility/debug escape hatch до тех пор, пока Accounts не доказанно покрывает эквивалентный функционал. `Accounts` становится primary top-level поверхностью для работы с клиентом; прямая работа с внутренностями Marzban — через System/Technical.
- **Кто:** пользователь.
- **Почему:** параллельное сосуществование двух "списков пользователей" на top-level вводит администратора в заблуждение о том, какая модель актуальна; разделение по смыслу действия ("работа с клиентом" vs "работа с Marzban напрямую") понятнее, чем сосуществование.
- **Связано:** PH7-05/09, `docs/ADMIN_PANEL_REDESIGN.md` (ADMIN-UX-03).

## DL-051 — Admin Dashboard widget priority during and after PH4 grace

- **Дата:** 2026-08-26.
- **Вопрос:** какие агрегаты приоритетны на главном Dashboard новой admin panel, учитывая, что PH4-05 grace campaign сейчас активна.
- **Варианты:** (a) приоритет grace campaign, затем operational health, затем expiring soon, tickets компактно; (b) равнозначные виджеты без явного приоритета; (c) большой support/tickets analytics блок.
- **Выбрано:** (a), в порядке: 1) Grace campaign progress (Day N/14, Telegram BOUND/WAITING, real devices observed vs. child-backed vs. still legacy, reconcile/compatibility blockers) — построен как условный блок, который естественно исчезает/сворачивается после завершения grace, Dashboard не должен архитектурно зависеть от миграционной кампании; явно не показывать вводящий в заблуждение "17/17 MIGRATED", если речь о parent/genesis readiness, а не о реальных customer-устройствах (см. `docs/ADMIN_PANEL_REDESIGN.md` §5). 2) Operational health (MGBoost/broker/Marzban/child worker/nodes/resolver errors/`ERROR_RECONCILE`/compatibility) — компактно при отсутствии проблем, визуально приоритизируется при наличии. 3) Expiring soon (today/≤3/≤7/≤30 дней). Tickets — компактный счётчик `N open / N unanswered` со ссылкой на экран, без большого analytics-блока.
- **Кто:** пользователь.
- **Почему:** во время активной grace-кампании это самая operationally значимая информация; после её окончания освободившееся место естественно займут billing/WL/operational alerts (Wave C/D), без переделки layout.
- **Связано:** PH4-05/06, PH7, `docs/ADMIN_PANEL_REDESIGN.md` (ADMIN-UX-04).

## DL-052 — Admin frontend technology direction (no framework rewrite)

- **Дата:** 2026-08-26.
- **Вопрос:** остаться ли на текущем vanilla JS approach для новой admin panel, или перейти на SPA-framework (React/Vue/Svelte).
- **Варианты:** (a) vanilla JS, модуляризовать текущий монолитный `admin.js` на ES modules; (b) server-rendered/Jinja + JS; (c) SPA framework с build chain.
- **Выбрано:** (a). Текущий vanilla JS уже безопасен (safe rendering helpers, event delegation, отсутствие inline handlers, совместимость с текущим strict CSP без `'unsafe-inline'`/`'unsafe-eval'`) и не требует build chain. Framework добавил бы build chain, CSP-риски и deploy-сложность без реальной выгоды на масштабе одного small-team deployment. `admin.js` разбивается на ES-модули (`core.js` с общими safe primitives — `html`/`esc`, `adminFetch`, CSRF, common dialogs — плюс по модулю на домен: `accounts.js`, `devices.js`, `migration.js` и т.д.). Точная декомпозиция может быть скорректирована реализующим агентом после анализа code boundaries.
- **Кто:** пользователь.
- **Почему:** масштаб проекта не оправдывает SPA-framework; текущий подход уже соответствует security-требованиям проекта (PH1-01 CSP/no-inline conventions), а миграция на framework не даёт архитектурной выгоды, соразмерной cost/risk.
- **Связано:** PH1-01, PH7, `docs/ADMIN_PANEL_REDESIGN.md` (ADMIN-UX-05).

## DL-053 — PH5-03 package-bucket consumption order

- **Дата:** 2026-08-27.
- **Вопрос:** каким образом детерминированно отнести package consumption к
  конкретному bucket, когда одновременно существуют несколько rollover
  packages, чтобы unused-only refund был доказуемым.
- **Варианты:** FIFO по grant; LIFO по grant; пропорциональное распределение
  с отдельным правилом округления.
- **Выбрано пользователем:** FIFO по immutable `granted_at ASC`; при точном
  равенстве timestamp — stable `bucket_id ASC`. Freeze/resume и переходы
  между WL periods не меняют исходный порядок. Refund возможен только при
  `derived_consumption` конкретного bucket, равном нулю, в той же атомарной
  транзакции, которая отзывает bucket.
- **Кто:** пользователь.
- **Почему:** гарантирует единственное воспроизводимое attribution для
  rollover и refund, не меняя уже записанные product/payment snapshots.
- **Связано:** PH5-03, OPD-13/32, DL-025–027, PH6-03/04/08.

## DL-054 — Manual-payment external_reference uniqueness scope and retention after cancellation

- **Дата:** 2026-08-27.
- **Вопрос:** независимый review PH5-09/10 (`af1effe`) обнаружил, что
  `mgboost_manual_payment_records.external_reference` сделан permanently
  UNIQUE, включая CANCELLED записи, и что ни один существующий DL явно не
  фиксировал (a) должен ли CANCELLED освобождать reference для повторного
  использования и (b) в каком scope должна действовать эта уникальность.
- **Варианты (retention):** CANCELLED освобождает reference; CANCELLED
  резервирует reference навсегда.
- **Варианты (scope):** глобально по всем manual payment methods; per
  payment_method/provider namespace; per отдельная провайдерская identity
  (например конкретный банк/SBP), которой в модели сейчас не существует.
- **Выбрано пользователем:** external_reference — исторический идентификатор
  денежного факта; после CANCELLED старый reference остаётся зарезервированным
  навсегда (запись не воскрешается под тем же transfer id). Пока запись
  находится в PENDING, ошибочно введённый reference исправляется в той же
  записи через существующий audited edit flow (DL-039), а не
  cancel+recreate. Проверка существующей модели подтвердила: единственный
  установленный precedent уникальности внешней ссылки — `UNIQUE(payment_channel,
  external_reference)` (`account_schema.py`/`provenance_schema.py`); внутри
  этого модуля `payment_channel` всегда `EXTERNAL_PAYMENT`, поэтому текущий
  table-wide `UNIQUE(external_reference)` эквивалентен этому precedent, а не
  расширяет его. `payment_method` — свободный текст без фиксированного
  provider-словаря (DL-030 не определяет provider identity), поэтому
  scoping по нему был бы ненадёжен (регистр/опечатки молча сузили бы
  anti-replay защиту) и никакой существующей provider-модели для этого нет.
  Текущий `UNIQUE(external_reference)` подтверждён корректным, без
  изменений кода.
- **Кто:** пользователь.
- **Почему:** сохранить воспроизводимость денежного факта и не изобретать
  несуществующую provider-модель ради scoping, который ничего не отличал бы
  внутри одного payment_channel.
- **Связано:** DL-029/030/039, PH5-09/10.

## DL-055 — PH7-05 Disable/Enable: обязательный reason/confirm поверх ADMIN-UX-02

- **Дата:** 2026-08-27.
- **Вопрос:** `docs/ADMIN_PANEL_REDESIGN.md` (ADMIN-UX-02) фиксирует для
  reversible-pause «No mandatory reason» и лёгкую toggle-подачу, тогда как
  инструкция владельца на срез operational-admin completion требует для
  Disable/Enable тот же контракт, что у остальных device-мутаций: preview +
  mandatory reason + confirmation.
- **Выбрано пользователем:** приоритет у прямой инструкции владельца от
  2026-08-27 — реализованные маршруты
  `POST /admin/accounts/{id}/devices/{slot}/disable|enable` требуют reason
  (3..300) и явного `confirm: true`, диалоги UI повторяют контракт
  Revoke/Free/Rebind. Обратимость и семантика паузы при этом не меняются:
  слот остаётся занятым, generation/UUID/HWID history не затрагиваются,
  Enable возвращает ту же допустимую generation; от Revoke/Free/Rebind
  операция не смешивается ни на уровне UX, ни на уровне backend-примитива
  (`DeviceSlotAdminStore` — единственный writer значения
  `mgboost_device_slots.desired_state='DISABLED'`).
- **Кто:** пользователь (инструкция); в этот DL записано агентом как
  единственное каноническое место правила.
- **Почему:** устранить противоречие между дизайн-доком и живой инструкцией;
  единый аудит-бар для всех административных мутаций устройств.
- **Связано:** DL-049, ADMIN-UX-02, PH7-05, PH7-08.

## DL-056 — Rebind на приостановленном (DISABLED) слоте снимает паузу

- **Дата:** 2026-08-27.
- **Вопрос:** независимый review `f7ea7f4..dec28f5` (PH7-01 expiry ops + PH7-05
  Disable/Enable) обнаружил, что `DeviceSlotStore.rebind()` не проверяет
  `mgboost_device_slots.desired_state` и безусловно переводит слот в `ACTIVE`
  при создании новой generation. Итог: Disable → Rebind тихо снимает паузу и
  запускает новое устройство активным, без отдельного Enable. Ни DL-049, ни
  ADMIN-UX-02, ни DL-055 явно не определяют это взаимодействие — это был
  самостоятельный выбор реализующего агента (задокументирован в коде/тестах/
  UI-тексте подтверждения Rebind, но не зафиксирован как owner decision).
- **Варианты:** (a) пауза снимается, successor стартует ACTIVE (текущая
  реализация); (b) Rebind запрещён, пока слот на паузе (требует явный Enable
  сначала); (c) successor наследует паузу (стартует DISABLED, требует
  отдельный Enable).
- **Выбрано:** (a) — поведение GLM оставлено без изменений. Rebind — самая
  тяжёлая и явная операция среди device-мутаций (двухшаговое confirm,
  compromise/replacement flow по DL-049), поэтому решение admin'а заменить
  устройство считается приоритетнее более лёгкой предыдущей паузы. Код и
  тесты изменений не требуют; UI уже раскрывает это в тексте подтверждения
  Rebind («successor стартует активным (пауза не наследуется)»).
- **Кто:** пользователь.
- **Почему:** устранить product ambiguity, найденную независимым review,
  до production deploy — без owner decision это оставалось невалидированным
  предположением агента, а не зафиксированной политикой.
- **Связано:** DL-049, DL-055, PH7-05, `src/device_slots.py::rebind`,
  `src/device_slot_admin.py`.

## DL-057 — Megochel consolidation: survivor, absorbed fate, device limit, name

- **Дата:** 2026-08-27.
- **Вопрос:** read-only анализ (предыдущая сессия) обнаружил, что `MegochelPC`
  (account 5) и `MegochelAndroid` (account 6) — два реальных, независимо
  провизионированных parent-аккаунта одного человека. Существующие
  primitives не поддерживают слияние уже созданных аккаунтов
  (`mgboost_legacy_alias_groups` — 1:1 с account, собирается только при
  bootstrap; `mgboost_legacy_account_aliases` полностью immutable и
  `legacy_username` глобально `UNIQUE`). Требовались явные решения по:
  survivor/absorbed, судьбе absorbed-аккаунта, device limit, итоговому
  человеческому имени, и минимальному новому primitive.
- **Выбрано:**
  1. Survivor = account 6 (`MegochelAndroid`) — там уже живёт единственная
     активная Telegram OWNER identity и единственный реально используемый
     opaque subscription credential; absorbed = account 5 (`MegochelPC`).
  2. Human-facing display_name survivor'а = `Megochel` — через новый
     аддитивный `mgboost_account_display_names` (см. PH7-13), поскольку в
     схеме нет свободного текстового поля для имени аккаунта, а PRIMARY
     alias обязан оставаться реальным legacy-именем.
  3. Device limit survivor'а = D6 — через новый canonical
     `legacy_paid_compat.increase_device_limit()` (см. PH7-13), а не через
     `ensure_legacy_paid_compat_entitlement()` (та функция технически не
     умеет менять план уже provisioned live-подписки — это отдельный,
     независимо обнаруженный gap). Реальное число физических устройств (3
     vs 4) не выясняется — trusted user, дополнительные слоты разрешены
     явно.
  4. `MegochelPC`-alias физически НЕ копируется на account 6 (нарушило бы
     глобальный `UNIQUE` по `legacy_username`); immutable-строка остаётся
     на account 5, а разрешение "старое имя → survivor" выполняется через
     новый `mgboost_account_merges`/`resolve_account_id()` supersession
     resolver, а не копированием.
  5. Account 5 после безопасной consolidation → `CLOSED`, никогда `DELETE`.
     Его live legacy-compat subscription при этом → `CANCELLED` (явно
     одобренная owner semantics), сохраняясь как immutable evidence, а не
     удаляясь.
  6. Grace/evidence/history account 5 не переписываются и не переносятся —
     остаются attributed к исходному `account_id` навсегда; `CLOSED`
     account не считается operationally active ни в одном read model.
  7. Rollback merge — только через explicit append-only reversal (новая
     `REVERSED`-запись + CAS flip `status`, никогда `DELETE` merge-record);
     terminal generation/child никогда не воскрешается (PH3-05 policy).
  8. Genesis-child absorbed account: сначала canonical `Revoke` → `Free`
     (переиспользуя PH3-05 `process_revoke`/`process_free` без нового
     кода), и только затем `close_account()` — так что под `CLOSED`
     parent никогда не остаётся осиротевшая `ACTIVE` ветка.
  9. Legacy Marzban `MegochelPC`/`MegochelAndroid` не трогаются этой
     операцией вообще — их органическая миграция на child-пользователей
     survivor'а идёт уже существующим, отдельным маршрутом.
- **Кто:** пользователь (владелец).
- **Почему:** зафиксировать реальный merge/consolidation flow как owner
  decision, а не как невалидированное предположение реализующего агента —
  особенно выбор survivor'а, судьба absorbed-подписки и итоговое имя, ни
  одно из которых не выводится однозначно из существующего кода/схемы.
- **Связано:** PH3-01, PH4-01, PH3-05, PH4-03, PH7-13, `src/account_consolidation.py`,
  `src/account_consolidation_schema.py`, `src/legacy_paid_compat.py::increase_device_limit`.

## DL-058 — `tpl-<public_id>` per-account provisioning template: Вариант A принят, cleanup — backlog

- **Дата:** 2026-08-28.
- **Вопрос:** независимый review PH5-11/PH5-12 (`f228b46..b22e5f8`) обнаружил,
  что per-account infrastructure-owned Marzban template user
  (`tpl-<public_id>`, один на каждый commercial account) можно прочитать
  двумя способами: (A) технически необходимо, потому что существующий
  `ChildProvisioningStore.prepare_child_ensure` жёстко требует, чтобы
  source-alias принадлежал ТОМУ ЖЕ `account_id`; (B) избыточная per-customer
  стоимость — это ограничение защищает от клонирования чужого
  ДИФФЕРЕНЦИРОВАННОГО legacy-контента, а STANDARD-шаблон одинаков для всех
  клиентов по построению, так что 1:1 — следствие переиспользования
  существующей таблицы, а не security-необходимость. Два независимых
  под-ревью в рамках одной сессии разошлись именно на этом пункте; ни одно
  не решало самостоятельно.
- **Выбрано:** Вариант A — оставить per-account `tpl-<public_id>` как есть
  для текущего rollout. Обоснование (владелец): `tpl-*` — исключительно
  infrastructure-provisioning source, не customer-facing identity; его
  `source_contract_hash` (64-hex) используется для клонирования конфигурации
  child'а, а сам template UUID/subscription URL никогда не передаются
  клиенту и не могут использоваться как самостоятельная customer
  subscription (подтверждено по коду: `opaque_resolver.py` читает только
  `source_contract_hash` из `mgboost_provisioning_templates`, child получает
  собственный Marzban-minted UUID — `child_provisioning.py`). Per-account
  template не расширяет broker security model (не даёт новую authority
  сверх уже существующего same-account alias-scoping). Redesign на
  shared/pooled system-template (Вариант B) отклонён для этого запуска —
  реальная архитектурная работа вне скоупа первого коммерческого launch.
- **Явное условие owner'а (не просто рекомендация — inline STOP-триггер):**
  это решение недействительно, и деплой должен быть немедленно
  остановлен, если ревью в любой момент обнаружит, что (1) template
  UUID/credential каким-либо путём может попасть клиенту, (2) template
  способен использоваться как самостоятельная customer subscription, или
  (3) per-account template создаёт дополнительную security authority сверх
  уже существующей same-account alias-изоляции. Независимая проверка этого
  условия на момент принятия решения (2026-08-28) подтвердила: ни один из
  трёх триггеров не наблюдается в текущем коде.
- **Backlog (не реализовано в этой сессии, обязательно завести отдельно):**
  lifecycle/cleanup `tpl-<public_id>` при terminal `close_account()`/
  консолидации/удалении аккаунта. Подтверждённый факт: `close_account()`
  (`src/account_consolidation.py`) сейчас не знает о
  `mgboost_provisioning_templates` вообще — при закрытии аккаунта
  инфраструктурный Marzban-пользователь `tpl-*` остаётся `ACTIVE`
  бессрочно, без аудита и без reversal. Будущая реализация обязана: (a)
  затрагивать только template-ресурсы, доказанно принадлежащие закрываемому
  `account_id` (тот же паттерн проверки владения, что уже используется в
  `child_provisioning.py::prepare_child_ensure`); (b) быть crash-safe и
  idempotent (get-or-transition, не безусловная мутация); (c) писать
  audit-запись с before/after/reason, как остальные terminal-операции
  аккаунта (PH3-05 Revoke/Free, DL-057 consolidation).
- **Кто:** пользователь (владелец).
- **Почему:** зафиксировать выбор между двумя равно защитимыми прочтениями
  одного и того же кода как owner decision, а не как невалидированное
  предположение любого из реализующих/ревьюирующих агентов — вместе с явным,
  проверяемым условием, при котором решение перестаёт действовать.
- **Связано:** PH5-11, PH5-12, `src/commercial_signup.py::ensure_template_for_account`,
  `src/child_provisioning.py::prepare_child_ensure`, `src/account_consolidation.py::close_account`,
  DL-057.

## DL-059 — ACTIVE LIMITED + newly-approved exact WL inbound: auto-add это legitimate topology convergence (PH6-09)

- **Решение (owner, 2026-08-28):** если (a) у аккаунта есть действующий
  canonical LIMITED WL entitlement, (b) текущий период активен и quota не
  exhausted, (c) child ACTIVE/INCLUDED, (d) появился новый inbound, который
  ЯВНО входит в текущую approved/versioned exact PH0-05/PH6-01 WL topology,
  (e) fresh topology assertion = OK и (f) identity/UUID/generation child
  доказаны — тогда scheduled reconciliation АВТОМАТИЧЕСКИ добавляет этот
  newly-approved WL inbound существующему ACTIVE child через существующий
  PH6-07 drift-repair path. Это legitimate topology convergence, а НЕ
  ERROR_RECONCILE (отменяет прежний консервативный flag-only выбор
  `WL_UNEXPECTED_WHILE_INCLUDED` для нового approved тега у ACTIVE child).
- **Canonical semantics (симметричная):**
  - ACTIVE + approved WL missing → safely add;
  - DISABLED + approved WL present → safely remove (уже было в PH6-07);
  - ambiguous / unknown / identity mismatch → ERROR_RECONCILE, 0 mutation.
- **Жёсткие ограничения:** только exact current approved WL tag; никакого
  fuzzy `wl` matching; unknown/wl-like tag не trusted и не auto-add (PH6-01
  gate блокирует весь цикл, как и раньше); target строится ТОЛЬКО из current
  approved topology; mutation меняет только `inbounds.vless`;
  UUID/proxies/expire/data_limit/status не трогаются; topology
  unknown/mismatch/unreachable → fail closed, 0 mutation;
  entitlement/pool/quota fresh-recheck непосредственно перед repair epoch
  (TOCTOU-фикс PH6-07 сохранён); NEW в PH6-09: дополнительно требуется
  FRESH trusted usage telemetry (`src/wl_freshness.usage_freshness`) —
  stale/unknown usage блокирует auto-add и любой access-increase.
- **Доказуемая scope-механика:** «newly-approved» — это НЕ весь текущий
  allowlist и не эвристика: append-only реестр
  `mgboost_wl_topology_versions` (`ph6_09_wl_topology_versions_v1`)
  записывает точный набор тегов каждой positively-asserted config_version;
  child получает только `tags_added_since(версия его замороженного
  manifest)`. Неизвестная версия → пустое множество → 0 auto-add (fail
  closed). Тег, который child'а provisioning сознательно никогда не включал,
  остаётся неавто-добавляемым, пока не появится approved-версия, которая его
  вводит.
- **Scope-границы:** решение НЕ распространяется на STANDARD/NONE,
  UNLIMITED и UNLIMITED-quota periods (структурный abstain без изменений);
  commercial-canary запуск — не эта фаза.
- **Кто:** owner (постановка PH6-09).
- **Почему:** PH6-06 review задокументировал newly-added-inbound gap и
  временный conservative flag-only ответ; без canonical semantics для
  approved expansion коммерческий LIMITED WL нельзя включать без ручного
  разового вмешательства при каждом обновлении topology.
- **Связано:** PH0-05, PH6-01, PH6-06, PH6-07, PH6-09.

# Contradictions and migration hazards

1. Current production Stars 199/349 совпадает по цене с будущим WL, но schema не содержит plan/WL/device semantics; старые invoices нельзя молча переинтерпретировать.
2. Current HWID permissive: `src/routes/sub.py` проверяет только `hwid:`; fail-closed без telemetry сломает clients.
3. Current one-user/shared-UUID модель несовместима с child revoke/allocation; local device delete не отзывает UUID.
4. Current proxy требует raw Marzban token; простое hash-only изменение колонки сломает resolver. Нужен новый child-config path.
5. PH1-01 удалил frontend `localStorage` Marzban JWT и ввёл server session; PH1-05 изолировал service SUDO за typed localhost broker. Оставшиеся risks — process-local admin session state до PH8-02 и сам full-SUDO credential внутри broker до будущего narrow upstream authority.
6. PH1-03/04 закрыли world-readable sensitive files и root runtime MGBoost; Marzban container всё ещё root. После PH1-05 MGBoost/Filin не получают SUDO/JWT напрямую, но compromised main с broker HMAC key всё ещё может вызвать десять transitional legacy operations и dormant typed child ensure до дальнейшего сужения authority.
7. Exact WL tags существуют только live, в repo пока нет authoritative versioned config; stale hosts делают fuzzy match опасным.
8. Marzban usage агрегируется по UTC-hour; rolling mid-hour period нельзя точно считать суммой whole-hour rows.
9. Current `audit_log` полезен, но не даёт immutable actor/before/after/reason для всех admin changes.
10. Current Stars меняет только expiry. New purchase/plan/package/period child sync требует entitlement/outbox, а old invoice snapshot должен сохраниться.
11. PH1-06 прекратил новые raw-token записи и README теперь рекомендует fragment для legacy LK; сам legacy bearer и одноразовая совместимость со старыми `?token=` bookmarks сохраняются до staged PH2-01/Phase 4 migration.
12. README требует `SECRET_KEY`, но current код его не использует; нельзя считать его защитой session/HMAC.
13. Production untracked `extra_configs.json` — drift/evidence; не удалять без ownership/retention.
14. Partial update semantics подтверждены только Marzban 0.8.4; upgrade обязан повторить contract tests.
15. В current schema нет structured external payment/order, `payment_channel` и `mutation_source`; ручное продление нельзя надёжно связать с конкретным переводом или отличить от admin grant.
16. Current admin manual expiry changes не фиксируют amount/currency/payment method/reference и не имеют payment-level idempotency; повторное применение одного перевода предотвращается только операционной внимательностью.
17. Generic Filin HMAC API — отдельная automation integration с широкими Marzban operations, а не payment/admin session; её caller или request нельзя считать доказательством внешней оплаты.
18. Legacy external-payment provenance нельзя выводить из username, prefix или note. Миграция обязана использовать подтверждённые records, а при их отсутствии сохранять явное `UNKNOWN_LEGACY`, не выдумывая историю.
19. Current code хранит только legacy/free-form Stars tariff state и не моделирует утверждённый `RUB-2026-08-23-v1`; новую таблицу нельзя подменять правкой старых invoice/payment snapshots при PH5-01/09 migration.
20. Marzban current `subscription_url` не является полным inventory реально работающих legacy aliases: 42 из 45 distinct tokens, сохранённых у devices, всё ещё resolve к stored username, хотя почти все отличаются от current user API URL. PH1-06 не может безопасно revoke/rotate user tokens до staged Phase 4 inventory/migration.
21. Старое предположение PH1-05 о непубликуемом `/internal/` неверно: production nginx допускает этот route с IP `155.212.142.20`, а MGBoost затем проверяет Filin HMAC. Отсутствие вызовов в retained journal sample не разрешает удалить create/renew/delete contract.
22. **SUPERSEDED by DL-046:** legacy `beykusios` имел VLESS и retired Shadowsocks proxy credentials; прежний contract ошибочно требовал клонировать оба. Историческая причина и failed gate сохранены в `docs/PHASE3_CHILD_PROVISIONING.md`.
23. **CLOSED by DL-046 and completed 2026-08-25:** real Marzban 0.8.4 staging подтвердил, что disabled Shadowsocks metadata нельзя клонировать при live topology 25 VLESS/0 SS. Product contract теперь VLESS-only; прямой Marzban DB write и создание SS topology отвергнуты. Typed production cleanup удалил 7/7 retired records с нулевым functional drift; повторный exact-25 VLESS-only child gate прошёл CREATE/EXISTING/lost-ACK/reread/outage checks.

# Roadmap maintenance checklist

Перед завершением будущего изменения:

- указан PH/SEC ID и правдивый статус;
- добавлены новые blockers/OPD;
- обновлён `CHANGELOG.md`;
- обновлён Decision Log при выборе;
- описаны migration/rollback;
- пройдены positive/negative tests;
- raw token/TG ID/HWID/UUID/password/API key не раскрыты;
- Marzban/Xray reread/verified;
- exact WL topology не изменилась либо изменение versioned/approved.
- manual/external-payment catalog, validation, audit или reconciliation change имеет changelog и Decision Log entry.
