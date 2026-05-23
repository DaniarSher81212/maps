"""
app/repositories/base.py — Базовый репозиторий

Что такое "Репозиторий" (Repository Pattern)?
    Паттерн проектирования, который изолирует логику работы с БД
    от бизнес-логики.

    Без паттерна:
        # Логика распределения сама работает с БД — плохо!
        def allocate(work_id):
            session.query(Work).filter(Work.id == work_id)...

    С паттерном:
        # Репозиторий знает как достать данные
        work = work_repo.get_by_id(work_id)
        # Бизнес-логика работает с объектами, не с SQL
        allocate(work)

    Преимущества:
      - Легко тестировать (можно подменить репозиторий на фиктивный)
      - Код распределения не зависит от конкретной БД
      - Все SQL-запросы в одном месте — легко найти и оптимизировать
"""

from typing import Generic, Optional, Type, TypeVar

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.database import Base

# TypeVar — "тип-переменная" для Generic.
# T должен быть подклассом Base (любой ORM-моделью).
# Это позволяет BaseRepository[Work] или BaseRepository[Material].
T = TypeVar("T", bound=Base)


class BaseRepository(Generic[T]):
    """
    Базовый репозиторий с общими CRUD-операциями.

    CRUD = Create, Read, Update, Delete (Создать, Читать, Обновить, Удалить).

    Параметр Generic[T] означает, что репозиторий работает с конкретным типом:
        work_repo = BaseRepository(session, Work)
        material_repo = BaseRepository(session, Material)
    """

    def __init__(self, session: Session, model: Type[T]) -> None:
        """
        Инициализация репозитория.

        Args:
            session: Сессия SQLAlchemy (открытое соединение с БД)
            model:   Класс ORM-модели (Work, Material, Warehouse и т.д.)
        """
        self.session = session
        self.model = model

    def get_by_id(self, record_id: int) -> Optional[T]:
        """
        Получить запись по первичному ключу.

        Args:
            record_id: ID записи

        Returns:
            Объект модели или None если не найден
        """
        return self.session.get(self.model, record_id)

    def get_all(self) -> list[T]:
        """
        Получить все записи таблицы.

        Внимание: для больших таблиц используйте get_paginated()!
        """
        stmt = select(self.model)
        return list(self.session.scalars(stmt).all())

    def add(self, instance: T) -> T:
        """
        Добавить новую запись в сессию (не в БД!).

        Запись будет сохранена в БД только после session.commit().
        Это позволяет добавить много записей и сохранить их одним коммитом —
        гораздо быстрее, чем коммитить каждую запись отдельно.
        """
        self.session.add(instance)
        return instance

    def add_all(self, instances: list[T]) -> None:
        """
        Добавить список записей. Используйте для массовой вставки.
        Намного эффективнее чем add() в цикле для больших данных.
        """
        self.session.add_all(instances)

    def delete(self, instance: T) -> None:
        """Удалить запись из БД."""
        self.session.delete(instance)

    def flush(self) -> None:
        """
        Отправить изменения в БД без коммита.

        Зачем нужен flush?
            Например, добавили Work → нужен его id → делаем flush →
            получаем id → используем для создания Requirement.
            При этом транзакция ещё открыта (можно откатить).
        """
        self.session.flush()

    def count(self) -> int:
        """Подсчитать количество записей в таблице."""
        from sqlalchemy import func
        stmt = select(func.count()).select_from(self.model)
        result = self.session.scalar(stmt)
        return result or 0
