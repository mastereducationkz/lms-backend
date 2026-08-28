FROM python:3.11-slim

WORKDIR /app

# Установка системных зависимостей
# ffmpeg: transcode YouTube downloads to HLS (video ingest worker, scheduler container)
# fonts-dejavu-core: Cyrillic glyphs for the student report PDF (src/reports/pdf.py)
RUN apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    ffmpeg \
    fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# Deno: JS runtime yt-dlp needs to solve YouTube's nsig challenge. Without it YouTube
# throttles video downloads to a crawl (read timeouts). yt-dlp auto-detects deno on PATH.
#
# The download retries because GitHub's release CDN returns 503 often enough to have failed
# four builds and deploys in a single evening, and a plain `curl -fsSL` gives up on the first
# one. Eight attempts with widening backoff, each with curl's own retry; the archive is then
# verified non-empty, because a truncated download would otherwise fail later in unzip with a
# far less obvious message.
RUN apt-get update && apt-get install -y --no-install-recommends curl unzip ca-certificates \
    && ARCH="$(uname -m)" \
    && case "$ARCH" in \
         x86_64)        DENO_ARCH=x86_64-unknown-linux-gnu ;; \
         aarch64|arm64) DENO_ARCH=aarch64-unknown-linux-gnu ;; \
         *) echo "unsupported arch: $ARCH" && exit 1 ;; \
       esac \
    && for attempt in 1 2 3 4 5 6 7 8; do \
         curl -fsSL --retry 5 --retry-delay 5 --retry-all-errors --retry-connrefused \
           "https://github.com/denoland/deno/releases/latest/download/deno-${DENO_ARCH}.zip" \
           -o /tmp/deno.zip && break; \
         echo "deno download attempt ${attempt} failed; retrying"; \
         sleep $((attempt * 5)); \
       done \
    && test -s /tmp/deno.zip \
    && unzip -o /tmp/deno.zip -d /usr/local/bin \
    && chmod +x /usr/local/bin/deno \
    && rm /tmp/deno.zip \
    && apt-get purge -y unzip \
    && apt-get autoremove -y \
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
