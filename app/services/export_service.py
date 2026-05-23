"""
app/services/export_service.py — Экспорт результатов распределения в Excel

Структура Excel-отчёта (6 листов):
──────────────────────────────────────────────────────────────────────────────
  Лист 1: «Распределение (факт)»
      Фактически распределённые материалы — со складов своего завода/филиала
      и из поставок. Одна строка = один склад + одна работа + один материал.
      Стоимость = средняя взвешенная по всем партиям склада.

  Лист 2: «Возможное движение»
      Материалы других филиалов, которые могли бы покрыть дефицит.
      НЕ распределены фактически — требуют согласования переброса.
      Одна строка = один чужой склад + одна работа + один материал.

  Лист 3: «Движение склада»
      Детальный расход: с какого склада, на какую работу,
      какой материал, какое количество.
      Только фактические списания (not is_possible).

  Лист 4: «Остатки складов»
      Текущие остатки по складам ПОСЛЕ распределения.
      Показывает: начальный остаток → распределено → остаток.

  Лист 5: «Дефицит»
      Позиции «К закупу»: что не покрыто ни складом, ни поставками.

  Лист 6: «Обеспечённость»
      % покрытия каждой потребности.
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


def export_allocation_results(
    session_id: str,
    output_dir: Optional[str] = None,
) -> Path:
    """
    Сформировать Excel-отчёт по результатам сессии распределения.

    Args:
        session_id: ID сессии распределения
        output_dir: Папка для сохранения (по умолчанию из settings)

    Returns:
        Путь к созданному .xlsx файлу
    """
    out_dir = Path(output_dir or settings.export_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    file_path = out_dir / f"maps_results_{session_id}_{timestamp}.xlsx"

    logger.info("Формирование Excel-отчёта | сессия: %s", session_id)

    with get_session() as session:
        df_fact       = _get_fact_allocation_df(session, session_id)
        df_possible   = _get_possible_allocation_df(session, session_id)
        df_movement   = _get_movement_df(session, session_id)
        df_balances   = _get_warehouse_balances_df(session, session_id)
        df_deficit    = _get_deficit_df(session, session_id)
        df_coverage   = _get_coverage_df(session)

    with pd.ExcelWriter(file_path, engine="openpyxl") as writer:
        _write_sheet(writer, df_fact,     "Распределение (факт)")
        _write_sheet(writer, df_possible, "Возможное движение")
        _write_sheet(writer, df_movement, "Движение склада")
        _write_sheet(writer, df_balances, "Остатки складов")
        _write_sheet(writer, df_deficit,  "Дефицит")
        _write_sheet(writer, df_coverage, "Обеспеченность")

    logger.info(
        "Отчёт создан: %s | факт=%d, возможное=%d, дефицит=%d",
        file_path, len(df_fact), len(df_possible), len(df_deficit),
    )
    return file_path


# =============================================================================
# Лист 1: Фактическое распределение
# =============================================================================

def _get_fact_allocation_df(session, session_id: str) -> pd.DataFrame:
    """
    Фактически распределённые материалы (sklad + postavka).

    Каждая строка показывает:
      - Какая работа получила материал
      - Откуда (склад, договор поставки)
      - Сколько
      - По какой средней цене и на какую сумму
    """
    sql = text("""
        SELECT
            w.prioritet                     AS "Приоритет",
            w.kod_raboty                    AS "Код работы",
            w.filial                        AS "Филиал работы",
            w.zavod                         AS "Завод работы",
            w.data_nachala                  AS "Дата начала",
            w.data_okonchaniya              AS "Дата окончания",
            m.sys_nomer                     AS "Системный номер",
            m.naimenovanie                  AS "Наименование материала",
            m.ed_izm                        AS "Ед.изм",
            ar.istochnik                    AS "Источник",
            ar.tip_raspredeleniya           AS "Тип распределения",
            wh.kod_sklada                   AS "Код склада",
            wh.filial                       AS "Филиал склада",
            wh.zavod                        AS "Завод склада",
            CAST(ar.kolichestvo AS NUMERIC(18,4))        AS "Количество",
            CAST(ar.srednyaya_stoimost AS NUMERIC(18,2)) AS "Средняя цена",
            CAST(ar.summa AS NUMERIC(18,2))              AS "Сумма"
        FROM allocation_results ar
        JOIN works     w  ON ar.work_id      = w.id
        JOIN materials m  ON ar.material_id  = m.id
        LEFT JOIN warehouses wh ON ar.warehouse_id = wh.id
        WHERE ar.session_id = :sid
          AND ar.is_possible = FALSE
        ORDER BY
            w.prioritet,
            w.data_nachala,
            w.kod_raboty,
            m.sys_nomer,
            ar.istochnik
    """)
    rows = session.execute(sql, {"sid": session_id}).fetchall()
    return _to_df(rows)


# =============================================================================
# Лист 2: Возможное движение (другой филиал)
# =============================================================================

def _get_possible_allocation_df(session, session_id: str) -> pd.DataFrame:
    """
    Возможное покрытие из складов других филиалов.

    Эти строки НЕ являются фактическим распределением.
    Они показывают: «Если бы разрешили межфилиальный перевод —
    вот откуда можно взять и сколько это стоит».

    Используется для принятия решений о переброске ресурсов.
    """
    sql = text("""
        SELECT
            w.prioritet                     AS "Приоритет",
            w.kod_raboty                    AS "Код работы",
            w.filial                        AS "Филиал работы",
            w.zavod                         AS "Завод работы",
            w.data_okonchaniya              AS "Нужно к дате",
            m.sys_nomer                     AS "Системный номер",
            m.naimenovanie                  AS "Наименование материала",
            m.ed_izm                        AS "Ед.изм",
            wh.kod_sklada                   AS "Склад-источник",
            wh.filial                       AS "Филиал склада",
            wh.zavod                        AS "Завод склада",
            CAST(ar.kolichestvo AS NUMERIC(18,4))        AS "Возможное кол-во",
            CAST(ar.srednyaya_stoimost AS NUMERIC(18,2)) AS "Средняя цена",
            CAST(ar.summa AS NUMERIC(18,2))              AS "Оценочная сумма",
            'Требует согласования'          AS "Статус"
        FROM allocation_results ar
        JOIN works     w  ON ar.work_id     = w.id
        JOIN materials m  ON ar.material_id = m.id
        LEFT JOIN warehouses wh ON ar.warehouse_id = wh.id
        WHERE ar.session_id = :sid
          AND ar.is_possible = TRUE
          AND ar.istochnik = 'vozmozhnoe_sklad'
        ORDER BY
            w.prioritet,
            w.data_okonchaniya,
            w.kod_raboty,
            m.sys_nomer
    """)
    rows = session.execute(sql, {"sid": session_id}).fetchall()
    return _to_df(rows)


# =============================================================================
# Лист 3: Движение склада (детальное, по партиям)
# =============================================================================

def _get_movement_df(session, session_id: str) -> pd.DataFrame:
    """
    Детальное движение по складам — только фактические списания.

    Показывает цепочку: склад → работа → материал → количество → остаток.
    Одна строка = одна партия, списанная на одну работу.
    Сортировка: склад → материал → дата движения.
    """
    sql = text("""
        SELECT
            wh.kod_sklada                   AS "Склад",
            wh.filial                       AS "Филиал склада",
            wh.zavod                        AS "Завод склада",
            m.sys_nomer                     AS "Системный номер",
            m.naimenovanie                  AS "Наименование материала",
            m.ed_izm                        AS "Ед.изм",
            sb.nomer_partii                 AS "Номер партии",
            sb.data_postupleniya            AS "Дата поступления партии",
            CAST(sb.kolichestvo AS NUMERIC(18,4))  AS "Нач. кол-во партии",
            w.kod_raboty                    AS "Работа-получатель",
            w.filial                        AS "Филиал работы",
            w.prioritet                     AS "Приоритет работы",
            CAST(ABS(sm.izmenenie) AS NUMERIC(18,4)) AS "Списано",
            CAST(sm.ostatok AS NUMERIC(18,4))        AS "Остаток партии после"
        FROM stock_movements sm
        JOIN warehouses  wh ON sm.warehouse_id = wh.id
        JOIN materials    m ON sm.material_id  = m.id
        LEFT JOIN stock_batches sb ON sm.batch_id  = sb.id
        LEFT JOIN works          w ON sm.work_id   = w.id
        WHERE sm.session_id = :sid
        ORDER BY
            wh.kod_sklada,
            m.sys_nomer,
            sb.data_postupleniya,
            sm.id
    """)
    rows = session.execute(sql, {"sid": session_id}).fetchall()
    return _to_df(rows)


# =============================================================================
# Лист 4: Остатки складов после распределения
# =============================================================================

def _get_warehouse_balances_df(session, session_id: str) -> pd.DataFrame:
    """
    Остатки по складам ПОСЛЕ распределения.

    Показывает для каждого материала на каждом складе:
      • Начальный остаток (до распределения) = kolichestvo
      • Фактически распределено в этой сессии
      • Остаток после = dostupno (уже обновлено движком)

    Используется для контроля: что осталось на складах.
    """
    sql = text("""
        SELECT
            wh.kod_sklada                       AS "Склад",
            wh.filial                           AS "Филиал склада",
            wh.zavod                            AS "Завод склада",
            m.sys_nomer                         AS "Системный номер",
            m.naimenovanie                      AS "Наименование материала",
            m.ed_izm                            AS "Ед.изм",
            m.gruppa                            AS "Группа",
            sb.nomer_partii                     AS "Номер партии",
            sb.data_postupleniya                AS "Дата поступления",
            CAST(sb.kolichestvo AS NUMERIC(18,4))   AS "Нач. остаток",
            CAST(sb.kolichestvo - sb.dostupno AS NUMERIC(18,4)) AS "Распределено",
            CAST(sb.dostupno    AS NUMERIC(18,4))   AS "Текущий остаток",
            CAST(sb.stoimost_za_ed AS NUMERIC(18,2)) AS "Цена за ед.",
            CAST(sb.dostupno * sb.stoimost_za_ed AS NUMERIC(18,2)) AS "Сумма остатка"
        FROM stock_batches sb
        JOIN warehouses wh ON sb.warehouse_id = wh.id
        JOIN materials   m ON sb.material_id  = m.id
        ORDER BY
            wh.kod_sklada,
            m.sys_nomer,
            sb.data_postupleniya
    """)
    # Параметр session_id не используется — остатки глобальные, не привязаны к сессии.
    # Но метод принимает его для единообразия API.
    rows = session.execute(sql).fetchall()
    return _to_df(rows)


# =============================================================================
# Лист 5: Дефицит
# =============================================================================

def _get_deficit_df(session, session_id: str) -> pd.DataFrame:
    """
    Позиции дефицита — что нужно закупить.

    Включает только те потребности, которые не были покрыты
    ни складом, ни поставками (ни фактически, ни как возможное).
    """
    sql = text("""
        SELECT
            w.prioritet                     AS "Приоритет",
            w.kod_raboty                    AS "Код работы",
            w.filial                        AS "Филиал работы",
            w.zavod                         AS "Завод работы",
            m.sys_nomer                     AS "Системный номер",
            m.naimenovanie                  AS "Наименование материала",
            m.ed_izm                        AS "Ед.изм",
            CAST(r.potrebnost AS NUMERIC(18,4))    AS "Потребность",
            CAST(r.raspredeleno AS NUMERIC(18,4))  AS "Распределено (факт)",
            CAST(d.deficit_qty AS NUMERIC(18,4))   AS "Дефицит (к закупу)",
            d.needed_by                     AS "Нужно к дате",
            'К закупу'                      AS "Статус"
        FROM deficit_records d
        JOIN works       w ON d.work_id     = w.id
        JOIN materials   m ON d.material_id = m.id
        JOIN requirements r ON d.requirement_id = r.id
        WHERE d.session_id = :sid
        ORDER BY
            w.prioritet,
            d.needed_by,
            m.sys_nomer
    """)
    rows = session.execute(sql, {"sid": session_id}).fetchall()
    return _to_df(rows)


# =============================================================================
# Лист 6: Обеспечённость
# =============================================================================

def _get_coverage_df(session) -> pd.DataFrame:
    """
    Сводный отчёт обеспечённости каждой работы.

    Обеспечённость % = распределено / потребность × 100
    Включает как фактическое, так и возможное (для анализа).
    """
    sql = text("""
        SELECT
            w.prioritet                         AS "Приоритет",
            w.kod_raboty                        AS "Код работы",
            w.filial                            AS "Филиал",
            w.zavod                             AS "Завод",
            w.data_okonchaniya                  AS "Дата окончания",
            m.sys_nomer                         AS "Системный номер",
            m.naimenovanie                      AS "Наименование",
            m.ed_izm                            AS "Ед.изм",
            CAST(r.potrebnost   AS NUMERIC(18,4))  AS "Потребность",
            CAST(r.raspredeleno AS NUMERIC(18,4))  AS "Распределено (факт)",
            CAST(r.deficit      AS NUMERIC(18,4))  AS "Дефицит",
            CASE
                WHEN r.potrebnost > 0
                THEN ROUND(r.raspredeleno / r.potrebnost * 100, 1)
                ELSE 0
            END                                 AS "% обеспеченности",
            CASE
                WHEN r.deficit = 0              THEN 'Обеспечено'
                WHEN r.raspredeleno = 0         THEN 'Не обеспечено'
                ELSE 'Частично обеспечено'
            END                                 AS "Статус"
        FROM requirements r
        JOIN works     w ON r.work_id     = w.id
        JOIN materials m ON r.material_id = m.id
        ORDER BY
            w.prioritet,
            w.data_okonchaniya,
            w.kod_raboty,
            m.sys_nomer
    """)
    rows = session.execute(sql).fetchall()
    return _to_df(rows)


# =============================================================================
# Вспомогательные функции
# =============================================================================

def _to_df(rows) -> pd.DataFrame:
    """Преобразовать результат SQL-запроса в DataFrame."""
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows, columns=list(rows[0]._fields))


def _write_sheet(writer: pd.ExcelWriter, df: pd.DataFrame, sheet_name: str) -> None:
    """
    Записать DataFrame в лист Excel с автоподбором ширины колонок.

    Если DataFrame пустой — создаём лист с заголовком «Нет данных».
    """
    if df.empty:
        pd.DataFrame({"Информация": ["Нет данных для этой сессии"]}).to_excel(
            writer, sheet_name=sheet_name, index=False
        )
        return

    df.to_excel(writer, sheet_name=sheet_name, index=False)

    # Автоподбор ширины колонок
    ws = writer.sheets[sheet_name]
    for col_idx, col_name in enumerate(df.columns, 1):
        max_len = max(
            len(str(col_name)),
            df[col_name].astype(str).str.len().max() if not df.empty else 0,
        )
        col_width = min(max(int(max_len) + 2, 10), 65)
        ws.column_dimensions[ws.cell(1, col_idx).column_letter].width = col_width
