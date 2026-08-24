# MGBoost Panel

Прокси подписок и админ-панель для Marzban VPN.

## Документация разработки

- [`ROADMAP.md`](ROADMAP.md) — канонический источник истины для будущей модернизации, security remediation, миграций и продуктовых решений.
- [`CHANGELOG.md`](CHANGELOG.md) — обязательный changelog пользовательских и операционных изменений.

Текущий README описывает действующую legacy-архитектуру. Он не заменяет roadmap. При реализации roadmap-задачи код, roadmap, changelog и затронутые разделы README должны обновляться вместе.

## Что умеет

- Проксирует `/sub/{token}` — перехватывает подписку Marzban, применяет фильтры и добавляет extra конфиги
- Добавляет глобальные extra конфиги (Hysteria2 и др.) ко всем подпискам
- Индивидуальные конфиги для конкретных пользователей
- Фильтрация нод по fragment URI — можно скрыть конкретные ноды для пользователя
- Учёт трафика Hysteria2 и добавление его в `subscription-userinfo`
- Логирование запросов подписок в SQLite (hash-reference токена, юзер, User-Agent, IP)
- Админ-панель (SPA) — управление пользователями, нодами, конфигами

## Требования

- Python 3.10+
- `pip install -r requirements.txt`

## Установка

```bash
git clone <repo> /opt/mgboost-panel
cd /opt/mgboost-panel
cp .env.example .env
nano .env          # заполнить MARZBAN_URL, SECRET_KEY и т.д.
pip3 install -r requirements.txt
python3 main.py    # проверить что запускается
```

## Настройка .env

```env
MARZBAN_URL=http://127.0.0.1:8000   # public subscription endpoints Marzban
MARZBAN_SERVICE_MODE=broker         # privileged calls only through localhost broker
MARZBAN_BROKER_URL=http://127.0.0.1:8002
MARZBAN_BROKER_AUTH_KEY=...         # >=32 CSPRNG bytes; not a Marzban credential
LISTEN_HOST=127.0.0.1
LISTEN_PORT=8001
DATA_DIR=./data                      # папка с БД и JSON
SECRET_KEY=changeme                  # поменяй!
ADMIN_SESSION_TTL_SECONDS=1800       # абсолютный TTL admin session
ADMIN_SESSION_COOKIE_SECURE=1        # обязательно 1 в production HTTPS
PRIMARY_MGBOOST_ADMIN_ACTOR_ID=      # empty = dormant PH3-06 writes fail closed
PRIMARY_MGBOOST_ADMIN_LOGIN=         # authenticated server-side login allowlist
DEVICE_SLOT_HMAC_KEY=                # future PH3 slot verifier key; >=32 CSPRNG bytes
ADMIN_LOGIN_RATE_WINDOW_SECONDS=300  # окно failed-login limiter
ADMIN_LOGIN_RATE_IDENTITY_FAILURES=5 # failures на IP+username за окно
ADMIN_LOGIN_RATE_IP_FAILURES=20      # общий IP spray budget за окно
```

Для локальной разработки по обычному `http://127.0.0.1` допустимо временно установить
`ADMIN_SESSION_COOKIE_SECURE=0`. В production это значение должно оставаться `1`.

## Аутентификация админ-панели

Браузер отправляет логин и пароль Marzban только в MGBoost endpoint
`POST /admin/session/login`. MGBoost получает Marzban JWT server-side и хранит его в
process-local session store; JWT не возвращается в JavaScript и не сохраняется в
`localStorage`. Браузер получает только opaque session cookie с
`Secure`, `HttpOnly`, `SameSite=Strict` и ограниченным TTL.

Изменяющие admin-запросы требуют CSRF token из текущей session. Logout, expiry и
rotation инвалидируют соответствующую server session. Перезапуск текущего
single-process MGBoost принудительно завершает все admin sessions; shared session
store понадобится только перед будущим multi-worker rollout.

## Деплой (systemd)

```bash
groupadd --system mgboost
useradd --system --gid mgboost --home-dir /nonexistent --shell /usr/sbin/nologin mgboost
groupadd --system mgboost-broker
useradd --system --gid mgboost-broker --home-dir /nonexistent --shell /usr/sbin/nologin mgboost-broker
chown root:mgboost /opt/MGBoost_Panel/.env
chmod 640 /opt/MGBoost_Panel/.env
chown -R mgboost:mgboost /opt/MGBoost_Panel/data
find /opt/MGBoost_Panel/data -type d -exec chmod 700 {} \;
find /opt/MGBoost_Panel/data -type f -exec chmod 600 {} \;
install -d -m 750 -o root -g mgboost-broker /etc/mgboost
install -m 640 -o root -g mgboost-broker broker.env.example /etc/mgboost/marzban-broker.env
cp mgboost-panel.service /etc/systemd/system/
cp mgboost-marzban-broker.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now mgboost-marzban-broker mgboost-panel
systemctl status mgboost-panel
```

Команды создания user/group при повторном deploy должны выполняться идемпотентно
(`getent`/`id` перед `groupadd`/`useradd`). Unit запускает процесс без root и
разрешает запись только в `/opt/MGBoost_Panel/data`; source tree и system paths
остаются read-only для сервиса.

Production sensitive data must not be world readable. MGBoost `.env` использует
`0640 root:mgboost`, active data — `0600 mgboost:mgboost`, private data directory —
`0700`. Marzban `.env`/SQLite/Xray credential configs и backup copies используют
`0600 root:root`, private Marzban data directory — `0700`. Не применять broad
`0777`; owners меняются только для конкретного runtime identity.

## Настройка nginx

Замени блоки `location` в конфиге сайта на содержимое `nginx.conf.example`.

Было (убрать):
```nginx
location /sub-admin/ {
    alias /opt/sub_proxy/;
    index panel.html;
    ...
}
```

Стало (nginx только проксирует, сервер сам отдаёт SPA):
```nginx
location /sub-admin/ {
    proxy_pass http://127.0.0.1:8001/sub-admin/;
    ...
}
```

## Миграция с sub_proxy

При первом запуске `main.py` автоматически читает старые JSON файлы из `DATA_DIR` и переносит данные в SQLite:

- `extra_configs.json` → таблица `extra_configs`
- `per_user_configs.json` → таблица `per_user_configs`
- `node_filters.json` → таблица `node_filters`
- `hysteria_stats.json` → таблица `hysteria_stats`

Старые файлы не удаляются. Миграция запускается один раз (пропускается если таблицы уже заполнены).

## Структура проекта

```
mgboost-panel/
├── main.py                 # точка входа
├── requirements.txt
├── .env.example
├── src/
│   ├── config.py           # загрузка .env
│   ├── database.py         # SQLite: хранение и миграция
│   ├── marzban.py          # клиент к Marzban API
│   ├── subscription.py     # логика обработки подписки
│   ├── server.py           # HTTP сервер с роутингом
│   └── routes/
│       ├── sub.py          # GET /sub/{token}
│       ├── admin.py        # /admin/* API
│       └── panel.py        # SPA → frontend/index.html
├── frontend/
│   └── index.html          # админ-панель
└── data/
    └── db.sqlite3          # база данных (gitignored)
```

## Client Dashboard (Личный кабинет)

Предпочтительная ссылка на legacy-личный кабинет передаёт bearer только во
fragment, который не отправляется HTTP-серверу:

```
https://your-domain/lk/#token=SUBSCRIPTION_TOKEN
```

Токен пока остаётся тем же legacy bearer из `/sub/{token}`. Старые bookmarks с
`?token=` принимаются для обратной совместимости один раз: frontend немедленно
удаляет query из browser URL state, после чего same-origin API использует
`X-MGBoost-Subscription`. Новая opaque-token модель описана в PH2-01, но не
включается до parent-account и staged Phase 4 migration.

**Возможности:**
- Статус аккаунта (активен / истёк / отключён), дата истечения, счётчик трафика
- Трафик по нодам с прогресс-барами
- Кнопка «Скопировать ссылку подписки»
- Инструкция по подключению (Hiddify, Streisand, v2rayNG)
- История устройств (последние 10 User-Agent из БД)

**Настройка:** privileged LK/Stars/bot/Filin операции выполняет отдельный
localhost-only broker. Основной MGBoost `.env` не должен содержать Marzban
admin credentials. Они устанавливаются только в root-managed broker environment:

```env
# /etc/mgboost/marzban-broker.env (0640 root:mgboost-broker)
MARZBAN_URL=http://127.0.0.1:8000
MARZBAN_ADMIN_USER=dedicated_mgboost_service_admin
MARZBAN_ADMIN_PASS=<high-entropy credential>
MARZBAN_BROKER_AUTH_KEY=<same CSPRNG key as main service>
MARZBAN_BROKER_LISTEN_HOST=127.0.0.1
MARZBAN_BROKER_LISTEN_PORT=8002
```

Broker принимает десять совместимых typed legacy operations и отдельный
dormant typed `child.user.ensure`; все операции HMAC-authenticated
requests. Произвольного Marzban proxy в нём нет. При broker outage текущий
`/sub/{legacy_token}` продолжает работать напрямую через public Marzban
subscription endpoint; privileged LK/Stars/bot/Filin операции fail closed.

Filin HMAC replay state is stored durably in SQLite as SHA-256 references,
so the same signed nonce is rejected across processes and restarts. Legacy
signature v1 remains byte-compatible. New mutation callers should use v2 and
send a stable `X-Filin-Idempotency-Key` for one logical operation while using
a fresh timestamp/nonce for every retry. The v2 signed payload is:

```text
v2
METHOD
RAW_PATH_WITH_QUERY
TIMESTAMP
NONCE
SHA256(IDEMPOTENCY_KEY)
SHA256(BODY)
```

MGBoost stores only hashes of the nonce, operation key, request and response.
Completed/pending duplicate operations return `409` and are not re-executed;
the caller must reconcile through the corresponding read operation. Keep
`INTERNAL_API_REQUIRE_V2_MUTATIONS=0` until every external mutation caller has
adopted v2; switching it to `1` deliberately rejects legacy v1 mutations with
`428` while leaving signed reads compatible.

Для аварийного rollback существует только явный режим
`MARZBAN_SERVICE_MODE=direct`: старый код получает текущий service credential
обратно в своё окружение. Это не production target и не требует изменения
пользовательских UUID/subscription URLs/HWID.

### Encrypted backups and token-safe logs

PH1-06 stores new local subscription references as `sha256:<hex>` verifiers;
the real legacy `/sub/{token}` remains unchanged and valid. LK accepts old
`?token=` bookmarks once, removes the value from browser URL state and sends
subsequent same-origin API calls in `X-MGBoost-Subscription`. Application and
nginx sensitive-route logs must redact the full path/query and omit Referer.

Daily encrypted SQLite backup units are
`mgboost-secure-backup.service`/`.timer`. They require root-owned mode `0600`
`/etc/mgboost/backup.passphrase` and write root-only artifacts under
`/var/backups/mgboost`. Installation, the one-time encrypted quarantine,
restore verification, retention and rollback order are defined in
`docs/PHASE1_RETENTION_AND_BACKUP.md`.

## API эндпоинты (принимает сервер)

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/sub/{token}` | Подписка с фильтрами и extra конфигами |
| POST | `/admin/session/login` | Создать server-side admin session |
| GET | `/admin/session` | Получить статус и CSRF текущей session |
| POST | `/admin/session/logout` | Отозвать текущую session |
| POST | `/admin/session/rotate` | Перевыпустить session ID и CSRF |
| GET/POST/PUT/DELETE | `/admin/marzban/{allowlisted_path}` | Server-side broker к разрешённым Marzban операциям |
| GET | `/admin/configs` | Список глобальных extra конфигов |
| POST | `/admin/configs` | Добавить конфиг |
| DELETE | `/admin/configs/{id}` | Удалить конфиг |
| POST | `/admin/configs/reorder` | Изменить порядок конфигов |
| GET | `/admin/stats` | Статистика Hysteria2 |
| POST | `/admin/stats` | Обновить трафик Hysteria2 |
| GET | `/admin/per-user-configs` | Индивидуальные конфиги |
| POST | `/admin/per-user-configs` | Сохранить индивидуальные конфиги |
| GET | `/admin/node-filters` | Фильтры нод |
| POST | `/admin/node-filters` | Сохранить фильтры нод |
| GET | `/*` | Админ-панель (SPA) |

Все `/admin/*` endpoints, кроме login/status handshake, требуют opaque admin cookie;
все изменяющие запросы дополнительно требуют `X-CSRF-Token`. Старый browser
`Authorization: Bearer <Marzban JWT>` больше не является способом входа в MGBoost.
