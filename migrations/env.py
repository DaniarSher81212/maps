"""
migrations/env.py — Конфигурация среды выполнения Alembic

Что делает этот файл?
    Alembic запускает этот скрипт при каждой команде (revision, upgrade, downgrade...).
    Файл отвечает за два вещи:
      1. Подключение к БД — берём URL из нашего settings (файл .env), не из alembic.ini
      2. Метаданные моделей — говорим Alembic какие таблицы существуют, чтобы он мог
         сравнивать текущую схему БД с моделями и генерировать миграции автоматически

Два режима работы Alembic:
    offline — генерирует SQL-скрипты без подключения к БД (для ревью / CI)
    online  — подключается к БД и применяет миграции напрямую

Использование:
    alembic revision --autogenerate -m "добавить таблицу xxx"
    alembic upgrade head
    alembic downgrade -1
"""

from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool
from alembic import context

# Alembic Config — доступ к настройкам из alembic.ini
config = context.config

# Настройка логирования из секции [loggers] в alembic.ini
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# ── Подключаем метаданные наших моделей ──────────────────────────────────────
# Base.metadata содержит описание всех таблиц из app/db/models.py.
# Alembic использует его для --autogenerate: сравнивает модели с реальной схемой БД
# и генерирует ALTER TABLE / CREATE TABLE / DROP TABLE автоматически.
from app.db.models import Base  # noqa: E402
target_metadata = Base.metadata

# ── URL подключения к БД из нашего .env ──────────────────────────────────────
# Берём db_url из settings (он читает DATABASE_URL или собирает из DB_HOST/PORT/...).
# Переопределяем значение из alembic.ini — там стоит заглушка.
from app.core.config import settings  # noqa: E402
config.set_main_option("sqlalchemy.url", settings.db_url)


def run_migrations_offline() -> None:
    """
    Оффлайн-режим: генерировать SQL без подключения к БД.

    Удобно для:
        - Ревью миграций перед применением
        - CI/CD пайплайнов без доступа к БД
        - Генерации скриптов для DBA

    Запуск: alembic upgrade head --sql
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # compare_type=True — Alembic будет сравнивать и типы колонок (VARCHAR(50) vs VARCHAR(100))
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """
    Онлайн-режим: подключиться к БД и применить миграции.

    Стандартный режим для разработки и деплоя.
    Запуск: alembic upgrade head
    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        # NullPool — не держать соединения в пуле (Alembic — одноразовый процесс)
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # compare_type=True — отслеживать изменения типов колонок
            compare_type=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
