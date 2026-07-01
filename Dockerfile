FROM python:3.11-slim

WORKDIR /app

# Установка системных зависимостей
# ffmpeg: transcode YouTube downloads to HLS (video ingest worker, scheduler container)
RUN apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Копирование requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копирование кода
COPY src/ ./src/
COPY scripts/ ./scripts/
COPY alembic/ ./alembic/
COPY alembic.ini .

# Создание директории для uploads
RUN mkdir -p uploads

# Создание стартового скрипта с миграциями
RUN echo '#!/bin/bash\n\
set -e\n\
echo "🔄 Running Alembic migrations..."\n\
alembic upgrade head\n\
echo "✅ Migrations completed"\n\
echo "🚀 Starting FastAPI application with 4 workers..."\n\
uvicorn src.app:socket_app --host 0.0.0.0 --port 8000 --workers 4\n\
' > /app/start.sh && chmod +x /app/start.sh

# Открытие порта
EXPOSE 8000

# Запуск приложения с автоматическими миграциями
CMD ["/app/start.sh"]
