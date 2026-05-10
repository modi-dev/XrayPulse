# Используем официальный образ Python
FROM python:3.11-slim

# Устанавливаем рабочую директорию внутри контейнера
WORKDIR /app

# Копируем файл с зависимостями и устанавливаем их
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем весь код приложения
COPY . .

# Исправляем привязку хоста: заменяем 127.0.0.1 на 0.0.0.0,
# иначе контейнер не будет принимать внешние подключения.
RUN sed -i "s/app.run(host='127.0.0.1', port=5000)/app.run(host='0.0.0.0', port=5000)/" app.py

# Открываем порт, который слушает приложение
EXPOSE 5000

# Переменные окружения по умолчанию (можно переопределить при запуске)
ENV ERROR_LOG_PATH=/var/log/host_error.log \
    AUTH_ENABLED=true \
    GEO_LOOKUP_ENABLED=true \
    FLASK_HOST=0.0.0.0 \
    PORT=5000

# Запускаем приложение
CMD ["python", "app.py"]
