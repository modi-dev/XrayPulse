import re
import ipaddress

ERROR_LOG_PATH = './error.log'

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
    msg = re.sub(r'\b\d{1,3}(?:\.\d{1,3}){3}\b', '<IP>', msg)
    msg = re.sub(r'\b[a-zA-Z0-9.-]+\.\w+:\d+\b', '<HOST:PORT>', msg)
    msg = re.sub(r'\s+', ' ', msg).strip()
    return msg[:250]

def parse_xray_errors(limit=1000):
    extracted_data = []
    try:
        with open(ERROR_LOG_PATH, 'r') as f:
            lines = f.readlines()[-limit:]
            for line in lines:
                # Ищем: Дата + Уровень + Сообщение
                match = re.search(r'^(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}).*?\[(Error|Warning|Info)\] (.*)', line)
                if match:
                    ts, level, raw_msg = match.group(1), match.group(2), match.group(3)
                    
                    if level == 'Info' and 'failed' not in raw_msg.lower():
                        continue

                    # Извлекаем КУДА (Destination)
                    # Не ставим "direct/api" по умолчанию, чтобы не вводить в заблуждение.
                    dest = "inbound/unknown"
                    dest_match = re.search(r'tcp:(\[[0-9a-fA-F:]+\]:\d+|[a-zA-Z0-9.-]+:\d+)', raw_msg)
                    if dest_match:
                        dest = dest_match.group(1)
                    elif 'api' in raw_msg.lower():
                        dest = "api/internal"

                    # Извлекаем ОТКУДА (Source)
                    source = "Client"
                    src_match = re.search(r'from (\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}|\[[0-9a-fA-F:]+\]|(?:[0-9a-fA-F]{1,4}:){2,7}[0-9a-fA-F]{1,4}):', raw_msg)
                    if src_match:
                        source = src_match.group(1).strip('[]')

                    src_port = None
                    src_port_match = re.search(r'from (?:\d{1,3}(?:\.\d{1,3}){3}|\[[0-9a-fA-F:]+\]|(?:[0-9a-fA-F]{1,4}:){2,7}[0-9a-fA-F]{1,4}):(\d+)', raw_msg)
                    if src_port_match:
                        src_port = int(src_port_match.group(1))

                    # Фоллбек: читаем endpoint-пару из read tcp A:B->C:D
                    tcp_flow_match = re.search(
                        r'read tcp ([^\s>]+)->([^\s:]+(?::\d+))',
                        raw_msg
                    )
                    if tcp_flow_match:
                        left_endpoint = tcp_flow_match.group(1)
                        right_endpoint = tcp_flow_match.group(2)
                        left_host, left_port = split_host_port(left_endpoint)
                        right_host, right_port = split_host_port(right_endpoint)

                        # Если source не определили через "from ...", берем peer (правую часть) как клиент.
                        if source == "Client" and right_host:
                            source = right_host.strip('[]')
                            if src_port is None:
                                src_port = right_port

                        # Если destination остался unknown, хотя в сообщении есть tcp-пара,
                        # показываем локальный endpoint, на котором произошла ошибка чтения.
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
                    extracted_data.append({
                        "ts": ts,
                        "src": source,
                        "src_kind": source_kind,
                        "src_port": src_port,
                        "dest": dest,
                        "dest_host": dest_host,
                        "dest_port": dest_port,
                        "msg": clean_msg.strip()[:350],
                        "error_type": normalized_type
                    })
    except Exception as e:
        print(f"Парсинг упал: {e}")
    
    return extracted_data