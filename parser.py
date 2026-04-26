import re

ERROR_LOG_PATH = '/usr/local/x-ui/error.log'

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

                    clean_msg = re.sub(r'\[\d{8,12}\]\s*', '', raw_msg)
                    extracted_data.append({
                        "ts": ts,
                        "src": source,
                        "dest": dest,
                        "msg": clean_msg.strip()[:200]
                    })
    except Exception as e:
        print(f"Парсинг упал: {e}")
    
    return extracted_data