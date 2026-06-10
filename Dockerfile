# =============================================================================
# Dockerfile — контейнер для MAPS
#
# Многоэтапная сборка (multi-stage build):
#   - Этап builder: устанавливает зависимости Python в отдельном слое
#   - Этап runtime: копирует только готовые пакеты — образ меньше
#
# Сборка:
#   docker build -t maps .
#
# Запуск (с docker-compose.yml):
#   docker compose up -d
# =============================================================================

# ── Этап 1: установка зависимостей ───────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /build

# Системные библиотеки, нужные для компиляции psycopg2 (клиент PostgreSQL)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Копируем только файлы зависимостей (до копирования кода — лучше кэшируется)
COPY pyproject.toml requirements.txt ./

# Устанавливаем зависимости в /build/packages (не в систему)
RUN pip install --upgrade pip && \
    pip install --prefix=/build/packages -r requirements.txt


# ── Этап 2: финальный образ ───────────────────────────────────────────────────
FROM python:3.11-slim

WORKDIR /app

# Только runtime-библиотека PostgreSQL (не весь dev-пакет)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    && rm -rf /var/lib/apt/lists/*

# Копируем пакеты Python из этапа builder
COPY --from=builder /build/packages /usr/local

# Копируем код приложения
COPY . .

# Устанавливаем само приложение (без зависимостей — они уже есть)
RUN pip install --no-deps -e .

# Создаём директории для данных и экспорта
RUN mkdir -p /app/data/exports /app/logs

# Запускаем от непривилегированного пользователя (безопаснее чем root)
RUN useradd -m -u 1000 maps
RUN chown -R maps:maps /app
USER maps

# MAPS слушает на порту 8000
EXPOSE 8000

# Переменные окружения по умолчанию (переопределяются через .env или docker-compose)
ENV APP_ENV=production
ENV LOG_LEVEL=INFO
ENV SESSION_PREFIX=session
ENV DATA_DIR=/app/data
ENV EXPORT_DIR=/app/data/exports

# Команда запуска
CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--log-level", "info"]
