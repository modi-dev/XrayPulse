import os
import sqlite3
import json
import ipaddress
import logging
import time
import base64
import re
import secrets
import threading
from logging.handlers import RotatingFileHandler
from datetime import datetime, timedelta
from urllib.request import urlopen
from urllib.parse import quote
from functools import wraps
from dotenv import load_dotenv
from flask import Flask, flash, jsonify, redirect, render_template, request, send_from_directory, session, url_for
# Объединяем итоговый импорт из database
from database import (
    init_db,
    save_errors,
    get_aggregated_history,
    cleanup_old_logs,
    get_history_records,
    get_history_summary,
    get_filter_picklists,
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


def xray_timestamp_to_iso(ts):
    """Нормализует время из лога Xray (YYYY/MM/DD ...) в ISO 8601 без смещения часового пояса."""
    if not ts or not isinstance(ts, str):
        return ts
    s = ts.strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", s):
        return s
    normalized = s.replace("/", "-")
    try:
        if "." in normalized:
            head, frac = normalized.split(".", 1)
            frac = (frac + "000000")[:6]
            dt = datetime.strptime(f"{head}.{frac}", "%Y-%m-%d %H:%M:%S.%f")
        else:
            dt = datetime.strptime(normalized, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return normalized.replace(" ", "T")
    return dt.isoformat(timespec="microseconds")


def _encode_history_cursor(ts, rowid):
    payload = json.dumps({"ts": ts, "rid": int(rowid)}, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_history_cursor(token):
    if not token or not isinstance(token, str):
        return None, None
    pad = "=" * (-len(token) % 4)
    try:
        raw = base64.urlsafe_b64decode(token + pad)
        data = json.loads(raw.decode("utf-8"))
        return data.get("ts"), int(data["rid"])
    except (ValueError, json.JSONDecodeError, KeyError, TypeError):
        return None, None


def _format_history_row(row):
    """Добавляет ISO-время; поле time — единый формат для API."""
    out = dict(row)
    raw_ts = out.get("time")
    if raw_ts:
        out["time"] = xray_timestamp_to_iso(raw_ts)
    return out


def _format_event_row(row):
    out = dict(row)
    ts = out.get("timestamp")
    if ts:
        out["timestamp"] = xray_timestamp_to_iso(ts)
    return out


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


# override=True: значения из .env перекрывают уже заданные в окружении процесса переменные
# (иначе, например, AUTH_ENABLED=false в системе/IDE игнорирует строку в .env).
load_dotenv(override=True)
init_monitor_job_file_logging()


def error_log_path():
    """Путь к error.log после load_dotenv (не полагаться на импорт parser до dotenv)."""
    raw = os.getenv("ERROR_LOG_PATH", "/usr/local/x-ui/error.log").strip()
    return os.path.abspath(raw or "/usr/local/x-ui/error.log")


app = Flask(__name__)

AUTH_ENABLED = os.getenv("AUTH_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")
GEO_LOOKUP_ENABLED = os.getenv("GEO_LOOKUP_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")
GEO_LOOKUP_DAILY_LIMIT = int(os.getenv("GEO_LOOKUP_DAILY_LIMIT", "1699"))

_dash_user = os.getenv("DASHBOARD_USER")
_dash_pass = os.getenv("DASHBOARD_PASS")
USER_DATA = {_dash_user: _dash_pass} if _dash_user and _dash_pass is not None else {}

AUTH_LOCKOUT_MAX_ATTEMPTS = max(1, int(os.getenv("AUTH_LOCKOUT_MAX_ATTEMPTS", "5")))
AUTH_LOCKOUT_SECONDS = max(30, int(os.getenv("AUTH_LOCKOUT_SECONDS", "300")))
AUTH_TRUST_X_FORWARDED = os.getenv("AUTH_TRUST_X_FORWARDED", "").strip().lower() in ("1", "true", "yes", "on")

_flask_secret = (os.getenv("FLASK_SECRET_KEY") or "").strip()
if not _flask_secret:
    _flask_secret = secrets.token_hex(32)
    print("[XrayPulse] WARNING: FLASK_SECRET_KEY не задан; сгенерирован временный ключ (сессии сбросятся после перезапуска).")
app.config["SECRET_KEY"] = _flask_secret
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=7)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

_login_lock = threading.Lock()
_login_failures = {}  # ip -> {"failures": int, "locked_until": float epoch}


def _auth_client_ip():
    """IP для учёта попыток входа. X-Forwarded-For только при явном доверии (прокси)."""
    if AUTH_TRUST_X_FORWARDED:
        xff = (request.headers.get("X-Forwarded-For") or "").strip()
        if xff:
            return xff.split(",")[0].strip()[:200]
    return (request.remote_addr or "unknown")[:200]


def _login_locked_until(ip):
    """Время окончания блокировки (epoch) или 0."""
    with _login_lock:
        rec = _login_failures.get(ip)
        if not rec:
            return 0.0
        until = float(rec.get("locked_until") or 0)
        if time.time() >= until:
            return 0.0
        return until


def _login_record_failure(ip):
    with _login_lock:
        rec = _login_failures.get(ip) or {"failures": 0, "locked_until": 0.0}
        now = time.time()
        if now < float(rec.get("locked_until") or 0):
            return
        if now >= float(rec.get("locked_until") or 0) and rec.get("locked_until"):
            rec["failures"] = 0
            rec["locked_until"] = 0.0
        rec["failures"] = int(rec.get("failures", 0)) + 1
        if rec["failures"] >= AUTH_LOCKOUT_MAX_ATTEMPTS:
            rec["locked_until"] = now + float(AUTH_LOCKOUT_SECONDS)
            rec["failures"] = 0
            log_message(
                f"AUTH lockout ip={ip} duration_s={AUTH_LOCKOUT_SECONDS} "
                f"max_attempts={AUTH_LOCKOUT_MAX_ATTEMPTS}"
            )
        _login_failures[ip] = rec


def _login_clear_failures(ip):
    with _login_lock:
        _login_failures.pop(ip, None)

# Переменные конфигурации для стартового лога (DASHBOARD_USER / DASHBOARD_PASS намеренно не включены).
_STARTUP_LOG_ENV_KEYS = (
    "AUTH_ENABLED",
    "AUTH_LOCKOUT_MAX_ATTEMPTS",
    "AUTH_LOCKOUT_SECONDS",
    "AUTH_TRUST_X_FORWARDED",
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
    if (os.environ.get("FLASK_SECRET_KEY") or "").strip():
        log_message("  FLASK_SECRET_KEY: <задано, значение не выводится>")
    else:
        log_message("  FLASK_SECRET_KEY: <не задано — при старте сгенерирован временный ключ>")
    log_message(
        f"  Эффективно (после разбора): AUTH_ENABLED={AUTH_ENABLED}, "
        f"AUTH_LOCKOUT_MAX_ATTEMPTS={AUTH_LOCKOUT_MAX_ATTEMPTS}, "
        f"AUTH_LOCKOUT_SECONDS={AUTH_LOCKOUT_SECONDS}, AUTH_TRUST_X_FORWARDED={AUTH_TRUST_X_FORWARDED}, "
        f"GEO_LOOKUP_ENABLED={GEO_LOOKUP_ENABLED}, GEO_LOOKUP_DAILY_LIMIT={GEO_LOOKUP_DAILY_LIMIT}"
    )


def _safe_next_param(val):
    """Только относительный путь на этом сайте (без open-redirect)."""
    if not val or not isinstance(val, str):
        return None
    t = val.strip()
    if len(t) > 2048 or not t.startswith("/") or t.startswith("//"):
        return None
    return t


def _login_next_from_request():
    path = request.path
    qs = request.query_string.decode("utf-8", errors="ignore")
    nxt = f"{path}?{qs}" if qs else path
    login_path = url_for("login")
    if nxt == login_path:
        return None
    return _safe_next_param(nxt)


def auth_required(func):
    """Сессия дашборда; при AUTH_ENABLED=false — без проверки."""
    @wraps(func)
    def wrapped(*args, **kwargs):
        if not AUTH_ENABLED:
            return func(*args, **kwargs)
        if session.get("dash_user"):
            return func(*args, **kwargs)
        if request.path.startswith("/api/"):
            return jsonify({"error": "unauthorized", "login_url": url_for("login", _external=False)}), 401
        nxt = _login_next_from_request()
        return redirect(url_for("login", next=nxt) if nxt else url_for("login"))

    return wrapped


log_startup_config()

# Инициализируем БД при запуске
init_db()


@app.route("/login", methods=["GET", "POST"])
def login():
    if not AUTH_ENABLED:
        return redirect(url_for("index"))
    if session.get("dash_user"):
        nxt = _safe_next_param(request.args.get("next") or request.form.get("next"))
        return redirect(nxt or url_for("index"))
    if not USER_DATA:
        flash("Учётная запись дашборда не настроена (задайте DASHBOARD_USER и DASHBOARD_PASS).", "error")
        return render_template("login.html", next=None), 503

    ip = _auth_client_ip()
    if request.method == "POST":
        until = _login_locked_until(ip)
        if until > 0 and time.time() < until:
            rem = max(0, int(until - time.time()))
            mm, ss = rem // 60, rem % 60
            flash(f"Слишком много попыток входа. Повторите через {mm} мин. {ss} сек.", "error")
            return (
                render_template("login.html", next=_safe_next_param(request.form.get("next"))),
                429,
            )
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        if username in USER_DATA and USER_DATA.get(username) == password:
            _login_clear_failures(ip)
            session.permanent = True
            session["dash_user"] = username
            nxt = _safe_next_param(request.form.get("next"))
            return redirect(nxt or url_for("index"))
        _login_record_failure(ip)
        flash("Неверный логин или пароль.", "error")
        return render_template("login.html", next=_safe_next_param(request.form.get("next")))
    return render_template("login.html", next=_safe_next_param(request.args.get("next")))


@app.route("/logout")
def logout():
    session.pop("dash_user", None)
    return redirect(url_for("login"))


def update_logs_job():
    """Функция, которая будет бегать в фоне"""
    log_message("--- Начинается фоновое обновление логов ---")
    t0 = time.perf_counter()
    try:
        path = error_log_path()
        off, ino = get_error_log_ingestion_state(path)
        log_message(f"Инжест лога: offset={off!s}, inode={ino!s}.")
        new_logs, new_off, new_ino = read_new_error_log_events(path, off, ino)
        if new_logs:
            save_errors(new_logs)
        set_error_log_ingestion_state(path, new_off, new_ino)
        cleanup_old_logs(days=7)
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        if new_logs:
            log_message(
                f"Сохранено событий: {len(new_logs)}; смещение в логе (байт): {new_off}. "
                f"METRIC ingest duration_ms={elapsed_ms} saved={len(new_logs)}"
            )
        else:
            log_message(
                "Новых завершённых строк под фильтры не найдено (или только неполная строка в конце файла). "
                f"METRIC ingest duration_ms={elapsed_ms} saved=0"
            )
    except Exception as e:
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        log_message(f"КРИТИЧЕСКАЯ ОШИБКА в фоновом задании: {e} METRIC ingest duration_ms={elapsed_ms} saved=0 error=1")

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


@app.route("/favicon.ico")
def favicon():
    """Запрос вкладки по умолчанию; отдаём тот же SVG, что и в static/favicon.svg."""
    return send_from_directory(app.static_folder, "favicon.svg", mimetype="image/svg+xml")


@app.route('/')
@auth_required
def index():
    return render_template("index.html", auth_enabled=AUTH_ENABLED)

@app.route('/api/update')
@auth_required
def update_stats():
    t0 = time.perf_counter()
    path = error_log_path()
    off, ino = get_error_log_ingestion_state(path)
    current_stats, new_off, new_ino = read_new_error_log_events(path, off, ino)
    if current_stats:
        save_errors(current_stats)
    set_error_log_ingestion_state(path, new_off, new_ino)
    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    log_message(f"METRIC api_update duration_ms={elapsed_ms} ingested={len(current_stats)}")
    return jsonify({"status": "success", "ingested": len(current_stats)})

@app.route('/api/recent')
@auth_required
def api_recent():
    from database import get_latest_logs
    data = get_latest_logs(30) # Последние 30 событий
    return jsonify([{
        "time": xray_timestamp_to_iso(r[0]) if r[0] else None,
        "type": r[1],
        "desc": r[2]
    } for r in data])

@app.route('/api/history')
@auth_required
def api_history():
    try:
        t0 = time.perf_counter()
        period = request.args.get('period', '7d')
        try:
            limit = min(int(request.args.get('limit', 500)), 2000)
        except ValueError:
            limit = 500
        error_type_ids = request.args.getlist('error_type_id')
        source_ip_query = (request.args.get('source_ip') or '').strip() or None
        destination_keys = request.args.getlist('destination')
        cursor_token = request.args.get('cursor')

        cur_ts, cur_rid = _decode_history_cursor(cursor_token)

        rows, has_more = get_history_records(
            limit=limit,
            period=period,
            error_type_ids=error_type_ids or None,
            source_ip_query=source_ip_query,
            destination_keys=destination_keys or None,
            cursor_ts=cur_ts,
            cursor_rowid=cur_rid,
        )
        summary = get_history_summary(
            period=period,
            error_type_ids=error_type_ids or None,
            source_ip_query=source_ip_query,
            destination_keys=destination_keys or None,
        )

        formatted = []
        for row in rows:
            row = _format_history_row(row)
            source_profile = resolve_ip_profile(row.get("source"))
            row["source_location"] = source_profile.get("location")
            row["source_owner"] = source_profile.get("owner")
            row["source_asn"] = source_profile.get("asn")

            dest_profile = resolve_ip_profile(row.get("destination_host"))
            row["destination_owner"] = dest_profile.get("owner")
            row["destination_asn"] = dest_profile.get("asn")
            formatted.append(row)

        next_cursor = None
        if has_more and rows:
            next_cursor = _encode_history_cursor(rows[-1]["time"], rows[-1]["event_id"])

        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        log_message(
            "METRIC api_history "
            f"duration_ms={elapsed_ms} rows={len(formatted)} limit={limit} period={period} "
            f"has_more={int(has_more)} etypes={len(error_type_ids)} "
            f"src_ip_q={int(bool(source_ip_query))} dests={len(destination_keys)}"
        )

        return jsonify({
            "events": formatted,
            "next_cursor": next_cursor,
            "has_more": has_more,
            "summary": summary,
            "limit": limit,
        })
    except Exception as e:
        print(f"Unexpected Error fetching history: {e}") # Добавлено логирование для дебага
        return jsonify({"error": f"An unexpected error occurred: {str(e)}", "details": "Обратитесь к администратору."}), 500

@app.route('/api/filter-options')
@auth_required
def api_filter_options():
    try:
        period = request.args.get('period', '7d')
        return jsonify(get_filter_picklists(period))
    except Exception as e:
        print(f"Error filter-options: {e}")
        return jsonify({"error": str(e)}), 500


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
        out = []
        for row in rows:
            row = _format_event_row(row)
            source_profile = resolve_ip_profile(row.get("source_ip"))
            row["source_location"] = source_profile.get("location")
            row["source_owner"] = source_profile.get("owner")
            row["source_asn"] = source_profile.get("asn")

            dest_profile = resolve_ip_profile(row.get("destination_host"))
            row["destination_owner"] = dest_profile.get("owner")
            row["destination_asn"] = dest_profile.get("asn")
            out.append(row)
        return jsonify(out)
    except Exception as e:
        print(f"Error fetching events by type: {e}")
        return jsonify({"error": str(e)}), 500
if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5000)

