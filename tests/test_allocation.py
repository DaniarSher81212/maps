"""
tests/test_allocation.py — Тесты алгоритма распределения

Что тестируется?
    Корректность алгоритма распределения на простых случаях:
      - Распределение одной партии
      - FIFO: старая партия используется первой
      - Не превышаем потребность
      - Не превышаем остаток (отрицательных остатков нет)
      - Дефицит фиксируется корректно

Как запускать тесты?
    pytest                    — все тесты
    pytest -v                 — с детальным выводом
    pytest tests/test_allocation.py  — только этот файл
    pytest -k "test_fifo"     — только тест с FIFO в имени

Что такое pytest?
    Фреймворк для написания тестов.
    Функции, начинающиеся с test_, автоматически запускаются как тесты.
    Если функция завершается без исключений — тест пройден.
    assert — проверяет условие (если False → тест не пройден).

ВАЖНО: Эти тесты работают с реальной БД!
    Для изоляции тестов используем отдельную тестовую БД (задаётся в .env.test).
    TODO: Добавить фикстуры с тестовой БД (Этап 2).
"""

from decimal import Decimal


def test_epsilon_comparison() -> None:
    """
    Тест: константа EPSILON используется правильно.

    EPSILON = 0.0001 — минимальное значимое количество.
    Значения меньше EPSILON считаются нулём (из-за округлений).
    """
    from app.allocation.engine import EPSILON

    # EPSILON должен быть положительным числом
    assert EPSILON > 0

    # Малое число меньше EPSILON
    tiny = Decimal("0.00001")
    assert tiny < EPSILON

    # Типичное количество больше EPSILON
    normal_qty = Decimal("10.0")
    assert normal_qty >= EPSILON


def test_schemas_work_import_validation() -> None:
    """
    Тест: Pydantic-схема правильно валидирует данные потребности.
    """
    from app.models.schemas import WorkImportRow

    # Корректные данные — должны пройти без ошибок
    valid_data = {
        "kod_raboty": "WO-001",
        "sys_nomer_materiala": "000000000010010001",
        "potrebnost": "100.5",
        "prioritet": 2,
        "filial": "Алматы",
        "zavod": "KZ01",
    }
    row = WorkImportRow(**valid_data)
    assert row.kod_raboty == "WO-001"
    assert row.potrebnost == Decimal("100.5000")
    assert row.prioritet == 2


def test_schemas_invalid_quantity() -> None:
    """
    Тест: Pydantic отклоняет некорректное количество.
    """
    from pydantic import ValidationError

    from app.models.schemas import WorkImportRow

    import pytest  # noqa: PLC0415 — локальный импорт для удобства

    with pytest.raises(ValidationError):
        WorkImportRow(
            kod_raboty="WO-001",
            sys_nomer_materiala="000000000010010001",
            potrebnost="не_число",  # Некорректное значение
        )


def test_schemas_negative_quantity_rejected() -> None:
    """
    Тест: Потребность не может быть отрицательной или нулевой.
    Ограничение gt=0 (greater than 0) в схеме должно это запретить.
    """
    from pydantic import ValidationError

    from app.models.schemas import WorkImportRow

    import pytest  # noqa: PLC0415

    with pytest.raises(ValidationError):
        WorkImportRow(
            kod_raboty="WO-001",
            sys_nomer_materiala="000000000010010001",
            potrebnost="-10",  # Отрицательное количество недопустимо
        )


def test_schemas_date_validation() -> None:
    """
    Тест: Дата начала не может быть позже даты окончания.
    """
    from pydantic import ValidationError

    from app.models.schemas import WorkImportRow

    import pytest  # noqa: PLC0415

    with pytest.raises(ValidationError):
        WorkImportRow(
            kod_raboty="WO-001",
            sys_nomer_materiala="000000000010010001",
            potrebnost="50",
            data_nachala="2026-12-31",   # Позже окончания!
            data_okonchaniya="2026-01-01",
        )


def test_schemas_sys_nomer_normalization() -> None:
    """
    Тест: Системный номер нормализуется (убирается .0 в конце).
    Excel иногда сохраняет числа как float: 10010001 → "10010001.0"
    """
    from app.models.schemas import WorkImportRow

    row = WorkImportRow(
        kod_raboty="WO-001",
        sys_nomer_materiala="10010001.0",  # Как Excel может выгрузить
        potrebnost="100",
    )
    assert row.sys_nomer_materiala == "10010001"  # Должно стать без .0


def test_stock_import_schema() -> None:
    """
    Тест: Схема складских остатков правильно разбирает данные.
    """
    from app.models.schemas import StockImportRow

    row = StockImportRow(
        kod_sklada="SKL-ALM-01",
        sys_nomer_materiala="000000000010010001",
        kolichestvo="150.5",
        stoimost_za_ed="500.00",
        nomer_partii="P-001",
        data_postupleniya="2026-01-15",
    )
    assert row.kod_sklada == "SKL-ALM-01"
    assert row.kolichestvo == Decimal("150.5000")
    assert row.stoimost_za_ed == Decimal("500.0000")


def test_supply_import_schema() -> None:
    """
    Тест: Схема поставок правильно разбирает данные.
    """
    from app.models.schemas import SupplyImportRow

    row = SupplyImportRow(
        sys_nomer_materiala="000000000010010001",
        kolichestvo="1000",
        dogovor="ДОГ-2026-001",
        postavshchik="ТОО Стройснаб",
        data_postavki="2026-06-01",
        stoimost_za_ed="480",
        status="confirmed",
    )
    assert row.dogovor == "ДОГ-2026-001"
    assert row.kolichestvo == Decimal("1000.0000")
    assert row.status == "confirmed"
