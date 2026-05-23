"""
tests/test_engine.py — Тесты движка распределения (AllocationEngine)

Покрываем все 5 слоёв алгоритма:
  Слой 1  — Фактические списания (WriteOff): самый надёжный источник
  Слой 2  — Выдано не списано (IssuedNotWrittenOff): выдали, но не оформили
  Слой 3  — Склад (StockBatch): FIFO, приоритет завод → филиал
  Слой 3б — Возможное из другого филиала: только анализ, не факт
  Слой 4  — Поставки (Supply): только своего филиала
  Слой 5  — Дефицит (DeficitRecord): к закупу

Дополнительно:
  - Аварийные работы получают материал раньше обычных
  - Повторный запуск даёт те же результаты (тест исправления reset_dostupno)

Как устроена изоляция:
  Каждый тест получает отдельную транзакцию (фикстура session из conftest.py).
  После теста транзакция откатывается — данные не сохраняются в БД.
"""

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.allocation.engine import AllocationEngine
from app.db.models import AllocationResult, DeficitRecord

from tests.conftest import (
    make_batch,
    make_issued,
    make_material,
    make_requirement,
    make_supply,
    make_supply_line,
    make_warehouse,
    make_work,
    make_writeoff,
)


# =============================================================================
# Вспомогательные функции — используются во всех тестах этого файла
# =============================================================================

def run_engine(session: Session, session_id: str | None = None) -> None:
    """
    Создать и запустить AllocationEngine с уникальным ID сессии.

    Уникальный ID нужен потому, что AllocationSession.id — это строка с уникальным
    ограничением. Если один тест вызывает run_engine дважды — передаём разные ID.

    session_id оставлен как параметр для теста повторного запуска (test_repeated_run_*).
    """
    sid = session_id or f"test_{uuid.uuid4().hex}"
    engine = AllocationEngine(session, session_id=sid)
    engine.run()


def get_results(session: Session, work_id: int) -> list[AllocationResult]:
    """Получить все строки распределения (включая is_possible) для работы."""
    stmt = select(AllocationResult).where(AllocationResult.work_id == work_id)
    return list(session.scalars(stmt).all())


def get_deficits(session: Session, work_id: int) -> list[DeficitRecord]:
    """Получить записи дефицита (к закупу) для работы."""
    stmt = select(DeficitRecord).where(DeficitRecord.work_id == work_id)
    return list(session.scalars(stmt).all())


# =============================================================================
# Слой 1: Фактические списания (WriteOff)
# =============================================================================

def test_layer1_full_coverage(session):
    """
    Слой 1: списание полностью покрывает потребность — дефицита нет.

    Если WriteOff.kolichestvo >= Requirement.potrebnost, дефицита быть не должно.
    Результат: одна строка с istochnik='spisanie', kolichestvo=100.
    """
    mat = make_material(session)
    work = make_work(session)
    make_requirement(session, work, mat, qty=Decimal("100"))
    make_writeoff(session, work, mat, qty=Decimal("100"), price=Decimal("500"))

    run_engine(session)

    results = get_results(session, work.id)
    writeoff_rows = [r for r in results if r.istochnik == "spisanie"]
    assert len(writeoff_rows) == 1
    assert writeoff_rows[0].kolichestvo == Decimal("100")
    assert len(get_deficits(session, work.id)) == 0


def test_layer1_partial_coverage_remainder_to_deficit(session):
    """
    Слой 1: списание покрывает часть — остаток идёт в дефицит (нет других источников).

    WriteOff=60 из Requirement=100 → remaining=40 → deficit=40.
    """
    mat = make_material(session)
    work = make_work(session)
    make_requirement(session, work, mat, qty=Decimal("100"))
    make_writeoff(session, work, mat, qty=Decimal("60"), price=Decimal("500"))

    run_engine(session)

    results = get_results(session, work.id)
    writeoff_row = next(r for r in results if r.istochnik == "spisanie")
    assert writeoff_row.kolichestvo == Decimal("60")

    deficits = get_deficits(session, work.id)
    assert len(deficits) == 1
    assert deficits[0].deficit_qty == Decimal("40")


def test_layer1_weighted_average_cost(session):
    """
    Слой 1: несколько записей списания агрегируются в одну со средней взвешенной ценой.

    Два списания:  100 шт × 1000 = 100 000 тг
                    50 шт × 2000 = 100 000 тг
    Итого: 150 шт, сумма = 200 000 тг → средняя цена = 200 000 / 150 = 1333.3333 тг/шт.
    """
    mat = make_material(session)
    work = make_work(session)
    make_requirement(session, work, mat, qty=Decimal("150"))
    make_writeoff(session, work, mat, qty=Decimal("100"), price=Decimal("1000"))
    make_writeoff(session, work, mat, qty=Decimal("50"), price=Decimal("2000"))

    run_engine(session)

    results = get_results(session, work.id)
    row = next(r for r in results if r.istochnik == "spisanie")

    assert row.kolichestvo == Decimal("150")
    expected_avg = (Decimal("200000") / Decimal("150")).quantize(Decimal("0.0001"))
    assert row.srednyaya_stoimost == expected_avg


# =============================================================================
# Слой 2: Выдано не списано (IssuedNotWrittenOff)
# =============================================================================

def test_layer2_covers_requirement(session):
    """Слой 2: «Выдано не списано» полностью покрывает потребность."""
    mat = make_material(session)
    work = make_work(session)
    make_requirement(session, work, mat, qty=Decimal("50"))
    make_issued(session, work, mat, qty=Decimal("50"), price=Decimal("800"))

    run_engine(session)

    results = get_results(session, work.id)
    row = next(r for r in results if r.istochnik == "vydano")
    assert row.kolichestvo == Decimal("50")
    assert len(get_deficits(session, work.id)) == 0


def test_layer2_takes_only_remaining_after_layer1(session):
    """
    Слой 2 применяется к остатку после слоя 1, а не к полной потребности.

    WriteOff=70 из 100 → remaining=30.
    Issued=50 доступно → берём min(30, 50)=30, а не все 50.
    """
    mat = make_material(session)
    work = make_work(session)
    make_requirement(session, work, mat, qty=Decimal("100"))
    make_writeoff(session, work, mat, qty=Decimal("70"))
    make_issued(session, work, mat, qty=Decimal("50"))

    run_engine(session)

    results = get_results(session, work.id)
    writeoff_row = next(r for r in results if r.istochnik == "spisanie")
    issued_row = next(r for r in results if r.istochnik == "vydano")

    assert writeoff_row.kolichestvo == Decimal("70")
    assert issued_row.kolichestvo == Decimal("30")   # не 50, а только остаток
    assert len(get_deficits(session, work.id)) == 0


# =============================================================================
# Слой 3: Склад (StockBatch)
# =============================================================================

def test_layer3_basic_warehouse_coverage(session):
    """Слой 3: склад того же завода/филиала покрывает потребность."""
    mat = make_material(session)
    work = make_work(session, zavod="KZ01", filial="Алматы")
    wh = make_warehouse(session, filial="Алматы", zavod="KZ01")
    make_batch(session, wh, mat, qty=Decimal("100"), price=Decimal("500"))
    make_requirement(session, work, mat, qty=Decimal("80"))

    run_engine(session)

    results = get_results(session, work.id)
    row = next(r for r in results if r.istochnik == "sklad")
    assert row.kolichestvo == Decimal("80")
    assert len(get_deficits(session, work.id)) == 0


def test_layer3_fifo_older_batch_used_first(session):
    """
    Слой 3: FIFO — старая партия (ранняя дата поступления) расходуется первой.

    Две партии на одном складе:
      OLD (2024-01-01): 30 шт × 100 тг
      NEW (2026-01-01): 100 шт × 200 тг
    Потребность = 30 → должна уйти только OLD-партия.
    После распределения: old_batch.dostupno=0, new_batch.dostupno=100.
    """
    mat = make_material(session)
    work = make_work(session, zavod="KZ01", filial="Алматы")
    wh = make_warehouse(session, filial="Алматы", zavod="KZ01")

    old_batch = make_batch(
        session, wh, mat, qty=Decimal("30"), price=Decimal("100"),
        nomer_partii="OLD", data_postupleniya=date(2024, 1, 1),
    )
    new_batch = make_batch(
        session, wh, mat, qty=Decimal("100"), price=Decimal("200"),
        nomer_partii="NEW", data_postupleniya=date(2026, 1, 1),
    )

    make_requirement(session, work, mat, qty=Decimal("30"))

    run_engine(session)

    # _update_availability_in_db() обновляет те же Python-объекты через identity map
    assert old_batch.dostupno == Decimal("0")
    assert new_batch.dostupno == Decimal("100")


def test_layer3_zavod_priority_over_filial(session):
    """
    Слой 3: склад того же ЗАВОДА (подслой A) используется раньше склада того же ФИЛИАЛА
    (подслой B), даже если оба склада есть.

    Склад W-ZAVOD (KZ01): 50 шт  ← должен уйти полностью
    Склад W-FILIAL (KZ99, тот же Алматы): 100 шт  ← не тронут
    Потребность = 50 → берём всё из W-ZAVOD.
    """
    mat = make_material(session)
    work = make_work(session, zavod="KZ01", filial="Алматы")

    wh_zavod = make_warehouse(session, kod="W-ZAVOD", filial="Алматы", zavod="KZ01")
    batch_zavod = make_batch(session, wh_zavod, mat, qty=Decimal("50"))

    wh_filial = make_warehouse(session, kod="W-FILIAL", filial="Алматы", zavod="KZ99")
    batch_filial = make_batch(session, wh_filial, mat, qty=Decimal("100"))

    make_requirement(session, work, mat, qty=Decimal("50"))

    run_engine(session)

    assert batch_zavod.dostupno == Decimal("0")
    assert batch_filial.dostupno == Decimal("100")


def test_layer3_uses_filial_when_zavod_exhausted(session):
    """
    Слой 3: если склад своего завода исчерпан — берём со склада своего филиала.

    W-ZAVOD (KZ01): 30 шт  → уходит полностью
    W-FILIAL (KZ99, Алматы): 100 шт → покрывает остаток 70
    Потребность = 100.
    """
    mat = make_material(session)
    work = make_work(session, zavod="KZ01", filial="Алматы")

    wh_zavod = make_warehouse(session, kod="W-ZAVOD", filial="Алматы", zavod="KZ01")
    batch_zavod = make_batch(session, wh_zavod, mat, qty=Decimal("30"))

    wh_filial = make_warehouse(session, kod="W-FILIAL", filial="Алматы", zavod="KZ99")
    batch_filial = make_batch(session, wh_filial, mat, qty=Decimal("100"))

    make_requirement(session, work, mat, qty=Decimal("100"))

    run_engine(session)

    results = get_results(session, work.id)
    sklad_rows = [r for r in results if r.istochnik == "sklad"]

    # Две строки: одна из W-ZAVOD, вторая из W-FILIAL
    assert len(sklad_rows) == 2
    assert batch_zavod.dostupno == Decimal("0")
    assert batch_filial.dostupno == Decimal("30")  # 100 - 70 = 30 осталось
    assert len(get_deficits(session, work.id)) == 0


# =============================================================================
# Слой 3б: Возможное движение из другого филиала
# =============================================================================

def test_layer3b_possible_record_created_for_other_filial(session):
    """
    Слой 3б: материал из склада другого филиала записывается как «возможное»
    (is_possible=True), но НЕ покрывает дефицит.

    Дефицит создаётся, несмотря на наличие материала в другом филиале.
    Запись с istochnik='vozmozhnoe_sklad' — только для аналитического отчёта.
    """
    mat = make_material(session)
    work = make_work(session, filial="Алматы", zavod="KZ01")

    wh_other = make_warehouse(session, kod="W-AST", filial="Астана", zavod="KZ02")
    make_batch(session, wh_other, mat, qty=Decimal("100"))

    make_requirement(session, work, mat, qty=Decimal("50"))

    run_engine(session)

    results = get_results(session, work.id)
    possible = [r for r in results if r.is_possible]
    actual = [r for r in results if not r.is_possible]

    # Одна запись «возможного»
    assert len(possible) == 1
    assert possible[0].istochnik == "vozmozhnoe_sklad"
    assert possible[0].kolichestvo == Decimal("50")

    # Никакого фактического распределения
    assert len(actual) == 0

    # Дефицит есть, потому что «возможное» не считается покрытием
    deficits = get_deficits(session, work.id)
    assert len(deficits) == 1
    assert deficits[0].deficit_qty == Decimal("50")


def test_layer3b_no_possible_when_fully_covered(session):
    """Слой 3б: если потребность уже покрыта слоем 3, «возможное» не добавляется."""
    mat = make_material(session)
    work = make_work(session, filial="Алматы", zavod="KZ01")

    # Свой склад полностью покрывает потребность
    wh_own = make_warehouse(session, filial="Алматы", zavod="KZ01")
    make_batch(session, wh_own, mat, qty=Decimal("100"))

    # Чужой склад тоже есть (но не должен попасть в «возможное» при remaining=0)
    wh_other = make_warehouse(session, kod="W-AST", filial="Астана", zavod="KZ02")
    make_batch(session, wh_other, mat, qty=Decimal("100"), nomer_partii="P-AST")

    make_requirement(session, work, mat, qty=Decimal("50"))

    run_engine(session)

    results = get_results(session, work.id)
    possible = [r for r in results if r.is_possible]
    assert len(possible) == 0


# =============================================================================
# Слой 4: Поставки (Supply)
# =============================================================================

def test_layer4_supply_covers_remainder(session):
    """Слой 4: поставка покрывает остаток, когда склад пуст."""
    mat = make_material(session)
    work = make_work(session, filial="Алматы", zavod="KZ01")

    wh = make_warehouse(session, filial="Алматы", zavod="KZ01")
    supply = make_supply(session, wh, filial="Алматы")
    make_supply_line(session, supply, mat, qty=Decimal("80"), price=Decimal("600"))

    make_requirement(session, work, mat, qty=Decimal("80"))

    run_engine(session)

    results = get_results(session, work.id)
    row = next(r for r in results if r.istochnik == "postavka")
    assert row.kolichestvo == Decimal("80")
    assert len(get_deficits(session, work.id)) == 0


def test_layer4_supply_only_same_filial(session):
    """
    Слой 4: поставка другого филиала НЕ используется.

    Два договора: Алматы (50 шт) и Астана (100 шт).
    Работа из Алматы → берём только из Алматы, Астана не тронута.
    """
    mat = make_material(session)
    work = make_work(session, filial="Алматы", zavod="KZ01")

    wh_alm = make_warehouse(session, kod="W-ALM", filial="Алматы", zavod="KZ01")
    wh_ast = make_warehouse(session, kod="W-AST", filial="Астана", zavod="KZ02")

    supply_alm = make_supply(session, wh_alm, filial="Алматы", dogovor="ДОГ-ALM")
    supply_ast = make_supply(session, wh_ast, filial="Астана", dogovor="ДОГ-AST")

    line_alm = make_supply_line(session, supply_alm, mat, qty=Decimal("50"))
    line_ast = make_supply_line(session, supply_ast, mat, qty=Decimal("100"))

    make_requirement(session, work, mat, qty=Decimal("50"))

    run_engine(session)

    results = get_results(session, work.id)
    supply_rows = [r for r in results if r.istochnik == "postavka"]

    assert len(supply_rows) == 1
    assert supply_rows[0].supply_line_id == line_alm.id

    # Поставка из Астаны не изменилась (движка не трогал эту строку)
    assert line_ast.dostupno == Decimal("100")


# =============================================================================
# Слой 5: Дефицит (к закупу)
# =============================================================================

def test_layer5_full_deficit_when_no_sources(session):
    """
    Слой 5: если нет ни одного источника — вся потребность идёт в дефицит.

    Оценочная стоимость дефицита = deficit_qty × prognosnaya_tsena.
    """
    mat = make_material(session)
    work = make_work(session)
    make_requirement(session, work, mat, qty=Decimal("100"), prog_price=Decimal("2000"))

    run_engine(session)

    deficits = get_deficits(session, work.id)
    assert len(deficits) == 1
    assert deficits[0].deficit_qty == Decimal("100")
    # 100 шт × 2000 тг/шт = 200 000 тг
    assert deficits[0].estimated_cost == Decimal("200000.00")


def test_layer5_partial_deficit(session):
    """Слой 5: дефицит фиксируется только на остаток, не покрытый слоями 1-4."""
    mat = make_material(session)
    work = make_work(session, zavod="KZ01", filial="Алматы")
    wh = make_warehouse(session, filial="Алматы", zavod="KZ01")
    make_batch(session, wh, mat, qty=Decimal("60"))  # Склад покрывает 60
    make_requirement(session, work, mat, qty=Decimal("100"), prog_price=Decimal("1000"))

    run_engine(session)

    deficits = get_deficits(session, work.id)
    assert len(deficits) == 1
    assert deficits[0].deficit_qty == Decimal("40")
    assert deficits[0].estimated_cost == Decimal("40000.00")


# =============================================================================
# Аварийные работы: обрабатываются до всех обычных
# =============================================================================

def test_emergency_work_gets_stock_before_normal(session):
    """
    Аварийная работа получает материал первой, даже если начинается позже обычной.

    На складе ровно 50 шт — хватит только одной работе.
    Обычная начинается 01.01.2026, аварийная — 01.03.2026.
    Аварийная должна получить материал; обычная — дефицит.
    """
    mat = make_material(session)
    wh = make_warehouse(session, filial="Алматы", zavod="KZ01")
    make_batch(session, wh, mat, qty=Decimal("50"))

    normal_work = make_work(
        session, kod="NORM-01", filial="Алматы", zavod="KZ01",
        is_emergency=False, data_nachala=date(2026, 1, 1),
    )
    make_requirement(session, normal_work, mat, qty=Decimal("50"))

    emergency_work = make_work(
        session, kod="EMRG-01", filial="Алматы", zavod="KZ01",
        is_emergency=True, data_nachala=date(2026, 3, 1),
    )
    make_requirement(session, emergency_work, mat, qty=Decimal("50"))

    run_engine(session)

    # Аварийная работа: получила со склада
    emerg_results = get_results(session, emergency_work.id)
    assert any(r.istochnik == "sklad" for r in emerg_results)

    # Обычная работа: попала в дефицит (склад уже пуст)
    normal_deficits = get_deficits(session, normal_work.id)
    assert len(normal_deficits) == 1
    assert normal_deficits[0].deficit_qty == Decimal("50")


# =============================================================================
# Повторный запуск: reset_dostupno гарантирует корректный результат
# =============================================================================

def test_repeated_run_gives_same_result(session):
    """
    Второй запуск engine.run() даёт те же результаты, что первый.

    Это тест исправления основного бага: без reset_dostupno() второй запуск
    видел пустой склад (dostupno=0 от предыдущего запуска) и создавал дефицит.

    После исправления: reset_dostupno() восстанавливает dostupno=kolichestvo
    перед каждым запуском.

    Проверяем: после второго запуска batch.dostupno = 100 - 80 = 20 (как и после первого).
    """
    mat = make_material(session)
    work = make_work(session, zavod="KZ01", filial="Алматы")
    wh = make_warehouse(session, filial="Алматы", zavod="KZ01")
    batch = make_batch(session, wh, mat, qty=Decimal("100"))
    make_requirement(session, work, mat, qty=Decimal("80"))

    # Первый запуск
    run_engine(session, session_id="test_run1")

    # После первого запуска: взято 80, осталось 20
    # (batch.dostupno обновлён через identity map в _update_availability_in_db)

    # Второй запуск (reset_dostupno восстановит dostupno=100 перед обработкой)
    run_engine(session, session_id="test_run2")

    # Batch.dostupno обновлён во второй раз: опять 100 → взяли 80 → осталось 20
    assert batch.dostupno == Decimal("20")

    # Дефицита в результатах второго запуска нет
    stmt = select(DeficitRecord).where(
        DeficitRecord.work_id == work.id,
        DeficitRecord.session_id == "test_run2",
    )
    deficits_run2 = list(session.scalars(stmt).all())
    assert len(deficits_run2) == 0
