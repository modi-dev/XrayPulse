import sqlite3

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
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS error_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME,
                source TEXT,
                destination TEXT,
                error_type TEXT,
                description TEXT
            )
        ''')
        conn.commit()

def save_errors(data_list):
    with sqlite3.connect('xray_monitor.db') as conn:
        cursor = conn.cursor()
        for item in data_list:
            desc = "Неизвестная проблема"
            for key, val in ERROR_MAPPING.items():
                if key in item['msg'].lower():
                    desc = val
                    break
            
            cursor.execute('''
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
        cursor.execute('''
            SELECT error_type, description, SUM(count) as total 
            FROM error_history 
            GROUP BY error_type 
            ORDER BY total DESC
        ''')
        return cursor.fetchall()
        