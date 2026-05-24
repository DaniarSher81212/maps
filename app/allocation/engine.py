"""
app/allocation/engine.py — Ядро системы: алгоритм распределения материалов

Полная логика распределения (5 слоёв + анализ):
─────────────────────────────────────────────────────────────────────────────

  Порядок обработки работ:
      1. Аварийные работы (is_emergency=True) — по дате начала ASC
      2. Обычные работы — по дате начала ASC (ГЛАВНЫЙ), затем приоритет, филиал, код

  Для каждой потребности (работа + материал) — 5 слоёв:

  СЛОЙ 1 — Фактическое списание (WriteOff)
      Материалы, уже документально списанные на работу в SAP.
      Самый надёжный источник: документ проведён, материал снят с баланса.
      → Уменьшает remaining потребности
      → Записывается в AllocationResult с istochnik="spisanie"
      → НЕ изменяет остатки склада (уже списано ранее)

  СЛОЙ 2 — Выдано не списано (IssuedNotWrittenOff)
      Материал выдан на объект, но документ ещё не оформлен в SAP.
      → Уменьшает remaining потребности
      → Записывается с istochnik="vydano"
      → НЕ изменяет остатки склада (склад уже физически отдал материал)

  СЛОЙ 3 — Склад (фактическое распределение, только свой завод/филиал)
      Подслой A: склады того же завода (zavod == work.zavod)
      Подслой B: склады того же филиала (filial == work.filial, другой завод)
      → FIFO внутри каждого подслоя (старые партии первые)
      → Одна строка результата на склад со средней взвешенной ценой
      → Реально уменьшает dostupno у партий

  СЛОЙ 3б — Возможное движение (другие филиалы, только для анализа)
      Показывает, сколько МОГЛО БЫ покрыться с чужих складов.
      → НЕ уменьшает остатки (is_possible=True)
      → НЕ учитывается в дефиците — только для отчёта

  СЛОЙ 4 — Поставки (резервирование)
      Материалы в пути с подтверждённым договором.
      Поставки сопоставляются по ФИЛИАЛУ (не по заводу).
      → Уменьшает dostupno у строк поставки
      → Записывается с istochnik="postavka"

  СЛОЙ 5 — К закупу (дефицит)
      Остаток, не покрытый ни одним из слоёв 1-4.
      Стоимость закупа = remaining × prognosnaya_tsena.

Ключевые гарантии алгоритма:
    ✓ Нет отрицательных остатков
    ✓ Нет превышения потребности (remaining никогда < 0)
    ✓ Нет двойного распределения
    ✓ FIFO внутри складского слоя
    ✓ Средняя взвешенная стоимость для склада (не цена одной партии)
    ✓ Другие филиалы — только «возможное» (is_possible=True)
    ✓ Поставки — только своего филиала
"""

import uuid
from datetime import date, datetime  # date нужен для проверки завершённости работы
from decimal import ROUND_DOWN, Decimal
from typing import Optional

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.core.logging_config import get_logger
from app.db.models import (
    AllocationResult,
    AllocationSession,
    DeficitRecord,
    IssuedNotWrittenOff,
    Material,
    Requirement,
    StockBatch,
    StockMovement,
    SupplyLine,
    Work,
    WriteOff,
)
from app.repositories.material_repository import (
    IssuedNotWrittenOffRepository,
    StockBatchRepository,
    SupplyRepository,
    WriteOffRepository,
)
from app.repositories.work_repository import RequirementRepository

logger = get_logger(__name__)

# Минимальное значимое количество.
# Всё что меньше EPSILON считаем нулём — избегаем ошибок округления Decimal.
# Пример: 0.0000001 — это фактически ноль, не стоит его распределять.
EPSILON = Decimal("0.0001")


# =============================================================================
# Вспомогательный класс для накопления количества и стоимости по складу
# =============================================================================

class _WarehouseAccumulator:
    """
    Накапливает количество и стоимость со всех партий ОДНОГО склада.

    Зачем нужен этот класс?
        Один склад может иметь несколько партий с разными ценами.
        Пример:
            Склад W-001, Материал «Кабель ВВГ»:
              Партия 001: 100 м × 1500 тг/м = 150 000 тг  (поступила 01.01.2026)
              Партия 002:  50 м × 1600 тг/м =  80 000 тг  (поступила 15.02.2026)

        В отчёт нужна ОДНА строка на склад:
            Количество:    150 м
            Средняя цена:  230 000 / 150 = 1533.33 тг/м
            Сумма:         150 × 1533.33 = 230 000 тг

        Эта «средняя взвешенная цена» называется weighted average cost (WAC).
        Формула: avg = sum(qty_i × price_i) / sum(qty_i)

    Атрибуты:
        warehouse_id:       ID склада в БД
        is_possible:        True если это «возможное» (другой филиал), False если фактическое
        tip:                Тип приоритета: "zavod" / "filial" / "vozmozhnoe"
        total_qty:          Накопленное количество
        weighted_cost_sum:  Накопленная сумма (qty × price) для вычисления avg
    """

    def __init__(self, warehouse_id: int, is_possible: bool, tip: str) -> None:
        self.warehouse_id = warehouse_id
        self.is_possible = is_possible
        self.tip = tip
        self.total_qty = Decimal("0")
        # weighted_cost_sum = сумма произведений qty_i × price_i
        # нужна для расчёта средневзвешенной цены
        self.weighted_cost_sum = Decimal("0")

    def add(self, qty: Decimal, cost_per_unit: Decimal) -> None:
        """
        Добавить одну партию в накопитель.

        Накапливаем количество и взвешенную сумму отдельно,
        чтобы потом правильно посчитать среднюю цену.
        """
        self.total_qty += qty
        self.weighted_cost_sum += qty * cost_per_unit

    @property
    def avg_cost(self) -> Decimal:
        """
        Средняя взвешенная стоимость за единицу.

        Если количество нулевое (ничего не накопили) — возвращаем ноль.
        Иначе: avg = sum(qty_i × price_i) / sum(qty_i)
        """
        if self.total_qty < EPSILON:
            return Decimal("0")
        # quantize округляет до 4 знаков после запятой
        return (self.weighted_cost_sum / self.total_qty).quantize(Decimal("0.0001"))

    @property
    def total_sum(self) -> Decimal:
        """
        Общая сумма = количество × средняя цена.
        Округляем до 2 знаков (копейки/тиыны).
        """
        return (self.total_qty * self.avg_cost).quantize(Decimal("0.01"))


# =============================================================================
# Основной движок распределения
# =============================================================================

class AllocationEngine:
    """
    Движок распределения материалов — главный класс MAPS.

    Принцип работы:
        1. При создании (.__init__) инициализируем кэши и буферы
        2. .run() запускает полный цикл:
           a. Создаём сессию распределения в БД
           b. Загружаем данные слоёв 1 и 2 в память
           c. Обнуляем предыдущие результаты
           d. Получаем отсортированные потребности
           e. Для каждой потребности: _process_requirement()
           f. Сохраняем всё накопленное одним batch-INSERT
           g. Обновляем остатки (dostupno) в партиях и поставках
           h. Фиксируем сессию как completed

    Оптимизации производительности:
        - Слои 1 и 2 загружаются один раз в память (словари по ключам)
        - Изменения остатков накапливаются в кэше _batch_cache / _supply_cache
          и сохраняются в БД одним UPDATE в конце (не на каждую строку)
        - Результаты и движения накапливаются в буферах и сохраняются
          через bulk_save_objects() — намного быстрее построчного INSERT

    Использование:
        with get_session() as session:
            engine = AllocationEngine(session, session_id="2026-05-23_run1")
            result_session = engine.run()
    """

    def __init__(self, session: Session, session_id: Optional[str] = None) -> None:
        """
        Инициализация движка.

        Args:
            session:    Сессия SQLAlchemy (транзакция к PostgreSQL)
            session_id: ID сессии распределения. Если None — генерируется автоматически.
                        Формат по умолчанию: "20260523_143055_a1b2c"
        """
        self.session = session
        # ID сессии — уникальный идентификатор этого запуска
        self.session_id = session_id or f"{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:5]}"

        # Репозитории — объекты для работы с разными таблицами БД
        self.req_repo = RequirementRepository(session)
        self.batch_repo = StockBatchRepository(session)
        self.supply_repo = SupplyRepository(session)
        self.writeoff_repo = WriteOffRepository(session)
        self.issued_repo = IssuedNotWrittenOffRepository(session)

        # Буферы для batch-INSERT в конце (намного быстрее, чем INSERT на каждую строку).
        # Мы накапливаем объекты в памяти, затем одной командой сохраняем всё в БД.
        self._allocation_buffer: list[AllocationResult] = []
        self._movement_buffer: list[StockMovement] = []
        self._deficit_buffer: list[DeficitRecord] = []

        # Кэши актуальных остатков (обновляются в памяти, в БД пишутся в конце).
        # Зачем кэши? При обработке 5000 потребностей нельзя делать UPDATE
        # на каждое партийное списание — это 5000+ запросов.
        # Вместо этого: меняем в памяти, сохраняем одним UPDATE в конце.
        # {batch_id: доступное_количество}
        self._batch_cache: dict[int, Decimal] = {}
        # {supply_line_id: доступное_количество}
        self._supply_cache: dict[int, Decimal] = {}

        # Предзагруженные данные слоёв 1 и 2.
        # Ключ: (work_id, material_id). Значение: список записей.
        # Загружаются один раз в run() перед основным циклом.
        self._writeoff_cache: dict[tuple[int, int], list[WriteOff]] = {}
        self._issued_cache: dict[tuple[int, int], list[IssuedNotWrittenOff]] = {}

        # Статистика для вывода после завершения
        self._stats = {
            "requirements_processed": 0,  # Всего обработано потребностей
            "allocated_writeoff": 0,       # Строк из слоя 1 (списание)
            "allocated_issued": 0,         # Строк из слоя 2 (выдано не списано)
            "allocated_warehouse": 0,      # Строк из слоя 3 (склад)
            "possible_warehouse": 0,       # Строк «возможного» (другие филиалы)
            "allocated_supply": 0,         # Строк из слоя 4 (поставки)
            "deficit_records": 0,          # Строк дефицита (к закупу)
        }

    # =========================================================================
    # Публичный метод запуска
    # =========================================================================

    def run(self) -> AllocationSession:
        """
        Запустить полный цикл распределения.

        Последовательность действий:
            1. Создать запись сессии в БД (статус "running")
            2. Загрузить данные слоёв 1 и 2 в память (один раз для всего)
            3. Сбросить предыдущие результаты (raspredeleno, deficit → 0)
            4. Получить все потребности в правильном порядке
            5. Обработать каждую потребность по всем слоям
            6. Сохранить накопленные результаты в БД (batch-INSERT)
            7. Обновить остатки партий и поставок в БД
            8. Зафиксировать сессию как "completed"

        Returns:
            Объект AllocationSession с итоговой статистикой
        """
        logger.info("=" * 60)
        logger.info("Запуск распределения | Сессия: %s", self.session_id)
        logger.info("=" * 60)

        # Шаг 1: Создаём запись сессии (чтобы можно было отслеживать прогресс)
        alloc_session = self._create_session_record()

        try:
            # Шаг 2: Загружаем слои 1 и 2 в память
            # Это выполняется ОДИН РАЗ для всего алгоритма (не для каждой потребности)
            logger.info("Загрузка данных слоя 1 (фактические списания)...")
            self._writeoff_cache = self.writeoff_repo.get_all_grouped_by_work_material()

            logger.info("Загрузка данных слоя 2 (выдано не списано)...")
            self._issued_cache = self.issued_repo.get_all_grouped_by_work_material()

            # Шаг 3: Сбрасываем результаты предыдущего запуска.
            # reset_allocation обнуляет raspredeleno/deficit у потребностей.
            # reset_dostupno восстанавливает dostupno=kolichestvo у партий и поставок,
            # иначе повторный запуск увидит «пустой» склад от предыдущего запуска.
            self.req_repo.reset_allocation()
            self.batch_repo.reset_dostupno()
            self.supply_repo.reset_dostupno()

            # Шаг 4: Получаем все потребности, отсортированные по приоритету
            requirements = self.req_repo.get_for_allocation()
            total = len(requirements)
            logger.info("Потребностей к распределению: %d", total)

            # Шаг 5: Обрабатываем каждую потребность
            for idx, (req, work, material) in enumerate(requirements, 1):
                if idx % 500 == 0:
                    logger.info("Обработано %d / %d...", idx, total)
                self._process_requirement(req, work, material)

            # Шаги 6-7: Сохраняем и обновляем
            logger.info("Сохраняем результаты в БД...")
            self._flush_buffers_to_db()
            self._update_availability_in_db()

            # Шаг 8: Фиксируем сессию
            alloc_session = self._complete_session(alloc_session)
            logger.info("Готово. Статистика: %s", self._stats)

        except Exception as exc:
            # Если что-то пошло не так — помечаем сессию как failed и пробрасываем ошибку
            alloc_session.status = "failed"
            alloc_session.notes = str(exc)
            logger.error("Ошибка при распределении: %s", exc, exc_info=True)
            raise

        return alloc_session

    # =========================================================================
    # Обработка одной потребности — все 5 слоёв
    # =========================================================================

    def _process_requirement(
        self, req: Requirement, work: Work, material: Material
    ) -> None:
        """
        Обработать одну потребность (работа + материал) по всем слоям.

        remaining — это сколько ещё осталось покрыть.
        Начинается с (potrebnost - уже_распределено), уменьшается по мере покрытия.

        Слои обрабатываются последовательно: каждый следующий получает
        только то, что не покрыл предыдущий.

        Возможное движение (другие филиалы) обрабатывается параллельно со слоем 3:
        оно показывает потенциальное покрытие, но не уменьшает remaining.
        """
        # Сколько ещё не распределено
        remaining = req.potrebnost - req.raspredeleno
        if remaining < EPSILON:
            # Потребность уже полностью покрыта (например, при повторном запуске)
            return

        self._stats["requirements_processed"] += 1
        logger.debug(
            "Потребность: %s | %s | нужно=%.4f",
            work.kod_raboty, material.sys_nomer, remaining,
        )

        # ── Проверяем: завершена ли работа? ──
        # Если дата окончания работы раньше сегодняшнего дня — работа завершена.
        # Для завершённых работ закупать материалы уже не имеет смысла:
        # они либо уже списаны (Слой 1), либо выданы (Слой 2), либо нет.
        # Поэтому для завершённых работ обрабатываем только Слои 1 и 2,
        # а Слои 3 (склад), 4 (поставки) и 5 (дефицит) — пропускаем.
        is_completed = (
            work.data_okonchaniya is not None                   # дата окончания указана
            and work.data_okonchaniya < date.today()            # и она уже в прошлом
        )

        if is_completed:
            # Для завершённой работы: применяем только фактически случившееся
            # (то, что уже документально оформлено — Слои 1 и 2).
            remaining = self._apply_writeoff_layer(req, work, material, remaining)
            if remaining >= EPSILON:
                remaining = self._apply_issued_layer(req, work, material, remaining)
            # Дефицит не фиксируем — закупать уже незачем (работа завершена).
            # Склад и поставки не резервируем — они нужны для будущих работ.
            logger.debug(
                "  [завершена] %s — пропускаем слои 3-5 (работа завершена %s)",
                work.kod_raboty, work.data_okonchaniya,
            )
            return  # Выходим из метода, не обрабатывая слои 3-5

        # ── СЛОЙ 1: Фактические списания ──
        # Материалы, уже документально списанные на эту работу в SAP.
        remaining = self._apply_writeoff_layer(req, work, material, remaining)

        # ── СЛОЙ 2: Выдано не списано ──
        # Материалы, выданные на объект без оформления документа.
        if remaining >= EPSILON:
            remaining = self._apply_issued_layer(req, work, material, remaining)

        # ── СЛОЙ 3: Склад своего завода и своего филиала ──
        # Фактическое распределение со склада (остатки реально уменьшаются).
        if remaining >= EPSILON:
            remaining = self._allocate_from_warehouse(req, work, material, remaining)

        # ── СЛОЙ 3б: Возможное движение из других филиалов ──
        # Показываем возможный резерв, НО не уменьшаем remaining и не меняем остатки.
        # Выполняем ВСЕГДА (не только при remaining > 0) — для полноты картины в отчёте.
        self._record_possible_from_other_filial(req, work, material, remaining)

        # ── СЛОЙ 4: Поставки своего филиала ──
        if remaining >= EPSILON:
            remaining = self._allocate_from_supplies(req, work, material, remaining)

        # ── СЛОЙ 5: К закупу (дефицит) ──
        if remaining >= EPSILON:
            self._record_deficit(req, work, material, remaining)

    # =========================================================================
    # Слой 1: Фактические списания (WriteOff)
    # =========================================================================

    def _apply_writeoff_layer(
        self,
        req: Requirement,
        work: Work,
        material: Material,
        remaining: Decimal,
    ) -> Decimal:
        """
        Обработать слой 1: применить фактические списания к потребности.

        Как работает:
            1. Ищем в _writeoff_cache записи для пары (work_id, material_id)
            2. Суммируем количество и стоимость по всем записям
            3. Берём не больше, чем remaining (ограничение: нельзя закрыть больше потребности)
            4. Создаём ОДНУ строку AllocationResult (агрегированно)

        Почему не меняем остатки склада?
            Этот материал уже был списан ранее — в прошлых периодах.
            Алгоритм не должен трогать текущие складские остатки.

        Args:
            req:       Объект потребности (будем обновлять raspredeleno)
            work:      Объект работы (нужен work.id)
            material:  Объект материала (нужен material.id)
            remaining: Сколько ещё нужно покрыть

        Returns:
            Обновлённый remaining (уменьшен на количество из списаний)
        """
        key = (work.id, material.id)
        writeoffs = self._writeoff_cache.get(key, [])  # Получаем из памяти, не из БД

        if not writeoffs:
            return remaining  # Списаний для этой пары нет — ничего не делаем

        # Суммируем все записи списания для этой работы+материала
        total_qty = sum((wo.kolichestvo for wo in writeoffs), Decimal("0"))
        total_summa = sum((wo.summa for wo in writeoffs), Decimal("0"))  # Общая сумма всех списаний

        # Берём не больше, чем осталось (нельзя перекрыть потребность)
        qty = min(remaining, total_qty).quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
        if qty < EPSILON:
            return remaining

        # Средняя взвешенная цена по всем записям списания
        if total_qty > EPSILON:
            avg_cost = (total_summa / total_qty).quantize(Decimal("0.0001"))
        else:
            avg_cost = Decimal("0")

        # Пропорциональная сумма (если взяли меньше total_qty)
        summa = (qty * avg_cost).quantize(Decimal("0.01"))

        # Создаём ОДНУ строку результата для всех записей списания (агрегированно)
        self._allocation_buffer.append(AllocationResult(
            session_id=self.session_id,
            requirement_id=req.id,
            work_id=work.id,
            material_id=material.id,
            istochnik="spisanie",  # Источник: фактическое списание
            warehouse_id=None,     # Не привязано к конкретному складу (уже списано)
            supply_line_id=None,
            kolichestvo=qty,
            srednyaya_stoimost=avg_cost,
            summa=summa,
            tip_raspredeleniya=None,
            is_possible=False,     # Это фактическое (не возможное) покрытие
        ))

        # Обновляем накопленное распределение и остаток
        req.raspredeleno += qty
        remaining -= qty
        self._stats["allocated_writeoff"] += 1

        logger.debug(
            "  [списание] %s | %s | qty=%.4f avg_cost=%.2f",
            work.kod_raboty, material.sys_nomer, qty, avg_cost,
        )

        return remaining

    # =========================================================================
    # Слой 2: Выдано не списано (IssuedNotWrittenOff)
    # =========================================================================

    def _apply_issued_layer(
        self,
        req: Requirement,
        work: Work,
        material: Material,
        remaining: Decimal,
    ) -> Decimal:
        """
        Обработать слой 2: применить «Выдано не списано» к потребности.

        Логика аналогична слою 1 (writeoff), но источник другой:
        материал выдан физически, но документ ещё не оформлен.

        Остатки склада НЕ меняем (склад уже физически отдал этот материал).

        Args:
            req, work, material: см. _apply_writeoff_layer
            remaining: сколько ещё нужно покрыть

        Returns:
            Обновлённый remaining
        """
        key = (work.id, material.id)
        issued_list = self._issued_cache.get(key, [])

        if not issued_list:
            return remaining  # Нет записей «выдано» для этой пары

        # Суммируем все записи
        total_qty = sum((item.kolichestvo for item in issued_list), Decimal("0"))
        total_summa = sum((item.summa for item in issued_list), Decimal("0"))

        # Ограничиваем remaining
        qty = min(remaining, total_qty).quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
        if qty < EPSILON:
            return remaining

        if total_qty > EPSILON:
            avg_cost = (total_summa / total_qty).quantize(Decimal("0.0001"))
        else:
            avg_cost = Decimal("0")

        summa = (qty * avg_cost).quantize(Decimal("0.01"))

        self._allocation_buffer.append(AllocationResult(
            session_id=self.session_id,
            requirement_id=req.id,
            work_id=work.id,
            material_id=material.id,
            istochnik="vydano",    # Источник: выдано не списано
            warehouse_id=None,     # Склад информативный, сюда не пишем
            supply_line_id=None,
            kolichestvo=qty,
            srednyaya_stoimost=avg_cost,
            summa=summa,
            tip_raspredeleniya=None,
            is_possible=False,
        ))

        req.raspredeleno += qty
        remaining -= qty
        self._stats["allocated_issued"] += 1

        logger.debug(
            "  [выдано] %s | %s | qty=%.4f avg_cost=%.2f",
            work.kod_raboty, material.sys_nomer, qty, avg_cost,
        )

        return remaining

    # =========================================================================
    # Слой 3: Фактическое распределение со склада
    # =========================================================================

    def _allocate_from_warehouse(
        self,
        req: Requirement,
        work: Work,
        material: Material,
        remaining: Decimal,
    ) -> Decimal:
        """
        Обработать слой 3: распределить материал из складских партий.

        Распределяем ТОЛЬКО из складов своего завода и своего филиала.
        Склады других филиалов показываем в _record_possible_from_other_filial.

        Алгоритм внутри слоя:
            1. Получаем партии, разбитые на группы: "zavod" / "filial" / "prochee"
            2. Обрабатываем группы "zavod" и "filial" (именно в этом порядке)
            3. Внутри группы — FIFO (партии с ранней датой поступления первые)
            4. Для каждого склада накапливаем qty и cost через _WarehouseAccumulator
            5. Создаём ОДНУ строку AllocationResult на склад (со средней ценой)
            6. Для каждой ПАРТИИ создаём StockMovement (детальное движение)

        Почему одна строка на склад, а не на партию?
            В отчёте нужна агрегированная информация:
            «Со склада W-001 взяли 150 м по средней цене 1533 тг/м».
            Детализация по партиям — в отдельном листе «Движение склада».

        Args:
            req, work, material: потребность, работа, материал
            remaining: сколько ещё нужно

        Returns:
            Обновлённый remaining после склада
        """
        # Получаем все доступные партии, разбитые по приоритету склада
        batches_by_priority = self.batch_repo.get_available_by_warehouse_priority(
            material_id=material.id,
            zavod=work.zavod,
            filial=work.filial,
        )

        # Обрабатываем группы в порядке приоритета: сначала "zavod", потом "filial"
        for priority_key in ("zavod", "filial"):
            if remaining < EPSILON:
                break  # Потребность уже покрыта

            batches = batches_by_priority[priority_key]
            if not batches:
                continue  # В этой группе нет партий

            # Накопители по складу: {warehouse_id: _WarehouseAccumulator}
            # Нужны для объединения нескольких партий одного склада в одну строку результата
            accumulators: dict[int, _WarehouseAccumulator] = {}

            for batch in batches:  # Партии уже отсортированы по FIFO в репозитории
                if remaining < EPSILON:
                    break

                # Актуальный остаток из кэша (не из БД — кэш быстрее)
                available = self._get_batch_available(batch)
                if available < EPSILON:
                    continue  # Партия пустая (исчерпана в предыдущих итерациях)

                # Берём не больше того, что есть и не больше того, что нужно
                qty = min(remaining, available).quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
                if qty < EPSILON:
                    continue

                cost = batch.stoimost_za_ed or Decimal("0")

                # Обновляем кэш: уменьшаем доступное количество в этой партии.
                # В БД обновим позже одним UPDATE, не сейчас.
                self._batch_cache[batch.id] = available - qty

                # Создаём запись о движении по КОНКРЕТНОЙ партии (детальный отчёт)
                self._movement_buffer.append(StockMovement(
                    session_id=self.session_id,
                    warehouse_id=batch.warehouse_id,
                    material_id=material.id,
                    batch_id=batch.id,
                    work_id=work.id,
                    izmenenie=-qty,           # Отрицательное = расход
                    ostatok=available - qty,  # Остаток партии ПОСЛЕ списания
                ))

                # Добавляем в накопитель склада для вычисления средней цены
                wh_id = batch.warehouse_id
                if wh_id not in accumulators:
                    # Первая партия этого склада — создаём накопитель
                    accumulators[wh_id] = _WarehouseAccumulator(
                        warehouse_id=wh_id,
                        is_possible=False,
                        tip=priority_key,  # "zavod" или "filial"
                    )
                accumulators[wh_id].add(qty, cost)

                # Обновляем remaining и накопленное распределение
                req.raspredeleno += qty
                remaining -= qty
                self._stats["allocated_warehouse"] += 1

                logger.debug(
                    "  [%s] склад=%d партия=%d списано=%.4f остаток=%.4f",
                    priority_key, batch.warehouse_id, batch.id, qty, available - qty,
                )

            # Создаём агрегированные строки результата: ОДНА на каждый склад
            for acc in accumulators.values():
                if acc.total_qty < EPSILON:
                    continue
                self._allocation_buffer.append(AllocationResult(
                    session_id=self.session_id,
                    requirement_id=req.id,
                    work_id=work.id,
                    material_id=material.id,
                    istochnik="sklad",           # Источник: фактическое распределение со склада
                    warehouse_id=acc.warehouse_id,
                    supply_line_id=None,
                    kolichestvo=acc.total_qty,
                    srednyaya_stoimost=acc.avg_cost,  # Средняя взвешенная по всем партиям склада
                    summa=acc.total_sum,
                    tip_raspredeleniya=acc.tip,       # "zavod" или "filial"
                    is_possible=False,               # Фактическое распределение
                ))

        return remaining

    # =========================================================================
    # Слой 3б: Возможное движение из других филиалов (только анализ)
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

        Что делает этот метод:
            • Смотрит на группу "prochee" (другой филиал)
            • НЕ уменьшает остатки склада (is_possible=True)
            • НЕ уменьшает remaining (не фактическое распределение)
            • Создаёт строки с is_possible=True для аналитического отчёта

        Зачем нужен этот анализ?
            Руководство может принять решение о межфилиальной переброске.
            Отчёт покажет: «Если бы перебросить материал из Алматы в Астану —
            можно было бы покрыть работу W-005 (50 м кабеля)».

        remaining — это сколько ещё не покрыто ПОСЛЕ слоя 3.
        Показываем не больше этого количества (нет смысла показывать больше потребности).
        Если remaining == 0 (всё покрыто слоями 1-3) — всё равно вызываем метод
        с remaining=0, он просто ничего не покажет.

        Args:
            req, work, material: потребность, работа, материал
            remaining: сколько ещё не покрыто (может быть 0)
        """
        if remaining < EPSILON:
            return  # Нечего показывать — всё уже покрыто

        batches_by_priority = self.batch_repo.get_available_by_warehouse_priority(
            material_id=material.id,
            zavod=work.zavod,
            filial=work.filial,
        )

        # Берём только "prochee" — склады других филиалов
        possible_batches = batches_by_priority["prochee"]
        if not possible_batches:
            return

        accumulators: dict[int, _WarehouseAccumulator] = {}
        # possible_remaining — сколько ещё можем "показать" (не распределять)
        possible_remaining = remaining

        for batch in possible_batches:
            if possible_remaining < EPSILON:
                break

            # Берём ФАКТИЧЕСКИЙ остаток партии (не из _batch_cache).
            # _batch_cache содержит уменьшенные значения после реального распределения.
            # Для «возможного» нам нужен оригинальный остаток (чужой склад не тронут).
            available = batch.dostupno
            if available < EPSILON:
                continue

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
                istochnik="vozmozhnoe_sklad",  # Источник: возможный (другой филиал)
                warehouse_id=acc.warehouse_id,
                supply_line_id=None,
                kolichestvo=acc.total_qty,
                srednyaya_stoimost=acc.avg_cost,
                summa=acc.total_sum,
                tip_raspredeleniya="vozmozhnoe",
                is_possible=True,   # ← Ключевой флаг: это только анализ, не факт
            ))

            logger.debug(
                "  [возможное] склад=%d qty=%.4f avg_cost=%.2f",
                acc.warehouse_id, acc.total_qty, acc.avg_cost,
            )

    # =========================================================================
    # Слой 4: Поставки (материалы в пути)
    # =========================================================================

    def _allocate_from_supplies(
        self,
        req: Requirement,
        work: Work,
        material: Material,
        remaining: Decimal,
    ) -> Decimal:
        """
        Обработать слой 4: зарезервировать материал из ожидаемых поставок.

        Ключевое правило (из ТЗ):
            Поставки идут на ФИЛИАЛ, не на завод.
            Работа получает поставки только своего филиала:
            work.filial == supply.filial

        Несколько договоров на один материал для одной работы:
            Если подходят несколько договоров — берём в порядке даты поставки
            (более ранние поставки — первыми, они надёжнее).
            В итоговом отчёте (широкая таблица) их объединяем:
            - Количество суммируем
            - Номера договоров перечисляем через запятую
            - Поставщиков перечисляем через запятую

        Args:
            req, work, material: потребность, работа, материал
            remaining: сколько ещё не покрыто

        Returns:
            Обновлённый remaining после поставок
        """
        # Получаем доступные строки поставок для этого материала и филиала работы.
        # Ключевой параметр: filial=work.filial — фильтруем по филиалу!
        supply_lines = self.supply_repo.get_available_lines_for_material(
            material_id=material.id,
            filial=work.filial,  # Поставки только своего филиала
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

            # Уменьшаем доступное количество в поставке (через кэш, не через БД)
            self._supply_cache[line.id] = available - qty

            cost = line.stoimost_za_ed or Decimal("0")
            total = (qty * cost).quantize(Decimal("0.01"))

            # Каждая строка поставки → отдельная строка результата.
            # При объединении в итоговом отчёте (export_service) будем агрегировать.
            self._allocation_buffer.append(AllocationResult(
                session_id=self.session_id,
                requirement_id=req.id,
                work_id=work.id,
                material_id=material.id,
                istochnik="postavka",            # Источник: поставка
                supply_line_id=line.id,          # Ссылка на строку поставки (с dogovor, postavshchik)
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

            logger.debug(
                "  [поставка] supply_line=%d qty=%.4f filial=%s",
                line.id, qty, work.filial,
            )

        return remaining

    # =========================================================================
    # Слой 5: Дефицит (К закупу)
    # =========================================================================

    def _record_deficit(
        self,
        req: Requirement,
        work: Work,
        material: Material,
        deficit_qty: Decimal,
    ) -> None:
        """
        Зафиксировать дефицит — позиция «К закупу».

        Вызывается когда все слои 1-4 исчерпаны, а remaining > 0.
        Стоимость дефицита = deficit_qty × prognosnaya_tsena из заявки.

        estimated_cost (оценочная стоимость):
            Используется для понимания, сколько нужно потратить на закупку.
            Если прогнозная цена не задана (= 0) — estimated_cost тоже 0.

        Args:
            req:         Потребность (обновляем поле deficit)
            work:        Работа
            material:    Материал
            deficit_qty: Количество к закупу
        """
        # Оценочная стоимость закупки = количество × прогнозная цена из заявки
        estimated_cost = (deficit_qty * req.prognosnaya_tsena).quantize(Decimal("0.01"))

        self._deficit_buffer.append(DeficitRecord(
            session_id=self.session_id,
            requirement_id=req.id,
            work_id=work.id,
            material_id=material.id,
            deficit_qty=deficit_qty,
            estimated_cost=estimated_cost,  # Стоимость к закупу по прогнозной цене
            needed_by=work.data_okonchaniya,  # К какой дате нужен материал
        ))

        # Обновляем поле deficit в потребности (для отчётов)
        req.deficit = deficit_qty
        self._stats["deficit_records"] += 1

        logger.debug(
            "  [дефицит] %s | %s | qty=%.4f estimated_cost=%.2f",
            work.kod_raboty, material.sys_nomer, deficit_qty, estimated_cost,
        )

    # =========================================================================
    # Вспомогательные методы: работа с кэшами
    # =========================================================================

    def _get_batch_available(self, batch: StockBatch) -> Decimal:
        """
        Получить актуальное доступное количество партии.

        Сначала смотрим в кэш (там актуальные данные после предыдущих распределений).
        Если партии нет в кэше — загружаем из объекта и кладём в кэш.

        Почему не берём из БД?
            В БД данные обновляются только в конце работы (batch update).
            Кэш обновляется мгновенно при каждом распределении.
        """
        if batch.id not in self._batch_cache:
            # Первый раз обращаемся к этой партии — инициализируем кэш
            self._batch_cache[batch.id] = batch.dostupno
        return self._batch_cache[batch.id]

    def _get_supply_available(self, line: SupplyLine) -> Decimal:
        """
        Получить актуальное доступное количество строки поставки.
        Аналогично _get_batch_available — работает через кэш.
        """
        if line.id not in self._supply_cache:
            self._supply_cache[line.id] = line.dostupno
        return self._supply_cache[line.id]

    # =========================================================================
    # Сохранение результатов в БД
    # =========================================================================

    def _flush_buffers_to_db(self) -> None:
        """
        Записать все накопленные результаты в БД одним batch-INSERT.

        Почему не вставляем построчно в процессе?
            bulk_save_objects() вставляет ВСЕ строки одним запросом к PostgreSQL.
            При тысячах записей это в 10-50 раз быстрее, чем отдельный INSERT на каждую.
        """
        if self._allocation_buffer:
            self.session.bulk_save_objects(self._allocation_buffer)
            logger.info(
                "Строк распределения (факт + возможное): %d",
                len(self._allocation_buffer),
            )

        if self._movement_buffer:
            self.session.bulk_save_objects(self._movement_buffer)
            logger.info("Движений склада (по партиям): %d", len(self._movement_buffer))

        if self._deficit_buffer:
            self.session.bulk_save_objects(self._deficit_buffer)
            logger.info("Записей дефицита: %d", len(self._deficit_buffer))

        # flush отправляет все незафиксированные изменения в PostgreSQL
        # (но не коммитит — транзакция всё ещё открыта)
        self.session.flush()

    def _update_availability_in_db(self) -> None:
        """
        Обновить поля dostupno в stock_batches и supply_lines.

        После того как все распределения сохранены, обновляем остатки в БД.
        Используем кэши: только те партии и строки поставок, которые изменились.
        """
        # Обновляем только изменённые партии (из _batch_cache)
        for batch_id, new_available in self._batch_cache.items():
            batch = self.session.get(StockBatch, batch_id)
            if batch is not None:
                batch.dostupno = new_available  # Обновляем в памяти, flush зафиксирует

        # Обновляем только изменённые строки поставок (из _supply_cache)
        for line_id, new_available in self._supply_cache.items():
            line = self.session.get(SupplyLine, line_id)
            if line is not None:
                line.dostupno = new_available

        # Сохраняем все UPDATE в PostgreSQL
        self.session.flush()

        logger.info(
            "Обновлено партий: %d, строк поставок: %d",
            len(self._batch_cache), len(self._supply_cache),
        )

    def _create_session_record(self) -> AllocationSession:
        """
        Создать запись сессии распределения в БД.

        Перед созданием удаляем все существующие сессии (и связанные данные).
        Система работает в режиме «одна сессия»: старые результаты всегда
        заменяются новыми. Порядок удаления важен из-за FK-зависимостей:
        сначала дочерние таблицы, потом родительская.

        Создаётся со статусом "running".
        При завершении обновляется на "completed" (или "failed").
        """
        # Удаляем все данные предыдущего запуска (каскадно по FK-порядку)
        self.session.execute(delete(StockMovement))
        self.session.execute(delete(AllocationResult))
        self.session.execute(delete(DeficitRecord))
        self.session.execute(delete(AllocationSession))
        self.session.flush()  # Применяем удаление перед вставкой новой сессии
        logger.info("Предыдущие результаты распределения удалены.")

        alloc_session = AllocationSession(
            id=self.session_id,
            status="running",
        )
        self.session.add(alloc_session)
        self.session.flush()  # Нужен flush чтобы запись появилась в БД (для FK в результатах)
        return alloc_session

    def _complete_session(self, alloc_session: AllocationSession) -> AllocationSession:
        """
        Зафиксировать успешное завершение сессии: обновить статус и статистику.
        """
        alloc_session.status = "completed"
        alloc_session.completed_at = datetime.now()
        alloc_session.total_requirements = self._stats["requirements_processed"]
        # total_allocated = сумма всех фактических распределений (слои 1, 2, 3, 4)
        alloc_session.total_allocated = (
            self._stats["allocated_writeoff"]
            + self._stats["allocated_issued"]
            + self._stats["allocated_warehouse"]
            + self._stats["allocated_supply"]
        )
        alloc_session.total_deficit = self._stats["deficit_records"]
        return alloc_session
