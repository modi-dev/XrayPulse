import os
import sqlite3
import json
import ipaddress
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime
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
    get_cached_ip_profile,
    upsert_ip_profile,
    get_geo_lookup_daily_count,
    increment_geo_lookup_daily_count,
    get_error_log_ingestion_state,
    set_error_log_ingestion_state,
)
from parser import read_new_error_log_events
from apscheduler.schedulers.background import BackgroundScheduler

# Путь к активному файлу лога задаётся в init_monitor_job_file_logging() после load_dotenv.
LOG_FILE = os.path.abspath("monitor_job.log")
_MONITOR_JOB_LOGGER = None
POST_ROOT_DIAG = {}


def init_monitor_job_file_logging():
    """RotatingFileHandler: monitor_job.log, monitor_job.log.1, … при переполнении."""
    global LOG_FILE, _MONITOR_JOB_LOGGER
    raw = os.getenv("MONITOR_JOB_LOG_PATH", "monitor_job.log").strip() or "monitor_job.log"
    LOG_FILE = os.path.abspath(raw)
    max_bytes = int(os.getenv("MONITOR_JOB_LOG_MAX_BYTES", str(5 * 1024 * 1024)))
    backup_count = int(os.getenv("MONITOR_JOB_LOG_BACKUP_COUNT", "5"))

    lg = logging.getLogger("xraypulse.monitor_job")
    lg.handlers.clear()
    lg.setLevel(logging.INFO)
    lg.propagate = False
    h = RotatingFileHandler(
        LOG_FILE,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    h.setFormatter(logging.Formatter("%(message)s"))
    lg.addHandler(h)
    _MONITOR_JOB_LOGGER = lg


def log_message(message):
    """Лог в консоль и в ротируемый monitor_job.log."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {message}"
    print(line)
    if _MONITOR_JOB_LOGGER is not None:
        _MONITOR_JOB_LOGGER.info("%s", line)
    else:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")


load_dotenv()
init_monitor_job_file_logging()


def error_log_path():
    """Путь к error.log после load_dotenv (не полагаться на импорт parser до dotenv)."""
    raw = os.getenv("ERROR_LOG_PATH", "/usr/local/x-ui/error.log").strip()
    return os.path.abspath(raw or "/usr/local/x-ui/error.log")


app = Flask(__name__)
auth = HTTPBasicAuth()

AUTH_ENABLED = os.getenv("AUTH_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")
GEO_LOOKUP_ENABLED = os.getenv("GEO_LOOKUP_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")
GEO_LOOKUP_DAILY_LIMIT = int(os.getenv("GEO_LOOKUP_DAILY_LIMIT", "1699"))

USER_DATA = {
    os.getenv("DASHBOARD_USER"): os.getenv("DASHBOARD_PASS")
}

# Переменные конфигурации для стартового лога (DASHBOARD_USER / DASHBOARD_PASS намеренно не включены).
_STARTUP_LOG_ENV_KEYS = (
    "AUTH_ENABLED",
    "ERROR_LOG_PATH",
    "ERROR_LOG_SKIP_HISTORY",
    "ERROR_LOG_LINE_MARKERS",
    "GEO_LOOKUP_ENABLED",
    "GEO_LOOKUP_DAILY_LIMIT",
    "MONITOR_JOB_LOG_PATH",
    "MONITOR_JOB_LOG_MAX_BYTES",
    "MONITOR_JOB_LOG_BACKUP_COUNT",
)


def log_startup_config():
    """Пишет в monitor_job.log и консоль значения окружения, кроме логина и пароля."""
    log_message("--- Старт: переменные окружения (логин и пароль дашборда не выводятся) ---")
    for key in _STARTUP_LOG_ENV_KEYS:
        raw = os.environ.get(key)
        if raw is None or str(raw).strip() == "":
            log_message(f"  {key}: <не задано в окружении>")
        else:
            log_message(f"  {key}: {raw}")
    log_message(
        f"  Эффективно (после разбора): AUTH_ENABLED={AUTH_ENABLED}, "
        f"GEO_LOOKUP_ENABLED={GEO_LOOKUP_ENABLED}, GEO_LOOKUP_DAILY_LIMIT={GEO_LOOKUP_DAILY_LIMIT}"
    )


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

log_startup_config()

# Инициализируем БД при запуске
init_db()

def update_logs_job():
    """Функция, которая будет бегать в фоне"""
    log_message("--- Начинается фоновое обновление логов ---")
    try:
        path = error_log_path()
        off, ino = get_error_log_ingestion_state(path)
        log_message(f"Инжест лога: offset={off!s}, inode={ino!s}.")
        new_logs, new_off, new_ino = read_new_error_log_events(path, off, ino)
        if new_logs:
            save_errors(new_logs)
        set_error_log_ingestion_state(path, new_off, new_ino)
        cleanup_old_logs(days=7)
        if new_logs:
            log_message(f"Сохранено событий: {len(new_logs)}; смещение в логе (байт): {new_off}.")
        else:
            log_message("Новых завершённых строк под фильтры не найдено (или только неполная строка в конце файла).")
    except Exception as e:
        log_message(f"КРИТИЧЕСКАЯ ОШИБКА в фоновом задании: {e}")

def resolve_ip_profile(ip):
    """Возвращает профиль IP: location/owner/asn/network_type."""
    if not ip or ip in ("Client", "N/A", "inbound/unknown", "api/internal"):
        return {"location": None, "owner": None, "asn": None, "network_type": None}

    ip_clean = str(ip).strip().strip("[]")
    try:
        ip_obj = ipaddress.ip_address(ip_clean)
        if ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_link_local:
            return {
                "location": "Private/Local",
                "owner": "Private/Local Network",
                "asn": None,
                "network_type": "private"
            }
    except ValueError:
        return {"location": None, "owner": None, "asn": None, "network_type": "not_ip"}

    cached = get_cached_ip_profile(ip_clean)
    if cached:
        country, city, owner, asn, network_type = cached
        return {
            "location": ", ".join(part for part in [country, city] if part) or None,
            "owner": owner,
            "asn": asn,
            "network_type": network_type
        }

    if not GEO_LOOKUP_ENABLED:
        return {"location": None, "owner": None, "asn": None, "network_type": "disabled"}

    day_key = datetime.now().strftime('%Y-%m-%d')
    used_today = get_geo_lookup_daily_count(day_key)
    if used_today >= GEO_LOOKUP_DAILY_LIMIT:
        return {"location": None, "owner": None, "asn": None, "network_type": "rate_limited"}

    increment_geo_lookup_daily_count(day_key)

    # Легкий публичный сервис без API-ключа; результат кэшируем в БД.
    try:
        with urlopen(f"https://ipwho.is/{quote(ip_clean)}", timeout=2.0) as response:
            payload = json.loads(response.read().decode("utf-8"))
            if payload.get("success"):
                country = payload.get("country") or ""
                city = payload.get("city") or ""
                conn = payload.get("connection", {}) or {}
                owner = conn.get("org") or conn.get("isp") or "Unknown owner"
                asn = str(conn.get("asn")) if conn.get("asn") else None
                network_type = payload.get("type") or "public"
                upsert_ip_profile(ip_clean, country, city, owner, asn, network_type)
                return {
                    "location": ", ".join(part for part in [country, city] if part) or "Unknown location",
                    "owner": owner,
                    "asn": asn,
                    "network_type": network_type
                }
    except Exception:
        return {"location": None, "owner": None, "asn": None, "network_type": "unresolved"}
    return {"location": None, "owner": None, "asn": None, "network_type": "unresolved"}

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
    path = error_log_path()
    off, ino = get_error_log_ingestion_state(path)
    current_stats, new_off, new_ino = read_new_error_log_events(path, off, ino)
    if current_stats:
        save_errors(current_stats)
    set_error_log_ingestion_state(path, new_off, new_ino)
    return jsonify({"status": "success", "ingested": len(current_stats)})

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
            source_profile = resolve_ip_profile(row.get("source"))
            row["source_location"] = source_profile.get("location")
            row["source_owner"] = source_profile.get("owner")
            row["source_asn"] = source_profile.get("asn")

            dest_profile = resolve_ip_profile(row.get("destination_host"))
            row["destination_owner"] = dest_profile.get("owner")
            row["destination_asn"] = dest_profile.get("asn")
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
            source_profile = resolve_ip_profile(row.get("source_ip"))
            row["source_location"] = source_profile.get("location")
            row["source_owner"] = source_profile.get("owner")
            row["source_asn"] = source_profile.get("asn")

            dest_profile = resolve_ip_profile(row.get("destination_host"))
            row["destination_owner"] = dest_profile.get("owner")
            row["destination_asn"] = dest_profile.get("asn")
        return jsonify(rows)
    except Exception as e:
        print(f"Error fetching events by type: {e}")
        return jsonify({"error": str(e)}), 500
if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5000)

