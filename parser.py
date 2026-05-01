import os
import re
import ipaddress

ERROR_LOG_PATH = os.getenv('ERROR_LOG_PATH', '/usr/local/x-ui/error.log')

# Строка лога: время (с необязательной дробной частью), [Level], опционально [session_id], текст.
_XRAY_LOG_LINE_RE = re.compile(
    r'^(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?)\s+\[(\w+)\]\s+(?:\[\d+\]\s*)?(.*)$'
)


def _error_log_line_markers():
    """Подстроки (нижний регистр): строка попадаёт в выборку, если содержит любую из них."""
    raw = os.getenv(
        'ERROR_LOG_LINE_MARKERS',
        'failed,invalid connection,server name mismatch',
    ).strip()
    if not raw:
        return ('failed', 'invalid connection', 'server name mismatch')
    return tuple(x.strip().lower() for x in raw.split(',') if x.strip())


def _line_matches_error_filter(line):
    line_l = line.lower()
    return any(m in line_l for m in _error_log_line_markers())


def split_host_port(endpoint):
    """Разбирает endpoint вида host:port или [ipv6]:port."""
    if not endpoint:
        return None, None
    endpoint = endpoint.strip()
    m = re.match(r'^\[([0-9a-fA-F:]+)\]:(\d+)$', endpoint)
    if m:
        return m.group(1), int(m.group(2))
    if ':' in endpoint:
        host, port = endpoint.rsplit(':', 1)
        if port.isdigit():
            return host, int(port)
    return endpoint, None


def normalize_error_type(raw_msg):
    """Нормализует текст ошибки, убирая переменные данные (IP/порт/идентификаторы)."""
    msg = re.sub(r'\[\d{8,12}\]\s*', '', raw_msg)
    msg = re.sub(r'\[[0-9a-fA-F:]+\]:(\d+)', '<IPV6:PORT>', msg)
    msg = re.sub(r'\[[0-9a-fA-F:]+\]', '<IPV6>', msg)
    msg = re.sub(r'(?<![\w:])(?:[0-9a-fA-F]{1,4}:){2,7}[0-9a-fA-F]{1,4}(?![\w:])', '<IPV6>', msg)
    msg = re.sub(r'\b\d{1,3}(?:\.\d{1,3}){3}:\d+\b', '<IP:PORT>', msg)
    # SNI до замены «голого» IPv4: иначе 45.159.249.97.sslip.io превращается в <IP>.sslip.io и не матчится.
    msg = re.sub(
        r'\bserver name mismatch:\s*\S+',
        'server name mismatch: <SNI>',
        msg,
        flags=re.IGNORECASE,
    )
    msg = re.sub(r'\b\d{1,3}(?:\.\d{1,3}){3}\b', '<IP>', msg)
    msg = re.sub(r'\b[a-zA-Z0-9.-]+\.\w+:\d+\b', '<HOST:PORT>', msg)
    msg = re.sub(r'\s+', ' ', msg).strip()
    return msg[:250]


def _try_parse_xray_log_line(line):
    """Одна строка лога (без завершающего \\n) -> dict или None."""
    line = line.rstrip('\r\n')
    if not line or not _line_matches_error_filter(line):
        return None

    match = _XRAY_LOG_LINE_RE.match(line)
    if not match:
        return None
    ts, _level, raw_msg = match.group(1), match.group(2), match.group(3)

    dest = "inbound/unknown"
    dest_match = re.search(r'tcp:(\[[0-9a-fA-F:]+\]:\d+|[a-zA-Z0-9.-]+:\d+)', raw_msg)
    if dest_match:
        dest = dest_match.group(1)
    elif 'api' in raw_msg.lower():
        dest = "api/internal"

    source = "Client"
    src_match = re.search(
        r'from (\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}|\[[0-9a-fA-F:]+\]|(?:[0-9a-fA-F]{1,4}:){2,7}[0-9a-fA-F]{1,4}):',
        raw_msg,
    )
    if src_match:
        source = src_match.group(1).strip('[]')

    src_port = None
    src_port_match = re.search(
        r'from (?:\d{1,3}(?:\.\d{1,3}){3}|\[[0-9a-fA-F:]+\]|(?:[0-9a-fA-F]{1,4}:){2,7}[0-9a-fA-F]{1,4}):(\d+)',
        raw_msg,
    )
    if src_port_match:
        src_port = int(src_port_match.group(1))

    tcp_flow_match = re.search(
        r'read tcp ([^\s>]+)->([^\s:]+(?::\d+))',
        raw_msg,
    )
    if tcp_flow_match:
        left_endpoint = tcp_flow_match.group(1)
        right_endpoint = tcp_flow_match.group(2)
        left_host, left_port = split_host_port(left_endpoint)
        right_host, right_port = split_host_port(right_endpoint)

        if source == "Client" and right_host:
            source = right_host.strip('[]')
            if src_port is None:
                src_port = right_port

        if dest == "inbound/unknown" and left_host:
            if left_port is not None:
                dest = f"{left_host}:{left_port}"
            else:
                dest = left_host

    dest_host = dest
    dest_port = None
    bracket_dest = re.match(r'^\[([0-9a-fA-F:]+)\]:(\d+)$', dest)
    plain_dest = re.match(r'^([^:]+):(\d+)$', dest)
    if bracket_dest:
        dest_host = bracket_dest.group(1)
        dest_port = int(bracket_dest.group(2))
    elif plain_dest:
        dest_host = plain_dest.group(1)
        dest_port = int(plain_dest.group(2))

    source_kind = "unknown"
    try:
        ip_obj = ipaddress.ip_address(source)
        source_kind = "private" if ip_obj.is_private else "public"
    except ValueError:
        source_kind = "unknown"

    clean_msg = re.sub(r'\[\d{8,12}\]\s*', '', raw_msg)
    normalized_type = normalize_error_type(clean_msg)
    return {
        "ts": ts,
        "src": source,
        "src_kind": source_kind,
        "src_port": src_port,
        "dest": dest,
        "dest_host": dest_host,
        "dest_port": dest_port,
        "msg": clean_msg.strip()[:350],
        "error_type": normalized_type,
    }


def read_new_error_log_events(path, byte_offset, stored_inode):
    """
    Читает только новые байты с byte_offset. Возвращает (events, new_byte_offset, inode).

    byte_offset / stored_inode из БД; (None, None) при первом запуске.
    Неполная строка в конце файла не сдвигает offset (дочитается при следующем тике).
    При ротации или усечении файла offset сбрасывается в 0.
    """
    path = path or ERROR_LOG_PATH
    try:
        st = os.stat(path)
    except OSError as e:
        print(f"Лог недоступен ({path}): {e}")
        return [], byte_offset if byte_offset is not None else 0, stored_inode if stored_inode is not None else 0

    inode = int(st.st_ino)
    size = st.st_size

    if byte_offset is None:
        skip_history = os.getenv("ERROR_LOG_SKIP_HISTORY", "true").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
        if skip_history:
            return [], size, inode
        byte_offset = 0
        stored_inode = inode

    if stored_inode is not None and stored_inode != inode:
        byte_offset = 0
    elif byte_offset > size:
        byte_offset = 0

    with open(path, "rb") as f:
        f.seek(byte_offset)
        raw = f.read()

    if not raw:
        return [], byte_offset, inode

    last_nl = raw.rfind(b"\n")
    if last_nl == -1:
        return [], byte_offset, inode

    complete = raw[: last_nl + 1]
    text = complete.decode("utf-8", errors="replace")
    new_offset = byte_offset + len(complete)

    extracted = []
    for line in text.splitlines():
        ev = _try_parse_xray_log_line(line)
        if ev:
            extracted.append(ev)

    return extracted, new_offset, inode


def parse_xray_errors(limit=1000):
    """Устаревшее чтение хвоста файла (последние limit строк). Для отладки / ручных сценариев."""
    extracted_data = []
    path = os.getenv("ERROR_LOG_PATH", "/usr/local/x-ui/error.log").strip() or "/usr/local/x-ui/error.log"
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-limit:]
        for line in lines:
            ev = _try_parse_xray_log_line(line)
            if ev:
                extracted_data.append(ev)
    except Exception as e:
        print(f"Парсинг упал: {e}")

    return extracted_data
