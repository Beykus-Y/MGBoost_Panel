# MGBoost Panel

Прокси подписок и админ-панель для Marzban VPN.

## Что умеет

- Проксирует `/sub/{token}` — перехватывает подписку Marzban, применяет фильтры и добавляет extra конфиги
- Добавляет глобальные extra конфиги (Hysteria2 и др.) ко всем подпискам
- Индивидуальные конфиги для конкретных пользователей
- Фильтрация нод по fragment URI — можно скрыть конкретные ноды для пользователя
- Учёт трафика Hysteria2 и добавление его в `subscription-userinfo`
- Логирование запросов подписок в SQLite (токен, юзер, User-Agent, IP)
- Админ-панель (SPA) — управление пользователями, нодами, конфигами

## Требования

- Python 3.8+
- `pip install -r requirements.txt` (только `python-dotenv`)

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
MARZBAN_URL=http://127.0.0.1:8000   # адрес Marzban
LISTEN_HOST=127.0.0.1
LISTEN_PORT=8001
DATA_DIR=./data                      # папка с БД и JSON
SECRET_KEY=changeme                  # поменяй!
```

## Деплой (systemd)

```bash
cp mgboost-panel.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now mgboost-panel
systemctl status mgboost-panel
```

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

Клиент открывает свой личный кабинет по ссылке:

```
https://your-domain/lk/?token=SUBSCRIPTION_TOKEN
```

Токен — это тот же токен подписки из `/sub/{token}`.

**Возможности:**
- Статус аккаунта (активен / истёк / отключён), дата истечения, счётчик трафика
- Трафик по нодам с прогресс-барами
- Кнопка «Скопировать ссылку подписки»
- Инструкция по подключению (Hiddify, Streisand, v2rayNG)
- История устройств (последние 10 User-Agent из БД)

**Настройка:** для показа трафика по нодам нужны учётные данные admin-пользователя Marzban в `.env`:

```env
MARZBAN_ADMIN_USER=admin
MARZBAN_ADMIN_PASS=your_password
```

Без этих переменных блок трафика по нодам будет пустым (остальные данные доступны без admin-прав).

## API эндпоинты (принимает сервер)

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/sub/{token}` | Подписка с фильтрами и extra конфигами |
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
