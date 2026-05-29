"""
app/services/import_service.py — Сервис импорта данных из Excel

Что делает этот модуль?
    Читает Excel-файлы с исходными данными и загружает их в PostgreSQL.
    Поддерживает девять типов файлов:

    Мастер-данные (загружаются первыми, задают структуру работ):

    1. Перечень работ (import_works)
       Что это: наименования, даты начала/окончания, филиал, завод, статус, приоритет.
       Используется как справочник — файл потребностей ссылается на работы отсюда.

    2. Перечень аварийных работ (import_emergency_works)
       Тот же формат, что и «Перечень работ», но все работы помечаются is_emergency=True.
       Алгоритм распределения обрабатывает их первыми, до любых плановых работ.

    Операционные данные (загружаются после мастер-данных):

    3. Потребности (import_requirements)
       Что нужно для каждой работы: материал, количество, прогнозная цена.
       Работы в этом файле НЕ создаются — они должны быть загружены через import_works.
       При каждом вызове выполняется полный сброс операционных данных.

    4. Потребности аварийных работ (import_emergency_requirements)
       Тот же формат, что и «Потребности», но только для аварийных работ.
       Не сбрасывает плановые потребности — только заменяет аварийные.

    5. Складские остатки (import_stock)
       Текущие остатки материалов на складах (партии).

    6. Поставки (import_supplies)
       Материалы, которые уже заказаны и ожидаются («в пути»).

    7. Фактические списания (import_writeoffs)
       Материалы, уже документально списанные на работы в SAP (Слой 1).

    8. Выдано не списано (import_issued_not_written_off)
       Материалы, выданные на объект без оформления документа (Слой 2).

    9. Согласованные перемещения (import_approved_transfers)
       Межфилиальные перемещения, подтверждённые руководством (Слой 3в).

Как устроен процесс импорта?
    Excel файл
       ↓ pandas.read_excel()           (чтение)
    DataFrame (таблица в памяти)
       ↓ Pydantic-валидация (каждая строка)
    Список Python-объектов
       ↓ SQLAlchemy ORM
    Таблицы PostgreSQL

Почему pandas?
    pandas умеет читать сложные Excel-файлы с разными форматами дат,
    смешанными типами данных и любым числом листов.
"""

from pathlib import Path
from typing import Optional, cast

import pandas as pd
from pydantic import ValidationError

from app.core.logging_config import get_logger
from app.db.database import get_session
from app.db.models import (
    AllocationResult,
    AllocationSession,
    ApprovedTransfer,
    DeficitRecord,
    IssuedNotWrittenOff,
    Requirement,
    StockBatch,
    StockMovement,
    Supply,
    SupplyLine,
    Warehouse,
    Work,
    WriteOff,
)
from app.models.schemas import (
    ApprovedTransferImportRow,
    IssuedNotWrittenOffImportRow,
    RequirementsImportRow,
    StockImportRow,
    SupplyImportRow,
    WorkImportRow,
    WorkListImportRow,
    WriteOffImportRow,
)
from app.repositories.material_repository import (
    ApprovedTransferRepository,
    MaterialRepository,
    StockBatchRepository,
)
from app.repositories.work_repository import RequirementRepository, WorkRepository

logger = get_logger(__name__)


# =============================================================================
# Карты столбцов Excel → поля схем Pydantic
# =============================================================================
# Что такое column_map?
#     Словарь вида {"Имя колонки в Excel": "имя_поля_в_схеме"}.
#     Позволяет переименовать колонки при импорте, не меняя бизнес-логику.
#     Если клиент даст файл с другими заголовками — меняем только этот словарь.

# Колонки для файла потребностей.
# Новый упрощённый формат: только материальные данные, без оргструктуры работы.
# Данные работы (филиал, завод, даты, приоритет) берутся из «Перечня работ»,
# загруженного заранее через import-works или import-emergency-works.
REQUIREMENTS_COLUMN_MAP = {
    "Код работы":           "kod_raboty",
    "Группа материалов":    "gruppa_materiala",
    "Системный номер":      "sys_nomer_materiala",
    "Наименование":         "naimenovanie_materiala",
    "Наименование материала": "naimenovanie_materiala",  # альтернативный заголовок
    "Ед.изм":               "ed_izm",
    "Потребность":          "potrebnost",
    "Прогнозная цена":      "prognosnaya_tsena",
}

# Колонки для файла складских остатков
STOCK_COLUMN_MAP = {
    "Код склада":           "kod_sklada",
    "Тип склада":           "tip_sklada",
    "Филиал склада":        "filial_sklada",
    "Завод склада":         "zavod_sklada",
    "Системный номер":      "sys_nomer_materiala",
    "Наименование материала": "naimenovanie_materiala",
    "Ед.изм":               "ed_izm",
    "Группа материала":     "gruppa_materiala",
    "Номер партии":         "nomer_partii",
    "Количество":           "kolichestvo",
    "Стоимость за ед":      "stoimost_za_ed",
    "Дата поступления":     "data_postupleniya",
}

# Колонки для файла поставок
SUPPLY_COLUMN_MAP = {
    "Договор":              "dogovor",
    "Поставщик":            "postavshchik",
    "Код склада":           "kod_sklada",
    "Филиал":               "filial",
    "Завод":                "zavod",
    "Системный номер":      "sys_nomer_materiala",
    "Наименование материала": "naimenovanie_materiala",
    "Ед.изм":               "ed_izm",
    "Дата поставки":        "data_postavki",
    "Количество":           "kolichestvo",
    "Стоимость за ед":      "stoimost_za_ed",
    "Статус":               "status",
}

# Колонки для файла фактических списаний (Слой 1)
WRITEOFF_COLUMN_MAP = {
    "Код работы":           "kod_raboty",
    "Системный номер":      "sys_nomer_materiala",
    "Наименование материала": "naimenovanie_materiala",
    "Ед.изм":               "ed_izm",
    "Количество":           "kolichestvo",
    "Стоимость за ед":      "stoimost_za_ed",
    "Сумма":                "summa",
    "Номер документа":      "nomer_dokumenta",
    "Дата списания":        "data_spisaniya",
}

# Колонки для файла «Выдано не списано» (Слой 2)
ISSUED_COLUMN_MAP = {
    "Код работы":           "kod_raboty",
    "Системный номер":      "sys_nomer_materiala",
    "Наименование материала": "naimenovanie_materiala",
    "Ед.изм":               "ed_izm",
    "Количество":           "kolichestvo",
    "Стоимость за ед":      "stoimost_za_ed",
    "Сумма":                "summa",
    "Код склада":           "kod_sklada",
    "Дата выдачи":          "data_vydachi",
}

# Колонки для файла согласованных перемещений.
# Совпадает со структурой листа «Возможное перемещение» из отчёта:
# пользователь копирует строки из этого листа, редактирует количество и загружает.
APPROVED_TRANSFERS_COLUMN_MAP = {
    "Системный номер":      "sys_nomer_materiala",   # Материал
    "Склад-источник":       "kod_sklada_istochnik",  # Откуда берём (другой филиал)
    "Филиал-получатель":    "to_filial",             # Кому отдаём
    "Кол-во согласовано":   "kolichestvo",           # Сколько согласовано к переброске
    # Альтернативные заголовки (на случай если пользователь взял лист напрямую из отчёта)
    "Возможное кол-во":     "kolichestvo",           # из листа «Возможное перемещение»
    "Куда (филиал)":        "to_filial",
}

# Колонки для файла «Перечень работ» и «Перечень аварийных работ».
# Этот файл — мастер-данные: наименования, даты, оргструктура работ.
# В нём НЕТ материалов или количеств.
# Загружается командами: maps import-works / maps import-emergency
WORKS_COLUMN_MAP = {
    "Код работы":           "kod_raboty",         # Обязательная колонка — ключ записи
    "Наименование работы":  "nazvanie",            # Человекочитаемое описание работы
    "Тип работы":           "tip_raboty",          # Категория: ТО, Ремонт, Монтаж...
    "Филиал":               "filial",              # Филиал компании
    "Подразделение":        "podrazdelenie",       # Подразделение внутри филиала
    "Центр затрат":         "centr_zatrat",        # Центр затрат (CC) из SAP
    "Завод":                "zavod",               # Код завода (Plant) в SAP
    "Дата начала":          "data_nachala",        # Влияет на порядок распределения!
    "Дата окончания":       "data_okonchaniya",    # Нужна к дате (needed_by)
    "Приоритет":            "prioritet",           # 1 — высший, 2 — средний, 3 — низший
    "Статус":               "status",              # active, completed, cancelled, on_hold
}


# =============================================================================
# Вспомогательные функции
# =============================================================================

def _read_excel(file_path: Path, sheet_name: int | str = 0) -> pd.DataFrame:
    """
    Прочитать Excel файл и вернуть DataFrame.

    Args:
        file_path:  Путь к файлу .xlsx
        sheet_name: Имя листа или его порядковый номер (0 = первый лист)

    Returns:
        DataFrame со строками файла

    Raises:
        FileNotFoundError: Если файл не найден
        ValueError:        Если файл повреждён или не Excel

    Почему dtype=str?
        Мы читаем всё как строки и доверяем Pydantic-валидации преобразование.
        Это избегает ситуации, когда pandas самостоятельно превращает
        код "10012345" в число 10012345.0.
    """
    if not file_path.exists():
        raise FileNotFoundError(f"Файл не найден: {file_path}")

    logger.info("Читаем файл: %s", file_path)

    df = pd.read_excel(
        file_path,
        sheet_name=sheet_name,
        dtype=str,               # Все колонки как строки — Pydantic сам приведёт типы
        keep_default_na=False,   # Пустые ячейки оставляем пустыми строками, не NaN
    )

    # Удаляем полностью пустые строки (где ВСЕ ячейки пустые)
    df = df.dropna(how="all")

    # Убираем лишние пробелы в именах колонок (типичная проблема Excel)
    df.columns = [str(c).strip() for c in df.columns]

    logger.info("Прочитано строк: %d, колонок: %d", len(df), len(df.columns))
    return df


def _rename_columns(df: pd.DataFrame, column_map: dict[str, str]) -> pd.DataFrame:
    """
    Переименовать колонки Excel согласно маппингу.

    Колонки, которых нет в маппинге — оставляем без изменений.
    Если в файле нет ожидаемой колонки — выводим предупреждение.
    """
    # Строим только тот маппинг, для которого колонки реально есть в файле
    rename_map = {
        excel_col: schema_field
        for excel_col, schema_field in column_map.items()
        if excel_col in df.columns
    }

    # Сообщаем об отсутствующих колонках (может быть нормально, поля необязательные)
    missing = set(column_map.keys()) - set(df.columns)
    if missing:
        logger.warning("В файле отсутствуют колонки: %s", sorted(missing))

    return df.rename(columns=rename_map)


def _get_or_create_warehouse(
    session,
    kod_sklada: str,
    tip_sklada: Optional[str],
    filial: Optional[str],
    zavod: Optional[str],
) -> Warehouse:
    """
    Получить или создать склад по коду.

    Склады создаются автоматически при импорте остатков/поставок.
    Код склада (kod_sklada) — уникальный идентификатор.
    """
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
        session.flush()  # flush нужен чтобы PostgreSQL назначил id прямо сейчас
    return warehouse


# =============================================================================
# Импорт потребностей (обычные работы)
# =============================================================================

def import_requirements(file_path: Path, sheet_name: int | str = 0) -> dict[str, int]:
    """
    Импортировать потребности работ в материалах из Excel.

    Новый упрощённый формат файла:
        | Код работы | Группа материалов | Системный номер |
        | Наименование | Ед.изм | Потребность | Прогнозная цена |

    Работы в этом файле НЕ создаются. Данные работ (филиал, завод, приоритет, даты)
    должны быть загружены заранее через import-works.
    Если работа с указанным кодом не найдена — строка пропускается с предупреждением.

    При каждом вызове выполняется полный сброс операционных данных (новый цикл планирования).

    Args:
        file_path:  Путь к Excel файлу
        sheet_name: Лист (0 = первый)

    Returns:
        Статистика {"requirements": 1200, "skipped": 5, "errors": 3}
    """
    df = _read_excel(file_path, sheet_name)
    df = _rename_columns(df, REQUIREMENTS_COLUMN_MAP)

    stats = {"requirements": 0, "skipped": 0, "errors": 0}
    errors: list[str] = []

    with get_session() as session:
        work_repo = WorkRepository(session)
        material_repo = MaterialRepository(session)
        req_repo = RequirementRepository(session)

        # Полный сброс операционных данных — начинаем новый цикл планирования.
        # Таблицу works НЕ очищаем — это мастер-данные, загруженные через import-works.
        from sqlalchemy import delete

        session.execute(delete(StockMovement))
        session.execute(delete(AllocationResult))
        session.execute(delete(DeficitRecord))
        session.execute(delete(AllocationSession))
        session.execute(delete(WriteOff))
        session.execute(delete(IssuedNotWrittenOff))
        session.execute(delete(SupplyLine))
        session.execute(delete(Supply))
        session.execute(delete(StockBatch))
        session.execute(delete(Requirement))
        logger.info("Сброс операционных данных перед новым импортом (работы сохранены)")

        # Предзагружаем словари для O(1) поиска
        work_kod_map = work_repo.get_kod_map()           # {kod_raboty: id}
        existing_materials = material_repo.get_id_map()  # {sys_nomer: id}
        new_materials_in_session: set[str] = set()

        for row_num, row in df.iterrows():
            row_dict = row.to_dict()

            try:
                validated = RequirementsImportRow(**row_dict)
            except ValidationError as e:
                error_msg = f"Строка {cast(int, row_num) + 2}: {e.errors()[0]['msg']}"
                errors.append(error_msg)
                stats["errors"] += 1
                logger.warning("Ошибка валидации | %s", error_msg)
                continue

            # Работа должна уже существовать — загрузите «Перечень работ» первым
            work_id = work_kod_map.get(validated.kod_raboty)
            if work_id is None:
                logger.warning(
                    "Строка %d: Работа '%s' не найдена. Загрузите «Перечень работ» первым.",
                    cast(int, row_num) + 2, validated.kod_raboty,
                )
                stats["skipped"] += 1
                continue

            # Создаём материал если его ещё нет (обновляем группу если изменилась)
            new_material_added = False
            if (validated.sys_nomer_materiala not in existing_materials
                    and validated.sys_nomer_materiala not in new_materials_in_session):
                material_repo.get_or_create(
                    sys_nomer=validated.sys_nomer_materiala,
                    naimenovanie=validated.naimenovanie_materiala,
                    ed_izm=validated.ed_izm,
                    gruppa=validated.gruppa_materiala,
                )
                new_materials_in_session.add(validated.sys_nomer_materiala)
                new_material_added = True

            if new_material_added or stats["requirements"] % 500 == 0:
                session.flush()

            material = material_repo.get_by_sys_nomer(validated.sys_nomer_materiala)
            if not material:
                continue

            req_repo.upsert(
                work_id=work_id,
                material_id=material.id,
                potrebnost=validated.potrebnost,
                prognosnaya_tsena=validated.prognosnaya_tsena,
            )
            stats["requirements"] += 1

        logger.info(
            "Импорт потребностей завершён: потребностей=%d, пропущено=%d, ошибок=%d",
            stats["requirements"], stats["skipped"], stats["errors"],
        )

    if errors:
        logger.warning("Первые ошибки при импорте:\n%s", "\n".join(errors[:10]))

    return stats


# =============================================================================
# Импорт аварийных работ
# =============================================================================

def import_emergency_works(file_path: Path, sheet_name: int | str = 0) -> dict[str, int]:
    """
    Импортировать перечень аварийных работ из Excel.

    Формат файла совпадает с «Перечнем работ» (WORKS_COLUMN_MAP):
        | Код работы | Наименование работы | Дата начала | Дата окончания |
        | Филиал | Завод | Приоритет | Статус |

    Все загруженные работы получат флаг is_emergency=True и будут обработаны
    алгоритмом распределения первыми (до любых плановых работ).

    Потребности аварийных работ загружаются отдельно через import_emergency_requirements.

    Args:
        file_path:  Путь к Excel файлу с перечнем аварийных работ
        sheet_name: Лист (по умолчанию первый)

    Returns:
        Статистика {"created": 5, "updated": 2, "errors": 0}
    """
    logger.info("Импорт ПЕРЕЧНЯ АВАРИЙНЫХ работ из: %s", file_path)

    df = _read_excel(file_path, sheet_name)
    df = _rename_columns(df, WORKS_COLUMN_MAP)

    stats = {"created": 0, "updated": 0, "errors": 0}

    with get_session() as session:
        from sqlalchemy import delete, select

        # Удаляем потребности аварийных работ перед переимпортом самих работ
        emergency_work_ids = select(Work.id).where(Work.is_emergency.is_(True))
        session.execute(delete(Requirement).where(Requirement.work_id.in_(emergency_work_ids)))
        session.execute(delete(Work).where(Work.is_emergency.is_(True)))
        logger.info("Удалены аварийные работы и их потребности перед новым импортом")

        for row_num, row in df.iterrows():
            row_dict = row.to_dict()

            try:
                validated = WorkListImportRow(**row_dict)
            except ValidationError as e:
                logger.warning("Строка %d: %s", cast(int, row_num) + 2, e.errors()[0]["msg"])
                stats["errors"] += 1
                continue

            # Ищем существующую работу с таким же кодом (может быть плановой)
            stmt = select(Work).where(Work.kod_raboty == validated.kod_raboty)
            work = session.scalar(stmt)

            if work is None:
                work = Work(
                    kod_raboty=validated.kod_raboty,
                    nazvanie=validated.nazvanie,
                    tip_raboty=validated.tip_raboty,
                    filial=validated.filial,
                    podrazdelenie=validated.podrazdelenie,
                    centr_zatrat=validated.centr_zatrat,
                    zavod=validated.zavod,
                    data_nachala=validated.data_nachala,
                    data_okonchaniya=validated.data_okonchaniya,
                    prioritet=validated.prioritet,
                    status=validated.status,
                    is_emergency=True,
                )
                session.add(work)
                stats["created"] += 1
            else:
                # Обновляем существующую работу и помечаем как аварийную
                work.is_emergency = True
                if validated.nazvanie:
                    work.nazvanie = validated.nazvanie
                if validated.data_nachala:
                    work.data_nachala = validated.data_nachala
                if validated.data_okonchaniya:
                    work.data_okonchaniya = validated.data_okonchaniya
                if validated.filial:
                    work.filial = validated.filial
                if validated.zavod:
                    work.zavod = validated.zavod
                if validated.prioritet != 3:
                    work.prioritet = validated.prioritet
                if validated.status != "active":
                    work.status = validated.status
                stats["updated"] += 1

            if (stats["created"] + stats["updated"]) % 500 == 0:
                session.flush()

        logger.info(
            "Импорт перечня аварийных работ завершён: создано=%d, обновлено=%d, ошибок=%d",
            stats["created"], stats["updated"], stats["errors"],
        )

    return stats


# =============================================================================
# Импорт потребностей аварийных работ
# =============================================================================

def import_emergency_requirements(file_path: Path, sheet_name: int | str = 0) -> dict[str, int]:
    """
    Импортировать потребности аварийных работ из Excel.

    Формат файла совпадает с обычным файлом потребностей:
        | Код работы | Группа материалов | Системный номер |
        | Наименование | Ед.изм | Потребность | Прогнозная цена |

    Работы должны быть загружены заранее через import_emergency_works.
    Если работа не найдена или не является аварийной — строка пропускается.

    При каждом вызове удаляются только требования аварийных работ (не плановых).

    Args:
        file_path:  Путь к Excel файлу с потребностями аварийных работ
        sheet_name: Лист (по умолчанию первый)

    Returns:
        Статистика {"requirements": 50, "skipped": 2, "errors": 0}
    """
    logger.info("Импорт ПОТРЕБНОСТЕЙ аварийных работ из: %s", file_path)

    df = _read_excel(file_path, sheet_name)
    df = _rename_columns(df, REQUIREMENTS_COLUMN_MAP)

    stats = {"requirements": 0, "skipped": 0, "errors": 0}
    errors: list[str] = []

    with get_session() as session:
        work_repo = WorkRepository(session)
        material_repo = MaterialRepository(session)
        req_repo = RequirementRepository(session)

        from sqlalchemy import delete, select

        # Удаляем только потребности аварийных работ
        emergency_work_ids = select(Work.id).where(Work.is_emergency.is_(True))
        session.execute(delete(Requirement).where(Requirement.work_id.in_(emergency_work_ids)))
        logger.info("Удалены потребности аварийных работ перед новым импортом")

        # Предзагружаем словари аварийных работ и материалов
        all_works = work_repo.get_kod_map()                    # {kod_raboty: id}
        emergency_ids = {
            row.id
            for row in session.execute(
                select(Work.id).where(Work.is_emergency.is_(True))
            ).all()
        }
        existing_materials = material_repo.get_id_map()
        new_materials_in_session: set[str] = set()

        for row_num, row in df.iterrows():
            row_dict = row.to_dict()

            try:
                validated = RequirementsImportRow(**row_dict)
            except ValidationError as e:
                error_msg = f"Строка {cast(int, row_num) + 2}: {e.errors()[0]['msg']}"
                errors.append(error_msg)
                stats["errors"] += 1
                logger.warning("Ошибка валидации | %s", error_msg)
                continue

            work_id = all_works.get(validated.kod_raboty)
            if work_id is None or work_id not in emergency_ids:
                logger.warning(
                    "Строка %d: Аварийная работа '%s' не найдена. Загрузите перечень аварийных работ первым.",
                    cast(int, row_num) + 2, validated.kod_raboty,
                )
                stats["skipped"] += 1
                continue

            new_material_added = False
            if (validated.sys_nomer_materiala not in existing_materials
                    and validated.sys_nomer_materiala not in new_materials_in_session):
                material_repo.get_or_create(
                    sys_nomer=validated.sys_nomer_materiala,
                    naimenovanie=validated.naimenovanie_materiala,
                    ed_izm=validated.ed_izm,
                    gruppa=validated.gruppa_materiala,
                )
                new_materials_in_session.add(validated.sys_nomer_materiala)
                new_material_added = True

            if new_material_added or stats["requirements"] % 500 == 0:
                session.flush()

            material = material_repo.get_by_sys_nomer(validated.sys_nomer_materiala)
            if not material:
                continue

            req_repo.upsert(
                work_id=work_id,
                material_id=material.id,
                potrebnost=validated.potrebnost,
                prognosnaya_tsena=validated.prognosnaya_tsena,
            )
            stats["requirements"] += 1

        logger.info(
            "Импорт потребностей аварийных работ завершён: потребностей=%d, пропущено=%d, ошибок=%d",
            stats["requirements"], stats["skipped"], stats["errors"],
        )

    if errors:
        logger.warning("Ошибки при импорте:\n%s", "\n".join(errors[:10]))

    return stats


# =============================================================================
# Импорт складских остатков (Слой 3)
# =============================================================================

def import_stock(file_path: Path, sheet_name: int | str = 0) -> dict[str, int]:
    """
    Импортировать складские остатки (партии материалов) из Excel.

    Перед импортом удаляем все существующие партии и загружаем заново.
    Это «снимок состояния склада на текущий момент»: всегда актуальные данные.

    ВНИМАНИЕ: Существующие партии будут УДАЛЕНЫ и заменены данными из файла!

    Args:
        file_path:  Путь к Excel файлу
        sheet_name: Лист (по умолчанию первый)

    Returns:
        Статистика {"warehouses": 10, "batches": 5000, "errors": 0}
    """
    df = _read_excel(file_path, sheet_name)
    df = _rename_columns(df, STOCK_COLUMN_MAP)

    stats = {"warehouses": 0, "batches": 0, "errors": 0}

    with get_session() as session:
        material_repo = MaterialRepository(session)

        # Очищаем старые данные перед загрузкой нового снимка.
        # StockMovement ссылается на StockBatch через FK — удаляем сначала движения,
        # иначе PostgreSQL не разрешит удалить партии (FK violation).
        from sqlalchemy import delete
        session.execute(delete(StockMovement))
        session.execute(delete(StockBatch))
        logger.info("Очищены старые складские остатки (партии и движения)")

        # Кэш складов — чтобы не делать запрос к БД для каждой партии
        warehouse_cache: dict[str, Warehouse] = {}

        for row_num, row in df.iterrows():
            row_dict = row.to_dict()

            try:
                validated = StockImportRow(**row_dict)
            except ValidationError as e:
                logger.warning("Строка %d: %s", cast(int, row_num) + 2, e.errors()[0]["msg"])
                stats["errors"] += 1
                continue

            # Склад: получаем или создаём (кэшируем чтобы не дублировать)
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

            # Материал: создаём или обновляем в справочнике
            material = material_repo.get_or_create(
                sys_nomer=validated.sys_nomer_materiala,
                naimenovanie=validated.naimenovanie_materiala,
                ed_izm=validated.ed_izm,
                gruppa=validated.gruppa_materiala,
            )

            # Flush нужен в двух случаях:
            #   1. Материал только что создан — его id == None, нужен flush чтобы
            #      PostgreSQL назначил autoincrement и batch.material_id заполнился
            #   2. Периодически каждые 1000 партий — освобождаем накопленное в памяти
            # Если материал уже существовал — его id уже установлен, flush не нужен
            if material.id is None or stats["batches"] % 1000 == 0:
                session.flush()

            # Создаём партию.
            # dostupno = kolichestvo: изначально весь материал доступен.
            # Алгоритм распределения будет уменьшать dostupno.
            batch = StockBatch(
                warehouse_id=wh.id,
                material_id=material.id,
                nomer_partii=validated.nomer_partii,
                kolichestvo=validated.kolichestvo,
                dostupno=validated.kolichestvo,  # Изначально весь остаток доступен
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
# Импорт поставок (Слой 4)
# =============================================================================

def import_supplies(file_path: Path, sheet_name: int | str = 0) -> dict[str, int]:
    """
    Импортировать поставки (материалы в пути) из Excel.

    Поставки — это заказанные, но ещё не поступившие материалы.
    Они привязаны к ФИЛИАЛУ (не к заводу): алгоритм сопоставляет
    филиал поставки с филиалом работы.

    Если несколько строк Excel относятся к одному договору — группируем их
    в одну запись Supply с несколькими строками SupplyLine.

    ВНИМАНИЕ: Существующие поставки будут УДАЛЕНЫ и заменены!

    Args:
        file_path:  Путь к Excel файлу
        sheet_name: Лист

    Returns:
        Статистика {"supplies": 200, "lines": 800, "errors": 0}
    """
    df = _read_excel(file_path, sheet_name)
    df = _rename_columns(df, SUPPLY_COLUMN_MAP)

    stats = {"supplies": 0, "lines": 0, "errors": 0}

    with get_session() as session:
        material_repo = MaterialRepository(session)

        # Полная замена: удаляем строки распределений ссылающихся на поставки,
        # затем сами поставки. Порядок важен из-за FK-ограничений.
        from sqlalchemy import delete, select

        all_supply_line_ids = select(SupplyLine.id)
        session.execute(delete(AllocationResult).where(
            AllocationResult.supply_line_id.in_(all_supply_line_ids)
        ))
        session.execute(delete(SupplyLine))
        session.execute(delete(Supply))
        logger.info("Удалены старые поставки и связанные строки распределений")

        # Кэши для предотвращения дублей в текущей сессии
        supply_cache: dict[str, Supply] = {}     # {ключ_договора: Supply}
        warehouse_cache: dict[str, Warehouse] = {}

        for row_num, row in df.iterrows():
            row_dict = row.to_dict()

            try:
                validated = SupplyImportRow(**row_dict)
            except ValidationError as e:
                logger.warning("Строка %d: %s", cast(int, row_num) + 2, e.errors()[0]["msg"])
                stats["errors"] += 1
                continue

            # Уникальный ключ для группировки: договор + склад + дата
            # Одна поставка (Supply) = один такой ключ
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

            # Создаём запись Supply один раз на договор
            if dogovor_key not in supply_cache:
                supply = Supply(
                    dogovor=validated.dogovor,
                    postavshchik=validated.postavshchik,
                    warehouse_id=warehouse_id,
                    filial=validated.filial,   # ← Ключевое поле для сопоставления с работой
                    zavod=validated.zavod,
                    data_postavki=validated.data_postavki,
                    status=validated.status,
                )
                session.add(supply)
                session.flush()  # Нужен id для строк поставки
                supply_cache[dogovor_key] = supply
                stats["supplies"] += 1
            else:
                supply = supply_cache[dogovor_key]

            # Материал в справочнике
            material = material_repo.get_or_create(
                sys_nomer=validated.sys_nomer_materiala,
                naimenovanie=validated.naimenovanie_materiala,
                ed_izm=validated.ed_izm,
            )
            # Flush нужен только если материал только что создан — нужен его id
            # для поля material_id в строке поставки SupplyLine
            if material.id is None:
                session.flush()

            # Строка поставки (конкретный материал в поставке)
            line = SupplyLine(
                supply_id=supply.id,
                material_id=material.id,
                kolichestvo=validated.kolichestvo,
                dostupno=validated.kolichestvo,  # Изначально весь объём доступен
                stoimost_za_ed=validated.stoimost_za_ed,
            )
            session.add(line)
            stats["lines"] += 1

        logger.info(
            "Импорт поставок завершён: поставок=%d, строк=%d, ошибок=%d",
            stats["supplies"], stats["lines"], stats["errors"],
        )

    return stats


# =============================================================================
# Импорт фактических списаний (Слой 1 распределения)
# =============================================================================

def import_writeoffs(file_path: Path, sheet_name: int | str = 0) -> dict[str, int]:
    """
    Импортировать фактические списания материалов на работы из Excel.

    Что это такое?
        Данные о материалах, которые уже физически списаны на работу в SAP.
        Это самый надёжный источник: документально подтверждён.

    Логика импорта:
        1. Очищаем старые записи (полная замена снимком)
        2. Для каждой строки: ищем работу и материал в БД, создаём WriteOff.
           ВНИМАНИЕ: Если работа или материал не найдены — пропускаем строку.
           Сначала нужно импортировать потребности (import_requirements)!

    Зачем нужна очистка?
        Файл списаний — это выгрузка из SAP на конкретную дату.
        При обновлении данных просто загружаем новый файл — старые удаляются.

    Args:
        file_path:  Путь к Excel файлу
        sheet_name: Лист

    Returns:
        Статистика {"writeoffs": 500, "errors": 5, "skipped": 10}
    """
    df = _read_excel(file_path, sheet_name)
    df = _rename_columns(df, WRITEOFF_COLUMN_MAP)

    stats = {"writeoffs": 0, "errors": 0, "skipped": 0}

    with get_session() as session:
        work_repo = WorkRepository(session)
        material_repo = MaterialRepository(session)

        # Очищаем старые записи перед загрузкой нового снимка
        from sqlalchemy import delete
        session.execute(delete(WriteOff))
        logger.info("Очищены старые записи списаний")

        # Предзагружаем словари работ и материалов для быстрого поиска
        work_kod_map = work_repo.get_kod_map()           # {kod_raboty: id}
        material_nomer_map = material_repo.get_id_map()  # {sys_nomer: id}

        for row_num, row in df.iterrows():
            row_dict = row.to_dict()

            try:
                validated = WriteOffImportRow(**row_dict)
            except ValidationError as e:
                logger.warning("Строка %d: %s", cast(int, row_num) + 2, e.errors()[0]["msg"])
                stats["errors"] += 1
                continue

            # Ищем ID работы в словаре (O(1))
            work_id = work_kod_map.get(validated.kod_raboty)
            if work_id is None:
                # Работа не найдена — пропускаем (сначала надо импортировать потребности)
                logger.warning(
                    "Строка %d: Работа '%s' не найдена в БД. Импортируйте потребности сначала.",
                    cast(int, row_num) + 2, validated.kod_raboty,
                )
                stats["skipped"] += 1
                continue

            # Ищем ID материала
            material_id = material_nomer_map.get(validated.sys_nomer_materiala)
            if material_id is None:
                # Материала нет в справочнике — создаём его (если есть наименование)
                material = material_repo.get_or_create(
                    sys_nomer=validated.sys_nomer_materiala,
                    naimenovanie=validated.naimenovanie_materiala,
                    ed_izm=validated.ed_izm,
                )
                session.flush()
                material_id = material.id
                # Обновляем локальный словарь чтобы не создавать дубли
                material_nomer_map[validated.sys_nomer_materiala] = material_id

            # Рассчитываем сумму если не передана (qty × price)
            from decimal import Decimal
            summa = validated.summa
            if summa == Decimal("0") and validated.stoimost_za_ed > 0:
                summa = (validated.kolichestvo * validated.stoimost_za_ed).quantize(Decimal("0.01"))

            # Создаём запись о списании
            writeoff = WriteOff(
                work_id=work_id,
                material_id=material_id,
                kolichestvo=validated.kolichestvo,
                stoimost_za_ed=validated.stoimost_za_ed,
                summa=summa,
                nomer_dokumenta=validated.nomer_dokumenta,
                data_spisaniya=validated.data_spisaniya,
            )
            session.add(writeoff)
            stats["writeoffs"] += 1

            # Периодический flush для производительности
            if stats["writeoffs"] % 500 == 0:
                session.flush()

        logger.info(
            "Импорт списаний завершён: записей=%d, ошибок=%d, пропущено=%d",
            stats["writeoffs"], stats["errors"], stats["skipped"],
        )

    return stats


# =============================================================================
# Импорт «Выдано не списано» (Слой 2 распределения)
# =============================================================================

def import_issued_not_written_off(file_path: Path, sheet_name: int | str = 0) -> dict[str, int]:
    """
    Импортировать данные «Выдано не списано» из Excel.

    Что это такое?
        Материалы, которые уже выданы со склада на объект (бригада получила),
        но документ списания в SAP ещё не проведён.
        Это «серая зона» между выдачей и официальным списанием.

    Особенности:
        - Поле «Код склада» — только информативное, остатки склада НЕ изменяются
        - При обработке алгоритмом этот слой уменьшает remaining,
          но НЕ записывает движение по складу (материал уже физически ушёл)

    Args:
        file_path:  Путь к Excel файлу
        sheet_name: Лист

    Returns:
        Статистика {"issued": 200, "errors": 5, "skipped": 3}
    """
    df = _read_excel(file_path, sheet_name)
    df = _rename_columns(df, ISSUED_COLUMN_MAP)

    stats = {"issued": 0, "errors": 0, "skipped": 0}

    with get_session() as session:
        work_repo = WorkRepository(session)
        material_repo = MaterialRepository(session)

        # Очищаем старые данные
        from sqlalchemy import delete
        session.execute(delete(IssuedNotWrittenOff))
        logger.info("Очищены старые записи 'Выдано не списано'")

        work_kod_map = work_repo.get_kod_map()
        material_nomer_map = material_repo.get_id_map()
        warehouse_cache: dict[str, Warehouse] = {}

        for row_num, row in df.iterrows():
            row_dict = row.to_dict()

            try:
                validated = IssuedNotWrittenOffImportRow(**row_dict)
            except ValidationError as e:
                logger.warning("Строка %d: %s", cast(int, row_num) + 2, e.errors()[0]["msg"])
                stats["errors"] += 1
                continue

            # Ищем работу
            work_id = work_kod_map.get(validated.kod_raboty)
            if work_id is None:
                logger.warning(
                    "Строка %d: Работа '%s' не найдена. Сначала импортируйте потребности.",
                    cast(int, row_num) + 2, validated.kod_raboty,
                )
                stats["skipped"] += 1
                continue

            # Ищем или создаём материал
            material_id = material_nomer_map.get(validated.sys_nomer_materiala)
            if material_id is None:
                material = material_repo.get_or_create(
                    sys_nomer=validated.sys_nomer_materiala,
                    naimenovanie=validated.naimenovanie_materiala,
                    ed_izm=validated.ed_izm,
                )
                session.flush()
                material_id = material.id
                material_nomer_map[validated.sys_nomer_materiala] = material_id

            # Склад (если указан) — только для информации
            warehouse_id = None
            if validated.kod_sklada:
                if validated.kod_sklada not in warehouse_cache:
                    wh = _get_or_create_warehouse(
                        session=session,
                        kod_sklada=validated.kod_sklada,
                        tip_sklada=None,
                        filial=None,
                        zavod=None,
                    )
                    warehouse_cache[validated.kod_sklada] = wh
                warehouse_id = warehouse_cache[validated.kod_sklada].id

            # Рассчитываем сумму если не передана
            from decimal import Decimal
            summa = validated.summa
            if summa == Decimal("0") and validated.stoimost_za_ed > 0:
                summa = (validated.kolichestvo * validated.stoimost_za_ed).quantize(Decimal("0.01"))

            # Создаём запись «Выдано не списано»
            issued = IssuedNotWrittenOff(
                work_id=work_id,
                material_id=material_id,
                warehouse_id=warehouse_id,  # Информативно, остатки не трогаем
                kolichestvo=validated.kolichestvo,
                stoimost_za_ed=validated.stoimost_za_ed,
                summa=summa,
                data_vydachi=validated.data_vydachi,
            )
            session.add(issued)
            stats["issued"] += 1

            if stats["issued"] % 500 == 0:
                session.flush()

        logger.info(
            "Импорт 'Выдано не списано' завершён: записей=%d, ошибок=%d, пропущено=%d",
            stats["issued"], stats["errors"], stats["skipped"],
        )

    return stats


# =============================================================================
# Импорт перечня работ с наименованиями (мастер-данные)
# =============================================================================

def import_works(file_path: Path, sheet_name: int | str = 0) -> dict[str, int]:
    """
    Импортировать перечень работ с наименованиями из Excel.

    Что делает эта функция?
        Загружает мастер-данные о работах: наименование, даты, оргструктуру.
        Если работа уже существует в БД — обновляет её поля (upsert по kod_raboty).
        Если работы нет — создаёт новую запись.

    Когда запускать?
        Рекомендуется ДО import-requirements, чтобы при создании работ через
        import-requirements они уже имели наименования из перечня.
        Но можно и ПОСЛЕ: функция обновит существующие работы.

    Что НЕ делает:
        Не удаляет существующие работы — только добавляет/обновляет.
        Не затрагивает потребности, складские остатки, поставки и другие данные.

    Логика upsert (обновления):
        Поля обновляются только если в файле передано непустое значение.
        Это позволяет загружать неполные файлы — пустые поля не затрут существующие данные.
        Приоритет НЕ обновляется если он равен 3 (значение по умолчанию).
        Статус НЕ обновляется если он равен "active" (значение по умолчанию).

    Формат файла:
        | Код работы | Наименование работы | Дата начала | Дата окончания |
        | Филиал | Завод | Тип работы | Приоритет | Статус |

    Args:
        file_path:  Путь к Excel файлу с перечнем работ
        sheet_name: Лист (0 = первый лист, можно передать имя листа)

    Returns:
        Статистика {"created": 30, "updated": 20, "errors": 0}
    """
    # Читаем файл и переименовываем колонки согласно маппингу
    df = _read_excel(file_path, sheet_name)
    df = _rename_columns(df, WORKS_COLUMN_MAP)

    # Счётчики для итоговой статистики
    stats = {"created": 0, "updated": 0, "errors": 0}

    with get_session() as session:
        from sqlalchemy import select

        for row_num, row in df.iterrows():
            # Преобразуем строку DataFrame в обычный Python-словарь
            row_dict = row.to_dict()

            # --- Валидация строки через Pydantic ---
            # WorkListImportRow проверяет: обязательные поля заполнены,
            # дата начала не позже даты окончания, приоритет в диапазоне 1-3.
            try:
                validated = WorkListImportRow(**row_dict)
            except ValidationError as e:
                # Записываем только первую (самую понятную) ошибку из списка
                logger.warning("Строка %d: %s", cast(int, row_num) + 2, e.errors()[0]["msg"])
                stats["errors"] += 1
                continue  # Пропускаем строку с ошибкой и переходим к следующей

            # --- Ищем существующую работу по коду ---
            # SELECT * FROM works WHERE kod_raboty = validated.kod_raboty
            stmt = select(Work).where(Work.kod_raboty == validated.kod_raboty)
            work = session.scalar(stmt)  # scalar() вернёт объект Work или None

            if work is None:
                # --- Создаём новую работу ---
                # Работа не найдена в БД — создаём с данными из файла.
                # is_emergency=False: перечень работ не содержит аварийных
                # (аварийные загружаются отдельно через import-emergency).
                work = Work(
                    kod_raboty=validated.kod_raboty,
                    nazvanie=validated.nazvanie,
                    tip_raboty=validated.tip_raboty,
                    filial=validated.filial,
                    podrazdelenie=validated.podrazdelenie,
                    centr_zatrat=validated.centr_zatrat,
                    zavod=validated.zavod,
                    data_nachala=validated.data_nachala,
                    data_okonchaniya=validated.data_okonchaniya,
                    prioritet=validated.prioritet,
                    status=validated.status,
                    is_emergency=False,  # Перечень работ — не аварийные
                )
                session.add(work)
                stats["created"] += 1

            else:
                # --- Обновляем существующую работу ---
                # Работа уже есть в БД (например, была создана через import-requirements).
                # Обновляем только непустые поля: не затираем данные если в файле пусто.

                if validated.nazvanie:
                    # Обновляем наименование если в файле оно указано
                    work.nazvanie = validated.nazvanie

                if validated.data_nachala:
                    # Обновляем дату начала если в файле указана
                    work.data_nachala = validated.data_nachala

                if validated.data_okonchaniya:
                    # Обновляем дату окончания если в файле указана
                    work.data_okonchaniya = validated.data_okonchaniya

                if validated.filial:
                    # Обновляем филиал если указан
                    work.filial = validated.filial

                if validated.zavod:
                    # Обновляем завод если указан
                    work.zavod = validated.zavod

                if validated.tip_raboty:
                    # Обновляем тип работы если указан
                    work.tip_raboty = validated.tip_raboty

                if validated.prioritet != 3:
                    # Обновляем приоритет только если он отличается от дефолтного значения (3).
                    # Иначе непоставленный приоритет в файле перезапишет реальный из потребностей.
                    work.prioritet = validated.prioritet

                if validated.status != "active":
                    # Обновляем статус только если он отличается от дефолта "active".
                    # Иначе пустой статус в файле сбросит "completed" или "cancelled".
                    work.status = validated.status

                stats["updated"] += 1

            # Периодический flush: каждые 500 записей отправляем данные в PostgreSQL.
            # Это необходимо для освобождения памяти при больших файлах.
            # Транзакция ещё не завершена — коммит произойдёт при выходе из get_session().
            if (stats["created"] + stats["updated"]) % 500 == 0:
                session.flush()

        logger.info(
            "Импорт перечня работ завершён: создано=%d, обновлено=%d, ошибок=%d",
            stats["created"], stats["updated"], stats["errors"],
        )

    return stats


# =============================================================================
# Импорт согласованных межфилиальных перемещений
# =============================================================================

def import_approved_transfers(
    file_path: Path,
    sheet_name: int | str = 0,
) -> dict[str, int]:
    """
    Загрузить файл согласованных межфилиальных перемещений.

    Что происходит при импорте:
        1. Все предыдущие записи УДАЛЯЮТСЯ (замена, не дополнение).
        2. Каждая строка файла создаёт запись ApprovedTransfer.
        3. После импорта нужно повторно запустить распределение:
           алгоритм учтёт согласованные перемещения как Layer 3в.

    Формат Excel:
        | Системный номер | Склад-источник | Филиал-получатель | Кол-во согласовано |

    Совет пользователю:
        Возьмите лист «Возможное перемещение» из Excel-отчёта,
        оставьте только согласованные строки и при необходимости
        скорректируйте количество в колонке «Кол-во согласовано».

    Args:
        file_path:  Путь к .xlsx файлу с согласованными перемещениями
        sheet_name: Лист Excel (по умолчанию первый)

    Returns:
        {"created": N, "errors": K, "skipped": M}
    """
    df = _read_excel(file_path, sheet_name)
    df = _rename_columns(df, APPROVED_TRANSFERS_COLUMN_MAP)

    stats: dict[str, int] = {"created": 0, "errors": 0, "skipped": 0}

    with get_session() as session:
        transfer_repo = ApprovedTransferRepository(session)
        mat_repo = MaterialRepository(session)

        # Удаляем предыдущие записи — новый файл полностью заменяет старый
        transfer_repo.delete_all()
        logger.info("Старые согласованные перемещения удалены. Загружаем новые...")

        # Кэш: {kod_sklada: Warehouse} — чтобы не делать SELECT для каждой строки
        warehouse_cache: dict[str, Optional[Warehouse]] = {}

        for idx, row in df.iterrows():
            row_dict = row.to_dict()

            try:
                validated = ApprovedTransferImportRow(**row_dict)
            except ValidationError as e:
                stats["errors"] += 1
                logger.warning(
                    "Строка %d (согласованные перемещения): ошибка валидации: %s",
                    idx, e.errors()[0]["msg"] if e.errors() else str(e),
                )
                continue

            # --- Находим материал ---
            material = mat_repo.get_by_sys_nomer(validated.sys_nomer_materiala)
            if material is None:
                # Материала нет в БД — перемещение не нужно создавать
                stats["skipped"] += 1
                logger.warning(
                    "Строка %d: материал '%s' не найден в БД — строка пропущена",
                    idx, validated.sys_nomer_materiala,
                )
                continue

            # --- Находим склад-источник ---
            kod_sklada = validated.kod_sklada_istochnik
            if kod_sklada not in warehouse_cache:
                from sqlalchemy import select as sa_select
                warehouse_cache[kod_sklada] = session.scalar(
                    sa_select(Warehouse).where(Warehouse.kod_sklada == kod_sklada)
                )

            warehouse = warehouse_cache[kod_sklada]
            if warehouse is None:
                stats["skipped"] += 1
                logger.warning(
                    "Строка %d: склад '%s' не найден в БД — строка пропущена",
                    idx, kod_sklada,
                )
                continue

            # --- Создаём запись согласованного перемещения ---
            transfer = ApprovedTransfer(
                material_id=material.id,
                from_warehouse_id=warehouse.id,
                to_filial=validated.to_filial,
                kolichestvo=validated.kolichestvo,
                dostupno=validated.kolichestvo,  # Сразу доступно всё согласованное количество
            )
            session.add(transfer)
            stats["created"] += 1

            # Периодический flush для освобождения памяти
            if stats["created"] % 200 == 0:
                session.flush()

    logger.info(
        "Импорт согласованных перемещений завершён: создано=%d, пропущено=%d, ошибок=%d",
        stats["created"], stats["skipped"], stats["errors"],
    )
    return stats
