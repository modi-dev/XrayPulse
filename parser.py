import re

ERROR_LOG_PATH = './error.log'

def normalize_error_type(raw_msg):
    """Нормализует текст ошибки, убирая переменные данные (IP/порт/идентификаторы)."""
    msg = re.sub(r'\[\d{8,12}\]\s*', '', raw_msg)
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
                    dest = "direct/api"
                    dest_match = re.search(r'tcp:([a-zA-Z0-9.-]+:\d+)', raw_msg)
                    if dest_match:
                        dest = dest_match.group(1)

                    # Извлекаем ОТКУДА (Source)
                    source = "Client"
                    src_match = re.search(r'from (\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}):', raw_msg)
                    if src_match:
                        source = src_match.group(1)

                    src_port = None
                    src_port_match = re.search(r'from \d{1,3}(?:\.\d{1,3}){3}:(\d+)', raw_msg)
                    if src_port_match:
                        src_port = int(src_port_match.group(1))

                    dest_host = dest.split(':')[0] if ':' in dest else dest
                    dest_port = int(dest.split(':')[-1]) if ':' in dest and dest.split(':')[-1].isdigit() else None

                    clean_msg = re.sub(r'\[\d{8,12}\]\s*', '', raw_msg)
                    normalized_type = normalize_error_type(clean_msg)
                    extracted_data.append({
                        "ts": ts,
                        "src": source,
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