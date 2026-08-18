"""
app/core/config.py — Конфигурация приложения

Что делает этот файл?
    Читает настройки из файла .env и переменных окружения.
    Все настройки собраны в одном месте — не нужно искать их по всему коду.

Как использовать в других модулях:
    from app.core.config import settings
    print(settings.db_url)          # строка подключения к БД
    print(settings.log_level)       # уровень логирования
"""

from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Класс настроек приложения.

    pydantic-settings автоматически:
      1. Читает переменные из файла .env
      2. Проверяет типы (строка, число, bool)
      3. Подставляет значения по умолчанию если переменная не задана

    Field(...) означает обязательное поле (без значения по умолчанию).
    Field("default") — поле с умолчанием.
    """

    model_config = SettingsConfigDict(
        env_file=".env",          # Читаем из файла .env в текущей папке
        env_file_encoding="utf-8",
        case_sensitive=False,     # DB_HOST и db_host — одно и то же
        extra="ignore",           # Неизвестные переменные в .env игнорируем
    )

    # --- Параметры подключения к PostgreSQL ---
    # Эти значения читаются из .env файла (переменные DB_HOST, DB_PORT и т.д.)
    db_host: str = Field(default="localhost", description="Хост PostgreSQL")
    db_port: int = Field(default=5432, description="Порт PostgreSQL")
    db_name: str = Field(default="maps_db", description="Имя базы данных")
    db_user: str = Field(default="maps_user", description="Пользователь БД")
    db_password: str = Field(default="password", description="Пароль БД")

    # --- Настройки приложения ---
    app_env: str = Field(default="development", description="Окружение: development | production")
    log_level: str = Field(default="INFO", description="Уровень логирования")
    session_prefix: str = Field(default="session", description="Префикс имён сессий")

    # --- Пути ---
    data_dir: str = Field(default="data", description="Папка с Excel-файлами")
    export_dir: str = Field(default="data/exports", description="Папка для экспорта результатов")

    # --- Аутентификация дашборда ---
    # Секретный ключ для подписи сессионных кук (SessionMiddleware).
    # Должен быть длинным случайным значением — менять при компрометации.
    secret_key: str = Field(default="change-me-in-production", description="Секрет для сессий")
    # Логин и пароль для входа в дашборд
    maps_username: str = Field(default="admin", description="Логин дашборда")
    maps_password: str = Field(default="CHANGE_ME", description="Пароль дашборда")

    # --- AI-анализ ---
    # Ключ Anthropic API для функции объяснения дефицита.
    # None если не задан — AI-анализ просто недоступен, остальное работает.
    anthropic_api_key: str | None = Field(default=None, description="Anthropic API key")

    # --- Вычисляемые поля ---
    # @computed_field — это поле вычисляется автоматически из других полей,
    # его НЕ нужно задавать в .env

    # Если задана — используется напрямую (перекрывает сборку из db_host/port/user/pass).
    # Удобно для socket-подключения: postgresql+psycopg2://user@/dbname?host=/var/run/postgresql
    database_url: str = Field(default="", description="Полный URL БД (опционально)")

    @computed_field
    @property
    def db_url(self) -> str:
        """
        Строка подключения к PostgreSQL в формате SQLAlchemy.

        Если в .env задана DATABASE_URL — используется она.
        Иначе собирается из отдельных параметров DB_HOST, DB_PORT и т.д.
        """
        if self.database_url:
            return self.database_url
        return (
            f"postgresql://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )

    @computed_field
    @property
    def is_development(self) -> bool:
        """Возвращает True если мы в режиме разработки."""
        return self.app_env.lower() == "development"


# Создаём единственный экземпляр настроек.
# Все модули импортируют именно этот объект — он создаётся один раз.
# Паттерн называется "Singleton" (одиночка).
settings = Settings()
