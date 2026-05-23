"""
app/db/database.py — Подключение к PostgreSQL и управление сессиями

Что такое SQLAlchemy?
    ORM (Object-Relational Mapper) — библиотека, которая позволяет работать
    с базой данных через Python-объекты, а не писать сырой SQL.

    Вместо:  cursor.execute("SELECT * FROM works WHERE id = 1")
    Пишем:   session.get(Work, 1)

Ключевые понятия:
    Engine  — "движок", управляет пулом соединений к БД
    Session — одна "сессия" работы с БД (открыть → работать → закрыть)
    Base    — базовый класс для всех ORM-моделей (таблиц)

Как использовать в других модулях:
    from app.db.database import get_session

    with get_session() as session:
        work = session.get(Work, work_id)
        print(work.kod_raboty)
"""

from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import settings
from app.core.logging_config import get_logger

logger = get_logger(__name__)


# =============================================================================
# Создание движка (Engine)
# =============================================================================
# Engine управляет пулом соединений — не открывает новое соединение на каждый
# запрос, а переиспользует уже открытые. Это важно для производительности.
#
# pool_size=5    — держать 5 постоянных соединений
# max_overflow=10 — допускать ещё до 10 "временных" при пиковой нагрузке
# pool_pre_ping=True — перед использованием проверять, что соединение живо
engine = create_engine(
    settings.db_url,
    pool_size=5,
    max_overflow=10,
    pool_pre_ping=True,
    echo=settings.is_development,  # В dev-режиме выводить SQL в лог (удобно для отладки)
)

# =============================================================================
# Фабрика сессий
# =============================================================================
# SessionLocal — это "шаблон" для создания сессий.
# autocommit=False — изменения сохраняются только при явном session.commit()
# autoflush=False  — не отправлять изменения в БД до commit()
SessionLocal = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
)


# =============================================================================
# Базовый класс для всех ORM-моделей
# =============================================================================
# Все классы-таблицы (Work, Material, Warehouse и т.д.) наследуются от Base.
# Это позволяет SQLAlchemy "видеть" все таблицы и управлять ими.
class Base(DeclarativeBase):
    """
    Базовый класс для всех моделей (таблиц) базы данных.

    Использование:
        class Work(Base):
            __tablename__ = "works"
            id: Mapped[int] = mapped_column(primary_key=True)
            ...
    """
    pass


# =============================================================================
# Контекстный менеджер для работы с сессией
# =============================================================================
@contextmanager
def get_session() -> Generator[Session, None, None]:
    """
    Контекстный менеджер для безопасной работы с сессией БД.

    Автоматически:
      - Открывает сессию
      - Делает commit при успехе
      - Делает rollback при ошибке (изменения отменяются)
      - Закрывает сессию

    Использование:
        with get_session() as session:
            session.add(new_work)
            # commit произойдёт автоматически при выходе из блока with

    Если внутри блока возникнет исключение — rollback произойдёт автоматически,
    и данные НЕ попадут в базу (атомарность операции).
    """
    session = SessionLocal()
    try:
        yield session          # Передаём сессию в блок with
        session.commit()       # Сохраняем все изменения если не было ошибок
    except Exception as exc:
        session.rollback()     # Откатываем изменения при любой ошибке
        logger.error("Ошибка сессии БД, выполняем rollback: %s", exc)
        raise                  # Пробрасываем исключение дальше
    finally:
        session.close()        # Возвращаем соединение в пул (всегда)


def check_connection() -> bool:
    """
    Проверяет, доступна ли база данных.

    Возвращает True если соединение успешно, False если нет.
    Используется при старте приложения.
    """
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))  # Простейший запрос для проверки
        logger.info("Подключение к PostgreSQL успешно: %s:%s/%s",
                    settings.db_host, settings.db_port, settings.db_name)
        return True
    except Exception as exc:
        logger.error("Не удалось подключиться к PostgreSQL: %s", exc)
        return False


def create_all_tables() -> None:
    """
    Создаёт все таблицы в базе данных (если они ещё не существуют).

    Вызывается один раз при первом запуске системы.
    Если таблицы уже есть — ничего не делает (не затирает данные).

    ВАЖНО: В production лучше использовать Alembic-миграции (alembic upgrade head),
    а не этот метод. Но для MVP и разработки этот метод удобен.
    """
    # Импортируем модели здесь, чтобы SQLAlchemy знал о всех таблицах
    import app.db.models  # noqa: F401 — импорт нужен для регистрации моделей

    Base.metadata.create_all(bind=engine)
    logger.info("Все таблицы созданы/проверены в БД")
