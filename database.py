import os
import sqlite3
from datetime import datetime, timedelta

DEFAULT_ERROR_DESCRIPTION = "Неизвестная проблема"

# Сопоставление подстроки в тексте сообщения (нижний регистр) → человекочитаемое описание.
# Порядок важен: берётся первое совпадение (сначала более специфичные шаблоны).
ERROR_DESCRIPTION_RULES: list[tuple[str, str]] = [
    ("io: read/write on closed pipe", "Разрыв соединения со стороны клиента (нормально)"),
    ("context deadline exceeded", "Таймаут: сервер не ответил вовремя"),
    ("connection timed out", "Таймаут соединения: сервер не ответил в отведенное время"),
    ("connection reset by peer", "Принудительный сброс сессии (возможно работа ТСПУ)"),
    ("proxy/vless/encoding", "Ошибка протокола: проверьте UUID или настройки"),
    ("failed to handler mux client connection", "Ошибка Mux: попробуйте отключить Mux на клиенте"),
    ("transport: bad stream ID", "Ошибка мультиплексирования потоков"),
    ("i/o timeout", "Таймаут: ресурс недоступен или порт закрыт хостером"),
    ("all retry attempts failed", "Все попытки переподключения исчерпаны"),
    ("wireguard: connection ends", "Обрыв Wireguard-туннеля (проверьте MTU или ключи)"),
    ("91.108.", "Сбой связи с дата-центром Telegram (Европа)"),
    ("149.154.", "Сбой связи с дата-центром Telegram (Азия/Инфо)"),
    ("telegram", "Проблема с доступом к серверам Telegram"),
    ("server name mismatch", "REALITY/TLS: SNI не совпадает с dest — проверьте sni/serverNames в клиенте, устаревший профиль или чужой трафик на порт"),
    ("reality: processed invalid connection", "REALITY: соединение отклонено (неверный TLS/SNI, сканирование или несовпадение настроек)"),
    ("failed to read client hello", "Попытка взлома или сканирования порта REALITY (отклонено)"),
    ("unsupported tls version", "Попытка взлома или сканирования порта REALITY (отклонено)"),
    ("unsupported tls", "Попытка взлома или сканирования порта REALITY (отклонено)"),
    ("incompatible cipher suite", "Попытка взлома или сканирования порта REALITY (отклонено)"),
    ("no cipher suite", "Попытка взлома или сканирования порта REALITY (отклонено)"),
    ("write: broken pipe", "Обрыв связи: клиент или фильтры провайдера разорвали поток"),
    ("failed to process request", "Внутренняя ошибка обработки запроса в Xray"),
    ("cannot assign requested address", "Ошибка IPv6: сервер пытается использовать несуществующий IPv6 адрес"),
    ("unexpected eof", "Обрыв Mux-соединения: поток данных неожиданно прерван (рекомендуется отключить Mux)"),
    ("timeout", "Превышено время ожидания (возможна блокировка порта или MTU)"),
]


def describe_error_message(msg: str) -> str:
    """Возвращает описание по первому подходящему правилу; подстроки сравниваются в нижнем регистре."""
    if not msg:
        return DEFAULT_ERROR_DESCRIPTION
    lowered = msg.lower()
    for needle, description in ERROR_DESCRIPTION_RULES:
        if needle in lowered:
            return description
    return DEFAULT_ERROR_DESCRIPTION

def init_db():
    with sqlite3.connect('xray_monitor.db') as conn:
        # Legacy table (для обратной совместимости и миграции)
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

        # Нормализованный справочник типов ошибок
        conn.execute('''
            CREATE TABLE IF NOT EXISTS error_types (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                normalized_text TEXT NOT NULL UNIQUE,
                description TEXT NOT NULL
            )
        ''')

        # События ошибок, связанные по error_type_id
        conn.execute('''
            CREATE TABLE IF NOT EXISTS error_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME NOT NULL,
                source_ip TEXT,
                source_port INTEGER,
                destination_host TEXT,
                destination_port INTEGER,
                raw_message TEXT NOT NULL,
                error_type_id INTEGER NOT NULL,
                UNIQUE (timestamp, raw_message),
                FOREIGN KEY(error_type_id) REFERENCES error_types(id)
            )
        ''')

        conn.execute('CREATE INDEX IF NOT EXISTS idx_events_timestamp ON error_events(timestamp DESC)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_events_error_type_id ON error_events(error_type_id)')

        # Кэш геолокации IP, чтобы не бить внешний сервис повторно.
        conn.execute('''
            CREATE TABLE IF NOT EXISTS ip_geo_cache (
                ip TEXT PRIMARY KEY,
                country TEXT,
                city TEXT,
                owner TEXT,
                asn TEXT,
                network_type TEXT,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        _ensure_ip_geo_cache_columns(conn)
        conn.execute('''
            CREATE TABLE IF NOT EXISTS geo_lookup_daily_usage (
                day TEXT PRIMARY KEY,
                requests_count INTEGER NOT NULL DEFAULT 0
            )
        ''')

        conn.execute('''
            CREATE TABLE IF NOT EXISTS log_ingestion_state (
                path TEXT PRIMARY KEY,
                byte_offset INTEGER NOT NULL,
                file_inode INTEGER NOT NULL
            )
        ''')

        _migrate_legacy_data(conn)
        conn.commit()

def get_error_log_ingestion_state(path):
    """Возвращает (byte_offset, file_inode) или (None, None), если записи ещё нет."""
    key = os.path.abspath(path)
    with sqlite3.connect('xray_monitor.db') as conn:
        row = conn.execute(
            'SELECT byte_offset, file_inode FROM log_ingestion_state WHERE path = ?',
            (key,),
        ).fetchone()
    if not row:
        return None, None
    return int(row[0]), int(row[1])


def set_error_log_ingestion_state(path, byte_offset, file_inode):
    key = os.path.abspath(path)
    with sqlite3.connect('xray_monitor.db') as conn:
        conn.execute(
            '''
            INSERT INTO log_ingestion_state (path, byte_offset, file_inode)
            VALUES (?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                byte_offset = excluded.byte_offset,
                file_inode = excluded.file_inode
            ''',
            (key, int(byte_offset), int(file_inode)),
        )
        conn.commit()


def save_errors(data_list):
    with sqlite3.connect('xray_monitor.db') as conn:
        for item in data_list:
            desc = describe_error_message(item.get('msg') or '')
            normalized_type = item.get('error_type') or item['msg']
            error_type_id = _get_or_create_error_type(conn, normalized_type, desc)
            conn.execute('''
                INSERT OR IGNORE INTO error_events (
                    timestamp, source_ip, source_port, destination_host, destination_port, raw_message, error_type_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (
                item['ts'],
                item.get('src'),
                item.get('src_port'),
                item.get('dest_host'),
                item.get('dest_port'),
                item['msg'],
                error_type_id
            ))
        conn.commit()

def get_latest_logs(limit=30):
    with sqlite3.connect('xray_monitor.db') as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT e.timestamp, t.normalized_text, t.description
            FROM error_events e
            JOIN error_types t ON t.id = e.error_type_id
            ORDER BY timestamp DESC 
            LIMIT ?
        ''', (limit,))
        return cursor.fetchall()

def get_aggregated_history(period='7d'):
    since = _period_to_since(period)
    with sqlite3.connect('xray_monitor.db') as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT t.id, t.normalized_text, t.description, COUNT(*) as total
            FROM error_events e
            JOIN error_types t ON t.id = e.error_type_id
            WHERE datetime(replace(e.timestamp, '/', '-')) >= datetime(?)
            GROUP BY t.id, t.normalized_text, t.description
            ORDER BY total DESC
        ''', (since,))
        return cursor.fetchall()

def get_history_records(limit=500, period='7d'):
    since = _period_to_since(period)
    with sqlite3.connect('xray_monitor.db') as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute('''
            SELECT
                e.timestamp AS time,
                COALESCE(e.source_ip, 'Client') AS source,
                CASE
                    WHEN e.destination_port IS NOT NULL THEN e.destination_host || ':' || e.destination_port
                    ELSE COALESCE(e.destination_host, 'direct/api')
                END AS destination,
                t.id AS error_type_id,
                t.normalized_text AS type,
                t.description AS desc,
                e.raw_message,
                e.destination_host,
                e.destination_port
            FROM error_events e
            JOIN error_types t ON t.id = e.error_type_id
            WHERE datetime(replace(e.timestamp, '/', '-')) >= datetime(?)
            ORDER BY e.timestamp DESC
            LIMIT ?
        ''', (since, limit))
        return [dict(row) for row in cursor.fetchall()]

def get_events_by_error_type(error_type_id, limit=200, period='7d'):
    since = _period_to_since(period)
    with sqlite3.connect('xray_monitor.db') as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute('''
            SELECT
                e.timestamp,
                e.source_ip,
                e.source_port,
                e.destination_host,
                e.destination_port,
                e.raw_message,
                t.normalized_text AS error_type,
                t.description
            FROM error_events e
            JOIN error_types t ON t.id = e.error_type_id
            WHERE e.error_type_id = ?
              AND datetime(replace(e.timestamp, '/', '-')) >= datetime(?)
            ORDER BY e.timestamp DESC
            LIMIT ?
        ''', (error_type_id, since, limit))
        return [dict(row) for row in cursor.fetchall()]

def cleanup_old_logs(days=7):
    """Удаляет записи старше указанного количества дней"""
    with sqlite3.connect('xray_monitor.db') as conn:
        limit_date = (datetime.now() - timedelta(days=days)).strftime('%Y/%m/%d %H:%M:%S')
        conn.execute('DELETE FROM error_events WHERE timestamp < ?', (limit_date,))
        conn.execute('''
            DELETE FROM error_types
            WHERE id NOT IN (SELECT DISTINCT error_type_id FROM error_events)
        ''')
        conn.commit()

def _get_or_create_error_type(conn, normalized_text, description):
    cursor = conn.execute('SELECT id FROM error_types WHERE normalized_text = ?', (normalized_text,))
    row = cursor.fetchone()
    if row:
        return row[0]
    conn.execute(
        'INSERT INTO error_types (normalized_text, description) VALUES (?, ?)',
        (normalized_text, description)
    )
    return conn.execute('SELECT id FROM error_types WHERE normalized_text = ?', (normalized_text,)).fetchone()[0]

def _migrate_legacy_data(conn):
    has_legacy_rows = conn.execute('SELECT COUNT(*) FROM error_history').fetchone()[0]
    has_events = conn.execute('SELECT COUNT(*) FROM error_events').fetchone()[0]
    if has_events > 0 or has_legacy_rows == 0:
        return

    cursor = conn.execute('SELECT timestamp, source, destination, error_type, description FROM error_history')
    for ts, source, destination, error_type, description in cursor.fetchall():
        et_id = _get_or_create_error_type(conn, error_type or 'UNKNOWN', description or 'Неизвестная проблема')
        dest_host = destination
        dest_port = None
        if destination and ':' in destination:
            possible_host, possible_port = destination.rsplit(':', 1)
            if possible_port.isdigit():
                dest_host = possible_host
                dest_port = int(possible_port)
        conn.execute('''
            INSERT OR IGNORE INTO error_events (
                timestamp, source_ip, source_port, destination_host, destination_port, raw_message, error_type_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (ts, source, None, dest_host, dest_port, error_type or '', et_id))

def _period_to_since(period):
    period_map = {
        '24h': timedelta(hours=24),
        '7d': timedelta(days=7),
        '30d': timedelta(days=30),
    }
    delta = period_map.get(period, timedelta(days=7))
    return (datetime.now() - delta).strftime('%Y-%m-%d %H:%M:%S')

def get_cached_ip_profile(ip):
    with sqlite3.connect('xray_monitor.db') as conn:
        row = conn.execute(
            'SELECT country, city, owner, asn, network_type FROM ip_geo_cache WHERE ip = ?',
            (ip,)
        ).fetchone()
        return row if row else None

def upsert_ip_profile(ip, country, city, owner, asn, network_type):
    with sqlite3.connect('xray_monitor.db') as conn:
        conn.execute('''
            INSERT INTO ip_geo_cache (ip, country, city, owner, asn, network_type, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(ip) DO UPDATE SET
                country=excluded.country,
                city=excluded.city,
                owner=excluded.owner,
                asn=excluded.asn,
                network_type=excluded.network_type,
                updated_at=CURRENT_TIMESTAMP
        ''', (ip, country, city, owner, asn, network_type))
        conn.commit()

def _ensure_ip_geo_cache_columns(conn):
    existing_columns = {row[1] for row in conn.execute("PRAGMA table_info(ip_geo_cache)").fetchall()}
    if 'owner' not in existing_columns:
        conn.execute("ALTER TABLE ip_geo_cache ADD COLUMN owner TEXT")
    if 'asn' not in existing_columns:
        conn.execute("ALTER TABLE ip_geo_cache ADD COLUMN asn TEXT")
    if 'network_type' not in existing_columns:
        conn.execute("ALTER TABLE ip_geo_cache ADD COLUMN network_type TEXT")

def get_geo_lookup_daily_count(day):
    with sqlite3.connect('xray_monitor.db') as conn:
        row = conn.execute(
            'SELECT requests_count FROM geo_lookup_daily_usage WHERE day = ?',
            (day,)
        ).fetchone()
        return int(row[0]) if row else 0

def increment_geo_lookup_daily_count(day):
    with sqlite3.connect('xray_monitor.db') as conn:
        conn.execute('''
            INSERT INTO geo_lookup_daily_usage (day, requests_count)
            VALUES (?, 1)
            ON CONFLICT(day) DO UPDATE SET
                requests_count = requests_count + 1
        ''', (day,))
        conn.commit()