"""
app/services/export_service.py — Экспорт результатов в Excel

Что делает этот модуль?
    Берёт результаты распределения из PostgreSQL и формирует Excel-отчёты.
    Каждый тип результата — отдельный лист в одном Excel файле.

Структура Excel-отчёта:
    Лист 1: "Распределение"   — что, кому, откуда, по какой цене
    Лист 2: "Движение склада" — расходы по складам и партиям
    Лист 3: "Дефицит"         — чего не хватает, сколько нужно закупить
    Лист 4: "Обеспечённость"  — % обеспечённости по работам и филиалам

Зачем pandas для экспорта?
    pandas.DataFrame.to_excel() умеет:
    - Создавать несколько листов в одном файле
    - Применять форматирование (ширина колонок)
    - Работать с большими объёмами данных эффективно
"""

from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd
from sqlalchemy import text

from app.core.config import settings
from app.core.logging_config import get_logger
from app.db.database import get_session

logger = get_logger(__name__)


def export_allocation_results(session_id: str, output_dir: Optional[str] = None) -> Path:
    """
    Экспортировать все результаты сессии распределения в Excel.

    Args:
        session_id: ID сессии распределения (например "20260523_143000_abc12")
        output_dir: Папка для сохранения файла (по умолчанию из settings)

    Returns:
        Путь к созданному Excel файлу
    """

    out_dir = Path(output_dir or settings.export_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Имя файла включает ID сессии и дату/время создания
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    file_path = out_dir / f"maps_results_{session_id}_{timestamp}.xlsx"

    logger.info("Формирование Excel отчёта для сессии: %s", session_id)

    with get_session() as session:
        # Используем pandas + SQL-запросы для эффективной выборки больших данных

        # --- Лист 1: Распределение ---
        df_allocation = _get_allocation_df(session, session_id)

        # --- Лист 2: Движение склада ---
        df_movements = _get_movements_df(session, session_id)

        # --- Лист 3: Дефицит ---
        df_deficit = _get_deficit_df(session, session_id)

        # --- Лист 4: Обеспечённость ---
        df_coverage = _get_coverage_df(session, session_id)

    # Записываем всё в один Excel файл с несколькими листами
    # ExcelWriter — объект для записи нескольких листов в один файл
    with pd.ExcelWriter(file_path, engine="openpyxl") as writer:
        _write_sheet(writer, df_allocation, "Распределение")
        _write_sheet(writer, df_movements, "Движение склада")
        _write_sheet(writer, df_deficit, "Дефицит")
        _write_sheet(writer, df_coverage, "Обеспеченность")

    logger.info(
        "Excel отчёт создан: %s (строк: распределение=%d, дефицит=%d)",
        file_path, len(df_allocation), len(df_deficit),
    )
    return file_path


def _write_sheet(writer: pd.ExcelWriter, df: pd.DataFrame, sheet_name: str) -> None:
    """
    Записать DataFrame как лист Excel с автоподбором ширины колонок.

    Args:
        writer:     Открытый ExcelWriter
        df:         Данные для записи
        sheet_name: Название листа
    """
    df.to_excel(writer, sheet_name=sheet_name, index=False)

    # Автоподбор ширины колонок (чтобы текст не обрезался)
    worksheet = writer.sheets[sheet_name]
    for col_idx, col in enumerate(df.columns, 1):
        # Максимальная ширина: длина заголовка vs длина самого длинного значения
        max_len = max(
            len(str(col)),
            df[col].astype(str).str.len().max() if not df.empty else 0,
        )
        # Ограничиваем: минимум 10, максимум 60 символов
        col_width = min(max(max_len + 2, 10), 60)
        # openpyxl использует буквы колонок (A, B, C...)
        col_letter = worksheet.cell(1, col_idx).column_letter
        worksheet.column_dimensions[col_letter].width = col_width


def _get_allocation_df(session, session_id: str) -> pd.DataFrame:
    """
    Получить данные распределения для экспорта.

    JOIN-запрос: объединяем результаты с данными работ, материалов, складов.
    """
    sql = text("""
        SELECT
            ar.id                       AS "№",
            w.kod_raboty                AS "Код работы",
            w.filial                    AS "Филиал работы",
            w.zavod                     AS "Завод работы",
            w.prioritet                 AS "Приоритет",
            w.data_nachala              AS "Дата начала",
            w.data_okonchaniya          AS "Дата окончания",
            m.sys_nomer                 AS "Системный номер",
            m.naimenovanie              AS "Наименование материала",
            m.ed_izm                    AS "Ед.изм",
            ar.istochnik                AS "Источник",
            wh.kod_sklada               AS "Код склада",
            wh.filial                   AS "Филиал склада",
            sb.nomer_partii             AS "Номер партии",
            sb.data_postupleniya        AS "Дата поступления партии",
            ar.tip_raspredeleniya       AS "Тип распределения",
            ar.kolichestvo              AS "Количество",
            ar.stoimost_za_ed           AS "Стоимость за ед",
            ar.summa                    AS "Сумма",
            ar.created_at               AS "Дата распределения"
        FROM allocation_results ar
        JOIN works w ON ar.work_id = w.id
        JOIN materials m ON ar.material_id = m.id
        LEFT JOIN warehouses wh ON ar.warehouse_id = wh.id
        LEFT JOIN stock_batches sb ON ar.batch_id = sb.id
        WHERE ar.session_id = :session_id
        ORDER BY w.prioritet, w.data_nachala, w.kod_raboty, m.sys_nomer
    """)
    result = session.execute(sql, {"session_id": session_id})
    rows = result.fetchall()
    return pd.DataFrame(rows, columns=result.keys())


def _get_movements_df(session, session_id: str) -> pd.DataFrame:
    """Получить движения склада для экспорта."""
    sql = text("""
        SELECT
            wh.kod_sklada               AS "Код склада",
            wh.filial                   AS "Филиал склада",
            m.sys_nomer                 AS "Системный номер",
            m.naimenovanie              AS "Наименование материала",
            m.ed_izm                    AS "Ед.изм",
            sb.nomer_partii             AS "Номер партии",
            sm.izmenenie                AS "Изменение (расход)",
            sm.ostatok                  AS "Остаток после",
            w.kod_raboty                AS "Работа-получатель",
            sm.data_dvizheniya          AS "Дата движения"
        FROM stock_movements sm
        JOIN warehouses wh ON sm.warehouse_id = wh.id
        JOIN materials m ON sm.material_id = m.id
        LEFT JOIN stock_batches sb ON sm.batch_id = sb.id
        LEFT JOIN works w ON sm.work_id = w.id
        WHERE sm.session_id = :session_id
        ORDER BY wh.kod_sklada, m.sys_nomer, sm.data_dvizheniya
    """)
    result = session.execute(sql, {"session_id": session_id})
    rows = result.fetchall()
    return pd.DataFrame(rows, columns=result.keys())


def _get_deficit_df(session, session_id: str) -> pd.DataFrame:
    """Получить дефицит для экспорта."""
    sql = text("""
        SELECT
            w.kod_raboty                AS "Код работы",
            w.filial                    AS "Филиал",
            w.zavod                     AS "Завод",
            w.prioritet                 AS "Приоритет",
            m.sys_nomer                 AS "Системный номер",
            m.naimenovanie              AS "Наименование материала",
            m.ed_izm                    AS "Ед.изм",
            d.deficit_qty               AS "Объем дефицита",
            d.estimated_cost            AS "Оценочная стоимость",
            d.needed_by                 AS "Нужно к дате",
            'К закупу'                  AS "Статус"
        FROM deficit_records d
        JOIN works w ON d.work_id = w.id
        JOIN materials m ON d.material_id = m.id
        WHERE d.session_id = :session_id
        ORDER BY w.prioritet, d.needed_by, m.sys_nomer
    """)
    result = session.execute(sql, {"session_id": session_id})
    rows = result.fetchall()
    return pd.DataFrame(rows, columns=result.keys())


def _get_coverage_df(session, session_id: str) -> pd.DataFrame:  # noqa: ARG001 — session_id зарезервирован для фильтрации по сценарию
    """
    Сформировать отчёт об обеспечённости работ.

    Обеспечённость = (распределено / потребность) * 100%.
    Группируем по работам и считаем % закрытия по каждому материалу.
    """
    sql = text("""
        SELECT
            w.kod_raboty                        AS "Код работы",
            w.filial                            AS "Филиал",
            w.prioritet                         AS "Приоритет",
            w.data_okonchaniya                  AS "Дата окончания",
            m.sys_nomer                         AS "Системный номер",
            m.naimenovanie                      AS "Наименование",
            m.ed_izm                            AS "Ед.изм",
            r.potrebnost                        AS "Потребность",
            r.raspredeleno                      AS "Распределено",
            r.deficit                           AS "Дефицит",
            CASE
                WHEN r.potrebnost > 0
                THEN ROUND(r.raspredeleno / r.potrebnost * 100, 1)
                ELSE 0
            END                                 AS "% обеспеченности",
            CASE
                WHEN r.deficit = 0 THEN 'Обеспечено'
                WHEN r.raspredeleno = 0 THEN 'Не обеспечено'
                ELSE 'Частично обеспечено'
            END                                 AS "Статус обеспеченности"
        FROM requirements r
        JOIN works w ON r.work_id = w.id
        JOIN materials m ON r.material_id = m.id
        ORDER BY w.prioritet, w.data_okonchaniya, w.kod_raboty, m.sys_nomer
    """)
    result = session.execute(sql)
    rows = result.fetchall()
    return pd.DataFrame(rows, columns=result.keys())


