import sqlite3
from datetime import datetime, timedelta

ERROR_MAPPING = {
    "io: read/write on closed pipe": "Разрыв соединения со стороны клиента (нормально)",
    "connection reset by peer": "Сброс соединения сервером или провайдером",
    "context deadline exceeded": "Таймаут: сервер не ответил вовремя",
    "proxy/vless/encoding": "Ошибка протокола: проверьте UUID или настройки",
    "failed to handler mux client connection": "Ошибка Mux: попробуйте отключить Mux на клиенте",
    "transport: bad stream ID": "Ошибка мультиплексирования потоков",
    "i/o timeout": "Таймаут ввода-вывода: удаленный ресурс не отвечает или порт заблокирован",
    "all retry attempts failed": "Все попытки переподключения исчерпаны",
    "write: broken pipe": "Обрыв соединения клиентом (часто из-за смены сети или закрытия приложения)",
    "telegram": "Проблема с доступом к серверам Telegram",
    "wireguard: connection ends": "Обрыв Wireguard-туннеля (проверьте MTU или ключи)",
    "91.108.": "Сбой связи с дата-центром Telegram (Европа)",
    "149.154.": "Сбой связи с дата-центром Telegram (Азия/Инфо)",
    "timeout": "Превышено время ожидания (возможна блокировка порта или MTU)",
    "failed to read client hello": "Попытка взлома или сканирования порта REALITY (отклонено)",
    "write: broken pipe": "Обрыв связи: клиент или фильтры провайдера (РФ) разорвали поток",
    "connection reset by peer": "Принудительный сброс сессии (возможно работа ТСПУ/РКН)",
    "i/o timeout": "Таймаут: ресурс недоступен или порт закрыт хостером",
    "failed to process request": "Внутренняя ошибка обработки запроса в Xray",
    "cannot assign requested address": "Ошибка IPv6: сервер пытается использовать несуществующий IPv6 адрес",
    "unexpected EOF": "Обрыв Mux-соединения: поток данных неожиданно прерван (рекомендуется отключить Mux)",
    "connection timed out": "Таймаут соединения: сервер не ответил в отведенное время"
}

def init_db():
    with sqlite3.connect('xray_monitor.db') as conn:
        # Существующая таблица...
        conn.execute('''
            CREATE TABLE IF NOT EXISTS error_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME,
                source TEXT,
                destination TEXT,
                error_type TEXT,
                description TEXT
            )
        ''')
        # ДОБАВЛЯЕМ ИНДЕКСЫ для мгновенной сортировки и фильтрации
        conn.execute('CREATE INDEX IF NOT EXISTS idx_timestamp ON error_history(timestamp DESC)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_error_type ON error_history(error_type)')
        conn.commit()

def save_errors(data_list):
    with sqlite3.connect('xray_monitor.db') as conn:
        for item in data_list:
            desc = "Неизвестная проблема"
            for key, val in ERROR_MAPPING.items():
                if key in item['msg'].lower():
                    desc = val
                    break
            # Проверяем, нет ли уже такой записи (по времени и тексту), чтобы не дублировать при фоновом чтении
            exists = conn.execute('SELECT 1 FROM error_history WHERE timestamp=? AND error_type=?', 
                                 (item['ts'], item['msg'])).fetchone()
            if not exists:
                conn.execute('''
                    INSERT INTO error_history (timestamp, source, destination, error_type, description)
                    VALUES (?, ?, ?, ?, ?)
                ''', (item['ts'], item['src'], item['dest'], item['msg'], desc))
        conn.commit()

def get_latest_logs(limit=30):
    with sqlite3.connect('xray_monitor.db') as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT timestamp, error_type, description 
            FROM error_history 
            ORDER BY timestamp DESC 
            LIMIT ?
        ''', (limit,))
        return cursor.fetchall()

def get_aggregated_history():
    with sqlite3.connect('xray_monitor.db') as conn:
        cursor = conn.cursor()
        # ИСПРАВЛЕНО: используем COUNT(*) вместо SUM(count)
        cursor.execute('''
            SELECT error_type, description, COUNT(*) as total 
            FROM error_history 
            GROUP BY error_type 
            ORDER BY total DESC
        ''')
        return cursor.fetchall()

def cleanup_old_logs(days=7):
    """Удаляет записи старше указанного количества дней"""
    with sqlite3.connect('xray_monitor.db') as conn:
        limit_date = (datetime.now() - timedelta(days=days)).strftime('%Y/%m/%d %H:%M:%S')
        conn.execute('DELETE FROM error_history WHERE timestamp < ?', (limit_date,))
        conn.commit()