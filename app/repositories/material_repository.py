"""
app/repositories/material_repository.py — Репозитории материалов, складов и поставок

Содержит специализированные запросы для:
  - Справочника материалов (Material)
  - Складских партий (StockBatch)
  - Строк поставок (SupplyLine)
  - Фактических списаний (WriteOff) — Слой 1
  - Выдано не списано (IssuedNotWrittenOff) — Слой 2

Каждый репозиторий — это «умный» доступ к одной таблице.
Бизнес-логика (engine.py) вызывает методы репозитория и получает готовые объекты,
не вникая в детали SQL.
"""

from decimal import Decimal
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.orm import Session, joinedload

from app.core.logging_config import get_logger
from app.db.models import (
    ApprovedTransfer,
    IssuedNotWrittenOff,
    Material,
    StockBatch,
    Supply,
    SupplyLine,
    Warehouse,
    WriteOff,
)
from app.repositories.base import BaseRepository

logger = get_logger(__name__)


# =============================================================================
# Репозиторий справочника материалов
# =============================================================================

class MaterialRepository(BaseRepository[Material]):
    """
    Репозиторий для работы со справочником материалов.

    Материал в MAPS — это позиция номенклатуры SAP MM с уникальным sys_nomer.
    """

    def __init__(self, session: Session) -> None:
        super().__init__(session, Material)

    def get_by_sys_nomer(self, sys_nomer: str) -> Optional[Material]:
        """
        Найти материал по системному номеру SAP.

        Args:
            sys_nomer: Системный номер, например "000000000010012345"

        Returns:
            Объект Material или None
        """
        stmt = select(Material).where(Material.sys_nomer == sys_nomer)
        return self.session.scalar(stmt)

    def get_or_create(
        self,
        sys_nomer: str,
        naimenovanie: Optional[str] = None,
        ed_izm: Optional[str] = None,
        gruppa: Optional[str] = None,
    ) -> Material:
        """
        Получить материал или создать если не существует.

        Используется при импорте любых Excel-файлов: если материал встречается
        впервые — создаём, если уже есть — обновляем наименование (если стало известно).

        Args:
            sys_nomer:    Системный номер (ключ поиска)
            naimenovanie: Наименование (обновляется если поле было пустым)
            ed_izm:       Единица измерения
            gruppa:       Группа материала

        Returns:
            Существующий или новый объект Material
        """
        material = self.get_by_sys_nomer(sys_nomer)
        if material is None:
            # Материала нет в БД — создаём новую запись
            material = Material(
                sys_nomer=sys_nomer,
                naimenovanie=naimenovanie,
                ed_izm=ed_izm,
                gruppa=gruppa,
            )
            self.add(material)
            logger.debug("Создан новый материал: %s", sys_nomer)
        else:
            # Материал уже есть — дополняем поля, если они были пустыми
            # (не заменяем существующие данные, только добавляем недостающие)
            if naimenovanie and not material.naimenovanie:
                material.naimenovanie = naimenovanie
            if ed_izm and not material.ed_izm:
                material.ed_izm = ed_izm
        return material

    def get_id_map(self) -> dict[str, int]:
        """
        Вернуть словарь {sys_nomer: id} для всех материалов.

        Используется при импорте для быстрого поиска ID по номеру SAP (O(1))
        вместо SELECT-запроса для каждой строки Excel.
        """
        stmt = select(Material.sys_nomer, Material.id)
        rows = self.session.execute(stmt).all()
        return {row.sys_nomer: row.id for row in rows}


# =============================================================================
# Репозиторий складских партий (Слой 3 распределения)
# =============================================================================

class StockBatchRepository(BaseRepository[StockBatch]):
    """
    Репозиторий для работы со складскими партиями.

    Каждая партия — это конкретный приход материала на склад с ценой и датой.
    При распределении используется FIFO: более старые партии расходуются первыми.
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

        FIFO (First In First Out) = самые старые партии — первыми.
        Логика: сначала расходуем то, что дольше лежит на складе.

        Args:
            material_id: ID материала в БД
            min_qty:     Минимальное доступное количество (исключаем пустые партии)

        Returns:
            Список партий, отсортированных от старой к новой по дате поступления
        """
        stmt = (
            select(StockBatch)
            # joinedload — загружаем связанный склад (Warehouse) ОДНИМ запросом, а не N+1
            .options(joinedload(StockBatch.warehouse))
            .where(
                StockBatch.material_id == material_id,
                StockBatch.dostupno >= min_qty,  # Только ненулевые остатки
            )
            .order_by(
                StockBatch.data_postupleniya.asc().nulls_last(),  # FIFO: старые первые
                StockBatch.id.asc(),  # При одинаковой дате — по порядку создания
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
        Получить партии, разделённые на три группы по приоритету склада.

        Приоритеты складов согласно ТЗ:
            "zavod"   — Склад того же завода, что и работа (высший приоритет)
                        Материал из «своего» завода — оптимально по логистике
            "filial"  — Склад того же филиала, другого завода (средний)
                        Можно взять внутри филиала
            "prochee" — Склады ДРУГИХ филиалов (для анализа, НЕ распределяем)
                        Показываем как «возможное» — требует согласования переброски

        Зачем три группы?
            Алгоритм сначала полностью использует "zavod"-партии,
            затем "filial"-партии, а "prochee" только показывает в отчёте
            как возможный резерв без фактического списания.

        Args:
            material_id: ID материала
            zavod:       Код завода работы (может быть None — у работы нет завода)
            filial:      Код филиала работы (может быть None)
            min_qty:     Минимальное доступное количество

        Returns:
            Словарь с тремя ключами: {"zavod": [...], "filial": [...], "prochee": [...]}
        """
        # Получаем ВСЕ доступные партии для этого материала (один запрос к БД)
        stmt = (
            select(StockBatch)
            .join(Warehouse, StockBatch.warehouse_id == Warehouse.id)
            .where(
                StockBatch.material_id == material_id,
                StockBatch.dostupno >= min_qty,
            )
            .order_by(
                # Единая FIFO-сортировка по всем группам
                StockBatch.data_postupleniya.asc().nulls_last(),
                StockBatch.id.asc(),
            )
        )
        all_batches = list(self.session.scalars(stmt).all())

        # Разделяем на три группы в Python (не в SQL — проще читать, одинаково быстро)
        result: dict[str, list[StockBatch]] = {
            "zavod": [],    # Тот же завод — высший приоритет
            "filial": [],   # Тот же филиал, другой завод — средний приоритет
            "prochee": [],  # Другой филиал — только для анализа
        }

        for batch in all_batches:
            wh = batch.warehouse
            if wh is None:
                # Партия без склада (нет связи) — отправляем в "prochee"
                result["prochee"].append(batch)
                continue

            if zavod and wh.zavod == zavod:
                # Склад в том же заводе → группа "zavod"
                result["zavod"].append(batch)
            elif filial and wh.filial == filial:
                # Склад в том же филиале (но другой завод) → группа "filial"
                result["filial"].append(batch)
            else:
                # Всё остальное (другой филиал) → "prochee"
                result["prochee"].append(batch)

        return result

    def reset_dostupno(self) -> None:
        """
        Восстановить доступное количество всех партий до полного остатка.

        Вызывается в начале каждого запуска AllocationEngine.run() — ПОСЛЕ
        сброса потребностей, но ДО основного цикла распределения.

        Зачем нужен сброс?
            Алгоритм распределения уменьшает dostupno у партий в процессе
            работы и записывает эти изменения в БД в конце (_update_availability_in_db).
            Если запустить распределение повторно без сброса — склад покажется пустым,
            потому что dostupno от предыдущего запуска остался нулевым.

        Принцип: dostupno = kolichestvo означает «весь материал снова доступен».
        Это НЕ отменяет реальные выдачи — kolichestvo остаётся неизменным
        и отражает реальный физический остаток на складе.
        """
        stmt = update(StockBatch).values(dostupno=StockBatch.kolichestvo)
        self.session.execute(stmt)
        logger.info("Остатки партий сброшены: dostupno = kolichestvo")

    def get_batches_for_warehouse_material(
        self,
        warehouse_id: int,
        material_id: int,
        min_qty: Decimal = Decimal("0.0001"),
    ) -> list[StockBatch]:
        """
        Получить FIFO-упорядоченные партии конкретного склада для данного материала.

        Используется в Layer 3в (согласованные перемещения):
        когда мы знаем конкретный склад-источник, нужно взять его партии FIFO.

        Args:
            warehouse_id: ID склада-источника (из ApprovedTransfer.from_warehouse_id)
            material_id:  ID материала
            min_qty:      Минимальный остаток (пустые партии не берём)

        Returns:
            Список партий отсортированных от старой к новой (FIFO)
        """
        stmt = (
            select(StockBatch)
            .where(
                StockBatch.warehouse_id == warehouse_id,
                StockBatch.material_id == material_id,
                StockBatch.dostupno >= min_qty,
            )
            .order_by(
                StockBatch.data_postupleniya.asc().nulls_last(),
                StockBatch.id.asc(),
            )
        )
        return list(self.session.scalars(stmt).all())

    def bulk_create(self, batches: list[StockBatch]) -> None:
        """Массовое добавление партий (для импорта Excel)."""
        self.session.add_all(batches)


# =============================================================================
# Репозиторий поставок (Слой 4 распределения)
# =============================================================================

class SupplyRepository(BaseRepository[Supply]):
    """
    Репозиторий для работы с поставками («материалы в пути»).

    Важный нюанс: поставки в MAPS привязаны к ФИЛИАЛУ, не к заводу.
    При распределении из поставок мы проверяем:
        supply.filial == work.filial (филиал поставки = филиал работы)
    Внутри завода поставки не разделяются — это особенность бизнес-процесса.
    """

    def __init__(self, session: Session) -> None:
        super().__init__(session, Supply)

    def get_available_lines_for_material(
        self,
        material_id: int,
        filial: Optional[str] = None,          # НОВЫЙ параметр: фильтр по филиалу
        exclude_statuses: Optional[list[str]] = None,
        min_qty: Decimal = Decimal("0.0001"),
    ) -> list[SupplyLine]:
        """
        Получить строки поставок для материала, подходящие для данной работы.

        Ключевое правило: поставки идут на ФИЛИАЛ, не на завод.
        Поэтому мы фильтруем по filial работы, а не по zavod.

        Сортировка: ближайшие даты поставки — первыми (логично: чем раньше придёт,
        тем надёжнее покрытие).

        Args:
            material_id:      ID материала в БД
            filial:           Филиал работы — фильтруем поставки только этого филиала.
                              Если None — берём все (для случаев без привязки к филиалу).
            exclude_statuses: Статусы поставок, которые НЕ берём.
                              По умолчанию исключаем "cancelled" и "arrived"
                              ("arrived" = уже прибыло, должно быть в складских остатках)
            min_qty:          Минимальный доступный остаток

        Returns:
            Список строк поставок, отсортированных по дате (ближайшие — первые)
        """
        # Если не передан список исключений — используем дефолтный
        if exclude_statuses is None:
            exclude_statuses = ["cancelled", "arrived"]

        # Строим запрос шаг за шагом для наглядности
        stmt = (
            select(SupplyLine)
            .join(Supply, SupplyLine.supply_id == Supply.id)  # Соединяем строку с заголовком поставки
            .where(
                SupplyLine.material_id == material_id,
                SupplyLine.dostupno >= min_qty,
                Supply.status.notin_(exclude_statuses),  # Исключаем отменённые и прибывшие
            )
            .order_by(
                Supply.data_postavki.asc().nulls_last(),  # Ближайшие поставки первые
                SupplyLine.id.asc(),
            )
            # Загружаем Supply вместе (нужен для доступа к dogovor, postavshchik, filial)
            .options(joinedload(SupplyLine.supply))
        )

        # ── Фильтр по филиалу (КЛЮЧЕВОЕ БИЗНЕС-ПРАВИЛО) ──
        # Поставки идут на филиал, не на завод.
        # Работа получает только те поставки, которые адресованы её филиалу.
        if filial:
            # Добавляем условие: filial поставки = filial работы
            stmt = stmt.where(Supply.filial == filial)

        return list(self.session.scalars(stmt).all())

    def reset_dostupno(self) -> None:
        """
        Восстановить доступное количество всех строк поставок до полного объёма.

        Аналогично StockBatchRepository.reset_dostupno() — вызывается
        в начале каждого запуска AllocationEngine перед основным циклом.

        Зачем нужен сброс?
            Алгоритм резервирует количество из строк поставок (уменьшает dostupno).
            Без сброса повторный запуск видит «исчерпанные» поставки.
        """
        stmt = update(SupplyLine).values(dostupno=SupplyLine.kolichestvo)
        self.session.execute(stmt)
        logger.info("Остатки строк поставок сброшены: dostupno = kolichestvo")


# =============================================================================
# Репозиторий согласованных перемещений (Слой 3в распределения)
# =============================================================================

class ApprovedTransferRepository:
    """
    Репозиторий для работы с согласованными межфилиальными перемещениями.

    ApprovedTransfer — это разрешение на использование материала с чужого склада:
    «Можно взять X единиц материала M со склада W (другой филиал) для филиала F».

    Загружается один раз в начале AllocationEngine.run(), хранится в памяти
    как словарь {(material_id, to_filial): [ApprovedTransfer, ...]} для O(1) доступа.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def get_grouped_by_material_filial(
        self,
    ) -> dict[tuple[int, str], list[ApprovedTransfer]]:
        """
        Загрузить все согласованные перемещения и сгруппировать по (material_id, to_filial).

        Ключ словаря — пара (ID материала, код филиала-получателя).
        Это позволяет алгоритму мгновенно найти все доступные перемещения
        для любой пары материал+работа.

        Returns:
            {(material_id, to_filial): [ApprovedTransfer, ...]}
        """
        stmt = (
            select(ApprovedTransfer)
            # Загружаем связанный warehouse чтобы потом иметь доступ к from_warehouse_id
            .order_by(ApprovedTransfer.id.asc())
        )
        all_transfers = list(self.session.scalars(stmt).all())

        result: dict[tuple[int, str], list[ApprovedTransfer]] = {}
        for t in all_transfers:
            key = (t.material_id, t.to_filial)
            result.setdefault(key, []).append(t)

        logger.debug(
            "Загружено согласованных перемещений: %d (по %d парам материал+филиал)",
            len(all_transfers), len(result),
        )
        return result

    def reset_dostupno(self) -> None:
        """
        Сбросить dostupno = kolichestvo для всех согласованных перемещений.

        Вызывается в начале каждого запуска AllocationEngine перед основным циклом.
        Без сброса повторный запуск увидит исчерпанные разрешения от предыдущего запуска.
        """
        from sqlalchemy import update as sa_update
        stmt = sa_update(ApprovedTransfer).values(dostupno=ApprovedTransfer.kolichestvo)
        self.session.execute(stmt)
        logger.info("Согласованные перемещения сброшены: dostupno = kolichestvo")

    def delete_all(self) -> None:
        """
        Удалить все записи согласованных перемещений.

        Вызывается при импорте нового файла согласований:
        предыдущий набор полностью заменяется новым.
        """
        from sqlalchemy import delete as sa_delete
        self.session.execute(sa_delete(ApprovedTransfer))
        logger.info("Все согласованные перемещения удалены (перед новым импортом)")

    def count(self) -> int:
        """Вернуть количество записей согласованных перемещений."""
        from sqlalchemy import func
        return self.session.scalar(
            select(func.count(ApprovedTransfer.id))
        ) or 0


# =============================================================================
# Репозиторий фактических списаний (Слой 1 распределения)
# =============================================================================

class WriteOffRepository(BaseRepository[WriteOff]):
    """
    Репозиторий для работы с фактическими списаниями (WriteOff).

    WriteOff — это подтверждённые документом расходы материала на работу.
    Это первый и самый надёжный слой покрытия потребности.
    """

    def __init__(self, session: Session) -> None:
        super().__init__(session, WriteOff)

    def get_all_grouped_by_work_material(
        self,
    ) -> dict[tuple[int, int], list[WriteOff]]:
        """
        Загрузить все записи списаний и сгруппировать по (work_id, material_id).

        Зачем загружать всё сразу в память?
            В алгоритме мы обрабатываем тысячи потребностей.
            Если делать SELECT для каждой потребности — N+1 запросов → медленно.
            Один SELECT всей таблицы + группировка в Python — быстрее.

        Returns:
            Словарь {(work_id, material_id): [WriteOff, ...]}
            Ключ — пара идентификаторов, значение — список записей.
        """
        # Загружаем все записи одним запросом
        stmt = select(WriteOff)
        all_writeoffs = list(self.session.scalars(stmt).all())

        # Группируем в словарь для быстрого доступа O(1) по ключу
        result: dict[tuple[int, int], list[WriteOff]] = {}
        for wo in all_writeoffs:
            key = (wo.work_id, wo.material_id)
            # setdefault создаёт пустой список если ключа ещё нет
            result.setdefault(key, []).append(wo)

        logger.debug("Загружено записей списания: %d (по %d уникальным парам работа+материал)",
                     len(all_writeoffs), len(result))
        return result


# =============================================================================
# Репозиторий «Выдано не списано» (Слой 2 распределения)
# =============================================================================

class IssuedNotWrittenOffRepository(BaseRepository[IssuedNotWrittenOff]):
    """
    Репозиторий для работы с «Выдано не списано» (IssuedNotWrittenOff).

    Слой 2 распределения: материал выдан на объект, документ не оформлен.
    Обрабатывается сразу после фактических списаний (Слой 1).
    """

    def __init__(self, session: Session) -> None:
        super().__init__(session, IssuedNotWrittenOff)

    def get_all_grouped_by_work_material(
        self,
    ) -> dict[tuple[int, int], list[IssuedNotWrittenOff]]:
        """
        Загрузить все записи «Выдано не списано» и сгруппировать по (work_id, material_id).

        Аналогично WriteOffRepository — загружаем один раз для всего алгоритма.

        Returns:
            Словарь {(work_id, material_id): [IssuedNotWrittenOff, ...]}
        """
        stmt = select(IssuedNotWrittenOff)
        all_issued = list(self.session.scalars(stmt).all())

        result: dict[tuple[int, int], list[IssuedNotWrittenOff]] = {}
        for item in all_issued:
            key = (item.work_id, item.material_id)
            result.setdefault(key, []).append(item)

        logger.debug("Загружено записей 'выдано не списано': %d (по %d парам работа+материал)",
                     len(all_issued), len(result))
        return result
