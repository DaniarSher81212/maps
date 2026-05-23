"""
app/allocation/engine.py — Ядро системы: алгоритм распределения материалов

Логика распределения (три фазы):
─────────────────────────────────────────────────────────────────────────────
  ФАЗА 1 — Склад (фактическое распределение)
      Приоритет 1: склады того же завода (zavod == work.zavod)
      Приоритет 2: склады того же филиала (filial == work.filial)

      Для каждого приоритета:
        • Обходим партии в порядке FIFO (старые первыми)
        • Списываем со склада, обновляем доступные остатки
        • Агрегируем по складу: одна строка результата = один склад
        • Стоимость = СРЕДНЯЯ ВЗВЕШЕННАЯ по всем партиям склада

  ФАЗА 1б — Возможное движение (другой филиал, НЕ распределяем)
      Склады чужих филиалов (filial != work.filial и zavod != work.zavod)
      • Остатки НЕ трогаем (is_possible = True)
      • Показываем: сколько теоретически можно перебросить
      • Стоимость также взвешенная средняя

  ФАЗА 2 — Поставки (фактическое резервирование)
      Если после фазы 1 ещё остался дефицит — резервируем из поставок

  ФАЗА 3 — Дефицит
      Остаток, который не покрыт ни складом, ни поставками → «К закупу»

Ключевые гарантии:
    ✓ Нет отрицательных остатков
    ✓ Нет превышения потребности
    ✓ Нет двойного распределения
    ✓ FIFO внутри каждого приоритета
    ✓ Средняя взвешенная стоимость (не цена одной партии)
    ✓ Другие филиалы показаны отдельно как «возможное»
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

# Минимальное значимое количество (всё что меньше — считаем нулём)
EPSILON = Decimal("0.0001")


# =============================================================================
# Вспомогательный класс для накопления количества и стоимости по складу
# =============================================================================

class _WarehouseAccumulator:
    """
    Накапливает количество и стоимость со всех партий одного склада.

    Зачем нужен?
        Один склад может иметь несколько партий с разными ценами.
        Нам нужна одна строка результата на склад со средней взвешенной ценой.

    Средняя взвешенная цена:
        avg = (qty1 * price1 + qty2 * price2) / (qty1 + qty2)

    Пример:
        Партия 1: 100 м × 1500 тг = 150 000 тг
        Партия 2:  50 м × 1600 тг =  80 000 тг
        Итого: 150 м, средняя цена = 230 000 / 150 = 1533.33 тг/м
    """

    def __init__(self, warehouse_id: int, is_possible: bool, tip: str) -> None:
        self.warehouse_id = warehouse_id
        self.is_possible = is_possible      # True = возможное, False = фактическое
        self.tip = tip                      # "zavod" | "filial" | "vozmozhnoe"
        self.total_qty = Decimal("0")
        self.weighted_cost_sum = Decimal("0")  # sum(qty_i * price_i)

    def add(self, qty: Decimal, cost_per_unit: Decimal) -> None:
        """Добавить партию в накопитель."""
        self.total_qty += qty
        self.weighted_cost_sum += qty * cost_per_unit

    @property
    def avg_cost(self) -> Decimal:
        """Средняя взвешенная стоимость за единицу."""
        if self.total_qty < EPSILON:
            return Decimal("0")
        return (self.weighted_cost_sum / self.total_qty).quantize(Decimal("0.0001"))

    @property
    def total_sum(self) -> Decimal:
        """Общая сумма = количество × средняя цена."""
        return (self.total_qty * self.avg_cost).quantize(Decimal("0.01"))


# =============================================================================
# Основной движок распределения
# =============================================================================

class AllocationEngine:
    """
    Движок распределения материалов.

    Использование:
        with get_session() as session:
            engine = AllocationEngine(session, session_id="2026-05-23_run1")
            report = engine.run()
    """

    def __init__(self, session: Session, session_id: Optional[str] = None) -> None:
        self.session = session
        self.session_id = session_id or f"{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:5]}"

        self.req_repo = RequirementRepository(session)
        self.batch_repo = StockBatchRepository(session)
        self.supply_repo = SupplyRepository(session)

        # Буферы для batch-insert в конце (намного быстрее чем построчно)
        self._allocation_buffer: list[AllocationResult] = []
        self._movement_buffer: list[StockMovement] = []
        self._deficit_buffer: list[DeficitRecord] = []

        # Кэш доступности партий и поставок в памяти.
        # Обновляется при каждом распределении, в БД пишется в конце одним UPDATE.
        self._batch_cache: dict[int, Decimal] = {}
        self._supply_cache: dict[int, Decimal] = {}

        self._stats = {
            "requirements_processed": 0,
            "allocated_warehouse": 0,
            "possible_warehouse": 0,
            "allocated_supply": 0,
            "deficit_records": 0,
        }

    # =========================================================================
    # Публичный запуск
    # =========================================================================

    def run(self) -> AllocationSession:
        """Запустить полный цикл распределения."""
        logger.info("=" * 60)
        logger.info("Запуск распределения | Сессия: %s", self.session_id)
        logger.info("=" * 60)

        alloc_session = self._create_session_record()

        try:
            self.req_repo.reset_allocation()
            requirements = self.req_repo.get_for_allocation()
            total = len(requirements)
            logger.info("Потребностей к распределению: %d", total)

            for idx, (req, work, material) in enumerate(requirements, 1):
                if idx % 500 == 0:
                    logger.info("Обработано %d / %d...", idx, total)
                self._process_requirement(req, work, material)

            logger.info("Сохраняем результаты...")
            self._flush_buffers_to_db()
            self._update_availability_in_db()
            alloc_session = self._complete_session(alloc_session)

            logger.info("Готово. Статистика: %s", self._stats)

        except Exception as exc:
            alloc_session.status = "failed"
            alloc_session.notes = str(exc)
            logger.error("Ошибка при распределении: %s", exc, exc_info=True)
            raise

        return alloc_session

    # =========================================================================
    # Обработка одной потребности
    # =========================================================================

    def _process_requirement(
        self, req: Requirement, work: Work, material: Material
    ) -> None:
        """Обработать одну потребность по всем фазам."""
        remaining = req.potrebnost - req.raspredeleno
        if remaining < EPSILON:
            return

        self._stats["requirements_processed"] += 1

        logger.debug(
            "Потребность: %s | %s | нужно=%.4f",
            work.kod_raboty, material.sys_nomer, remaining,
        )

        # Фаза 1: фактическое распределение со склада (свой завод + свой филиал)
        remaining = self._allocate_from_warehouse(req, work, material, remaining)

        # Фаза 1б: возможное распределение (чужой филиал) — остатки не трогаем
        # Показываем сколько могли бы взять, если бы разрешили межфилиальный перевод
        if remaining >= EPSILON:
            self._record_possible_from_other_filial(req, work, material, remaining)

        # Фаза 2: резервирование из поставок (если ещё не хватает)
        if remaining >= EPSILON:
            remaining = self._allocate_from_supplies(req, work, material, remaining)

        # Фаза 3: дефицит (всё что осталось после склада и поставок)
        if remaining >= EPSILON:
            self._record_deficit(req, work, material, remaining)

    # =========================================================================
    # Фаза 1: Фактическое распределение со склада
    # =========================================================================

    def _allocate_from_warehouse(
        self,
        req: Requirement,
        work: Work,
        material: Material,
        remaining: Decimal,
    ) -> Decimal:
        """
        Распределить материал из складских партий — только свой завод и свой филиал.

        Алгоритм:
            1. Получаем партии, сгруппированные по приоритету
            2. Группы "zavod" и "filial" — фактическое распределение
            3. Внутри группы: FIFO (старые партии первыми)
            4. Для каждого склада накапливаем qty и cost → считаем среднюю цену
            5. Создаём одну строку AllocationResult на склад (агрегированно)

        Returns:
            Остаток потребности после фазы 1
        """
        batches_by_priority = self.batch_repo.get_available_by_warehouse_priority(
            material_id=material.id,
            zavod=work.zavod,
            filial=work.filial,
        )

        for priority_key in ("zavod", "filial"):
            if remaining < EPSILON:
                break

            batches = batches_by_priority[priority_key]
            if not batches:
                continue

            # Накопители по складу: {warehouse_id: _WarehouseAccumulator}
            # Нужны чтобы объединить несколько партий одного склада в одну строку
            accumulators: dict[int, _WarehouseAccumulator] = {}

            for batch in batches:  # Уже отсортированы по FIFO в репозитории
                if remaining < EPSILON:
                    break

                available = self._get_batch_available(batch)
                if available < EPSILON:
                    continue

                qty = min(remaining, available).quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
                if qty < EPSILON:
                    continue

                cost = batch.stoimost_za_ed or Decimal("0")

                # Обновляем кэш — реальное уменьшение остатка
                self._batch_cache[batch.id] = available - qty

                # Записываем движение (расход) по конкретной партии
                self._movement_buffer.append(StockMovement(
                    session_id=self.session_id,
                    warehouse_id=batch.warehouse_id,
                    material_id=material.id,
                    batch_id=batch.id,
                    work_id=work.id,
                    izmenenie=-qty,
                    ostatok=available - qty,
                ))

                # Накапливаем для средней стоимости по складу
                wh_id = batch.warehouse_id
                if wh_id not in accumulators:
                    accumulators[wh_id] = _WarehouseAccumulator(
                        warehouse_id=wh_id,
                        is_possible=False,
                        tip=priority_key,
                    )
                accumulators[wh_id].add(qty, cost)

                req.raspredeleno += qty
                remaining -= qty
                self._stats["allocated_warehouse"] += 1

                logger.debug(
                    "  [%s] склад=%d партия=%d списано=%.4f остаток=%.4f",
                    priority_key, batch.warehouse_id, batch.id, qty, available - qty,
                )

            # Создаём агрегированные строки результата (одна на склад)
            for acc in accumulators.values():
                if acc.total_qty < EPSILON:
                    continue
                self._allocation_buffer.append(AllocationResult(
                    session_id=self.session_id,
                    requirement_id=req.id,
                    work_id=work.id,
                    material_id=material.id,
                    istochnik="sklad",
                    warehouse_id=acc.warehouse_id,
                    kolichestvo=acc.total_qty,
                    srednyaya_stoimost=acc.avg_cost,
                    summa=acc.total_sum,
                    tip_raspredeleniya=acc.tip,
                    is_possible=False,
                ))

        return remaining

    # =========================================================================
    # Фаза 1б: Возможное движение из других филиалов (НЕ фактическое)
    # =========================================================================

    def _record_possible_from_other_filial(
        self,
        req: Requirement,
        work: Work,
        material: Material,
        remaining: Decimal,
    ) -> None:
        """
        Показать возможное покрытие из складов других филиалов.

        Что делаем:
            • Смотрим на группу "prochee" (другой филиал)
            • НЕ уменьшаем остатки (is_possible=True)
            • Показываем: сколько могли бы взять при межфилиальном переводе
            • Сумма показывается для оценки стоимости возможного переброса

        remaining — сколько ещё не покрыто после своих складов.
        Показываем не больше этого количества.
        """
        batches_by_priority = self.batch_repo.get_available_by_warehouse_priority(
            material_id=material.id,
            zavod=work.zavod,
            filial=work.filial,
        )

        possible_batches = batches_by_priority["prochee"]
        if not possible_batches:
            return

        # Накопители по складу-источнику
        accumulators: dict[int, _WarehouseAccumulator] = {}
        possible_remaining = remaining  # Сколько ещё можно было бы взять

        for batch in possible_batches:  # FIFO
            if possible_remaining < EPSILON:
                break

            # Берём фактический остаток партии (не трогаем кэш — мы не распределяем)
            available = batch.dostupno
            if available < EPSILON:
                continue

            # Показываем не больше того, сколько ещё нужно работе
            qty = min(possible_remaining, available).quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
            if qty < EPSILON:
                continue

            cost = batch.stoimost_za_ed or Decimal("0")

            wh_id = batch.warehouse_id
            if wh_id not in accumulators:
                accumulators[wh_id] = _WarehouseAccumulator(
                    warehouse_id=wh_id,
                    is_possible=True,
                    tip="vozmozhnoe",
                )
            accumulators[wh_id].add(qty, cost)
            possible_remaining -= qty

            self._stats["possible_warehouse"] += 1

        # Создаём строки «возможного» распределения (агрегированно по складу)
        for acc in accumulators.values():
            if acc.total_qty < EPSILON:
                continue
            self._allocation_buffer.append(AllocationResult(
                session_id=self.session_id,
                requirement_id=req.id,
                work_id=work.id,
                material_id=material.id,
                istochnik="vozmozhnoe_sklad",
                warehouse_id=acc.warehouse_id,
                kolichestvo=acc.total_qty,
                srednyaya_stoimost=acc.avg_cost,
                summa=acc.total_sum,
                tip_raspredeleniya="vozmozhnoe",
                is_possible=True,
            ))

            logger.debug(
                "  [возможное] склад=%d qty=%.4f avg_cost=%.2f",
                acc.warehouse_id, acc.total_qty, acc.avg_cost,
            )

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
        """Зарезервировать материал из ожидаемых поставок."""
        supply_lines = self.supply_repo.get_available_lines_for_material(
            material_id=material.id,
        )

        for line in supply_lines:
            if remaining < EPSILON:
                break

            available = self._get_supply_available(line)
            if available < EPSILON:
                continue

            qty = min(remaining, available).quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
            if qty < EPSILON:
                continue

            self._supply_cache[line.id] = available - qty

            cost = line.stoimost_za_ed or Decimal("0")
            total = (qty * cost).quantize(Decimal("0.01"))

            self._allocation_buffer.append(AllocationResult(
                session_id=self.session_id,
                requirement_id=req.id,
                work_id=work.id,
                material_id=material.id,
                istochnik="postavka",
                supply_line_id=line.id,
                warehouse_id=line.supply.warehouse_id if line.supply else None,
                kolichestvo=qty,
                srednyaya_stoimost=cost,
                summa=total,
                tip_raspredeleniya=None,
                is_possible=False,
            ))

            req.raspredeleno += qty
            remaining -= qty
            self._stats["allocated_supply"] += 1

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
        """Зафиксировать дефицит — позиция «К закупу»."""
        self._deficit_buffer.append(DeficitRecord(
            session_id=self.session_id,
            requirement_id=req.id,
            work_id=work.id,
            material_id=material.id,
            deficit_qty=deficit_qty,
            estimated_cost=Decimal("0"),
            needed_by=work.data_okonchaniya,
        ))
        req.deficit = deficit_qty
        self._stats["deficit_records"] += 1

        logger.debug(
            "  [дефицит] %s | %s | qty=%.4f",
            work.kod_raboty, material.sys_nomer, deficit_qty,
        )

    # =========================================================================
    # Вспомогательные методы
    # =========================================================================

    def _get_batch_available(self, batch: StockBatch) -> Decimal:
        """Актуальное доступное количество партии (из кэша)."""
        if batch.id not in self._batch_cache:
            self._batch_cache[batch.id] = batch.dostupno
        return self._batch_cache[batch.id]

    def _get_supply_available(self, line: SupplyLine) -> Decimal:
        """Актуальное доступное количество строки поставки (из кэша)."""
        if line.id not in self._supply_cache:
            self._supply_cache[line.id] = line.dostupno
        return self._supply_cache[line.id]

    def _flush_buffers_to_db(self) -> None:
        """Записать все накопленные результаты в БД одним batch insert."""
        if self._allocation_buffer:
            self.session.bulk_save_objects(self._allocation_buffer)
            logger.info("Строк распределения (факт + возможное): %d", len(self._allocation_buffer))

        if self._movement_buffer:
            self.session.bulk_save_objects(self._movement_buffer)
            logger.info("Движений склада (фактических): %d", len(self._movement_buffer))

        if self._deficit_buffer:
            self.session.bulk_save_objects(self._deficit_buffer)
            logger.info("Записей дефицита: %d", len(self._deficit_buffer))

        self.session.flush()

    def _update_availability_in_db(self) -> None:
        """Обновить поля dostupno в stock_batches и supply_lines."""
        for batch_id, new_available in self._batch_cache.items():
            batch = self.session.get(StockBatch, batch_id)
            if batch is not None:
                batch.dostupno = new_available

        for line_id, new_available in self._supply_cache.items():
            from app.db.models import SupplyLine as SL
            line = self.session.get(SL, line_id)
            if line is not None:
                line.dostupno = new_available

        self.session.flush()
        logger.info(
            "Обновлено партий: %d, строк поставок: %d",
            len(self._batch_cache), len(self._supply_cache),
        )

    def _create_session_record(self) -> AllocationSession:
        alloc_session = AllocationSession(id=self.session_id, status="running")
        self.session.add(alloc_session)
        self.session.flush()
        return alloc_session

    def _complete_session(self, alloc_session: AllocationSession) -> AllocationSession:
        alloc_session.status = "completed"
        alloc_session.completed_at = datetime.now()
        alloc_session.total_requirements = self._stats["requirements_processed"]
        alloc_session.total_allocated = self._stats["allocated_warehouse"] + self._stats["allocated_supply"]
        alloc_session.total_deficit = self._stats["deficit_records"]
        return alloc_session
