import os
import sqlite3
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template
from flask_httpauth import HTTPBasicAuth
# Объединяем итоговый импорт из database
from database import init_db, save_errors, get_aggregated_history, cleanup_old_logs
from parser import parse_xray_errors
from apscheduler.schedulers.background import BackgroundScheduler

LOG_FILE = "monitor_job.log" # Добавляем константу для файла логов

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

USER_DATA = {
    os.getenv("DASHBOARD_USER"): os.getenv("DASHBOARD_PASS")
}


@auth.verify_password
def verify_password(username, password):
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

# Запускаем планировщик
scheduler = BackgroundScheduler()
scheduler.add_job(func=update_logs_job, trigger="interval", seconds=30)
scheduler.start()

@app.route('/')
@auth.login_required
def index():
    return render_template('index.html')

@app.route('/api/update')
@auth.login_required
def update_stats():
    current_stats = parse_xray_errors()
    if current_stats:
        save_errors(current_stats)
    return jsonify({"status": "success"})

@app.route('/api/recent')
@auth.login_required
def api_recent():
    from database import get_latest_logs
    data = get_latest_logs(30) # Последние 30 событий
    return jsonify([{
        "time": r[0],
        "type": r[1],
        "desc": r[2]
    } for r in data])

@app.route('/api/history')
@auth.login_required
def api_history():
    try:
        with sqlite3.connect('xray_monitor.db') as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            # Берем последние 500 записей без группировки
            cursor.execute('''
                SELECT timestamp, source, destination, error_type as type, description as desc
                FROM error_history
                ORDER BY timestamp DESC
                LIMIT 500
            ''')
            rows = cursor.fetchall()
            return jsonify([dict(row) for row in rows])
    except sqlite3.Error as e:
        print(f"SQLite Error fetching history: {e}") # Добавлено логирование для дебага
        return jsonify({"error": f"Database query failed: {str(e)}", "details": "Проверьте подключение к БД или схему таблицы."}), 500
    except Exception as e:
        print(f"Unexpected Error fetching history: {e}") # Добавлено логирование для дебага
        return jsonify({"error": f"An unexpected error occurred: {str(e)}", "details": "Обратитесь к администратору."}), 500
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)

