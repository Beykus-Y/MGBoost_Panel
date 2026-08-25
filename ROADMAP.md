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

## [~] PH0-05 — Exact versioned WL topology

**Сделано:** 12 live tags и два nodes подтверждены.
**Осталось:** versioned config с node IDs/roles/assertions; fuzzy matching запрещён.
**Blocks:** PH6-01/06. **Tests:** exact live set, stale hosts excluded.
**Rollback:** prior allowlist применим только после live validation.

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

## [ ] PH2-06 — Subscription/API abuse controls — P2

**Depends:** trusted proxy + PH2-01.
**Scope:** rate/body limits, malformed/oversized IDs, deadlines, uniform failures, trusted XFF only from nginx.
**Accept/tests:** один client не блокирует/лимитирует других; fuzz/slow/burst/spoofed-XFF.
**Rollback:** versioned, scoped, time-limited relaxation.

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

## [~] PH4-03 — Internal canary migration

**Depends:** PH3-06/09, PH4-01/02.
**Cohort order:** internal users -> несколько DIRECT/Stars subscriptions -> несколько DIRECT/external-payment subscriptions -> mass migration. Не считать internal-only canary достаточным.
**Accept:** representative clients прошли migrate/device rebind/revoke и admin-only Telegram ownership rebind; account identity, payment provenance и manual renewal flow сохраняются; metrics/support runbook готовы.
**Tests:** real client matrix, cached/direct UUID, Stars/external channel and renewal assertions; Telegram rebind preserves account/devices/history and ordinary flow preserves token/UUID, while compromise flow rotates opaque token. **Rollback:** pause cohort, preserve evidence/new child state.

**Сделано:**
- Real internal-cohort canary on production account 1 (`beykusios`): controlled migrate/revoke/free on a synthetic device slot, legacy user and the account's real live device untouched throughout (see `AGENT_HANDOFF.md` history).
- Reviewed DIRECT enrollment foundation (additive, dormant, no route wiring): `src/direct_enrollment_schema.py` (`mgboost_direct_enrollment_intents`, `mgboost_direct_account_reviews` -- separate from and never touching PH3-06's INTERNAL-only `mgboost_internal_account_reviews`) and `src/direct_enrollment.py` (`DirectEnrollmentStore`). `AccountStore.create_account('DIRECT')` is the only account-creation path used; a durable pre-account-creation intent row makes retries after a crash converge on one account instead of allocating an orphan. Ambiguous ownership fails closed (nothing written); one legacy username can never bind to two accounts (checked in-application and backstopped by the existing DB `UNIQUE` constraint). `TELEGRAM_STARS`: only `paid`/`plan_committed`/`applied` `stars_invoices` become a canonical `mgboost_payment_records` row via the existing `ProvenanceStore`; refused/refunded/manual-review invoices and payer/ownership mismatches are rejected; duplicate invoice recording is idempotent. Minimal admin-only `record_external_payment()` primitive covers `payment_channel='EXTERNAL_PAYMENT'`/`mutation_source='MANUAL_PAYMENT'` as a PH5-09 prerequisite only (PH5-09 itself is not implemented). One orchestration flow (`process_direct_stars_enrollment`) converges to exactly one account/alias/payment across a simulated crash/retry. 16 focused tests (`tests/test_direct_enrollment.py`) plus full regression (`842 passed, 3 skipped`, zero regressions).
**Осталось:** no real DIRECT/Stars or DIRECT/external-payment account has been enrolled or migrated yet -- this phase only adds the reviewed-enrollment/payment-provenance foundation those cohorts need. Still blocked on the owner supplying (or authorizing selection of) real DIRECT/Stars and DIRECT/external-payment candidate identities; real PH3-05 device revoke and real PH2-05 ownership rebind on a non-internal account; metrics/support runbook.

## [ ] PH4-04 — New opaque URL rollout

**Depends:** canary, PH2-01. **Scope:** Telegram/LK/admin показывают новый URL; no query/log leak; secure one-time presentation/reissue.
**Accept:** новые accounts не зависят от legacy URL. **Tests:** full journey/rotation/log canary.
**Rollback:** pause issuance; issued token state сохраняется.

## [ ] PH4-05 — Approve and implement legacy grace period

**Depends:** telemetry. **Fixed policy:** OPD-09/DL-023 — grace period 14 дней.
**Accept:** per-account/cohort start/end, communications, support and metrics. **Tests:** exact UTC boundary at 14 days, inactive clients.
**Rollback:** extension только explicit/audited; revoked UUID не reopen.

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

## [ ] PH5-01 — Versioned six-plan catalog

**Depends:** PH3-01. **Scope:** stable plan codes/versions, approved prices/durations/devices/WL; independent Stars and `RUB-2026-08-23-v1` channel-price tables. Current free-form `stars_tariffs` requires migration.
**Accept/tests:** exact 12 plan-duration combinations in each applicable price channel; RUB values exactly match DL-040; invoice/payment snapshots unchanged by later catalog edits.
**Migration:** current 199/349 rows map only via explicit production plan; canary archived/retained by decision.

## [ ] PH5-02 — 30/60-day entitlement and WL-period semantics

**Depends:** PH5-01, PH6 period interface. **Policy:** 60d = two sequential 30d WL periods; Non-WL unlimited. По OPD-40/DL-044 повторная покупка того же plan — renewal с формулой `max(current_expiry, now) + purchased_duration`; накопленный срок создаёт последовательные 30-day WL periods, а не объединённый base quota.
**Accept:** purchase создаёт expiry/schedule без сброса WL на plain expiry admin action; active subscription продлевается от current expiry, expired — от текущего момента; каждая успешно оплаченная покупка добавляет duration ровно один раз.
**Tests:** boundary, second/следующие periods стартуют ровно один раз со fresh base quota; active/expired formula; repeated equal durations; timezone semantics explicit.
**Rollback:** immutable scheduled periods/invoice snapshot preserved.

## [ ] PH5-03 — Versioned WL package catalog

**Depends:** PH5-01/02 and entitlement ledger. **Fixed policy:** OPD-02/03/04/12/13/32 and DL-025–027 — rollover/freeze, Base rejection, base-first consumption and unused-only refund. **Approved Stars:** +50/79, +100/149, +250/349, +500/599. **Approved RUB v1:** +50/139, +100/249, +250/579, +500/999. Purchase/use only on WL-enabled plans.
**Accept:** invoice snapshot, eligibility, rollover bucket, adjustment/audit durable; period reset не удаляет remainder; expiry/Base transition freezes bucket; unused-only refund atomically revokes it.
**Tests:** all packages, base-first consumption, multiple period transitions, freeze/resume, stale callback, Base rejection, zero-consumption refund, partial-consumption refund denial, duplicate payment.
**Rollback:** stop sales; paid grant follows recorded product version.

## [ ] PH5-04 — Deterministic entitlement engine

**Depends:** PH3-01, PH5-01–03. **Inputs:** plan, packages, admin adjustments/overrides, slot add-ons, period.
**Accept:** one function returns effective expiry/device/WL plus explanation; no username hardcode.
**Tests:** combinations, deductions, unlimited/internal, expired plans.
**Migration:** calculation version pinned to migrated account.

## [ ] PH5-05 — Stars purchase + renewal

**Depends:** PH5-01/04; сохранить текущие payer/currency/amount/CAS/refund/reconcile strengths.
**Scope:** distinguish purchase/renewal, product version, outbox entitlement and child expiry sync. Повторная покупка того же plan всегда renewal; покупка другого plan проходит PH5-06 и не использует stacking.
**Accept/tests:** atomic/idempotent apply; repeated и concurrent successful payments каждого добавляют срок ровно один раз; duplicate callback не даёт double grant; crash/retry восстанавливает единственный apply; mismatches manual-review.
**Migration:** old invoices остаются expire-only snapshots, не переинтерпретируются.

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

## [ ] PH5-09 — Manual external-payment record and entitlement application

**Depends:** PH3-09, DL-034–036/040, admin session/audit. RUB catalog data blocker закрыт.
**Actor/channel:** только основной MGBoost admin; account source DIRECT, payment channel `EXTERNAL_PAYMENT`, mutation source `MANUAL_PAYMENT`.
**Store:** immutable plan/version/fixed-price-table version snapshot, exact amount, currency, payment method, external reference/comment, admin actor, timestamp, target account, result/idempotency/reconciliation. Telegram Stars не обязательны.
**Lifecycle:** pending record можно исправить до apply с audit before/after; после apply исходная запись immutable, исправление только append-only compensating operation с reason/reference.
**Accept:** первый rollout принимает только RUB; frontend не inject arbitrary account/plan/price/days/GB; versioned RUB catalog и exact amount проверяются server-side; duplicate reference/action не double-grant. Никаких balance, wholesale, margin или reseller settlement entities.
**Tests:** account IDOR, non-RUB rejection, stale price version, amount/price/plan tamper, pending edit/apply race, applied-record edit denial, compensating operation, negative days/GB, duplicate/replay/reference collision, manual package eligibility/refund, partial remote failure.
**Migration/rollback:** historical facts импортируются только по evidence; unknown amount/channel остаётся unknown, entitlement correction после apply только через compensation.

## [ ] PH5-10 — Manual external-payment renewal of the same parent account

**Depends:** PH3-08/09, PH5-04/09, outbox.
**Scope:** admin-confirmed external payment того же plan продлевает existing parent по `max(current_expiry, now) + purchased_duration`, синхронизирует active child expiry, сохраняет slots/HWIDs/current WL period и UUID без revoke причины. Другой plan направляется в PH5-06.
**Accept:** retry cannot create new parent/root user or double-add days; admin actor/payment/reference/channel recorded.
**Tests:** repeated same-duration purchase, concurrent Stars/admin/manual renew, duplicate callback/reference, crash/retry, 12 children, remote partial failure; каждый unique successful payment применяется ровно один раз.
**Rollback:** durable target and reconciliation/compensation; never subtract guessed days or restore stale child state.

# Phase 6 — WL quota

## [ ] PH6-01 — Runtime topology allowlist/assertions

**Depends:** PH0-05. **Scope:** exact 12 tags + exact two node IDs/roles/coefficient; stale exclusion; config version on events.
**Accept:** mismatch blocks destructive enforcement and alerts.
**Tests:** missing/extra/renamed/stale tag, non-WL traffic assertion. **Rollback:** prior validated version after live reread.

## [ ] PH6-02 — Immutable WL periods

**Depends:** PH5-02; units/anchor закрыты DL-016/DL-020.
**Fields:** UTC-hour-aligned start/end, decimal quota bytes (GB x 1,000,000,000), source/reason, closed/successor; ADMIN_RESET closes and creates, never rewrites consumed. Subscription expiry хранится отдельно.
**Tests:** two periods for 60d, overlap/gap, UTC/partial-hour/reset.
**Migration:** explicit initial period; no guessed historical usage.

## [ ] PH6-03 — Durable monotonic usage ledger/collector

**Depends:** PH6-01/02. **Require:** unique period/child/node/sample-hour, idempotency, non-decreasing usage, cursor/snapshot, one leader or CAS/shared lock, retry/reconcile.
**Caveat:** Marzban UTC-hour aggregation requires own snapshot/delta or UTC-hour alignment.
**Tests:** duplicate/out-of-order/node reset/two collectors/clock skew/delay.
**Rollback:** pause, retain ledger/cursor, replay idempotently.

## [ ] PH6-04 — Default shared parent WL pool

**Depends:** PH6-02/03 + children. **Policy:** sum child usage on two WL nodes; at quota disable all children. Family использует один общий 150 GB parent pool; optional advanced per-device allocation идёт через PH6-05, purchased traffic увеличивает этот же parent pool.
**Accept:** 60+20+10 = 90/100; at 100 disable exactly once.
**Tests:** concurrent children, slot changes, unlimited/non-WL. **Rollback:** derive desired from ledger, no consumed edit.

## [ ] PH6-05 — Optional manual per-device allocation

**Depends:** PH6-04. **Policy:** consumed monotonic; move only unspent; allocation >= consumed; device cap disables its child only.
**Transition policy (OPD-18):** shared/manual можно переключать в current period; перераспределяется только unspent remainder.
**Tests:** allocated 60/consumed 50 cannot shrink to 10; concurrency/shared-mode transition, repeated transition cannot refund consumption.

## [ ] PH6-06 — Exact inbound-only state machine — P2

**Depends:** PH1-05, PH6-01, children.
**States:** `ACTIVE -> DISABLE_PENDING -> DISABLED`; `DISABLED -> ENABLE_PENDING -> ACTIVE`; mismatch `ERROR_RECONCILE`. Local DB holds desired.
**Remote:** reread user, change only `inbounds.vless`, exact WL set, never proxies/UUID/expire/data_limit, then verify.
**Accept/tests:** WL blocked on cached/direct hosts, Non-WL works, offline node/retry/stale object/idempotency.
**Rollback:** compensating desired transition, never blind full-object restore.

## [ ] PH6-07 — Transactional outbox/reconciliation

**Depends:** PH6-06. **Scope:** local transaction writes quota desired+event; worker calls/rereads/verifies/observed/retries; periodic reconciliation.
**Tests:** crash before/after DB/remote/ACK, duplicates/restart/outage.
**Rollback:** worker pause leaves pending events; no blind rollback when remote succeeded.

## [ ] PH6-08 — Effective quota/adjustment ledger

**Depends:** PH5-03/04; package lifecycle decisions закрыты. **Breakdown:** base+purchased rollover+admin grant-deduction=effective; consumed/remaining; append-only package buckets/compensations. Base quota расходуется первой. Package remainder переносится через periods, не обнуляется reset, а при expiry/non-WL plan замораживается до возврата WL entitlement.
**Tests:** example 100+50+20-10=160; base-first, multi-period carry, freeze/resume, unused refund/stack; 83 consumed vs 60 effective -> exceeded.
**Rollback:** compensating entry only.

## [ ] PH6-09 — Overshoot/outage fail-safe

**Depends:** PH6-03/07. **Scope:** cadence/headroom, bounded overshoot, DB/Marzban/node outage, fail closed for activate/restore, never global disable on uncertain topology.
**Tests:** each outage/recovery. **Accept:** bound and alerts documented.

## [ ] PH6-10 — Subscription UX after exhaustion

**Depends:** PH6-06. **Fixed policy:** OPD-11/DL-028 — один informational placeholder, например `🔒 WL исчерпан • сброс <date>`; no `0.0.0.0` enforcement.
**Accept/tests:** placeholder безопасно parses во всех supported clients и объясняет reset; реальные WL hosts не дают доступ, block остаётся Marzban/Xray.
**Rollback:** remove decoration without enabling WL.

## [!] PH6-11 — Reseller-wide WL/package isolation — superseded 2026-08-23

**Причина:** отдельного reseller account/shared pool нет. External payment — канал прямой end-customer subscription.
**Сохранённое правило:** quota/packages/device slots всегда принадлежат конкретному parent account и не зависят от payment channel; это покрывают PH6-02–08. Manual package grant проходит PH5-09 с теми же eligibility/idempotency rules.

# Phase 7 — Admin controls

## [ ] PH7-01 — Expiry operations and child sync

**Depends:** PH3-08/outbox. **Ops:** +7/+30/+60, -N, exact date, end now; no WL reset.
**Accept/tests:** preview/reason, all children converge, timezone/concurrent Stars/12 children.
**Rollback:** audited compensating expiry, no raw DB edit.

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

## [ ] PH7-05 — Device slot administration

**Depends:** Phase 3. **Display:** slot/type, masked HWID/UUID, name, child, dates, traffic/WL, status, desired/observed.
**Ops:** unbind/disable/enable/revoke/free/rebind/add/remove/restore baseline.
**Accept/tests:** old UUID fails all nodes; permissions/stale UUID/partial failure/12 slots.

## [ ] PH7-06 — Explicit conflict resolution on limit reduction

**Depends:** PH7-05 and ticket workflow. **Fixed policy:** OPD-07/08 and DL-021/022 — downgrade только через ticket; active 5 -> limit 3 требует явного выбора, no silent automatic choice.
**Tests:** conflict/no conflict/concurrent slot/state changed before confirm.
**Rollback:** explicit new generation only.

## [ ] PH7-07 — WL override AUTO/FORCE_ENABLED/FORCE_DISABLED

**Depends:** PH6-06. **Fixed policy:** OPD-17/36 and DL-033/037 — affects desired only, never history; every FORCE override has mandatory expiry/reason, duration at most 90 days, and atomically returns to AUTO after expiry. FORCE enabled for internal/compensation, disabled independent of remainder. Purchase/renewal eligibility always follows actual plan; purchase/renewal neither clears override nor lets FORCE_ENABLED buy WL packages on a non-WL plan.
**Authority:** unlimited grants only by primary MGBoost admin.
**Tests:** each override with exhausted/available/unlimited, reject >90 days, expiry-to-AUTO, renewal during override, Base+FORCE_ENABLED package rejection, non-primary unlimited denial. **Rollback:** audited return AUTO.

## [ ] PH7-08 — Immutable administrative audit trail

**Depends:** PH1-01/PH2-05 actor model. **Fields:** who/when/account/operation/old/new/reason/source/order/correlation.
**Scope:** GB/reset/device/slot/expiry/plan/override/token/migration/revoke/Telegram ownership rebind; rebind stores old/new Telegram ID, primary-admin actor, reason and timestamp; no raw secrets.
**Tests:** complete before/after for success/failure/partial/reconcile, redaction; rejected and successful rebind actor/target assertions; Telegram IDs absent from unrelated logs/exports.
**Migration:** preserve current `audit_log` as legacy evidence.

## [ ] PH7-09 — Safe plan/entitlement admin

**Depends:** PH5-04/06 and admin ticket workflow. **Fixed policies:** approved OPD/DL decisions referenced by PH5-04/06 and PH7-06/07; no open plan/entitlement-transition decision remains for this task. **Scope:** preview effective change/conflicts/schedule/reasons/confirmations; no raw counters.
**Tests:** plan matrix, invalid transition, concurrent payment. **Rollback:** compensation with snapshot.

## [ ] PH7-10 — Manual external-payment admin UI

**Depends:** PH3-09, PH5-09; только основной admin.
**Account UI:** payment channel, plan, expiry, WL/devices; first rollout фиксирует RUB и позволяет выбрать только versioned fixed RUB price, exact amount/method/reference/comment с preview entitlement change. Pending можно исправить до apply; applied record read-only, correction создаётся отдельной compensation action.
**No separate reseller UI/login.** Raw subscription bearer, UUID и full HWID не нужны для payment operation.
**Controls:** backend проверяет admin session, target account и fixed catalog; confirmation/reason mandatory.
**Tests:** account IDOR, masked fields, non-RUB/manipulated plan/price/days/GB, pending edit/apply race, applied edit denial, duplicate reference and confirmation.

## [ ] PH7-11 — Immutable manual-payment mutation audit

**Depends:** PH7-08, PH5-09.
**Sources:** `SYSTEM`, `DIRECT_PURCHASE`, `MANUAL_PAYMENT`, `ADMIN`, `MIGRATION`, `PACKAGE`, `INTERNAL`.
**Fields:** admin actor, end account, operation, old/new, reason, amount/currency/method/reference, timestamp, result, idempotency and retry/reconciliation state.
**Scope:** manual create/link, renew, plan, package, expiry, refund/correction and failed/denied attempts.
**Accept/tests:** every mutation/reconciliation emits correlated append-only events; raw security credentials excluded; audit editable only through compensating event, never reseller API.

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
- PH0-05/PH6-01 block inbound removal.
- PH6-02/03/06/07 block WL sales/enforcement.
- PH5-01/03/04/05 and payment reconciliation block package sales; OPD-02/03/04/12/13/32 policies are already closed and are not blockers.
- PH4-05/06 plus successful migration verification block final legacy revoke; OPD-09 already fixes the grace period at 14 days and is not a blocker.
- PH2-03/PH3-02/PH6-03 block multi-worker.

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
