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

### Security

- Устранён stored/DOM XSS в админ-панели: API-controlled User-Agent, username, note, node/config names, inbound tags и другие динамические значения теперь экранируются единым безопасным render path; inline JavaScript handlers удалены, включена строгая script CSP.
- Marzban JWT удалён из browser `localStorage` и API responses. Админка использует CSPRNG opaque server-side session cookie (`Secure`, `HttpOnly`, `SameSite=Strict`), CSRF protection, TTL, logout/revoke/rotation и защиту от session fixation/login CSRF.
- Прямые browser-вызовы Marzban заменены explicit server-side path/method broker; upstream auth failure отзывает локальную admin session, а legacy browser Bearer authentication больше не принимается.
- Privileged service-интеграция Marzban вынесена в отдельный HMAC-authenticated localhost-only broker с десятью typed legacy operations и без generic proxy. Основной MGBoost в broker mode отклоняет Marzban SUDO credentials в своём environment; public legacy `/sub/{token}` остаётся прямым non-SUDO path.
- Telegram Stars pre-checkout теперь fail-closed проверяет доступность и eligibility целевого Marzban user перед подтверждением новой оплаты; уже полученный `successful_payment` по-прежнему сначала сохраняется durable и остаётся retryable при outage.
- Admin login получил failed-attempt rate limiting с отдельными IP+username/IP-spray budgets, `429`/`Retry-After`, validated proxy IP и bounded hashed in-memory keys; direct public Marzban `/api/admin/token` получил nginx `limit_req` deployment template.

### Changed

- После обновления администратору потребуется войти заново: legacy `mz_token` очищается, а admin asset использует новый cache-buster. Текущие admin sessions являются process-local и завершаются при перезапуске MGBoost.

### Operations

- Добавлены `ADMIN_SESSION_TTL_SECONDS` и `ADMIN_SESSION_COOKIE_SECURE`; Secure cookie обязателен в production HTTPS, отключение разрешено только для локальной HTTP-разработки.
- Production permissions существующих Marzban secrets/data сужены: `.env`, live SQLite, Xray credential config и найденные config backup copies теперь `0600 root:root`, private Marzban data directory — `0700`. MGBoost active `.env`/SQLite уже соответствовали `0600` и сохранены; runtime, access-denial и временный backup/restore smoke проверены.
- Регулярные MGBoost/Marzban DB backup jobs на production не обнаружены; их создание и утверждённая retention/quarantine policy остаются отдельным PH1-06, а не считаются выполненными этой permission-задачей.
- MGBoost production service переведён с root на dedicated `mgboost` system user. Systemd unit теперь ограничивает capabilities, filesystem writes, `/proc`, namespaces/devices/home/tmp и использует restrictive `UMask=0077`; effective exposure score снижен с 9.6 `UNSAFE` до 2.8 `OK`, runtime/SQLite/HTTP smoke пройдены.
- Добавлен обязательный no-user-impact gate перед PH1-05: зафиксированы production legacy subscription/config/UUID/expiry/HWID contracts, отдельная localhost broker topology, exact current Filin/Stars/LK/bot operation matrix, outage/rollback требования и regression tests. До реализации и staging-проверки broker полный security cutover помечен `NOT SAFE TO DEPLOY`.
- PH1-01 и PH1-05 развернуты на production отдельными verification gates. Владелец вручную подтвердил admin login без визуальной регрессии; exact legacy alias, aggregate UUID/expiry/access/config и 71 device/HWID bindings совпали до/после и после restart. PH1-05 broker работает отдельным Unix identity на `127.0.0.1:8002`, не опубликован nginx и не читает repository `.env`; основной MGBoost больше не содержит Marzban SUDO keys. Broker outage оставляет legacy `/sub` рабочим, а privileged/Filin operations fail closed и восстанавливаются после restart. Изменений UUID, subscription URL/token, HWID, тарифов, expiry и обязательной перенастройки клиентов — 0.

### Documentation

- Добавлен канонический `ROADMAP.md`: current-state baseline, security remediation P0–P3, phases 0–8, утверждённые тарифы/packages, child-device и WL architecture, migrations, тестовые gates, Open Product Decisions и Decision Log.
- Зафиксировано правило обязательного одновременного обновления roadmap и changelog.
- Roadmap дополнен reseller architecture: explicit subscription source/ownership, scoped capabilities, reseller-aware migration, отдельные billing/WL/device semantics, audit/reconciliation и нерешённые продуктовые вопросы.
