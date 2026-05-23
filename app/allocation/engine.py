"""
app/allocation/engine.py — Ядро системы: алгоритм распределения материалов

Это главный модуль системы. Здесь реализована вся бизнес-логика распределения.

Как работает алгоритм (пошагово):
    1. Загружаем все потребности работ из БД, отсортированные по приоритету
    2. Для каждой потребности (работа + материал + нужное количество):
       а) Считаем остаток: сколько ещё нужно = потребность - уже распределено
       б) ФАЗА 1 — Склад:
          - Берём партии материала, сгруппированные по приоритету склада:
            * Приоритет 1: склады того же завода, что и работа
            * Приоритет 2: склады того же филиала
            * Приоритет 3: все остальные склады
          - Внутри каждой группы — FIFO (старые партии первыми)
          - Списываем с каждой партии сколько можем
       в) ФАЗА 2 — Поставки (если после склада ещё не хватает):
          - Берём ожидаемые поставки (материалы в пути)
          - Сортируем по дате поставки (ближайшие первые)
          - Резервируем нужное количество
       г) ФАЗА 3 — Дефицит (если всё равно не хватает):
          - Создаём запись о дефиците → "к закупу"
    3. Сохраняем все результаты в БД (allocation_results, stock_movements, deficit_records)

Ключевые гарантии алгоритма:
    ✓ Нет отрицательных остатков (никогда не берём больше, чем есть)
    ✓ Нет превышения потребности (не распределяем больше, чем нужно)
    ✓ Нет двойного распределения (отслеживаем доступность в памяти)
    ✓ FIFO (строгое соблюдение порядка партий)
    ✓ Все суммы записываются отдельно для каждой партии
"""

import uuid
from datetime import datetime
from decimal import ROUND_DOWN, Decimal
from typing import Optional

from sqlalchemy.orm import Session

from app.core.logging_config import get_logger
from app.db.models import (
    AllocationResult,
    AllocationSession,
    DeficitRecord,
    Material,
    Requirement,
    StockBatch,
    StockMovement,
    SupplyLine,
    Work,
)
from app.repositories.material_repository import StockBatchRepository, SupplyRepository
from app.repositories.work_repository import RequirementRepository

logger = get_logger(__name__)

# Минимальное значимое количество — меньше этого считаем нулём.
# Нужно из-за округлений Decimal: 0.00001 не должен считаться "остатком".
EPSILON = Decimal("0.0001")


class AllocationEngine:
    """
    Движок распределения материалов.

    Пример использования:
        with get_session() as session:
            engine = AllocationEngine(session, session_id="2026-05-23_run1")
            engine.run()
    """

    def __init__(self, session: Session, session_id: Optional[str] = None) -> None:
        """
        Инициализация движка.

        Args:
            session:    Сессия БД (открытое соединение с PostgreSQL)
            session_id: Уникальный ID этого прогона. Если None — генерируется автоматически.
                        Используется для хранения результатов нескольких прогонов.
        """
        self.session = session
        # Генерируем ID вида "2026-05-23_abc12" если не задан явно
        self.session_id = session_id or f"{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:5]}"

        # Репозитории — наш интерфейс к БД
        self.req_repo = RequirementRepository(session)
        self.batch_repo = StockBatchRepository(session)
        self.supply_repo = SupplyRepository(session)

        # Буферы результатов — накапливаем в памяти, потом сохраняем одним bulk insert.
        # Это НАМНОГО быстрее чем сохранять каждую строку отдельно.
        self._allocation_buffer: list[AllocationResult] = []
        self._movement_buffer: list[StockMovement] = []
        self._deficit_buffer: list[DeficitRecord] = []

        # Кэш доступности — хранит актуальные остатки в памяти.
        # Ключ: batch_id или supply_line_id, Значение: доступное количество.
        # БД обновляется только в конце — это избегает тысяч отдельных UPDATE запросов.
        self._batch_cache: dict[int, Decimal] = {}
        self._supply_cache: dict[int, Decimal] = {}

        # Счётчики для статистики
        self._stats = {
            "requirements_processed": 0,
            "allocated_from_warehouse": 0,
            "allocated_from_supply": 0,
            "deficit_records": 0,
        }

    # =========================================================================
    # Публичный метод запуска
    # =========================================================================

    def run(self) -> AllocationSession:
        """
        Запустить полный цикл распределения.

        Returns:
            AllocationSession с результатами и статистикой прогона
        """
        logger.info("=" * 60)
        logger.info("Запуск распределения. Сессия: %s", self.session_id)
        logger.info("=" * 60)

        # Создаём запись о сессии в БД (статус "running")
        allocation_session = self._create_session_record()

        try:
            # 1. Сбрасываем предыдущие результаты распределения (raspredeleno → 0)
            self.req_repo.reset_allocation()

            # 2. Загружаем все потребности, отсортированные по приоритету
            requirements = self.req_repo.get_for_allocation()
            total = len(requirements)
            logger.info("Загружено потребностей: %d", total)

            # 3. Обрабатываем каждую потребность
            for i, (req, work, material) in enumerate(requirements, 1):
                if i % 500 == 0:
                    # Прогресс каждые 500 строк (для больших объёмов)
                    logger.info("Обработано %d/%d потребностей...", i, total)

                self._process_requirement(req, work, material)

            # 4. Сохраняем все результаты в БД (один batch insert)
            logger.info("Сохраняем результаты в БД...")
            self._flush_buffers_to_db()

            # 5. Обновляем поля raspredeleno/deficit в таблице requirements
            self._update_requirement_totals()

            # 6. Обновляем доступность партий и строк поставок в БД
            self._update_availability_in_db()

            # 7. Завершаем сессию
            allocation_session = self._complete_session(allocation_session)
            logger.info("Распределение завершено. Статистика: %s", self._stats)

        except Exception as exc:
            # При ошибке помечаем сессию как "failed"
            allocation_session.status = "failed"
            allocation_session.notes = str(exc)
            logger.error("Критическая ошибка при распределении: %s", exc, exc_info=True)
            raise

        return allocation_session

    # =========================================================================
    # Обработка одной потребности
    # =========================================================================

    def _process_requirement(
        self,
        req: Requirement,
        work: Work,
        material: Material,
    ) -> None:
        """
        Обработать одну потребность: распределить материал из всех источников.

        Args:
            req:      Потребность (work_id, material_id, potrebnost)
            work:     Работа (с данными о заводе, филиале, приоритете)
            material: Материал (системный номер, наименование)
        """
        # Сколько ещё нужно распределить
        remaining = req.potrebnost - req.raspredeleno

        if remaining < EPSILON:
            # Потребность уже закрыта (например, была обнулена вручную)
            return

        self._stats["requirements_processed"] += 1

        logger.debug(
            "Обработка: работа=%s | материал=%s | нужно=%.4f",
            work.kod_raboty, material.sys_nomer, remaining,
        )

        # --- ФАЗА 1: Распределение со склада ---
        remaining = self._allocate_from_warehouse(req, work, material, remaining)

        # --- ФАЗА 2: Распределение из поставок ---
        if remaining >= EPSILON:
            remaining = self._allocate_from_supplies(req, work, material, remaining)

        # --- ФАЗА 3: Фиксация дефицита ---
        if remaining >= EPSILON:
            self._record_deficit(req, work, material, remaining)

    # =========================================================================
    # Фаза 1: Склад
    # =========================================================================

    def _allocate_from_warehouse(
        self,
        req: Requirement,
        work: Work,
        material: Material,
        remaining: Decimal,
    ) -> Decimal:
        """
        Распределить материал со складских партий.

        Перебираем группы складов в порядке приоритета (завод → филиал → прочие).
        Внутри группы — FIFO (старые партии первыми).

        Returns:
            Остаток потребности после распределения со склада
        """
        # Получаем все доступные партии, сгруппированные по приоритету склада
        batches_by_priority = self.batch_repo.get_available_by_warehouse_priority(
            material_id=material.id,
            zavod=work.zavod,
            filial=work.filial,
        )

        # Перебираем группы в порядке убывания приоритета
        for priority_key in ("zavod", "filial", "prochee"):
            batches = batches_by_priority[priority_key]

            for batch in batches:
                if remaining < EPSILON:
                    break  # Потребность закрыта — выходим

                # Берём актуальное доступное количество из кэша
                # (кэш учитывает уже списанное в этом прогоне)
                available = self._get_batch_available(batch)
                if available < EPSILON:
                    continue  # Партия уже исчерпана

                # Списываем: не больше того, что нужно, и не больше того, что есть
                qty_to_allocate = min(remaining, available).quantize(
                    Decimal("0.0001"), rounding=ROUND_DOWN
                )

                if qty_to_allocate < EPSILON:
                    continue

                # Обновляем кэш
                self._batch_cache[batch.id] = available - qty_to_allocate

                # Рассчитываем стоимость
                cost_per_unit = batch.stoimost_za_ed or Decimal("0")
                total_cost = (qty_to_allocate * cost_per_unit).quantize(Decimal("0.01"))

                # Записываем результат распределения
                self._allocation_buffer.append(AllocationResult(
                    session_id=self.session_id,
                    requirement_id=req.id,
                    work_id=work.id,
                    material_id=material.id,
                    istochnik="sklad",
                    warehouse_id=batch.warehouse_id,
                    batch_id=batch.id,
                    kolichestvo=qty_to_allocate,
                    stoimost_za_ed=cost_per_unit,
                    summa=total_cost,
                    tip_raspredeleniya=priority_key,  # "zavod", "filial" или "prochee"
                ))

                # Записываем движение склада (расход = отрицательное число)
                new_batch_available = available - qty_to_allocate
                self._movement_buffer.append(StockMovement(
                    session_id=self.session_id,
                    warehouse_id=batch.warehouse_id,
                    material_id=material.id,
                    batch_id=batch.id,
                    work_id=work.id,
                    izmenenie=-qty_to_allocate,   # Отрицательное = расход
                    ostatok=new_batch_available,  # Остаток после списания
                ))

                # Обновляем счётчики
                req.raspredeleno += qty_to_allocate
                remaining -= qty_to_allocate
                self._stats["allocated_from_warehouse"] += 1

                logger.debug(
                    "  Склад [%s]: партия #%s, списано=%.4f, остаток=%.4f",
                    priority_key, batch.id, qty_to_allocate, new_batch_available,
                )

            if remaining < EPSILON:
                break  # Потребность закрыта на этом уровне приоритета

        return remaining

    # =========================================================================
    # Фаза 2: Поставки
    # =========================================================================

    def _allocate_from_supplies(
        self,
        req: Requirement,
        work: Work,
        material: Material,
        remaining: Decimal,
    ) -> Decimal:
        """
        Зарезервировать материал из ожидаемых поставок.

        Returns:
            Остаток потребности после резервирования из поставок
        """
        supply_lines = self.supply_repo.get_available_lines_for_material(
            material_id=material.id,
        )

        for line in supply_lines:
            if remaining < EPSILON:
                break

            available = self._get_supply_available(line)
            if available < EPSILON:
                continue

            qty_to_allocate = min(remaining, available).quantize(
                Decimal("0.0001"), rounding=ROUND_DOWN
            )

            if qty_to_allocate < EPSILON:
                continue

            # Обновляем кэш поставок
            self._supply_cache[line.id] = available - qty_to_allocate

            cost_per_unit = line.stoimost_za_ed or Decimal("0")
            total_cost = (qty_to_allocate * cost_per_unit).quantize(Decimal("0.01"))

            self._allocation_buffer.append(AllocationResult(
                session_id=self.session_id,
                requirement_id=req.id,
                work_id=work.id,
                material_id=material.id,
                istochnik="postavka",
                warehouse_id=line.supply.warehouse_id if line.supply else None,
                supply_line_id=line.id,
                kolichestvo=qty_to_allocate,
                stoimost_za_ed=cost_per_unit,
                summa=total_cost,
                tip_raspredeleniya=None,
            ))

            req.raspredeleno += qty_to_allocate
            remaining -= qty_to_allocate
            self._stats["allocated_from_supply"] += 1

            logger.debug(
                "  Поставка: строка #%s, зарезервировано=%.4f",
                line.id, qty_to_allocate,
            )

        return remaining

    # =========================================================================
    # Фаза 3: Дефицит
    # =========================================================================

    def _record_deficit(
        self,
        req: Requirement,
        work: Work,
        material: Material,
        deficit_qty: Decimal,
    ) -> None:
        """
        Зафиксировать дефицит — потребность, которую не удалось покрыть.

        Эти записи используются для формирования плана закупок.
        """
        # Оцениваем стоимость дефицита (если нет цены — 0)
        estimated_cost = Decimal("0")  # TODO: брать из последней цены материала

        self._deficit_buffer.append(DeficitRecord(
            session_id=self.session_id,
            requirement_id=req.id,
            work_id=work.id,
            material_id=material.id,
            deficit_qty=deficit_qty,
            estimated_cost=estimated_cost,
            needed_by=work.data_okonchaniya,
        ))

        req.deficit = deficit_qty
        self._stats["deficit_records"] += 1

        logger.debug(
            "  ДЕФИЦИТ: работа=%s, материал=%s, количество=%.4f",
            work.kod_raboty, material.sys_nomer, deficit_qty,
        )

    # =========================================================================
    # Вспомогательные методы
    # =========================================================================

    def _get_batch_available(self, batch: StockBatch) -> Decimal:
        """
        Получить актуальное доступное количество партии из кэша.

        Кэш инициализируется из поля batch.dostupno при первом обращении.
        Дальше — только из кэша (без обращения к БД).
        """
        if batch.id not in self._batch_cache:
            self._batch_cache[batch.id] = batch.dostupno
        return self._batch_cache[batch.id]

    def _get_supply_available(self, line: SupplyLine) -> Decimal:
        """Получить актуальное доступное количество строки поставки из кэша."""
        if line.id not in self._supply_cache:
            self._supply_cache[line.id] = line.dostupno
        return self._supply_cache[line.id]

    def _flush_buffers_to_db(self) -> None:
        """
        Записать все накопленные результаты в БД одним batch insert.

        Почему не сохраняли по одной строке?
            Каждый session.add() + commit() — это отдельный сетевой запрос к БД.
            При 100 000 строк это 100 000 запросов — медленно!
            bulk_save_objects() отправляет всё одним запросом — в 100x быстрее.
        """
        if self._allocation_buffer:
            self.session.bulk_save_objects(self._allocation_buffer)
            logger.info("Сохранено строк распределения: %d", len(self._allocation_buffer))

        if self._movement_buffer:
            self.session.bulk_save_objects(self._movement_buffer)
            logger.info("Сохранено движений склада: %d", len(self._movement_buffer))

        if self._deficit_buffer:
            self.session.bulk_save_objects(self._deficit_buffer)
            logger.info("Сохранено записей дефицита: %d", len(self._deficit_buffer))

        self.session.flush()

    def _update_requirement_totals(self) -> None:
        """
        Обновляем поля raspredeleno и deficit в таблице requirements.

        В процессе работы мы меняли req.raspredeleno в памяти.
        Теперь нужно записать эти значения в БД.
        """
        # SQLAlchemy сам отслеживает изменения в ORM-объектах (unit of work pattern).
        # После flush() все изменённые объекты будут записаны автоматически.
        self.session.flush()

    def _update_availability_in_db(self) -> None:
        """
        Обновить поля dostupno в stock_batches и supply_lines.

        Синхронизируем кэш (который менялся в памяти) с реальными данными в БД.
        """
        # Обновляем партии
        for batch_id, new_available in self._batch_cache.items():
            batch = self.session.get(StockBatch, batch_id)
            if batch is not None:
                batch.dostupno = new_available

        # Обновляем строки поставок
        for line_id, new_available in self._supply_cache.items():
            line = self.session.get(SupplyLine, line_id)
            if line is not None:
                line.dostupno = new_available

        self.session.flush()
        logger.info(
            "Обновлено партий: %d, строк поставок: %d",
            len(self._batch_cache), len(self._supply_cache),
        )

    def _create_session_record(self) -> AllocationSession:
        """Создать запись о сессии распределения в БД."""
        allocation_session = AllocationSession(
            id=self.session_id,
            status="running",
        )
        self.session.add(allocation_session)
        self.session.flush()
        return allocation_session

    def _complete_session(self, allocation_session: AllocationSession) -> AllocationSession:
        """Обновить запись сессии после завершения распределения."""
        allocation_session.status = "completed"
        allocation_session.completed_at = datetime.now()
        allocation_session.total_requirements = self._stats["requirements_processed"]
        allocation_session.total_allocated = (
            self._stats["allocated_from_warehouse"] + self._stats["allocated_from_supply"]
        )
        allocation_session.total_deficit = self._stats["deficit_records"]
        return allocation_session
