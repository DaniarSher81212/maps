"""
app/repositories/work_repository.py — Репозиторий работ и потребностей

Содержит запросы для:
  - Работ (Work)
  - Потребностей в материалах (Requirement)
"""

from decimal import Decimal
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.logging_config import get_logger
from app.db.models import Material, Requirement, Work
from app.repositories.base import BaseRepository

logger = get_logger(__name__)


class WorkRepository(BaseRepository[Work]):
    """
    Репозиторий для работы с таблицей works.
    """

    def __init__(self, session: Session) -> None:
        super().__init__(session, Work)

    def get_by_kod(self, kod_raboty: str) -> Optional[Work]:
        """Найти работу по бизнес-коду."""
        stmt = select(Work).where(Work.kod_raboty == kod_raboty)
        return self.session.scalar(stmt)

    def get_or_create(
        self,
        kod_raboty: str,
        tip_raboty: Optional[str] = None,
        filial: Optional[str] = None,
        podrazdelenie: Optional[str] = None,
        centr_zatrat: Optional[str] = None,
        zavod: Optional[str] = None,
        data_nachala=None,
        data_okonchaniya=None,
        prioritet: int = 3,
        status: str = "active",
    ) -> Work:
        """Получить работу или создать если не существует."""
        work = self.get_by_kod(kod_raboty)
        if work is None:
            work = Work(
                kod_raboty=kod_raboty,
                tip_raboty=tip_raboty,
                filial=filial,
                podrazdelenie=podrazdelenie,
                centr_zatrat=centr_zatrat,
                zavod=zavod,
                data_nachala=data_nachala,
                data_okonchaniya=data_okonchaniya,
                prioritet=prioritet,
                status=status,
            )
            self.add(work)
            logger.debug("Создана работа: %s", kod_raboty)
        return work

    def get_active_sorted(self) -> list[Work]:
        """
        Получить все активные работы, отсортированные для распределения.

        Порядок сортировки согласно ТЗ:
            1. Приоритет (ASC: 1 — первый, 3 — последний)
            2. Дата начала (ASC: более ранние работы — первые)
            3. Филиал (для детерминированности при одинаковых значениях)
            4. Код работы

        Этот порядок определяет, какая работа получит материалы первой
        при конкурентном распределении.
        """
        stmt = (
            select(Work)
            .where(Work.status == "active")
            .order_by(
                Work.prioritet.asc(),           # 1 → 2 → 3
                Work.data_nachala.asc().nulls_last(),
                Work.filial.asc().nulls_last(),
                Work.kod_raboty.asc(),
            )
        )
        return list(self.session.scalars(stmt).all())

    def get_kod_map(self) -> dict[str, int]:
        """Словарь {kod_raboty: id} для быстрого поиска при импорте."""
        stmt = select(Work.kod_raboty, Work.id)
        rows = self.session.execute(stmt).all()
        return {row.kod_raboty: row.id for row in rows}


class RequirementRepository(BaseRepository[Requirement]):
    """
    Репозиторий для работы с потребностями в материалах.
    """

    def __init__(self, session: Session) -> None:
        super().__init__(session, Requirement)

    def get_by_work_and_material(self, work_id: int, material_id: int) -> Optional[Requirement]:
        """Найти потребность по работе и материалу."""
        stmt = select(Requirement).where(
            Requirement.work_id == work_id,
            Requirement.material_id == material_id,
        )
        return self.session.scalar(stmt)

    def upsert(self, work_id: int, material_id: int, potrebnost: Decimal) -> Requirement:
        """
        Создать или обновить потребность.

        "Upsert" = Update + Insert.
        Если потребность уже есть (та же работа + тот же материал) —
        суммируем количество (дубли из Excel объединяются).
        Если нет — создаём новую.
        """
        req = self.get_by_work_and_material(work_id, material_id)
        if req is None:
            req = Requirement(
                work_id=work_id,
                material_id=material_id,
                potrebnost=potrebnost,
                raspredeleno=Decimal("0"),
                deficit=Decimal("0"),
            )
            self.add(req)
        else:
            # Суммируем если один материал встречается в нескольких строках
            req.potrebnost += potrebnost
        return req

    def get_for_allocation(self) -> list[tuple[Requirement, Work, Material]]:
        """
        Получить все потребности с данными работы и материала,
        отсортированные по приоритету работы.

        Возвращает кортежи (Requirement, Work, Material) для удобной работы.

        Это основной запрос, который питает алгоритм распределения.
        """
        stmt = (
            select(Requirement, Work, Material)
            .join(Work, Requirement.work_id == Work.id)
            .join(Material, Requirement.material_id == Material.id)
            .where(Work.status == "active")
            .order_by(
                Work.prioritet.asc(),
                Work.data_nachala.asc().nulls_last(),
                Work.filial.asc().nulls_last(),
                Work.kod_raboty.asc(),
                Material.sys_nomer.asc(),
            )
        )
        rows = self.session.execute(stmt).all()
        return [(row.Requirement, row.Work, row.Material) for row in rows]

    def reset_allocation(self) -> None:
        """
        Сбросить результаты предыдущего распределения (raspredeleno, deficit → 0).

        Вызывается перед новым запуском алгоритма — чтобы начать с чистого листа.
        """
        stmt = (
            update(Requirement)
            .values(raspredeleno=Decimal("0"), deficit=Decimal("0"))
        )
        self.session.execute(stmt)
        logger.info("Сброшены результаты предыдущего распределения")
