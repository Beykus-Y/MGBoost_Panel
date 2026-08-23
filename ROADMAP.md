# MGBoost Panel — canonical roadmap

Статус: **главный источник истины для модернизации MGBoost Panel**.
Baseline: main / `ccc1b4d` и production на 2026-08-23.
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

## [x] PH1-01 — Admin stored XSS и безопасная server session — P0

**Completed:** 2026-08-23. **Depends:** none; выполнена первой.
**Files:** `frontend/assets/admin.js`, `frontend/index.html`, `src/config.py`, `src/http_utils.py`, `src/marzban.py`, `src/security.py`, `src/server.py`, `src/routes/admin_session.py`, `src/routes/admin_proxy.py`, `src/routes/panel.py`, `.env.example`, `README.md`, `tests/test_admin_sessions.py`, `tests/test_admin_proxy.py`, `tests/test_admin_frontend_security.py`, `tests/test_admin_browser_e2e.py`.
**Implemented:** все admin API/user-controlled значения проходят через единый escaping `SafeMarkup`/DOM render path; inline event attributes удалены и заменены event delegation; Marzban JWT удалён из browser storage/API responses и хранится только в process-local server session; введены CSPRNG opaque session ID и CSRF token, hashed in-memory lookup, absolute TTL, Secure/HttpOnly/SameSite=Strict cookie, logout/revoke/rotation и fixation-safe login; mutation routes требуют constant-time CSRF check; cross-site-form-compatible login requests отклоняются; browser Marzban access переведён на explicit server-side path/method broker; upstream 401/403 отзывает local session; admin SPA получила strict script CSP, no-store/referrer/frame/nosniff headers. Текущий process-local store соответствует single-worker baseline и не разрешает multi-worker до PH8-02.
**Acceptance evidence:** malicious User-Agent, note, username, node/config name, inbound tag и URI отображаются только как text; production CSP исполняется без inline JS; browser не получает Marzban JWT; legacy Bearer auth отклоняется; logout, expiry, rotation и upstream auth failure инвалидируют session.
**Tests:** `test_admin_sessions.py` (server-only JWT, cookie flags, login guard, fixation, CSRF, expiry, logout, rotation), `test_admin_proxy.py` (allowlist/query/path/auth-failure), `test_admin_frontend_security.py` (sink/source matrix и реальный JS render path), `test_admin_browser_e2e.py` (headless Chromium, malicious API values под production CSP). Full regression: 341 passed, 1 browser test additionally passed in Playwright environment.
**Migration/rollback:** admin asset получил новый cache-buster; SPA посылает `Clear-Site-Data: "storage"` и JS удаляет legacy `mz_token`. После deploy существующие browser JWT sessions потребуют новый login. Rollback допустим только к server-side session implementation и никогда не должен возвращать JWT в JavaScript/localStorage.

## [ ] PH1-02 — Marzban SUDO CSPRNG rotation и login rate limit — P1

**Depends:** выполненный PH1-05 external mutation boundary и maintenance coordination. Credential storage/caller cutover code завершён PH1-05; здесь остаются фактическая production rotation, login rate limit и проверка JWT invalidation/lifetime. Это устраняет прежний circular dependency PH1-02↔PH1-05. **Files:** `src/marzban.py`, broker env, Marzban auth/config.
**Scope:** service credential >=128-bit; correct form encoding; atomic rotation; rate limit; проверить JWT invalidation/lifetime.
**Accept/tests:** старый password/JWT не работают; 429 brute-force; special characters и outage ordering; broker smoke.
**Migration/rollback:** controlled secret delivery; отдельный не логируемый rollback credential.

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

**Completed locally/staged:** 2026-08-24; production deployment не выполнялся. **Depends:** PH1-01 server-session boundary, PH0-08 caller inventory и обязательный no-user-impact gate DL-043; не зависит от завершённой rotation PH1-02. **Files:** `broker_main.py`, `src/broker_protocol.py`, `src/broker_operations.py`, `src/broker_server.py`, `src/service_marzban.py`, `src/legacy_contract.py`, `src/routes/internal.py`, `src/routes/lk.py`, `src/bot_support.py`, `src/routes/admin.py`, `mgboost-marzban-broker.service`, `mgboost-panel.service`, `broker.env.example`, `.env.example`, `README.md`, `docs/PHASE1_BACKWARD_COMPATIBILITY.md`, `scripts/verify_broker_against_staging.py`, `scripts/verify_legacy_sub_restart_staging.py`, `tests/test_marzban_broker.py`, `tests/test_phase1_legacy_compat.py`.
**Выбранная topology (DL-043):** отдельный localhost broker-service хранит/получает Marzban service SUDO credential; основной MGBoost после cutover не содержит этот credential в environment. Broker не публикуется через nginx. Public Marzban `/sub/{legacy_token}` и `/sub/{legacy_token}/info` остаются direct non-SUDO paths, поэтому broker outage не должен останавливать legacy subscriptions. Browser admin использует только user-entered server-side Marzban session PH1-01, не service credential.
**Implemented Stage A:** exact typed allowlist, HMAC-SHA256/timestamp/nonce/body-hash caller authentication, replay rejection, loopback-only literal bind/URL validation, bounded HTTP concurrency, safe target pseudonym audit и deny unknown operation/field/path. Filin create/renew/delete сохранены.
**Implemented Stage B code/config boundary:** service bot/LK/Stars/internal calls используют broker facade; лишний SUDO lookup из public token-info linking удалён; main startup fail-fast отклоняет наличие `MARZBAN_ADMIN_USER/PASS` в broker mode. Фактическая production credential rotation остаётся PH1-02 и не выполнялась.
**Current legacy allow:** user get/usage/list, node list/usage, inbound list, exact current create, renewal (`add_days`/`expire`/`data_limit`/`status`), Stars expire-only update и exact current delete. Это transitional compatibility surface, а не будущая entitlement API.
**Future narrow allow после legacy retirement:** lookup, child create, expire, enable/disable, only `inbounds.vless`, traffic/node reads. **Always deny:** raw SUDO/JWT exposure, arbitrary URL/path, unknown fields and untyped destructive payloads. Не менять UUID/proxies/inbounds/config/expiry semantics вне явно выбранной operation.
**Acceptance evidence:** все 10 legacy operations прошли direct-vs-broker response/effect comparison на isolated official Marzban 0.8.4 VLESS staging. HMAC Filin create/renew/delete пройдены через реальный MGBoost HTTP route. Broker/Marzban/MGBoost restart, broker outage, Marzban outage/recovery, pre-effect partial failure, Stars post-effect recovery и rollback-direct contracts пройдены. Legacy `/sub` остался byte-identical при broker up/down и после MGBoost restart; main test process имел 0 Marzban SUDO env keys. Production read-only validation: 25 users, 0 username-contract mismatches, 0 missing VLESS UUID, 0 missing subscription URL.
**Tests:** `375 passed, 1 skipped`; `tests/test_marzban_broker.py` покрывает all-10 allowlist, HMAC/replay/deny/outage/restart/partial/Filin/rollback; Stars suite покрывает checkout fail-closed, durable paid retry и exact-expire recovery; reproducible staging scripts перечислены выше. Browser E2E skip относится к отсутствующему Playwright в текущем base environment и был пройден отдельно для PH1-01; PH1-05 frontend не меняет.
**Known preserved legacy behavior:** Filin `data_limit=0` преобразуется старым кодом в JSON `null`; Marzban 0.8.4 при partial update оставляет прежнее значение. PH1-05 сохраняет это буквально. Filin add-days не имеет durable operation ID, поэтому blind retry после unknown response остаётся неоднозначным legacy contract; durable/shared idempotency относится к PH2-03 и не заменяет безопасный Stars recovery.
**Migration/rollback:** production не выкатывался. Cutover не требует DB/user migration. Emergency direct-call rollback задаёт `MARZBAN_SERVICE_MODE=direct`, возвращает текущий service credential только main process и не меняет user UUID/token/expiry/HWID; после восстановления broker credential снова изолируется. Подробный pre/post runbook — compatibility report.

## [ ] PH1-06 — Stop raw subscription leakage/controlled rotation — P1

**Depends:** PH2-01 for final reissue; backup/restore verification and confirmed rotation/reissue strategy before cleanup. Retention policy itself is fixed by DL-042. **Files:** `src/server.py`, `src/database.py`, `src/routes/sub.py`, `src/routes/lk.py`, nginx/journal.
**Scope:** redact sensitive paths; no new raw-token DB/query/log entries; inventory and classify legacy evidence before cleanup. По DL-043 Phase 1 не меняет legacy user subscription URL/token: controlled user-token rotation/reissue выполняется только staged migration Phase 4. Rotation service/admin credentials остаётся обязательной и не отменяется наличием credential в retained backup/quarantine snapshot.
**Fixed retention (DL-042):** sensitive legacy nginx/application/journal logs — 30 days; ordinary operational logs without credentials — maximum 60 days; regular DB backups — 90 days. Before deleting legacy token evidence, create exactly one encrypted quarantine snapshot and retain it 180 days. Quarantine access is limited to the minimum necessary owner/service identity.
**Accept/tests:** canary bearer absent from app/nginx/journal/analytics and every new backup; retention classification, access-denial, encrypted quarantine restore and expiry/deletion dry-run verified; rotated service/admin credential works and retained copy no longer works. Existing legacy subscription aliases continue работать до Phase 4 revoke gate.
**Migration/cleanup:** canary cohort/support plan; verify backup and isolated restore; confirm Phase 4 user-token rotation/reissue strategy; create and verify one encrypted quarantine snapshot; rotate service/admin credentials; then controlled deletion after each retention boundary. No destructive cleanup or legacy user-token revoke before all prerequisites pass.

## [ ] PH1-07 — Patch applicable dependency DoS via staging — P2

**Scope:** Marzban `python-multipart 0.0.7`, MGBoost `aiohttp 3.10.11`, transitive inventory.
**Accept/tests:** patched immutable build; advisory payload/load/soak; Telegram/OpenRouter/proxy integration.
**Migration/rollback:** staging only first; no production in-place package upgrade; previous image digest retained.

## [ ] PH1-08 — Remove password from Marzban login notifications — P2

**Scope:** failed-login Telegram/Discord reports. **Accept:** password never enters report/log.
**Tests:** canary password absent everywhere. **Rollback:** never restore password field.

# Phase 2 — Security foundation

## [ ] PH2-01 — Random 256-bit MGBoost opaque subscription tokens — P2

**Depends:** PH1-06, PH3 account identity, Phase 4 migration.
**Never:** TG ID/hex/SHA256(TG_ID)/concatenated SHA. **Scope:** CSPRNG >=256-bit, hash/verifier DB, account binding/version, individual revoke/reissue; old token invalid after rotation; no full token logs.
**Ownership-rebind rule:** ordinary Telegram binding change does not rotate opaque token or child UUID. If primary admin marks suspected compromise, opaque token rotation is mandatory in the same recovery workflow; child UUIDs remain unchanged unless a separate device/credential revoke is explicitly requested.
**Target:** `sub.beykus.fun/{opaque_token}` with route collision contract.
**Accept/tests:** DB leak не даёт URL; entropy/tamper/enumeration/timing; per-token revoke; ordinary ownership rebind preserves token; compromise flow invalidates old opaque token and issues a new one without implicit UUID rotation.
**Migration/rollback:** versioned legacy alias; revoked token никогда не реактивируется.

## [ ] PH2-02 — LK device-name XSS и inline handlers — P2

**Depends:** PH1-01 frontend conventions. **Files:** `frontend/assets/lk.js`, `frontend/lk.html`, весь frontend.
**Scope:** `textContent`, opaque dataset IDs, `addEventListener`; no inline onclick.
**Accept/tests:** quotes/entities/tags/backslashes остаются текстом; CSP без unsafe-inline; rename/delete E2E.
**Rollback:** не возвращать unsafe handlers.

## [ ] PH2-03 — Shared durable Internal HMAC replay protection — P2

**Keep:** HMAC-SHA256, timestamp, nonce, body hash, constant-time. **Add:** atomic shared nonce consume+TTL, idempotency/CAS.
**Accept/tests:** replay блокируется same/other worker и после restart/cache flood.
**Rollback:** fail closed при store outage.

## [ ] PH2-04 — Headers/cache/error hardening — P3

**Depends:** PH1-01, PH2-02 перед strict CSP.
**Scope:** no-store, Referrer-Policy, HSTS, CSP, frame protection, uniform invalid subscription, hide unnecessary docs/version.
**Tests:** header/frame/cache/referrer/status/body/timing. **Rollout:** CSP report-only first.

## [ ] PH2-05 — Admin/user session and ownership lifecycle

**Depends:** PH1-01, PH2-01.
**Scope:** logout/revoke/rotation/TTL/scopes, CSRF/Origin, Telegram ownership recovery/rebind, authz after authn.
**Fixed ownership recovery policy (OPD-39/DL-041):** первый rollout — только ручной rebind основным MGBoost admin; self-service recovery/codes отсутствуют. HWID и possession subscription URL не являются достаточным proof. После successful atomic rebind старый Telegram binding немедленно revoked; dual active ownership запрещён.
**Preserve:** тот же parent account, plan, expiry, WL periods, traffic history, device slots, HWIDs, child users и UUID. Обычный rebind не вращает opaque token/UUID; suspected compromise требует одновременной rotation opaque token по PH2-01.
**Audit:** immutable old/new Telegram ID, primary-admin actor, reason, timestamp, result/correlation; IDs не выводятся в общие application/access logs.
**Tests:** IDOR, CSRF, fixation, expiry/replay, multi-TG legacy ownership, non-primary denial, HWID-only/token-possession-only denial, atomic old-binding revoke, data/child/UUID preservation, ordinary no-rotation, compromise mandatory token rotation, partial-failure rollback/reconcile.
**Out of scope:** self-service recovery и recovery codes; возможны только через новое future product decision.

## [ ] PH2-06 — Subscription/API abuse controls — P2

**Depends:** trusted proxy + PH2-01.
**Scope:** rate/body limits, malformed/oversized IDs, deadlines, uniform failures, trusted XFF only from nginx.
**Accept/tests:** один client не блокирует/лимитирует других; fuzz/slow/burst/spoofed-XFF.
**Rollback:** versioned, scoped, time-limited relaxation.

## [ ] PH2-07 — No persistent raw upstream token in new resolver

**Depends:** PH2-01, PH1-05, PH3 children.
**Scope:** opaque token -> account/slot -> child config without stored raw legacy token; если нужен recoverable upstream secret, отдельно design encryption/rotation.
**Tests:** DB+source leak, revoke, broker outage. **Migration:** raw legacy exists only in marked bridge with retirement.

## [!] PH2-08 — Reseller tenant authentication/capabilities — superseded 2026-08-23

**Причина:** владелец уточнил, что отдельного reseller login/tenant нет; external payment применяет только основной MGBoost admin.
**Сохранённые security requirements перенесены:** PH1-01/05 и PH2-05 защищают admin session/broker; PH5-09 проверяет account/catalog/price/idempotency; negative tests запрещают arbitrary username/price/GB/free entitlement и raw bearer export. Shared Filin HMAC не является payment/admin session.

# Phase 3 — Parent account + child devices

## [ ] PH3-01 — Parent account/identity/entitlement schema

**Inputs (approved):** six-plan Stars catalog, `RUB-2026-08-23-v1`, OPD-06 device limits and OPD-14 WL pool model. **Entities:** accounts, Telegram identity links, plan versions, subscriptions, entitlements/overrides. Telegram ID не credential. No open product decision blocks schema design.
**Accept:** account независим от Marzban username; `billing_required`, WL quota и device limit вычисляются entitlement engine.
**Tests:** identity uniqueness/IDOR, plan snapshots, multiple identity policy.
**Migration/rollback:** additive schema/backfill preview; legacy data не удалять.

## [ ] PH3-02 — Atomic device slots with generation

**Depends:** PH3-01. **Entities:** BASE/ADDON/INTERNAL slot, stable number, generation, HWID verifier/masked form, desired/observed status, child mapping.
**Capacity policy:** current paid limits строго 3/6/12; future commercial/technical cap 99; INTERNAL configurable/unlimited. Add-on purchase сейчас не запускается.
**Accept:** DB atomically ограничивает entitlement capacity; один HWID -> тот же active slot/generation; не process `RLock`.
**Tests:** два запроса за последний slot, 3/6/12, future cap 99, INTERNAL unlimited, duplicates, multi-worker, slot generation reuse.
**Rollback:** additive; не переиспользовать старый credential generation.

## [ ] PH3-03 — Lazy idempotent child Marzban creation

**Depends:** PH1-05, PH3-02. **Scope:** child создаётся при первом занятии slot; отдельный UUID; не создавать 12 заранее.
**Accept:** 2 занятых Family slots = 2 child users; retry/timeout не создаёт duplicate/orphan.
**Tests:** remote-created/local-ACK-failed, collisions, concurrent requests.
**Rollback:** remote reread/reconcile до retry/delete.

## [ ] PH3-04 — HWID fail-closed compatibility gate

**Depends:** PH3-02/03, PH3-07 telemetry, implementation readiness of fixed admin-only ownership recovery in PH2-05. Product policy OPD-39 закрыта.
**Policy:** нет supported HWID -> config не выдавать; unknown+free -> assign; full -> clear refusal; known -> same slot/generation. HWID остаётся practical, не cryptographic identity.
**Accept/tests:** compatibility list опубликован; каждый client/version, missing/spoofed/copied HWID, reinstall/device rebind; HWID не принимается как proof Telegram ownership.
**Rollback:** staged feature flag только в migration window; не unlimited silent bypass.

## [ ] PH3-05 — Real child revoke/disable/free/rebind

**Depends:** PH3-03, broker. **Scope:** revoke/free инвалидирует Marzban/Xray UUID; rebind увеличивает generation; old cached config не работает.
**Deletion policy (DL-019/038):** немедленно revoke/disable, сохранить tombstone/history 180 дней; physical delete разрешён только после retention expiry, successful reconciliation и проверки отсутствия живых references. Immutable audit history сохраняется по своей policy.
**Accept/tests:** direct old UUID отклонён на всех nodes; offline-node reconcile; rebind race; delete до 180 дней запрещён; delete после 180 дней не выполняется при live reference/reconcile error.
**Rollback:** новый generation, никогда восстановление leaked UUID.

## [ ] PH3-06 — Internal/god entitlements без username hardcode

**Depends:** PH3-01. **Fixed policy:** OPD-15/DL-032 — versioned internal plans плюс explicit per-account overrides с обязательными expiry/reason; `billing_required=false`, WL unlimited, configurable/unlimited devices. Только primary MGBoost admin может выдать unlimited.
**Never hardcode:** `beykus*`, `megochel*`, `german`, `pensioner`, `client_buy_9`.
**Accept/tests:** special access только plan/entitlement; ordinary/non-primary admin не получает или не выдаёт unlimited flags; override expiry возвращает вычисление к plan/AUTO; source scan hardcodes.
**Migration:** internal accounts — canary cohort и тоже child users.

## [ ] PH3-07 — Privacy-safe HWID/client compatibility telemetry

**Depends:** PH1-06. **Scope:** aggregate client/version/HWID-present без raw token/full HWID; retention/access control.
**Accept:** supported/unsupported client share известна до fail-closed. **Tests:** redaction canary, aggregation, retention.
**Rollout:** observe-only -> decision -> staged enforce.

## [ ] PH3-08 — Parent expiry/status -> all children

**Depends:** PH3-01/03, outbox interface. **Scope:** idempotent child synchronization; expiry change не сбрасывает WL period.
**Accept/tests:** 1/3/6/12 children converge; partial failure visible; concurrent Stars/admin updates.
**Rollback:** reconcile from durable desired state.

## [ ] PH3-09 — Account/payment/mutation provenance model

**Depends:** PH3-01, PH0-08.
**Model:** account ownership/source остаётся `DIRECT` или `INTERNAL`; payment channel отдельно хранит `TELEGRAM_STARS`, `EXTERNAL_PAYMENT`, `ADMIN_GRANT`; mutation source включает `MANUAL_PAYMENT`. VPN account/credentials принадлежат end user, MGBoost остаётся authority children/UUID/devices/enforcement.
**Constraints:** payment record связан с account/entitlement/admin actor, но не меняет ownership; never infer channel from username/prefix.
**Accept:** WL/device limits всегда принадлежат end subscription независимо от payment channel; child creation идёт через один slot engine.
**Tests:** Stars/external/admin provenance, cross-account IDOR, duplicate reference, direct renewal without account replacement.
**Migration/rollback:** channel backfill только по evidence; иначе `UNKNOWN_LEGACY`, не выдумывать external payment.

# Phase 4 — Legacy migration

## [ ] PH4-01 — Legacy subscription alias bridge

**Depends:** PH2-01/07, PH3-01–05.
**Flow:** legacy account -> supported HWID -> find/assign slot -> lazy child -> child config; migrated device не получает shared UUID.
**Accept/tests:** valid/invalid/revoked legacy; missing HWID/full slots/repeat request.
**Rollback:** shared legacy credential остаётся до explicit revoke; no duplicate children.

## [ ] PH4-02 — Durable migration state machine

**Depends:** PH4-01. **States:** `LEGACY`, `MIGRATING`, `MIGRATED`, `LEGACY_REVOKE_PENDING`, `LEGACY_REVOKED`, `ERROR_RECONCILE`.
**Accept/tests:** durable/idempotent transitions; duplicate/crash boundary tests.
**Rollback:** после revoke backward transition запрещён; recovery выдаёт new credential.

## [ ] PH4-03 — Internal canary migration

**Depends:** PH3-06/09, PH4-01/02.
**Cohort order:** internal users -> несколько DIRECT/Stars subscriptions -> несколько DIRECT/external-payment subscriptions -> mass migration. Не считать internal-only canary достаточным.
**Accept:** representative clients прошли migrate/device rebind/revoke и admin-only Telegram ownership rebind; account identity, payment provenance и manual renewal flow сохраняются; metrics/support runbook готовы.
**Tests:** real client matrix, cached/direct UUID, Stars/external channel and renewal assertions; Telegram rebind preserves account/devices/history and ordinary flow preserves token/UUID, while compromise flow rotates opaque token. **Rollback:** pause cohort, preserve evidence/new child state.

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

**Depends:** PH5-01, PH6 period interface. **Policy:** 60d = two sequential 30d WL periods; Non-WL unlimited.
**Accept:** purchase creates expiry/schedule without resetting WL on plain expiry admin action.
**Tests:** boundary, second period starts once/fresh; timezone semantics explicit.
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
**Scope:** distinguish purchase/renewal, product version, outbox entitlement and child expiry sync.
**Accept/tests:** no double grant; mismatches manual-review; crash recovery.
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
**Scope:** admin-confirmed external payment extends the existing parent entitlement, synchronizes active child expiry, preserves slots/HWIDs/current WL period и UUID без revoke причины.
**Accept:** retry cannot create new parent/root user or double-add days; admin actor/payment/reference/channel recorded.
**Tests:** concurrent Stars/admin/manual renew, duplicate reference, 12 children, remote partial failure.
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
- **Execution evidence:** `docs/PHASE1_BACKWARD_COMPATIBILITY.md`; production read-only baseline 25 users/25 UUID/current URLs, 71 active device rows; `tests/test_phase1_legacy_compat.py`, `tests/test_marzban_broker.py` и два reproducible staging scripts. На 2026-08-24 all-10 direct-vs-broker, outage/recovery/restart, Filin HMAC и byte-identical legacy `/sub` gates пройдены; verdict обновлён на SAFE TO DEPLOY FOR EXISTING USERS при соблюдении documented cutover/preflight.
- **Связано:** PH1-01/02/05/06, PH2-04, Phase 3/4, Filin HMAC integration, deployment/rollback runbooks.

# Contradictions and migration hazards

1. Current production Stars 199/349 совпадает по цене с будущим WL, но schema не содержит plan/WL/device semantics; старые invoices нельзя молча переинтерпретировать.
2. Current HWID permissive: `src/routes/sub.py` проверяет только `hwid:`; fail-closed без telemetry сломает clients.
3. Current one-user/shared-UUID модель несовместима с child revoke/allocation; local device delete не отзывает UUID.
4. Current proxy требует raw Marzban token; простое hash-only изменение колонки сломает resolver. Нужен новый child-config path.
5. PH1-01 удалил frontend `localStorage` Marzban JWT и ввёл server session; PH1-05 изолировал service SUDO за typed localhost broker. Оставшиеся risks — process-local admin session state до PH8-02 и сам full-SUDO credential внутри broker до будущего narrow upstream authority.
6. PH1-03/04 закрыли world-readable sensitive files и root runtime MGBoost; Marzban container всё ещё root. После PH1-05 MGBoost/Filin не получают SUDO/JWT напрямую, но compromised main с broker HMAC key всё ещё может вызвать десять transitional allowlisted operations до их дальнейшего сужения.
7. Exact WL tags существуют только live, в repo пока нет authoritative versioned config; stale hosts делают fuzzy match опасным.
8. Marzban usage агрегируется по UTC-hour; rolling mid-hour period нельзя точно считать суммой whole-hour rows.
9. Current `audit_log` полезен, но не даёт immutable actor/before/after/reason для всех admin changes.
10. Current Stars меняет только expiry. New purchase/plan/package/period child sync требует entitlement/outbox, а old invoice snapshot должен сохраниться.
11. README честно описывает legacy LK bearer query/raw token logging; после PH1-06/PH2-01 это должно быть обновлено в том же change.
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
