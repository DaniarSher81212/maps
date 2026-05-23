"""
app/repositories/material_repository.py — Репозиторий материалов и складских остатков

Содержит специализированные запросы для:
  - Справочника материалов (Material)
  - Складских партий (StockBatch)
  - Строк поставок (SupplyLine)
"""

from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.core.logging_config import get_logger
from app.db.models import Material, StockBatch, Supply, SupplyLine, Warehouse
from app.repositories.base import BaseRepository

logger = get_logger(__name__)


class MaterialRepository(BaseRepository[Material]):
    """
    Репозиторий для работы со справочником материалов.
    """

    def __init__(self, session: Session) -> None:
        super().__init__(session, Material)

    def get_by_sys_nomer(self, sys_nomer: str) -> Optional[Material]:
        """
        Найти материал по системному номеру SAP.

        Args:
            sys_nomer: Системный номер материала (например "000000000010012345")

        Returns:
            Material или None
        """
        stmt = select(Material).where(Material.sys_nomer == sys_nomer)
        return self.session.scalar(stmt)

    def get_or_create(self, sys_nomer: str, naimenovanie: Optional[str] = None,
                      ed_izm: Optional[str] = None, gruppa: Optional[str] = None) -> Material:
        """
        Получить материал или создать если не существует.

        Этот метод часто используется при импорте:
        мы не знаем заранее, есть ли материал в БД,
        поэтому проверяем и создаём при необходимости.

        Args:
            sys_nomer:    Системный номер
            naimenovanie: Наименование (обновляется если уже есть)
            ed_izm:       Единица измерения
            gruppa:       Группа материала

        Returns:
            Существующий или новый объект Material
        """
        material = self.get_by_sys_nomer(sys_nomer)
        if material is None:
            # Материала нет — создаём
            material = Material(
                sys_nomer=sys_nomer,
                naimenovanie=naimenovanie,
                ed_izm=ed_izm,
                gruppa=gruppa,
            )
            self.add(material)
            logger.debug("Создан новый материал: %s", sys_nomer)
        else:
            # Материал есть — обновляем наименование если оно стало известно
            if naimenovanie and not material.naimenovanie:
                material.naimenovanie = naimenovanie
            if ed_izm and not material.ed_izm:
                material.ed_izm = ed_izm
        return material

    def get_id_map(self) -> dict[str, int]:
        """
        Вернуть словарь {sys_nomer: id} для всех материалов.

        Используется для быстрого поиска ID по номеру SAP
        без обращения к БД на каждую строку (оптимизация для импорта).

        Например:
            id_map = material_repo.get_id_map()
            material_id = id_map["000000000010012345"]  # O(1) поиск
        """
        stmt = select(Material.sys_nomer, Material.id)
        rows = self.session.execute(stmt).all()
        return {row.sys_nomer: row.id for row in rows}


class StockBatchRepository(BaseRepository[StockBatch]):
    """
    Репозиторий для работы со складскими партиями.
    """

    def __init__(self, session: Session) -> None:
        super().__init__(session, StockBatch)

    def get_available_for_material(
        self,
        material_id: int,
        min_qty: Decimal = Decimal("0.0001"),
    ) -> list[StockBatch]:
        """
        Получить все доступные партии для материала, отсортированные по FIFO.

        FIFO (First In First Out) = сначала используем самые старые партии.
        Сортировка: data_postupleniya ASC (старые даты — первые).

        Args:
            material_id: ID материала в БД
            min_qty:     Минимальное доступное количество (фильтруем пустые)

        Returns:
            Список партий, отсортированных от старой к новой
        """
        stmt = (
            select(StockBatch)
            .options(joinedload(StockBatch.warehouse))  # Загружаем склад сразу (join)
            .where(
                StockBatch.material_id == material_id,
                StockBatch.dostupno >= min_qty,  # Только с ненулевым остатком
            )
            .order_by(
                StockBatch.data_postupleniya.asc().nulls_last(),  # FIFO: старые первые
                StockBatch.id.asc(),  # При одинаковой дате — по порядку добавления
            )
        )
        return list(self.session.scalars(stmt).all())

    def get_available_by_warehouse_priority(
        self,
        material_id: int,
        zavod: Optional[str],
        filial: Optional[str],
        min_qty: Decimal = Decimal("0.0001"),
    ) -> dict[str, list[StockBatch]]:
        """
        Получить партии, сгруппированные по приоритету склада.

        Согласно ТЗ, приоритеты:
            1 — склады того же завода (zavod совпадает с заводом работы)
            2 — склады того же филиала (но другого завода)
            3 — все остальные склады

        Args:
            material_id: ID материала
            zavod:       Код завода работы (может быть None)
            filial:      Код филиала работы (может быть None)
            min_qty:     Минимальное доступное количество

        Returns:
            Словарь {"zavod": [...], "filial": [...], "prochee": [...]}
        """
        # Получаем все доступные партии с данными склада
        stmt = (
            select(StockBatch)
            .join(Warehouse, StockBatch.warehouse_id == Warehouse.id)
            .where(
                StockBatch.material_id == material_id,
                StockBatch.dostupno >= min_qty,
            )
            .order_by(
                StockBatch.data_postupleniya.asc().nulls_last(),
                StockBatch.id.asc(),
            )
        )
        all_batches = list(self.session.scalars(stmt).all())

        # Разбиваем на три группы по приоритету
        result: dict[str, list[StockBatch]] = {
            "zavod": [],    # Приоритет 1: тот же завод
            "filial": [],   # Приоритет 2: тот же филиал, другой завод
            "prochee": [],  # Приоритет 3: остальные
        }

        for batch in all_batches:
            wh = batch.warehouse
            if wh is None:
                result["prochee"].append(batch)
                continue

            if zavod and wh.zavod == zavod:
                # Склад в том же заводе — высший приоритет
                result["zavod"].append(batch)
            elif filial and wh.filial == filial:
                # Склад в том же филиале — средний приоритет
                result["filial"].append(batch)
            else:
                # Остальные склады
                result["prochee"].append(batch)

        return result

    def bulk_create(self, batches: list[StockBatch]) -> None:
        """Массовое добавление партий (для импорта Excel)."""
        self.session.add_all(batches)


class SupplyRepository(BaseRepository[Supply]):
    """
    Репозиторий для работы с поставками.
    """

    def __init__(self, session: Session) -> None:
        super().__init__(session, Supply)

    def get_available_lines_for_material(
        self,
        material_id: int,
        exclude_statuses: Optional[list[str]] = None,
        min_qty: Decimal = Decimal("0.0001"),
    ) -> list[SupplyLine]:
        """
        Получить строки поставок для материала, отсортированные по дате поставки.

        Используется на втором этапе распределения — после склада.
        Берём поставки с ближайшей датой поставки (они придут раньше).

        Args:
            material_id:      ID материала
            exclude_statuses: Статусы поставок, которые НЕ берём (например "cancelled")
            min_qty:          Минимальное доступное количество

        Returns:
            Строки поставок, отсортированные по дате (ближайшие — первые)
        """
        if exclude_statuses is None:
            exclude_statuses = ["cancelled", "arrived"]

        stmt = (
            select(SupplyLine)
            .join(Supply, SupplyLine.supply_id == Supply.id)
            .where(
                SupplyLine.material_id == material_id,
                SupplyLine.dostupno >= min_qty,
                Supply.status.notin_(exclude_statuses),
            )
            .order_by(
                Supply.data_postavki.asc().nulls_last(),  # Ближайшие поставки первые
                SupplyLine.id.asc(),
            )
            .options(joinedload(SupplyLine.supply))
        )
        return list(self.session.scalars(stmt).all())
