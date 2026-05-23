"""
tests/test_repositories.py — Тесты ключевых методов репозиториев

Тестируем логику запросов, которую нельзя проверить без БД:
  - RequirementRepository.get_for_allocation(): правильный порядок очереди
  - RequirementRepository.reset_allocation(): сброс raspredeleno и deficit
  - StockBatchRepository.get_available_by_warehouse_priority(): правильная группировка

Почему эти тесты важны?
  Репозитории содержат SQL-запросы с ORDER BY, CASE, фильтрами.
  Unit-тесты без БД не могут проверить правильность этих запросов.
  Интеграционные тесты с реальной PostgreSQL — единственный надёжный способ.
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.repositories.material_repository import StockBatchRepository
from app.repositories.work_repository import RequirementRepository

from tests.conftest import (
    make_batch,
    make_material,
    make_requirement,
    make_warehouse,
    make_work,
)


# =============================================================================
# RequirementRepository.get_for_allocation() — порядок очереди распределения
# =============================================================================

class TestGetForAllocation:
    """
    Тесты метода get_for_allocation() — главного запроса алгоритма.

    Этот метод определяет ПОРЯДОК, в котором потребности получают материал.
    Ошибка в сортировке → менее важная работа получает материал раньше важной.
    """

    def test_emergency_work_before_normal(self, session):
        """
        Аварийная работа (is_emergency=True) идёт в очереди ПЕРЕД обычной,
        даже если аварийная начинается ПОЗЖЕ.

        Дата аварийной: 01.06.2026 (позже)
        Дата обычной:   01.01.2026 (раньше)
        Ожидаем: аварийная — первая.
        """
        mat = make_material(session)

        normal = make_work(
            session, kod="NORM-01",
            is_emergency=False, data_nachala=date(2026, 1, 1),
        )
        emergency = make_work(
            session, kod="EMRG-01",
            is_emergency=True, data_nachala=date(2026, 6, 1),
        )
        make_requirement(session, normal, mat)
        make_requirement(session, emergency, mat)

        rows = RequirementRepository(session).get_for_allocation()
        work_ids = [r[1].id for r in rows]

        assert work_ids.index(emergency.id) < work_ids.index(normal.id)

    def test_earlier_date_first_for_normal_works(self, session):
        """
        Для обычных работ ГЛАВНЫЙ приоритет — дата начала.
        Более ранняя дата → раньше в очереди.
        """
        mat = make_material(session)
        later = make_work(session, kod="LATE-01", data_nachala=date(2026, 6, 1))
        earlier = make_work(session, kod="EARL-01", data_nachala=date(2026, 2, 1))
        make_requirement(session, later, mat)
        make_requirement(session, earlier, mat)

        rows = RequirementRepository(session).get_for_allocation()
        work_ids = [r[1].id for r in rows]

        assert work_ids.index(earlier.id) < work_ids.index(later.id)

    def test_same_date_higher_priority_first(self, session):
        """
        При одинаковой дате начала: приоритет 1 идёт раньше приоритета 3.
        (Меньшее число = выше приоритет: 1 → 2 → 3)
        """
        mat = make_material(session)
        low_prio = make_work(session, kod="PRIO-3", prioritet=3, data_nachala=date(2026, 5, 1))
        high_prio = make_work(session, kod="PRIO-1", prioritet=1, data_nachala=date(2026, 5, 1))
        make_requirement(session, low_prio, mat)
        make_requirement(session, high_prio, mat)

        rows = RequirementRepository(session).get_for_allocation()
        work_ids = [r[1].id for r in rows]

        assert work_ids.index(high_prio.id) < work_ids.index(low_prio.id)

    def test_cancelled_works_excluded(self, session):
        """Отменённые работы (status='cancelled') не попадают в очередь распределения."""
        mat = make_material(session)
        active = make_work(session, kod="ACT-01")
        cancelled = make_work(session, kod="CNCL-01")

        # Помечаем работу как отменённую уже после создания
        cancelled.status = "cancelled"
        session.flush()

        make_requirement(session, active, mat)
        make_requirement(session, cancelled, mat)

        rows = RequirementRepository(session).get_for_allocation()
        work_ids = [r[1].id for r in rows]

        assert active.id in work_ids
        assert cancelled.id not in work_ids

    def test_returns_triples_with_correct_types(self, session):
        """Метод возвращает список кортежей (Requirement, Work, Material)."""
        from app.db.models import Material, Requirement, Work

        mat = make_material(session)
        work = make_work(session)
        make_requirement(session, work, mat, qty=Decimal("42"))

        rows = RequirementRepository(session).get_for_allocation()

        assert len(rows) == 1
        req, w, m = rows[0]
        assert isinstance(req, Requirement)
        assert isinstance(w, Work)
        assert isinstance(m, Material)
        assert req.potrebnost == Decimal("42")


# =============================================================================
# RequirementRepository.reset_allocation() — сброс результатов
# =============================================================================

class TestResetAllocation:
    """
    Тесты метода reset_allocation() — сброс результатов предыдущего запуска.

    Без этого метода повторный запуск алгоритма считал бы двойную сумму.
    """

    def test_zeros_raspredeleno_and_deficit(self, session):
        """
        reset_allocation() обнуляет raspredeleno и deficit у ВСЕХ потребностей.

        Симулируем состояние после распределения (raspredeleno=75, deficit=25),
        вызываем reset, проверяем что поля обнулились.
        """
        mat = make_material(session)
        work = make_work(session)
        req = make_requirement(session, work, mat, qty=Decimal("100"))

        # Вручную устанавливаем «послераспределённое» состояние
        req.raspredeleno = Decimal("75")
        req.deficit = Decimal("25")
        session.flush()

        RequirementRepository(session).reset_allocation()
        # После bulk UPDATE через session.execute(...) SQLAlchemy 2.0
        # обновляет объекты в identity map через стратегию 'evaluate'
        session.expire(req)  # Принудительно инвалидируем кэш объекта

        assert req.raspredeleno == Decimal("0")
        assert req.deficit == Decimal("0")

    def test_resets_multiple_requirements(self, session):
        """reset_allocation() сбрасывает ВСЕ потребности, а не только одну."""
        mat = make_material(session)
        work1 = make_work(session, kod="W-RST-01")
        work2 = make_work(session, kod="W-RST-02")

        req1 = make_requirement(session, work1, mat, qty=Decimal("100"))
        req2 = make_requirement(session, work2, mat, qty=Decimal("200"))

        req1.raspredeleno = Decimal("60")
        req2.raspredeleno = Decimal("180")
        req2.deficit = Decimal("20")
        session.flush()

        RequirementRepository(session).reset_allocation()
        session.expire(req1)
        session.expire(req2)

        assert req1.raspredeleno == Decimal("0")
        assert req2.raspredeleno == Decimal("0")
        assert req2.deficit == Decimal("0")


# =============================================================================
# StockBatchRepository.get_available_by_warehouse_priority() — группировка партий
# =============================================================================

class TestGetAvailableByWarehousePriority:
    """
    Тесты метода get_available_by_warehouse_priority() — ключевой метод слоя 3.

    Метод делит все партии по трём группам:
      'zavod'   — склад того же завода (наивысший приоритет)
      'filial'  — склад того же филиала, другого завода (средний)
      'prochee' — склад другого филиала (только для анализа)
    """

    def test_same_zavod_goes_to_zavod_group(self, session):
        """Партия со склада того же завода → группа 'zavod'."""
        mat = make_material(session)
        wh = make_warehouse(session, filial="Алматы", zavod="KZ01")
        batch = make_batch(session, wh, mat)

        result = StockBatchRepository(session).get_available_by_warehouse_priority(
            material_id=mat.id, zavod="KZ01", filial="Алматы",
        )

        assert batch in result["zavod"]
        assert batch not in result["filial"]
        assert batch not in result["prochee"]

    def test_same_filial_other_zavod_goes_to_filial_group(self, session):
        """Партия со склада того же филиала, другого завода → группа 'filial'."""
        mat = make_material(session)
        # KZ99 — другой завод, но тот же филиал Алматы
        wh = make_warehouse(session, filial="Алматы", zavod="KZ99")
        batch = make_batch(session, wh, mat)

        result = StockBatchRepository(session).get_available_by_warehouse_priority(
            material_id=mat.id, zavod="KZ01", filial="Алматы",
        )

        assert batch in result["filial"]
        assert batch not in result["zavod"]
        assert batch not in result["prochee"]

    def test_other_filial_goes_to_prochee_group(self, session):
        """Партия со склада другого филиала → группа 'prochee'."""
        mat = make_material(session)
        wh = make_warehouse(session, filial="Астана", zavod="KZ02")
        batch = make_batch(session, wh, mat)

        result = StockBatchRepository(session).get_available_by_warehouse_priority(
            material_id=mat.id, zavod="KZ01", filial="Алматы",
        )

        assert batch in result["prochee"]
        assert batch not in result["zavod"]
        assert batch not in result["filial"]

    def test_zero_dostupno_batches_excluded(self, session):
        """Партии с dostupno=0 не попадают ни в одну группу (исчерпаны)."""
        mat = make_material(session)
        wh = make_warehouse(session, filial="Алматы", zavod="KZ01")
        batch = make_batch(session, wh, mat, qty=Decimal("100"))

        # Обнуляем доступное количество (как после полного распределения)
        batch.dostupno = Decimal("0")
        session.flush()

        result = StockBatchRepository(session).get_available_by_warehouse_priority(
            material_id=mat.id, zavod="KZ01", filial="Алматы",
        )

        assert batch not in result["zavod"]
        assert batch not in result["filial"]
        assert batch not in result["prochee"]

    def test_fifo_ordering_within_group(self, session):
        """
        Внутри каждой группы партии отсортированы по FIFO:
        ранняя дата поступления → первая в списке.
        """
        mat = make_material(session)
        wh = make_warehouse(session, filial="Алматы", zavod="KZ01")

        old_batch = make_batch(
            session, wh, mat, nomer_partii="OLD",
            data_postupleniya=date(2024, 1, 1),
        )
        new_batch = make_batch(
            session, wh, mat, nomer_partii="NEW",
            data_postupleniya=date(2026, 1, 1),
        )

        result = StockBatchRepository(session).get_available_by_warehouse_priority(
            material_id=mat.id, zavod="KZ01", filial="Алматы",
        )

        zavod_batches = result["zavod"]
        assert zavod_batches[0].id == old_batch.id   # старая — первая
        assert zavod_batches[1].id == new_batch.id   # новая — вторая

    def test_all_groups_populated_when_batches_in_each(self, session):
        """
        Если есть партии во всех трёх группах — метод корректно заполняет каждую.
        """
        mat = make_material(session)

        wh_zavod = make_warehouse(session, kod="W-Z", filial="Алматы", zavod="KZ01")
        wh_filial = make_warehouse(session, kod="W-F", filial="Алматы", zavod="KZ99")
        wh_other = make_warehouse(session, kod="W-O", filial="Астана", zavod="KZ02")

        b_zavod = make_batch(session, wh_zavod, mat, nomer_partii="Z")
        b_filial = make_batch(session, wh_filial, mat, nomer_partii="F")
        b_other = make_batch(session, wh_other, mat, nomer_partii="O")

        result = StockBatchRepository(session).get_available_by_warehouse_priority(
            material_id=mat.id, zavod="KZ01", filial="Алматы",
        )

        assert b_zavod in result["zavod"]
        assert b_filial in result["filial"]
        assert b_other in result["prochee"]
