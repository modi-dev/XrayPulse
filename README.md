# XrayPulse

Веб-дашборд для анализа ошибок Xray из `error.log`.

## Возможности

- Парсинг ошибок Xray (`IPv4/IPv6`) с нормализацией типов ошибок.
- История событий в SQLite.
- Топ причин ошибок и тренд по времени.
- Детализация по типу ошибки (события, source/destination, raw message).
- Обогащение IP-профиля (location/owner/asn) с кэшем и дневным лимитом.
- Фоновое обновление логов через scheduler.

## Стек

- Python 3.10+
- Flask
- APScheduler
- python-dotenv
- flask-httpauth
- SQLite

## Быстрый старт

1. Создайте и активируйте виртуальное окружение.
2. Установите зависимости:

```bash
pip install -r requirements.txt
```

3. Скопируйте `.env.example` в `.env` и заполните значения.
4. Запустите приложение:

```bash
python app.py
```

5. Откройте:

- [http://127.0.0.1:5000](http://127.0.0.1:5000)

## Переменные окружения

- `AUTH_ENABLED` — включить Basic Auth (`true/false`)
- `DASHBOARD_USER` — логин
- `DASHBOARD_PASS` — пароль
- `ERROR_LOG_PATH` — путь к логу Xray
- `GEO_LOOKUP_ENABLED` — включить IP enrichment (`true/false`)
- `GEO_LOOKUP_DAILY_LIMIT` — дневной лимит внешних lookup запросов

## Структура

- `app.py` — Flask API и scheduler
- `parser.py` — парсинг и нормализация ошибок Xray
- `database.py` — схема БД, агрегации, кэш профилей IP
- `templates/index.html` — UI
- `static/js/app.js` — клиентская логика и визуализация

## Примечания

- База данных создается автоматически: `xray_monitor.db`.
- Для локальной отладки можно отключить auth: `AUTH_ENABLED=false`.
- Для прод-окружения рекомендуется оставить `AUTH_ENABLED=true`.
