import os
import sqlite3
import json
import ipaddress
from urllib.request import urlopen
from urllib.parse import quote
from functools import wraps
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request
from flask_httpauth import HTTPBasicAuth
# Объединяем итоговый импорт из database
from database import (
    init_db,
    save_errors,
    get_aggregated_history,
    cleanup_old_logs,
    get_history_records,
    get_events_by_error_type,
    get_cached_ip_geo,
    upsert_ip_geo,
)
from parser import parse_xray_errors
from apscheduler.schedulers.background import BackgroundScheduler

LOG_FILE = "monitor_job.log" # Добавляем константу для файла логов
POST_ROOT_DIAG = {}

def log_message(message):
    """Простая функция логирования в файл и вывод в консоль."""
    import datetime
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_entry = f"[{timestamp}] {message}\n"
    print(log_entry.strip()) # Выводим в консоль для мгновенной обратной связи
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(log_entry)

load_dotenv()

app = Flask(__name__)
auth = HTTPBasicAuth()

AUTH_ENABLED = os.getenv("AUTH_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")

USER_DATA = {
    os.getenv("DASHBOARD_USER"): os.getenv("DASHBOARD_PASS")
}

def auth_required(func):
    """Условно включает проверку auth через AUTH_ENABLED."""
    if not AUTH_ENABLED:
        @wraps(func)
        def wrapped(*args, **kwargs):
            return func(*args, **kwargs)
        return wrapped
    return auth.login_required(func)


@auth.verify_password
def verify_password(username, password):
    if not AUTH_ENABLED:
        return username
    if username in USER_DATA and USER_DATA.get(username) == password:
        return username

# Инициализируем БД при запуске
init_db()

def update_logs_job():
    """Функция, которая будет бегать в фоне"""
    log_message("--- Начинается фоновое обновление логов ---")
    try:
        new_logs = parse_xray_errors(limit=500)
        if new_logs:
            save_errors(new_logs)
            cleanup_old_logs(days=7)
            log_message("Успешно сохранено и очищены старые логи.")
        else:
            log_message("Новых ошибок не обнаружено. Логирование пропущено.")
    except Exception as e:
        log_message(f"КРИТИЧЕСКАЯ ОШИБКА в фоновом задании: {e}")

def resolve_ip_location(ip):
    """Возвращает строку вида 'Country, City' для публичного IP."""
    if not ip or ip in ("Client", "N/A"):
        return None
    try:
        ip_obj = ipaddress.ip_address(ip)
        if ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_link_local:
            return "Private/Local"
    except ValueError:
        return None

    cached = get_cached_ip_geo(ip)
    if cached:
        country, city = cached
        return ", ".join(part for part in [country, city] if part)

    # Легкий публичный сервис без API-ключа; результат кэшируем в БД.
    try:
        with urlopen(f"https://ipwho.is/{quote(ip)}", timeout=2.0) as response:
            payload = json.loads(response.read().decode("utf-8"))
            if payload.get("success"):
                country = payload.get("country") or ""
                city = payload.get("city") or ""
                upsert_ip_geo(ip, country, city)
                return ", ".join(part for part in [country, city] if part) or "Unknown location"
    except Exception:
        return None
    return None

@app.before_request
def diagnose_unexpected_root_post():
    """Диагностика источника POST-запросов в корень '/'."""
    if request.path != '/' or request.method != 'POST':
        return

    # Ключ для анти-спама: ip + user-agent
    client_ip = request.headers.get('X-Forwarded-For', request.remote_addr or 'unknown')
    ua = request.headers.get('User-Agent', 'unknown')
    key = f"{client_ip}|{ua}"

    import time
    now_ts = int(time.time())
    last = POST_ROOT_DIAG.get(key, 0)

    # Логируем не чаще раза в 30 секунд на источник
    if now_ts - last >= 30:
        POST_ROOT_DIAG[key] = now_ts
        origin = request.headers.get('Origin', '-')
        referer = request.headers.get('Referer', '-')
        content_type = request.headers.get('Content-Type', '-')
        log_message(
            "DIAG POST / "
            f"ip={client_ip} ua={ua} origin={origin} referer={referer} content_type={content_type}"
        )

# Запускаем планировщик
scheduler = BackgroundScheduler()
scheduler.add_job(func=update_logs_job, trigger="interval", seconds=30)
scheduler.start()

@app.route('/')
@auth_required
def index():
    return render_template('index.html')

@app.route('/api/update')
@auth_required
def update_stats():
    current_stats = parse_xray_errors()
    if current_stats:
        save_errors(current_stats)
    return jsonify({"status": "success"})

@app.route('/api/recent')
@auth_required
def api_recent():
    from database import get_latest_logs
    data = get_latest_logs(30) # Последние 30 событий
    return jsonify([{
        "time": r[0],
        "type": r[1],
        "desc": r[2]
    } for r in data])

@app.route('/api/history')
@auth_required
def api_history():
    try:
        period = request.args.get('period', '7d')
        rows = get_history_records(500, period)
        for row in rows:
            row["source_location"] = resolve_ip_location(row.get("source"))
        return jsonify(rows)
    except Exception as e:
        print(f"Unexpected Error fetching history: {e}") # Добавлено логирование для дебага
        return jsonify({"error": f"An unexpected error occurred: {str(e)}", "details": "Обратитесь к администратору."}), 500

@app.route('/api/error-types')
@auth_required
def api_error_types():
    try:
        period = request.args.get('period', '7d')
        rows = get_aggregated_history(period)
        return jsonify([{
            "id": row[0],
            "error_type": row[1],
            "description": row[2],
            "count": row[3]
        } for row in rows])
    except Exception as e:
        print(f"Error fetching error types: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/error-types/<int:error_type_id>/events')
@auth_required
def api_error_type_events(error_type_id):
    try:
        period = request.args.get('period', '7d')
        rows = get_events_by_error_type(error_type_id, 200, period)
        for row in rows:
            row["source_location"] = resolve_ip_location(row.get("source_ip"))
        return jsonify(rows)
    except Exception as e:
        print(f"Error fetching events by type: {e}")
        return jsonify({"error": str(e)}), 500
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)

