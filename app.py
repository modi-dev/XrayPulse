import os
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template
from flask_httpauth import HTTPBasicAuth
from werkzeug.security import generate_password_hash, check_password_hash
from database import init_db, save_errors, get_aggregated_history
from parser import parse_xray_errors
from apscheduler.schedulers.background import BackgroundScheduler
from database import init_db, save_errors, cleanup_old_logs, get_aggregated_history
from parser import parse_xray_errors

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
    print("Фоновое обновление логов...")
    new_logs = parse_xray_errors(limit=500)
    save_errors(new_logs)
    cleanup_old_logs(days=7)

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
        data = get_aggregated_history()
        # Форматируем для фронтенда
        return jsonify([{"type": r[0], "desc": r[1], "count": r[2]} for r in data])
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
