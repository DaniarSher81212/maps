"""
tests/conftest.py — Общие фикстуры для всех тестов

Что такое фикстура (fixture)?
    Функция, помеченная @pytest.fixture, которая подготавливает данные
    или окружение для теста. Pytest автоматически вызывает её перед тестом
    и завершает после (если есть yield).

Стратегия изоляции тестов (ВАЖНО):
    Каждый тест работает в отдельной транзакции, которая ОТКАТЫВАЕТСЯ
    после завершения теста. Это значит:
      - Тест может создавать, изменять, удалять любые данные
      - После теста БД возвращается в исходное состояние
      - Тесты не зависят друг от друга

    Реализация: connection → begin() → Session → test → rollback()

Тестовая БД:
    Используем отдельную БД maps_test_db (не maps_db!).
    Создаётся один раз перед всеми тестами, удаляется после.
"""

import subprocess
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.db.models import (
    Base,
    IssuedNotWrittenOff,
    Material,
    Requirement,
    StockBatch,
    Supply,
    SupplyLine,
    Warehouse,
    Work,
    WriteOff,
)

# URL тестовой БД — отдельная от рабочей, чтобы тесты не затрагивали реальные данные
TEST_DB_URL = "postgresql+psycopg2://dan@/maps_test_db?host=/var/run/postgresql"


# =============================================================================
# Фикстуры: БД и сессия
# =============================================================================

@pytest.fixture(scope="session")
def pg_engine():
    """
    Создать тестовую БД и схему ОДИН РАЗ для всей сессии тестов.

    scope="session" означает: фикстура живёт от первого до последнего теста.
    Создавать БД для каждого теста было бы слишком медленно.

    После всех тестов: схема удаляется (DROP TABLE), соединение закрывается.
    Сама БД maps_test_db остаётся (не удаляем, чтобы не терять права).
    """
    # Создаём тестовую БД, если ещё не существует
    subprocess.run(["createdb", "maps_test_db"], capture_output=True)

    engine = create_engine(TEST_DB_URL, echo=False)

    # Создаём все таблицы по нашим ORM-моделям
    Base.metadata.create_all(engine)

    yield engine

    # После всех тестов удаляем таблицы (данные уже откачены тестами)
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def session(pg_engine):
    """
    Сессия БД с автоматическим откатом транзакции после теста.

    Как работает изоляция:
        1. Открываем соединение
        2. Начинаем транзакцию (BEGIN)
        3. Привязываем Session к этому соединению
        4. Отдаём Session в тест → тест создаёт данные, делает flush/query
        5. После теста: ROLLBACK → все изменения теста отменяются
        6. Закрываем соединение

    Почему Session привязан к Connection, а не к Engine?
        Если бы Session использовал Engine напрямую, он бы сам управлял
        транзакциями (BEGIN/COMMIT). Мы хотим управлять транзакцией СНАРУЖИ
        — чтобы откатить всё одним ROLLBACK после теста.
    """
    connection = pg_engine.connect()
    transaction = connection.begin()

    # Привязываем Session к конкретному соединению
    # Это гарантирует, что все запросы идут через одно соединение
    sess = Session(bind=connection)

    yield sess

    # Завершаем тест: откатываем транзакцию → ничего не сохранилось в БД
    sess.close()
    transaction.rollback()
    connection.close()


# =============================================================================
# Вспомогательные функции для создания тестовых данных
# =============================================================================
# Каждая функция создаёт один объект и делает flush (чтобы получить ID от БД),
# но НЕ коммитит — транзакция откатится после теста.

def make_material(
    session: Session,
    sys_nomer: str = "MAT-T01",
    naimenovanie: str = "Тестовый материал",
    ed_izm: str = "шт",
) -> Material:
    """Создать материал для теста."""
    mat = Material(sys_nomer=sys_nomer, naimenovanie=naimenovanie, ed_izm=ed_izm)
    session.add(mat)
    session.flush()
    return mat


def make_work(
    session: Session,
    kod: str = "WO-TEST-01",
    filial: str = "Алматы",
    zavod: str = "KZ01",
    prioritet: int = 2,
    is_emergency: bool = False,
    data_nachala: date | None = None,
) -> Work:
    """Создать работу для теста."""
    work = Work(
        kod_raboty=kod,
        filial=filial,
        zavod=zavod,
        prioritet=prioritet,
        is_emergency=is_emergency,
        data_nachala=data_nachala or date(2026, 6, 1),
        data_okonchaniya=date(2026, 7, 1),
        status="active",
    )
    session.add(work)
    session.flush()
    return work


def make_requirement(
    session: Session,
    work: Work,
    material: Material,
    qty: Decimal = Decimal("100"),
    prog_price: Decimal = Decimal("1000"),
) -> Requirement:
    """Создать потребность (работа + материал + количество)."""
    req = Requirement(
        work_id=work.id,
        material_id=material.id,
        potrebnost=qty,
        prognosnaya_tsena=prog_price,
        raspredeleno=Decimal("0"),
        deficit=Decimal("0"),
    )
    session.add(req)
    session.flush()
    return req


def make_warehouse(
    session: Session,
    kod: str = "SKL-TEST-01",
    filial: str = "Алматы",
    zavod: str = "KZ01",
) -> Warehouse:
    """Создать склад для теста."""
    wh = Warehouse(kod_sklada=kod, tip_sklada="центральный", filial=filial, zavod=zavod)
    session.add(wh)
    session.flush()
    return wh


def make_batch(
    session: Session,
    warehouse: Warehouse,
    material: Material,
    qty: Decimal = Decimal("100"),
    price: Decimal = Decimal("500"),
    nomer_partii: str = "P-TEST-001",
    data_postupleniya: date | None = None,
) -> StockBatch:
    """
    Создать складскую партию.

    dostupno = qty (весь материал доступен по умолчанию,
    как после свежего импорта).
    """
    batch = StockBatch(
        warehouse_id=warehouse.id,
        material_id=material.id,
        nomer_partii=nomer_partii,
        kolichestvo=qty,
        dostupno=qty,        # Изначально весь остаток доступен
        stoimost_za_ed=price,
        data_postupleniya=data_postupleniya or date(2026, 1, 1),
    )
    session.add(batch)
    session.flush()
    return batch


def make_writeoff(
    session: Session,
    work: Work,
    material: Material,
    qty: Decimal = Decimal("50"),
    price: Decimal = Decimal("500"),
) -> WriteOff:
    """Создать запись фактического списания (Слой 1)."""
    wo = WriteOff(
        work_id=work.id,
        material_id=material.id,
        kolichestvo=qty,
        stoimost_za_ed=price,
        summa=(qty * price).quantize(Decimal("0.01")),
        nomer_dokumenta="МОВФ-TEST",
        data_spisaniya=date(2026, 5, 1),
    )
    session.add(wo)
    session.flush()
    return wo


def make_issued(
    session: Session,
    work: Work,
    material: Material,
    qty: Decimal = Decimal("20"),
    price: Decimal = Decimal("500"),
) -> IssuedNotWrittenOff:
    """Создать запись «Выдано не списано» (Слой 2)."""
    issued = IssuedNotWrittenOff(
        work_id=work.id,
        material_id=material.id,
        kolichestvo=qty,
        stoimost_za_ed=price,
        summa=(qty * price).quantize(Decimal("0.01")),
        data_vydachi=date(2026, 5, 10),
    )
    session.add(issued)
    session.flush()
    return issued


def make_supply(
    session: Session,
    warehouse: Warehouse,
    filial: str = "Алматы",
    zavod: str = "KZ01",
    dogovor: str = "ДОГ-TEST-001",
    postavshchik: str = "ТОО Тест-Снаб",
    status: str = "confirmed",
) -> Supply:
    """Создать заголовок поставки."""
    supply = Supply(
        dogovor=dogovor,
        postavshchik=postavshchik,
        filial=filial,
        zavod=zavod,
        data_postavki=date(2026, 7, 1),
        status=status,
        warehouse_id=warehouse.id,
    )
    session.add(supply)
    session.flush()
    return supply


def make_supply_line(
    session: Session,
    supply: Supply,
    material: Material,
    qty: Decimal = Decimal("50"),
    price: Decimal = Decimal("600"),
) -> SupplyLine:
    """
    Создать строку поставки (конкретный материал в поставке).

    dostupno = qty (весь объём ещё не зарезервирован).
    """
    line = SupplyLine(
        supply_id=supply.id,
        material_id=material.id,
        kolichestvo=qty,
        dostupno=qty,
        stoimost_za_ed=price,
    )
    session.add(line)
    session.flush()
    return line


# Экспортируем все хелперы для удобного импорта в тестах
__all__ = [
    "make_material",
    "make_work",
    "make_requirement",
    "make_warehouse",
    "make_batch",
    "make_writeoff",
    "make_issued",
    "make_supply",
    "make_supply_line",
]
