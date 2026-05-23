"""
app/services/import_service.py — Сервис импорта данных из Excel

Что делает этот модуль?
    Читает Excel-файлы с исходными данными и загружает их в PostgreSQL.
    Поддерживает три типа файлов:
      1. Потребности — что нужно для каждой работы
      2. Складские остатки — что есть на складах (партии)
      3. Поставки — что ожидается ("в пути")

Как устроен процесс импорта?
    Excel файл
       ↓ pandas.read_excel()
    DataFrame (таблица в памяти)
       ↓ Валидация через Pydantic (каждая строка)
    Список объектов (WorkImportRow, StockImportRow, SupplyImportRow)
       ↓ Сохранение в PostgreSQL через SQLAlchemy
    Таблицы БД (works, requirements, stock_batches, supplies)

Почему pandas?
    pandas умеет читать сложные Excel-файлы:
    - Любое количество листов
    - Разные форматы дат
    - Смешанные типы данных в колонках
    И возвращает удобную таблицу (DataFrame) для дальнейшей обработки.
"""

from pathlib import Path
from typing import Optional

import pandas as pd
from pydantic import ValidationError

from app.core.logging_config import get_logger
from app.db.database import get_session
from app.db.models import StockBatch, Supply, SupplyLine, Warehouse
from app.models.schemas import StockImportRow, SupplyImportRow, WorkImportRow
from app.repositories.material_repository import MaterialRepository, StockBatchRepository
from app.repositories.work_repository import RequirementRepository, WorkRepository

logger = get_logger(__name__)


# =============================================================================
# Конфигурация колонок Excel
# =============================================================================
# Здесь описываем соответствие: имя колонки в Excel → имя поля в схеме Pydantic.
# Это позволяет легко адаптировать импорт под разные форматы Excel-файлов
# без изменения бизнес-логики.

# Колонки для файла потребностей
REQUIREMENTS_COLUMN_MAP = {
    # "Имя колонки в Excel": "имя_поля_в_схеме"
    "Код работы": "kod_raboty",
    "Тип работы": "tip_raboty",
    "Филиал": "filial",
    "Подразделение": "podrazdelenie",
    "Центр затрат": "centr_zatrat",
    "Завод": "zavod",
    "Дата начала": "data_nachala",
    "Дата окончания": "data_okonchaniya",
    "Приоритет": "prioritet",
    "Статус": "status",
    "Системный номер": "sys_nomer_materiala",
    "Наименование материала": "naimenovanie_materiala",
    "Ед.изм": "ed_izm",
    "Потребность": "potrebnost",
}

# Колонки для файла складских остатков
STOCK_COLUMN_MAP = {
    "Код склада": "kod_sklada",
    "Тип склада": "tip_sklada",
    "Филиал склада": "filial_sklada",
    "Завод склада": "zavod_sklada",
    "Системный номер": "sys_nomer_materiala",
    "Наименование материала": "naimenovanie_materiala",
    "Ед.изм": "ed_izm",
    "Группа материала": "gruppa_materiala",
    "Номер партии": "nomer_partii",
    "Количество": "kolichestvo",
    "Стоимость за ед": "stoimost_za_ed",
    "Дата поступления": "data_postupleniya",
}

# Колонки для файла поставок
SUPPLY_COLUMN_MAP = {
    "Договор": "dogovor",
    "Поставщик": "postavshchik",
    "Код склада": "kod_sklada",
    "Филиал": "filial",
    "Завод": "zavod",
    "Системный номер": "sys_nomer_materiala",
    "Наименование материала": "naimenovanie_materiala",
    "Ед.изм": "ed_izm",
    "Дата поставки": "data_postavki",
    "Количество": "kolichestvo",
    "Стоимость за ед": "stoimost_za_ed",
    "Статус": "status",
}


# =============================================================================
# Вспомогательные функции
# =============================================================================

def _read_excel(file_path: Path, sheet_name: str = 0) -> pd.DataFrame:
    """
    Прочитать Excel файл и вернуть DataFrame.

    Args:
        file_path:  Путь к файлу .xlsx
        sheet_name: Имя листа или его номер (0 = первый лист)

    Returns:
        DataFrame со всеми строками файла

    Raises:
        FileNotFoundError: Если файл не найден
        ValueError:        Если файл повреждён или не является Excel
    """
    if not file_path.exists():
        raise FileNotFoundError(f"Файл не найден: {file_path}")

    logger.info("Читаем файл: %s", file_path)

    df = pd.read_excel(
        file_path,
        sheet_name=sheet_name,
        dtype=str,          # Читаем всё как строки — Pydantic сам приведёт типы
        keep_default_na=False,  # Не заменять пустые ячейки на NaN
    )

    # Убираем пустые строки (где все значения пустые)
    df = df.dropna(how="all")

    # Убираем пробелы в именах колонок (частая проблема Excel)
    df.columns = [str(c).strip() for c in df.columns]

    logger.info("Прочитано строк: %d, колонок: %d", len(df), len(df.columns))
    return df


def _rename_columns(df: pd.DataFrame, column_map: dict[str, str]) -> pd.DataFrame:
    """
    Переименовать колонки согласно маппингу.

    Переименовываем только те колонки, которые есть в файле.
    Лишние колонки (которых нет в маппинге) — оставляем как есть.
    """
    # Строим обратный маппинг только для существующих колонок
    rename_map = {
        excel_col: schema_field
        for excel_col, schema_field in column_map.items()
        if excel_col in df.columns
    }

    # Предупреждаем о пропущенных колонках
    missing = set(column_map.keys()) - set(df.columns)
    if missing:
        logger.warning("В файле отсутствуют колонки: %s", missing)

    return df.rename(columns=rename_map)


def _get_or_create_warehouse(session, kod_sklada: str, tip_sklada: Optional[str],
                              filial: Optional[str], zavod: Optional[str]) -> Warehouse:
    """Получить или создать запись о складе."""
    from sqlalchemy import select
    stmt = select(Warehouse).where(Warehouse.kod_sklada == kod_sklada)
    warehouse = session.scalar(stmt)
    if warehouse is None:
        warehouse = Warehouse(
            kod_sklada=kod_sklada,
            tip_sklada=tip_sklada,
            filial=filial,
            zavod=zavod,
        )
        session.add(warehouse)
        session.flush()  # Чтобы получить id склада
    return warehouse


# =============================================================================
# Импорт потребностей
# =============================================================================

def import_requirements(file_path: Path, sheet_name: str = 0) -> dict[str, int]:
    """
    Импортировать потребности работ в материалах из Excel.

    Файл содержит строки вида:
    | Код работы | Системный номер | Наименование | Потребность | Дата начала | ...

    Одна работа может иметь много материалов → много строк в Excel →
    → одна строка в works + много строк в requirements.

    Args:
        file_path:  Путь к Excel файлу
        sheet_name: Лист (по умолчанию первый)

    Returns:
        Статистика: {"works": 50, "requirements": 1200, "errors": 3}
    """
    df = _read_excel(file_path, sheet_name)
    df = _rename_columns(df, REQUIREMENTS_COLUMN_MAP)

    stats = {"works": 0, "requirements": 0, "errors": 0}
    errors: list[str] = []

    with get_session() as session:
        work_repo = WorkRepository(session)
        material_repo = MaterialRepository(session)
        req_repo = RequirementRepository(session)

        # Загружаем текущие данные для быстрого поиска (избегаем N+1 запросов)
        existing_works = work_repo.get_kod_map()      # {kod_raboty: id}
        existing_materials = material_repo.get_id_map()  # {sys_nomer: id}

        new_works_in_session: set[str] = set()
        new_materials_in_session: set[str] = set()

        for row_num, row in df.iterrows():
            row_dict = row.to_dict()

            # --- Валидация через Pydantic ---
            try:
                validated = WorkImportRow(**row_dict)
            except ValidationError as e:
                error_msg = f"Строка {row_num + 2}: {e.errors()[0]['msg']}"
                errors.append(error_msg)
                stats["errors"] += 1
                logger.warning("Ошибка валидации | %s", error_msg)
                continue

            # --- Обрабатываем работу (Work) ---
            if validated.kod_raboty not in existing_works and validated.kod_raboty not in new_works_in_session:
                work = work_repo.get_or_create(
                    kod_raboty=validated.kod_raboty,
                    tip_raboty=validated.tip_raboty,
                    filial=validated.filial,
                    podrazdelenie=validated.podrazdelenie,
                    centr_zatrat=validated.centr_zatrat,
                    zavod=validated.zavod,
                    data_nachala=validated.data_nachala,
                    data_okonchaniya=validated.data_okonchaniya,
                    prioritet=validated.prioritet,
                    status=validated.status,
                )
                new_works_in_session.add(validated.kod_raboty)
                stats["works"] += 1

            # --- Обрабатываем материал (Material) ---
            if validated.sys_nomer_materiala not in existing_materials and validated.sys_nomer_materiala not in new_materials_in_session:
                material_repo.get_or_create(
                    sys_nomer=validated.sys_nomer_materiala,
                    naimenovanie=validated.naimenovanie_materiala,
                    ed_izm=validated.ed_izm,
                )
                new_materials_in_session.add(validated.sys_nomer_materiala)

            # Flush чтобы получить ID новых объектов
            if stats["requirements"] % 500 == 0:
                session.flush()

            # --- Создаём потребность (Requirement) ---
            # Получаем ID работы и материала (могут быть новыми — флашим сначала)
            session.flush()

            # Ищем work и material по коду
            work = work_repo.get_by_kod(validated.kod_raboty)
            material = material_repo.get_by_sys_nomer(validated.sys_nomer_materiala)

            if work and material:
                req_repo.upsert(
                    work_id=work.id,
                    material_id=material.id,
                    potrebnost=validated.potrebnost,
                )
                stats["requirements"] += 1

        logger.info(
            "Импорт потребностей завершён: работ=%d, потребностей=%d, ошибок=%d",
            stats["works"], stats["requirements"], stats["errors"],
        )

    if errors:
        logger.warning("Ошибки при импорте:\n%s", "\n".join(errors[:10]))

    return stats


# =============================================================================
# Импорт складских остатков
# =============================================================================

def import_stock(file_path: Path, sheet_name: str = 0) -> dict[str, int]:
    """
    Импортировать складские остатки (партии материалов) из Excel.

    Перед импортом удаляем все существующие партии и загружаем заново.
    Это гарантирует, что остатки соответствуют актуальному снимку из SAP.

    Args:
        file_path:  Путь к Excel файлу
        sheet_name: Лист (по умолчанию первый)

    Returns:
        Статистика: {"warehouses": 10, "batches": 5000, "errors": 0}
    """
    df = _read_excel(file_path, sheet_name)
    df = _rename_columns(df, STOCK_COLUMN_MAP)

    stats = {"warehouses": 0, "batches": 0, "errors": 0}

    with get_session() as session:
        material_repo = MaterialRepository(session)

        # Очищаем старые данные (остатки на складе = снимок текущего момента)
        from sqlalchemy import delete
        session.execute(delete(StockBatch))
        logger.info("Очищены старые складские остатки")

        # Кэш складов и материалов чтобы не делать запрос на каждую строку
        warehouse_cache: dict[str, Warehouse] = {}

        for row_num, row in df.iterrows():
            row_dict = row.to_dict()

            try:
                validated = StockImportRow(**row_dict)
            except ValidationError as e:
                logger.warning("Строка %d: %s", row_num + 2, e.errors()[0]["msg"])
                stats["errors"] += 1
                continue

            # Склад
            if validated.kod_sklada not in warehouse_cache:
                wh = _get_or_create_warehouse(
                    session=session,
                    kod_sklada=validated.kod_sklada,
                    tip_sklada=validated.tip_sklada,
                    filial=validated.filial_sklada,
                    zavod=validated.zavod_sklada,
                )
                warehouse_cache[validated.kod_sklada] = wh
                stats["warehouses"] += 1
            else:
                wh = warehouse_cache[validated.kod_sklada]

            # Материал
            material = material_repo.get_or_create(
                sys_nomer=validated.sys_nomer_materiala,
                naimenovanie=validated.naimenovanie_materiala,
                ed_izm=validated.ed_izm,
                gruppa=validated.gruppa_materiala,
            )

            if stats["batches"] % 1000 == 0:
                session.flush()  # Периодически сбрасываем в БД

            material_after_flush = material
            session.flush()

            # Партия
            batch = StockBatch(
                warehouse_id=wh.id,
                material_id=material_after_flush.id,
                nomer_partii=validated.nomer_partii,
                kolichestvo=validated.kolichestvo,
                dostupno=validated.kolichestvo,  # Изначально всё доступно
                stoimost_za_ed=validated.stoimost_za_ed,
                data_postupleniya=validated.data_postupleniya,
            )
            session.add(batch)
            stats["batches"] += 1

        logger.info(
            "Импорт остатков завершён: складов=%d, партий=%d, ошибок=%d",
            stats["warehouses"], stats["batches"], stats["errors"],
        )

    return stats


# =============================================================================
# Импорт поставок
# =============================================================================

def import_supplies(file_path: Path, sheet_name: str = 0) -> dict[str, int]:
    """
    Импортировать поставки (материалы в пути) из Excel.

    Аналогично остаткам — очищаем и загружаем заново.

    Args:
        file_path:  Путь к Excel файлу
        sheet_name: Лист (по умолчанию первый)

    Returns:
        Статистика: {"supplies": 200, "lines": 800, "errors": 0}
    """
    df = _read_excel(file_path, sheet_name)
    df = _rename_columns(df, SUPPLY_COLUMN_MAP)

    stats = {"supplies": 0, "lines": 0, "errors": 0}

    with get_session() as session:
        material_repo = MaterialRepository(session)

        # Очищаем старые поставки
        from sqlalchemy import delete
        session.execute(delete(SupplyLine))
        session.execute(delete(Supply))
        logger.info("Очищены старые поставки")

        # Группируем по договору — одна поставка = один договор
        # Для Excel без группировки создаём отдельную "поставку" на каждый договор
        supply_cache: dict[str, Supply] = {}
        warehouse_cache: dict[str, Warehouse] = {}

        for row_num, row in df.iterrows():
            row_dict = row.to_dict()

            try:
                validated = SupplyImportRow(**row_dict)
            except ValidationError as e:
                logger.warning("Строка %d: %s", row_num + 2, e.errors()[0]["msg"])
                stats["errors"] += 1
                continue

            # Ключ для группировки поставок (договор + склад + дата)
            dogovor_key = (
                f"{validated.dogovor or 'no_contract'}"
                f"_{validated.kod_sklada or 'no_wh'}"
                f"_{validated.data_postavki}"
            )

            # Склад (если указан)
            warehouse_id = None
            if validated.kod_sklada:
                if validated.kod_sklada not in warehouse_cache:
                    wh = _get_or_create_warehouse(
                        session=session,
                        kod_sklada=validated.kod_sklada,
                        tip_sklada=None,
                        filial=validated.filial,
                        zavod=validated.zavod,
                    )
                    warehouse_cache[validated.kod_sklada] = wh
                warehouse_id = warehouse_cache[validated.kod_sklada].id

            # Поставка (создаём один раз на договор)
            if dogovor_key not in supply_cache:
                supply = Supply(
                    dogovor=validated.dogovor,
                    postavshchik=validated.postavshchik,
                    warehouse_id=warehouse_id,
                    filial=validated.filial,
                    zavod=validated.zavod,
                    data_postavki=validated.data_postavki,
                    status=validated.status,
                )
                session.add(supply)
                session.flush()
                supply_cache[dogovor_key] = supply
                stats["supplies"] += 1
            else:
                supply = supply_cache[dogovor_key]

            # Материал
            material = material_repo.get_or_create(
                sys_nomer=validated.sys_nomer_materiala,
                naimenovanie=validated.naimenovanie_materiala,
                ed_izm=validated.ed_izm,
            )
            session.flush()

            # Строка поставки
            line = SupplyLine(
                supply_id=supply.id,
                material_id=material.id,
                kolichestvo=validated.kolichestvo,
                dostupno=validated.kolichestvo,  # Изначально всё доступно
                stoimost_za_ed=validated.stoimost_za_ed,
            )
            session.add(line)
            stats["lines"] += 1

        logger.info(
            "Импорт поставок завершён: поставок=%d, строк=%d, ошибок=%d",
            stats["supplies"], stats["lines"], stats["errors"],
        )

    return stats
