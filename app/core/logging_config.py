"""
app/core/logging_config.py — Настройка системы логирования

Что такое логирование?
    Запись событий в ходе работы программы: что произошло, когда, с какими данными.
    Это как "бортовой журнал" системы.

Зачем это нужно?
    - Отладка: понять, где и почему что-то пошло не так
    - Аудит: кто запустил распределение, сколько строк обработано
    - Мониторинг: следить за работой системы в production

Уровни логирования (от наименее к наиболее критичному):
    DEBUG   — детальные сообщения для разработчика ("обрабатываем партию №123")
    INFO    — важные события ("распределение завершено, обработано 500 работ")
    WARNING — предупреждения ("партия пустая, пропускаем")
    ERROR   — ошибки, из-за которых что-то не работает

Использование в других модулях:
    from app.core.logging_config import get_logger
    logger = get_logger(__name__)
    logger.info("Начинаем импорт файла: %s", filename)
    logger.error("Не удалось подключиться к БД: %s", str(e))
"""

import logging
import sys
from pathlib import Path
from datetime import datetime

from app.core.config import settings


def setup_logging() -> None:
    """
    Настраивает глобальную систему логирования.

    Логи записываются в два места одновременно:
      1. Консоль (stdout) — видно сразу при запуске
      2. Файл в папке logs/ — сохраняется для последующего анализа

    Имя файла лога включает дату и время запуска, например:
        logs/maps_20260523_143000.log
    """
    # Создаём папку logs/ если её нет
    # exist_ok=True — не выдаёт ошибку если папка уже существует
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)

    # Имя файла лога: maps_YYYYMMDD_HHMMSS.log
    log_file = log_dir / f"maps_{datetime.now():%Y%m%d_%H%M%S}.log"

    # Формат строки лога:
    # 2026-05-23 14:30:00 | INFO     | app.allocation.engine | Начинаем распределение
    # ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
    # дата/время          | уровень  | модуль                | сообщение
    log_format = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    date_format = "%Y-%m-%d %H:%M:%S"

    # Определяем уровень логирования из настроек (.env файла)
    log_level = getattr(logging, settings.log_level.upper(), logging.INFO)

    # Настраиваем корневой логгер (влияет на все логгеры в проекте)
    logging.basicConfig(
        level=log_level,
        format=log_format,
        datefmt=date_format,
        handlers=[
            # Хендлер 1: вывод в консоль
            logging.StreamHandler(sys.stdout),
            # Хендлер 2: запись в файл (utf-8 для поддержки кириллицы)
            logging.FileHandler(log_file, encoding="utf-8"),
        ],
    )

    # Подавляем избыточные логи от библиотек
    # SQLAlchemy очень многословна на уровне DEBUG
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("alembic").setLevel(logging.INFO)


def get_logger(name: str) -> logging.Logger:
    """
    Создаёт логгер для конкретного модуля.

    Параметр name — обычно __name__ (имя текущего модуля Python).
    Это позволяет видеть в логе, откуда пришло сообщение.

    Пример использования:
        logger = get_logger(__name__)
        logger.info("Файл загружен")

    Результат в логе:
        2026-05-23 14:30:00 | INFO     | app.services.import_service | Файл загружен
    """
    return logging.getLogger(name)


# Настраиваем логирование при импорте этого модуля
setup_logging()
