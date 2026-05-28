"""
app/services/export_service.py — Экспорт результатов распределения в Excel

Структура Excel-отчёта (до 11 листов):
──────────────────────────────────────────────────────────────────────────────

  Результаты распределения:

  Лист 1: «Распределение» (главная широкая таблица)
      Одна строка = одна потребность (работа + материал).
      Все слои покрытия — в виде групп колонок:
        • Работа: код, филиал, завод, дата начала/конца, приоритет, аварийная
        • Материал: системный номер, наименование, ед.изм
        • Потребность: количество, прогнозная цена, прогнозная стоимость
        • Слой 1 «Сп-е»: кол-во, цена, сумма
        • Слой 2 «Выд-но»: кол-во, цена, сумма
        • Слой 3 «Склад»: кол-во, средняя цена, сумма
        • Слой 3в «Од-е» (Одобренные перемещения): кол-во, средняя цена, сумма
        • Слой 4 «Поставки»: договор(а), поставщик(и), кол-во, цена, сумма
        • «К закупу»: кол-во, прогнозная цена, сумма
        • «Обеспечённость %»: (покрытая стоимость / прогнозная стоимость) × 100

  Лист 2: «Движение склада»
      Детальное движение: склад → партия → работа → материал → количество → остаток.

  Лист 3: «Остатки складов после распределения»
      Текущие остатки после распределения.
      Показывает: начальный остаток → распределено → остаток.

  Лист 4: «Возможное перемещение»
      Склады других филиалов, которые МОГЛИ БЫ покрыть дефицит.
      Только для анализа — фактически не распределены.

  Лист 5: «Одобренные перемещения» (условный — только если загружены)
      Перемещения, которые были реально распределены через Слой 3в.

  Лист 6: «Обеспеченность»
      Сводка по работам: сколько покрыто, сколько в дефиците, % обеспечённости.

  Лист 7: «В пути»
      Поставки, которые распределены на конкретные работы и ожидают прихода.

  Лист 8: «Не распред. в пути»
      Поставки, из которых остался нераспределённый остаток.

  Исходные данные (для аудита и сверки):

  Лист 9:  «Исх: Потребности»      — все потребности до распределения
  Лист 10: «Исх: Остатки складов»  — начальные остатки до распределения
  Лист 11: «Исх: Поставки»         — все строки поставок, загруженные в систему

  Числа: разделитель дробной части — запятая (формат «1234,56»)
  Даты:  формат ДД.ММ.ГГГГ
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


# =============================================================================
# Вспомогательные функции форматирования
# =============================================================================

def _fmt_num(val: object, decimals: int = 2) -> str:
    """
    Форматировать число с запятой как разделителем дробной части (русский формат).

    Примеры:
        _fmt_num(1234.56)        → "1234,56"
        _fmt_num(1234.5678, 4)   → "1234,5678"
        _fmt_num(None)           → ""
        _fmt_num(float('nan'))   → ""

    Зачем строка, а не число?
        Excel-файл открывается у пользователей с русской локалью.
        Записывая строку с запятой, мы гарантируем корректный вид независимо от
        настроек системы. Это принятая практика для отчётных файлов.
    """
    if val is None:
        return ""
    try:
        f = float(val)  # type: ignore[arg-type]
        if pd.isna(f):
            return ""
        return f"{f:.{decimals}f}".replace(".", ",")
    except (ValueError, TypeError):
        return ""


def _fmt_date(val: object) -> str:
    """
    Форматировать дату в формате ДД.ММ.ГГГГ.

    Принимает: date, datetime, pandas Timestamp, строку.
    Возвращает строку "28.05.2026" или "" если пусто.
    """
    if val is None:
        return ""
    try:
        if pd.isna(val):  # type: ignore[arg-type]
            return ""
    except (TypeError, ValueError):
        pass
    if hasattr(val, "strftime"):
        return val.strftime("%d.%m.%Y")  # type: ignore[union-attr]
    s = str(val).strip()
    if not s or s in ("None", "NaT", "nan"):
        return ""
    # Попытка разобрать ISO-дату → DD.MM.YYYY
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            from datetime import datetime as dt
            return dt.strptime(s, fmt).strftime("%d.%m.%Y")
        except ValueError:
            continue
    return s  # Возвращаем как есть если не смогли разобрать


def export_allocation_results(
    session_id: str,
    output_dir: Optional[str] = None,
) -> Path:
    """
    Сформировать Excel-отчёт по результатам сессии распределения.

    Args:
        session_id: ID сессии распределения (например "20260523_143055_a1b2c")
        output_dir: Папка для сохранения. По умолчанию — из settings.export_dir.

    Returns:
        Path — путь к созданному .xlsx файлу

    Пример использования:
        from app.services.export_service import export_allocation_results
        file_path = export_allocation_results("20260523_143055_a1b2c")
        print(f"Отчёт сохранён: {file_path}")
    """
    # Определяем папку для сохранения
    out_dir = Path(output_dir or settings.export_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Формируем имя файла с временной меткой для уникальности
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    file_path = out_dir / f"maps_results_{session_id}_{timestamp}.xlsx"

    logger.info("Формирование Excel-отчёта | сессия: %s", session_id)

    # Запрашиваем данные из БД (одна открытая сессия для всех запросов)
    with get_session() as session:
        # ── Результаты распределения (7 основных листов) ──
        df_main         = _get_wide_allocation_df(session, session_id)
        df_movement     = _get_movement_df(session, session_id)
        df_balances     = _get_warehouse_balances_df(session, session_id)
        df_possible     = _get_possible_movements_df(session, session_id)
        df_approved     = _get_approved_transfers_df(session, session_id)
        df_coverage     = _get_coverage_by_work_df(session, session_id)
        df_transit      = _get_in_transit_allocated_df(session, session_id)
        df_transit_free = _get_in_transit_unallocated_df(session, session_id)

        # ── Исходные данные (3 дополнительных листа) ──
        df_src_req      = _get_source_requirements_df(session)
        df_src_stock    = _get_source_stock_df(session)
        df_src_supplies = _get_source_supplies_df(session)

    # Записываем в Excel
    with pd.ExcelWriter(file_path, engine="openpyxl") as writer:
        # Результаты распределения
        _write_sheet(writer, df_main,         "Распределение")
        _write_sheet(writer, df_movement,     "Движение склада")
        _write_sheet(writer, df_balances,     "Остатки складов после распределения")
        _write_sheet(writer, df_possible,     "Возможное перемещение")
        # Лист согласованных перемещений — только если они были загружены
        if not df_approved.empty:
            _write_sheet(writer, df_approved, "Одобренные перемещения")
        _write_sheet(writer, df_coverage,     "Обеспеченность")
        _write_sheet(writer, df_transit,      "В пути")
        _write_sheet(writer, df_transit_free, "Не распред. в пути")

        # Исходные данные
        _write_sheet(writer, df_src_req,      "Исх. Потребности")
        _write_sheet(writer, df_src_stock,    "Исх. Остатки складов")
        _write_sheet(writer, df_src_supplies, "Исх. Поставки")

    logger.info(
        "Отчёт создан: %s | строк в главной таблице: %d",
        file_path, len(df_main),
    )
    return file_path


# =============================================================================
# Лист 1: Главная широкая таблица (все слои в одной строке)
# =============================================================================

def _get_wide_allocation_df(session, session_id: str) -> pd.DataFrame:
    """
    Сформировать главную широкую таблицу распределения.

    Подход:
        1. Получаем все потребности с данными работы и материала
        2. Получаем все строки распределения для сессии (кроме is_possible)
        3. Группируем в Python по (requirement_id, istochnik)
        4. Строим одну строку на каждую потребность — с колонками для каждого слоя

    Почему Python, а не SQL PIVOT?
        PostgreSQL не имеет нативного PIVOT.
        CTEs с CASE WHEN — сложно поддерживать.
        pandas groupby/merge — читаемо и гибко.

    Args:
        session:    Сессия SQLAlchemy
        session_id: ID сессии

    Returns:
        DataFrame с одной строкой на потребность и всеми слоями в колонках
    """
    # ── Шаг 1: Загружаем все потребности ──
    reqs_sql = text("""
        SELECT
            r.id                            AS requirement_id,
            r.potrebnost,
            r.prognosnaya_tsena,
            r.raspredeleno,
            r.deficit,
            w.is_emergency,
            w.prioritet,
            w.kod_raboty,
            w.filial                        AS filial_raboty,
            w.zavod                         AS zavod_raboty,
            w.data_nachala,
            w.data_okonchaniya,
            m.sys_nomer,
            m.naimenovanie,
            m.ed_izm
        FROM requirements r
        JOIN works     w ON r.work_id     = w.id
        JOIN materials m ON r.material_id = m.id
        ORDER BY
            w.is_emergency DESC,
            w.data_nachala ASC NULLS LAST,
            CASE WHEN NOT w.is_emergency THEN w.prioritet ELSE 0 END ASC,
            w.filial ASC NULLS LAST,
            w.kod_raboty ASC,
            m.sys_nomer ASC
    """)
    reqs_rows = session.execute(reqs_sql).fetchall()
    if not reqs_rows:
        return pd.DataFrame()

    # Строим DataFrame потребностей; приводим Decimal → float
    df_reqs = pd.DataFrame(reqs_rows, columns=list(reqs_rows[0]._fields))
    for _col in ["potrebnost", "prognosnaya_tsena", "raspredeleno", "deficit"]:
        if _col in df_reqs.columns:
            df_reqs[_col] = df_reqs[_col].astype(float)

    # ── Шаг 2: Загружаем все строки распределения для сессии ──
    # Включаем данные о поставке (договор, поставщик) через JOIN
    alloc_sql = text("""
        SELECT
            ar.requirement_id,
            ar.istochnik,
            ar.kolichestvo,
            ar.srednyaya_stoimost,
            ar.summa,
            s.dogovor,
            s.postavshchik
        FROM allocation_results ar
        LEFT JOIN supply_lines sl ON ar.supply_line_id = sl.id
        LEFT JOIN supplies     s  ON sl.supply_id = s.id
        WHERE ar.session_id = :sid
          AND ar.is_possible = FALSE
    """)
    alloc_rows = session.execute(alloc_sql, {"sid": session_id}).fetchall()
    df_alloc = pd.DataFrame(alloc_rows, columns=list(alloc_rows[0]._fields)) if alloc_rows else pd.DataFrame(
        columns=["requirement_id", "istochnik", "kolichestvo", "srednyaya_stoimost", "summa", "dogovor", "postavshchik"]
    )
    # PostgreSQL возвращает Numeric-колонки как Decimal — приводим к float,
    # чтобы pandas мог делать арифметику без TypeError: Decimal + float
    for _col in ["kolichestvo", "srednyaya_stoimost", "summa"]:
        if _col in df_alloc.columns:
            df_alloc[_col] = df_alloc[_col].astype(float)

    # ── Шаг 3: Агрегируем каждый слой отдельно ──

    def _agg_layer(istochnik: str) -> pd.DataFrame:
        """Агрегировать все строки одного источника по requirement_id."""
        df = df_alloc[df_alloc["istochnik"] == istochnik].copy()
        if df.empty:
            return pd.DataFrame(columns=["requirement_id", "qty", "summa"])
        agg = df.groupby("requirement_id").agg(
            qty=("kolichestvo", "sum"),
            summa=("summa", "sum"),
        ).reset_index()
        # Средняя взвешенная цена = общая сумма / общее количество
        agg["avg_price"] = agg.apply(
            lambda row: round(float(row["summa"]) / float(row["qty"]), 2)
            if row["qty"] > 0 else 0.0,
            axis=1,
        )
        return agg

    # Слой 1: Списание
    df_sp = _agg_layer("spisanie")
    # Слой 2: Выдано не списано
    df_vd = _agg_layer("vydano")
    # Слой 3: Склад (только свой завод/филиал)
    df_sk = _agg_layer("sklad")
    # Слой 3в: Одобренное межфилиальное перемещение
    df_od = _agg_layer("odobren_perenos")

    # Слой 4: Поставки — особая агрегация (нужны договора и поставщики)
    df_pv_raw = df_alloc[df_alloc["istochnik"] == "postavka"].copy()
    if not df_pv_raw.empty:
        # Агрегируем: суммируем количество и стоимость,
        # номера договоров и поставщиков объединяем через запятую (уникальные значения)
        df_pv = df_pv_raw.groupby("requirement_id").agg(
            qty=("kolichestvo", "sum"),
            summa=("summa", "sum"),
            # lambda: из списка значений берём уникальные, убираем None, соединяем ", "
            dogovora=("dogovor", lambda x: ", ".join(filter(None, x.dropna().unique().tolist()))),
            postavshchiki=("postavshchik", lambda x: ", ".join(filter(None, x.dropna().unique().tolist()))),
        ).reset_index()
        df_pv["avg_price"] = df_pv.apply(
            lambda row: round(float(row["summa"]) / float(row["qty"]), 2)
            if row["qty"] > 0 else 0.0,
            axis=1,
        )
    else:
        df_pv = pd.DataFrame(
            columns=["requirement_id", "qty", "summa", "avg_price", "dogovora", "postavshchiki"]
        )

    # ── Шаг 4: Собираем широкую таблицу через последовательные LEFT JOIN ──

    df = df_reqs.copy()

    # Прогнозная стоимость потребности = потребность × прогнозная цена
    df["prognosnaya_stoimost"] = df["potrebnost"].astype(float) * df["prognosnaya_tsena"].astype(float)

    # Merge (LEFT JOIN в pandas): соединяем потребности с данными каждого слоя
    # Если для потребности нет данных по слою — поля будут NaN (заменим на 0)

    def _merge_layer(df_main: pd.DataFrame, df_layer: pd.DataFrame, prefix: str) -> pd.DataFrame:
        """Присоединить данные слоя к основной таблице с переименованием колонок."""
        if df_layer.empty:
            # Нет данных — добавляем пустые колонки (чтобы структура всегда одинаковая)
            df_main[f"{prefix}_qty"] = 0.0
            df_main[f"{prefix}_price"] = 0.0
            df_main[f"{prefix}_summa"] = 0.0
            return df_main
        # Переименовываем колонки с префиксом
        df_layer = df_layer.rename(columns={
            "qty": f"{prefix}_qty",
            "avg_price": f"{prefix}_price",
            "summa": f"{prefix}_summa",
        })
        df_merged = df_main.merge(
            df_layer[["requirement_id", f"{prefix}_qty", f"{prefix}_price", f"{prefix}_summa"]],
            on="requirement_id",
            how="left",  # LEFT JOIN: все потребности остаются, даже без распределения
        )
        # Заменяем NaN нулями (потребность не покрыта этим слоем)
        for col in [f"{prefix}_qty", f"{prefix}_price", f"{prefix}_summa"]:
            df_merged[col] = df_merged[col].fillna(0.0)
        return df_merged

    df = _merge_layer(df, df_sp, "sp")   # Слой 1: Списание
    df = _merge_layer(df, df_vd, "vd")   # Слой 2: Выдано
    df = _merge_layer(df, df_sk, "sk")   # Слой 3: Склад (свой завод/филиал)
    df = _merge_layer(df, df_od, "od")   # Слой 3в: Одобренное перемещение

    # Слой 4: Поставки — дополнительные колонки для договора и поставщика
    if not df_pv.empty:
        df_pv_merged = df_pv.rename(columns={
            "qty": "pv_qty",
            "avg_price": "pv_price",
            "summa": "pv_summa",
        })
        df = df.merge(
            df_pv_merged[["requirement_id", "pv_qty", "pv_price", "pv_summa", "dogovora", "postavshchiki"]],
            on="requirement_id",
            how="left",
        )
        for col in ["pv_qty", "pv_price", "pv_summa"]:
            df[col] = df[col].fillna(0.0)
        df["dogovora"] = df["dogovora"].fillna("")
        df["postavshchiki"] = df["postavshchiki"].fillna("")
    else:
        df["pv_qty"] = 0.0
        df["pv_price"] = 0.0
        df["pv_summa"] = 0.0
        df["dogovora"] = ""
        df["postavshchiki"] = ""

    # ── Шаг 5: Считаем обеспечённость ПО СТОИМОСТИ ──
    # Суммарная покрытая стоимость = сумма по всем слоям 1-4 + 3в (одобренные)
    df["covered_summa"] = (
        df["sp_summa"] + df["vd_summa"] + df["sk_summa"]
        + df["od_summa"] + df["pv_summa"]
    )

    # К закупу: количество = дефицит, стоимость = дефицит × прогнозная цена
    df["zakup_qty"] = df["deficit"].astype(float)
    df["zakup_price"] = df["prognosnaya_tsena"].astype(float)
    df["zakup_summa"] = df["zakup_qty"] * df["zakup_price"]

    # % обеспечённости по стоимости
    # Если прогнозная стоимость > 0 — считаем процент
    # Если = 0 (прогнозная цена не указана) — показываем по количеству
    df["coverage_pct"] = df.apply(
        lambda row: (
            round(row["covered_summa"] / row["prognosnaya_stoimost"] * 100, 1)
            if row["prognosnaya_stoimost"] > 0
            else (
                round(row["raspredeleno"] / row["potrebnost"] * 100, 1)
                if float(row["potrebnost"]) > 0 else 0.0
            )
        ),
        axis=1,
    )

    # ── Шаг 6: Формируем итоговую таблицу с понятными заголовками ──
    # Числа форматируем с запятой как разделителем дробной части (русский формат).
    # Даты — в формате ДД.ММ.ГГГГ.
    result = pd.DataFrame({
        # Признак аварийности
        "Аварийная": df["is_emergency"].apply(lambda x: "Да" if x else ""),

        # Данные работы
        "Приоритет":        df["prioritet"],
        "Код работы":       df["kod_raboty"],
        "Филиал":           df["filial_raboty"],
        "Завод":            df["zavod_raboty"],
        "Дата начала":      df["data_nachala"].apply(_fmt_date),
        "Дата окончания":   df["data_okonchaniya"].apply(_fmt_date),

        # Данные материала
        "Системный номер":  df["sys_nomer"],
        "Наименование":     df["naimenovanie"],
        "Ед.изм":           df["ed_izm"],

        # Потребность
        "Потребность":          df["potrebnost"].apply(lambda v: _fmt_num(v, 4)),
        "Прогнозная цена":      df["prognosnaya_tsena"].apply(lambda v: _fmt_num(v, 2)),
        "Прогнозная стоимость": df["prognosnaya_stoimost"].apply(lambda v: _fmt_num(v, 2)),

        # Слой 1: Списание
        "Сп: Кол-во":       df["sp_qty"].apply(lambda v: _fmt_num(v, 4)),
        "Сп: Цена":         df["sp_price"].apply(lambda v: _fmt_num(v, 2)),
        "Сп: Сумма":        df["sp_summa"].apply(lambda v: _fmt_num(v, 2)),

        # Слой 2: Выдано не списано
        "Вд: Кол-во":       df["vd_qty"].apply(lambda v: _fmt_num(v, 4)),
        "Вд: Цена":         df["vd_price"].apply(lambda v: _fmt_num(v, 2)),
        "Вд: Сумма":        df["vd_summa"].apply(lambda v: _fmt_num(v, 2)),

        # Слой 3: Склад своего завода/филиала
        "Сл: Кол-во":       df["sk_qty"].apply(lambda v: _fmt_num(v, 4)),
        "Сл: Ср.цена":      df["sk_price"].apply(lambda v: _fmt_num(v, 2)),
        "Сл: Сумма":        df["sk_summa"].apply(lambda v: _fmt_num(v, 2)),

        # Слой 3в: Одобренные межфилиальные перемещения
        "Од: Кол-во":       df["od_qty"].apply(lambda v: _fmt_num(v, 4)),
        "Од: Ср.цена":      df["od_price"].apply(lambda v: _fmt_num(v, 2)),
        "Од: Сумма":        df["od_summa"].apply(lambda v: _fmt_num(v, 2)),

        # Слой 4: Поставки
        "Пс: Договор(а)":   df["dogovora"],
        "Пс: Поставщик(и)": df["postavshchiki"],
        "Пс: Кол-во":       df["pv_qty"].apply(lambda v: _fmt_num(v, 4)),
        "Пс: Ср.цена":      df["pv_price"].apply(lambda v: _fmt_num(v, 2)),
        "Пс: Сумма":        df["pv_summa"].apply(lambda v: _fmt_num(v, 2)),

        # К закупу
        "Закуп: Кол-во":    df["zakup_qty"].apply(lambda v: _fmt_num(v, 4)),
        "Закуп: Цена":      df["zakup_price"].apply(lambda v: _fmt_num(v, 2)),
        "Закуп: Сумма":     df["zakup_summa"].apply(lambda v: _fmt_num(v, 2)),

        # Итоговый показатель
        "Обеспечённость %": df["coverage_pct"].apply(lambda v: _fmt_num(v, 1)),
    })

    return result


# =============================================================================
# Лист 2: Движение склада (детальное, по партиям)
# =============================================================================

def _get_movement_df(session, session_id: str) -> pd.DataFrame:
    """
    Детальное движение по складам — каждое списание с партии на работу.

    Показывает цепочку:
        Склад W-001 → Партия 0045 → Работа WO-123 → Материал 00010012345 → 50 м → Остаток: 150 м

    Используется для:
        - Аудита: кто, что, сколько взял
        - Сверки с SAP
        - Анализа движения конкретных партий

    Одна строка = одно списание с одной партии на одну работу.
    """
    sql = text("""
        SELECT
            wh.kod_sklada                           AS "Склад",
            wh.filial                               AS "Филиал склада",
            wh.zavod                                AS "Завод склада",
            m.sys_nomer                             AS "Системный номер",
            m.naimenovanie                          AS "Наименование материала",
            m.ed_izm                                AS "Ед.изм",
            sb.nomer_partii                         AS "Номер партии",
            sb.data_postupleniya                    AS "Дата поступления партии",
            CAST(sb.kolichestvo AS NUMERIC(18,4))   AS "Нач. кол-во партии",
            CAST(sb.stoimost_za_ed AS NUMERIC(18,2)) AS "Цена за ед.",
            w.kod_raboty                            AS "Работа-получатель",
            w.filial                                AS "Филиал работы",
            w.zavod                                 AS "Завод работы",
            w.prioritet                             AS "Приоритет работы",
            w.is_emergency                          AS "Аварийная работа",
            CAST(ABS(sm.izmenenie) AS NUMERIC(18,4)) AS "Списано",
            CAST(sm.ostatok AS NUMERIC(18,4))        AS "Остаток партии после",
            sm.data_dvizheniya                      AS "Дата движения"
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
    df = _to_df(rows)
    if df.empty:
        return df
    # Форматируем даты и числа
    for col in ["Дата поступления партии", "Дата движения"]:
        if col in df.columns:
            df[col] = df[col].apply(_fmt_date)
    for col in ["Нач. кол-во партии", "Цена за ед.", "Списано", "Остаток партии после"]:
        if col in df.columns:
            decimals = 4 if "кол-во" in col.lower() or "списано" in col.lower() or "остаток" in col.lower() else 2
            df[col] = df[col].apply(lambda v, d=decimals: _fmt_num(v, d))
    return df


# =============================================================================
# Лист 3: Остатки складов после распределения
# =============================================================================

def _get_warehouse_balances_df(session, _session_id: str = "") -> pd.DataFrame:
    """
    Текущие остатки по складам ПОСЛЕ распределения.

    Показывает для каждой партии:
        • Начальный остаток (kolichestvo — не менялся при импорте)
        • Распределено в этой сессии = kolichestvo - dostupno
        • Текущий остаток (dostupno — обновлён алгоритмом)
        • Сумма остатка = dostupno × stoimost_za_ed

    Используется для контроля: что осталось на складах после плана распределения.
    """
    sql = text("""
        SELECT
            wh.kod_sklada                           AS "Склад",
            wh.filial                               AS "Филиал склада",
            wh.zavod                                AS "Завод склада",
            m.sys_nomer                             AS "Системный номер",
            m.naimenovanie                          AS "Наименование материала",
            m.ed_izm                                AS "Ед.изм",
            m.gruppa                                AS "Группа",
            sb.nomer_partii                         AS "Номер партии",
            sb.data_postupleniya                    AS "Дата поступления",
            CAST(sb.kolichestvo AS NUMERIC(18,4))   AS "Нач. остаток",
            CAST(sb.kolichestvo - sb.dostupno AS NUMERIC(18,4)) AS "Распределено",
            CAST(sb.dostupno AS NUMERIC(18,4))      AS "Текущий остаток",
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
    # Остатки глобальные (не привязаны к сессии) — они уже обновлены алгоритмом
    rows = session.execute(sql).fetchall()
    df = _to_df(rows)
    if df.empty:
        return df
    if "Дата поступления" in df.columns:
        df["Дата поступления"] = df["Дата поступления"].apply(_fmt_date)
    for col in ["Нач. остаток", "Распределено", "Текущий остаток"]:
        if col in df.columns:
            df[col] = df[col].apply(lambda v: _fmt_num(v, 4))
    for col in ["Цена за ед.", "Сумма остатка"]:
        if col in df.columns:
            df[col] = df[col].apply(lambda v: _fmt_num(v, 2))
    return df


# =============================================================================
# Лист 4: Возможное перемещение из других филиалов
# =============================================================================

def _get_possible_movements_df(session, session_id: str) -> pd.DataFrame:
    """
    Возможное покрытие дефицита из складов других филиалов.

    Что показывает этот лист?
        Для каждой работы, у которой не хватает материала:
        «На складе в Алматы есть 200 м кабеля, который МОГ БЫ покрыть
         работу в Астане — нужно согласование на межфилиальный перенос».

    Важно:
        • is_possible=TRUE — это не фактическое распределение
        • В расчёте дефицита эти данные НЕ учитываются
        • Используется руководством для принятия решений о переброске ресурсов
        • Склад другого филиала не видит изменений в остатках
    """
    sql = text("""
        SELECT
            w.prioritet                             AS "Приоритет",
            w.is_emergency                          AS "Аварийная",
            w.kod_raboty                            AS "Код работы",
            w.filial                                AS "Филиал работы",
            w.zavod                                 AS "Завод работы",
            w.data_nachala                          AS "Дата начала",
            w.data_okonchaniya                      AS "Нужно к дате",
            m.sys_nomer                             AS "Системный номер",
            m.naimenovanie                          AS "Наименование материала",
            m.ed_izm                                AS "Ед.изм",
            wh.kod_sklada                           AS "Склад-источник",
            wh.filial                               AS "Филиал склада",
            wh.zavod                                AS "Завод склада",
            CAST(ar.kolichestvo AS NUMERIC(18,4))        AS "Возможное кол-во",
            CAST(ar.srednyaya_stoimost AS NUMERIC(18,2)) AS "Средняя цена",
            CAST(ar.summa AS NUMERIC(18,2))              AS "Оценочная сумма",
            'Требует согласования'                  AS "Статус"
        FROM allocation_results ar
        JOIN works     w  ON ar.work_id     = w.id
        JOIN materials m  ON ar.material_id = m.id
        LEFT JOIN warehouses wh ON ar.warehouse_id = wh.id
        WHERE ar.session_id = :sid
          AND ar.is_possible = TRUE
          AND ar.istochnik = 'vozmozhnoe_sklad'
        ORDER BY
            w.is_emergency DESC,
            w.data_nachala ASC NULLS LAST,
            CASE WHEN NOT w.is_emergency THEN w.prioritet ELSE 0 END ASC,
            w.kod_raboty,
            m.sys_nomer
    """)
    rows = session.execute(sql, {"sid": session_id}).fetchall()
    df = _to_df(rows)
    if df.empty:
        return df
    for col in ["Дата начала", "Нужно к дате"]:
        if col in df.columns:
            df[col] = df[col].apply(_fmt_date)
    for col in ["Возможное кол-во"]:
        if col in df.columns:
            df[col] = df[col].apply(lambda v: _fmt_num(v, 4))
    for col in ["Средняя цена", "Оценочная сумма"]:
        if col in df.columns:
            df[col] = df[col].apply(lambda v: _fmt_num(v, 2))
    return df


# =============================================================================
# Лист «Одобренные перемещения» (результат Layer 3в)
# =============================================================================

def _get_approved_transfers_df(session, session_id: str) -> pd.DataFrame:
    """
    Список согласованных перемещений, которые были использованы в распределении.

    Показывает: что именно было взято с чужих складов по одобренным переброскам.
    Если таблица approved_transfers пуста (пользователь не загружал файл) — лист не создаётся.
    """
    sql = text("""
        SELECT
            w.prioritet                                  AS "Приоритет",
            w.is_emergency                               AS "Аварийная",
            w.kod_raboty                                 AS "Код работы",
            w.filial                                     AS "Филиал работы",
            w.zavod                                      AS "Завод работы",
            w.data_nachala                               AS "Дата начала",
            w.data_okonchaniya                           AS "Нужно к дате",
            m.sys_nomer                                  AS "Системный номер",
            m.naimenovanie                               AS "Наименование материала",
            m.ed_izm                                     AS "Ед.изм",
            wh.kod_sklada                                AS "Склад-источник",
            wh.filial                                    AS "Филиал склада",
            wh.zavod                                     AS "Завод склада",
            CAST(ar.kolichestvo AS NUMERIC(18,4))        AS "Взято кол-во",
            CAST(ar.srednyaya_stoimost AS NUMERIC(18,2)) AS "Средняя цена",
            CAST(ar.summa AS NUMERIC(18,2))              AS "Сумма",
            'Одобрено руководством'                      AS "Статус"
        FROM allocation_results ar
        JOIN works     w  ON ar.work_id     = w.id
        JOIN materials m  ON ar.material_id = m.id
        LEFT JOIN warehouses wh ON ar.warehouse_id = wh.id
        WHERE ar.session_id = :sid
          AND ar.istochnik  = 'odobren_perenos'
        ORDER BY
            w.is_emergency DESC,
            w.data_nachala ASC NULLS LAST,
            w.kod_raboty,
            m.sys_nomer
    """)
    rows = session.execute(sql, {"sid": session_id}).fetchall()
    df = _to_df(rows)
    if df.empty:
        return df
    for col in ["Дата начала", "Нужно к дате"]:
        if col in df.columns:
            df[col] = df[col].apply(_fmt_date)
    for col in ["Взято кол-во"]:
        if col in df.columns:
            df[col] = df[col].apply(lambda v: _fmt_num(v, 4))
    for col in ["Средняя цена", "Сумма"]:
        if col in df.columns:
            df[col] = df[col].apply(lambda v: _fmt_num(v, 2))
    return df


# =============================================================================
# Лист 5: Обеспеченность по работам (сводная таблица)
# =============================================================================

def _get_coverage_by_work_df(session, session_id: str) -> pd.DataFrame:
    """
    Сформировать сводную таблицу обеспеченности по работам.

    Что показывает этот лист?
        По каждой работе (не по материалу!): насколько она обеспечена
        материалами в целом (по стоимости).

        Пример строки:
            WO-12345 | Ремонт насоса | Алматы | 3 матер. | 5 млн | 3.2 млн | 1.8 млн | 64%

    Формула обеспеченности % (ПО СТОИМОСТИ):
        % = стоимость_обеспечено / стоимость_потребности × 100
        Где стоимость_обеспечено = сумма всех НЕ-возможных (is_possible=FALSE) распределений.
        Если стоимость потребности = 0 (прогнозные цены не заданы) → 0%.

    Как считается стоимость потребности?
        SUM(r.potrebnost × r.prognosnaya_tsena)
        Суммируем по всем потребностям данной работы.

    Как считается стоимость обеспечено?
        Сумма ar.summa по allocation_results этой работы/сессии (is_possible=FALSE).
        Включает слои 1 (списание), 2 (выдано), 3 (склад), 4 (поставки).
        НЕ включает слой 5 (дефицит) и возможное перемещение (is_possible=TRUE).

    Как считается стоимость дефицита?
        SUM(dr.estimated_cost) из deficit_records.
        estimated_cost = deficit_qty × prognosnaya_tsena.

    Args:
        session:    Сессия SQLAlchemy
        session_id: ID сессии распределения

    Returns:
        DataFrame с одной строкой на работу (все работы, у которых есть потребности)
    """
    sql = text("""
        SELECT
            w.kod_raboty,
            w.nazvanie,
            w.filial,
            w.zavod,
            w.data_nachala,
            w.data_okonchaniya,
            w.is_emergency,
            COUNT(r.id)                                         AS materials_count,
            COALESCE(SUM(r.potrebnost * r.prognosnaya_tsena), 0) AS stoimost_potrebnosti,
            COALESCE((
                SELECT SUM(ar.summa)
                FROM allocation_results ar
                WHERE ar.work_id = w.id
                  AND ar.session_id = :sid
                  AND ar.is_possible = FALSE
            ), 0)                                               AS stoimost_obespecheno,
            COALESCE((
                SELECT SUM(dr.estimated_cost)
                FROM deficit_records dr
                WHERE dr.work_id = w.id
                  AND dr.session_id = :sid
            ), 0)                                               AS stoimost_deficita
        FROM works w
        JOIN requirements r ON r.work_id = w.id
        GROUP BY w.id, w.kod_raboty, w.nazvanie, w.filial, w.zavod,
                 w.data_nachala, w.data_okonchaniya, w.is_emergency
        ORDER BY w.data_nachala ASC NULLS LAST, w.kod_raboty
    """)

    rows = session.execute(sql, {"sid": session_id}).fetchall()

    if not rows:
        # Нет потребностей — возвращаем пустой DataFrame
        return pd.DataFrame()

    # Собираем DataFrame из сырых данных
    df = pd.DataFrame(rows, columns=list(rows[0]._fields))

    # Приводим числовые колонки к float (PostgreSQL возвращает Decimal)
    for col in ["stoimost_potrebnosti", "stoimost_obespecheno", "stoimost_deficita"]:
        df[col] = df[col].astype(float)

    # --- Считаем % обеспеченности ---
    # Если стоимость потребности > 0 — считаем процент по стоимости.
    # Если = 0 (прогнозные цены не заданы) — процент равен 0.
    df["coverage_pct"] = df.apply(
        lambda row: (
            round(float(row["stoimost_obespecheno"]) / float(row["stoimost_potrebnosti"]) * 100, 1)
            if float(row["stoimost_potrebnosti"]) > 0
            else 0.0
        ),
        axis=1,
    )

    # --- Формируем итоговую таблицу с русскими заголовками ---
    result = pd.DataFrame({
        "Код работы":           df["kod_raboty"],
        "Наименование работы":  df["nazvanie"].fillna(""),
        "Филиал":               df["filial"].fillna(""),
        "Завод":                df["zavod"].fillna(""),
        "Дата начала":          df["data_nachala"].apply(_fmt_date),
        "Дата окончания":       df["data_okonchaniya"].apply(_fmt_date),
        "Аварийная":            df["is_emergency"].apply(lambda x: "Да" if x else "Нет"),
        "Материалов":           df["materials_count"].astype(int),
        "Стоимость потребности": df["stoimost_potrebnosti"].apply(lambda v: _fmt_num(v, 2)),
        "Стоимость обеспечено":  df["stoimost_obespecheno"].apply(lambda v: _fmt_num(v, 2)),
        "Стоимость дефицита":    df["stoimost_deficita"].apply(lambda v: _fmt_num(v, 2)),
        "Обеспеченность %":      df["coverage_pct"].apply(lambda v: _fmt_num(v, 1)),
    })

    return result


# =============================================================================
# Лист 6: В пути — распределённые поставки
# =============================================================================

def _get_in_transit_allocated_df(session, session_id: str) -> pd.DataFrame:
    """
    Строки поставок («в пути»), которые были распределены на потребности
    в данной сессии (allocation_results WHERE istochnik='postavka').

    Показывает: кому, что, сколько и по какому договору распределено из поставок.
    """
    sql = text("""
        SELECT
            s.dogovor                           AS "Договор",
            s.postavshchik                      AS "Поставщик",
            s.data_postavki                     AS "Дата поставки",
            s.filial                            AS "Филиал поставки",
            s.zavod                             AS "Завод поставки",
            w.kod_raboty                        AS "Код работы",
            COALESCE(w.nazvanie, '')            AS "Наименование работы",
            m.sys_nomer                         AS "Системный номер",
            m.naimenovanie                      AS "Наименование материала",
            m.ed_izm                            AS "Ед.изм",
            ar.kolichestvo                      AS "Кол-во распределено",
            ar.srednyaya_stoimost               AS "Стоимость за ед",
            ar.summa                            AS "Сумма"
        FROM allocation_results ar
        JOIN supply_lines sl  ON sl.id  = ar.supply_line_id
        JOIN supplies s       ON s.id   = sl.supply_id
        JOIN requirements r   ON r.id   = ar.requirement_id
        JOIN works w          ON w.id   = r.work_id
        JOIN materials m      ON m.id   = ar.material_id
        WHERE ar.session_id   = :sid
          AND ar.istochnik    = 'postavka'
        ORDER BY s.data_postavki ASC NULLS LAST,
                 s.dogovor,
                 w.kod_raboty,
                 m.sys_nomer
    """)
    rows = session.execute(sql, {"sid": session_id}).fetchall()
    if not rows:
        return pd.DataFrame(columns=[
            "Договор", "Поставщик", "Дата поставки", "Филиал поставки", "Завод поставки",
            "Код работы", "Наименование работы", "Системный номер", "Наименование материала",
            "Ед.изм", "Кол-во распределено", "Стоимость за ед", "Сумма",
        ])
    df = pd.DataFrame(rows, columns=list(rows[0]._fields))
    if "Дата поставки" in df.columns:
        df["Дата поставки"] = df["Дата поставки"].apply(_fmt_date)
    if "Кол-во распределено" in df.columns:
        df["Кол-во распределено"] = df["Кол-во распределено"].apply(lambda v: _fmt_num(v, 4))
    for col in ["Стоимость за ед", "Сумма"]:
        if col in df.columns:
            df[col] = df[col].apply(lambda v: _fmt_num(v, 2))
    return df


# =============================================================================
# Лист 7: Не распределено в пути
# =============================================================================

def _get_in_transit_unallocated_df(session, _session_id: str = "") -> pd.DataFrame:
    """
    Строки поставок, у которых после распределения остался остаток dostupno > 0.

    Это материалы «в пути», которые никуда (или не полностью) не ушли.
    Помогает понять: что ещё можно использовать, и нет ли лишних закупок.

    Поле «Распределено» = kolichestvo - dostupno (сколько уже взяли из этой строки).
    """
    sql = text("""
        SELECT
            s.dogovor                           AS "Договор",
            s.postavshchik                      AS "Поставщик",
            s.data_postavki                     AS "Дата поставки",
            s.filial                            AS "Филиал",
            s.zavod                             AS "Завод",
            m.sys_nomer                         AS "Системный номер",
            m.naimenovanie                      AS "Наименование материала",
            m.ed_izm                            AS "Ед.изм",
            sl.kolichestvo                      AS "Кол-во всего",
            (sl.kolichestvo - sl.dostupno)      AS "Распределено",
            sl.dostupno                         AS "Остаток",
            sl.stoimost_za_ed                   AS "Стоимость за ед",
            (sl.dostupno * sl.stoimost_za_ed)   AS "Сумма остатка"
        FROM supply_lines sl
        JOIN supplies s   ON s.id  = sl.supply_id
        JOIN materials m  ON m.id  = sl.material_id
        WHERE sl.dostupno > 0
        ORDER BY s.data_postavki ASC NULLS LAST,
                 s.dogovor,
                 m.sys_nomer
    """)
    rows = session.execute(sql).fetchall()
    if not rows:
        return pd.DataFrame(columns=[
            "Договор", "Поставщик", "Дата поставки", "Филиал", "Завод",
            "Системный номер", "Наименование материала", "Ед.изм",
            "Кол-во всего", "Распределено", "Остаток", "Стоимость за ед", "Сумма остатка",
        ])
    df = pd.DataFrame(rows, columns=list(rows[0]._fields))
    if "Дата поставки" in df.columns:
        df["Дата поставки"] = df["Дата поставки"].apply(_fmt_date)
    for col in ["Кол-во всего", "Распределено", "Остаток"]:
        if col in df.columns:
            df[col] = df[col].apply(lambda v: _fmt_num(v, 4))
    for col in ["Стоимость за ед", "Сумма остатка"]:
        if col in df.columns:
            df[col] = df[col].apply(lambda v: _fmt_num(v, 2))
    return df


# =============================================================================
# Листы с исходными данными (для сверки и аудита)
# =============================================================================

def _get_source_requirements_df(session) -> pd.DataFrame:
    """
    Лист «Исх: Потребности» — все потребности до распределения.

    Зачем нужен этот лист?
        Позволяет сверить, что именно было загружено в систему:
        какие работы, какие материалы, какое количество.
        Помогает при аудите: «откуда взялась эта потребность?»
    """
    sql = text("""
        SELECT
            w.is_emergency                               AS "Аварийная",
            w.prioritet                                  AS "Приоритет",
            w.kod_raboty                                 AS "Код работы",
            COALESCE(w.nazvanie, '')                     AS "Наименование работы",
            w.filial                                     AS "Филиал",
            w.zavod                                      AS "Завод",
            w.data_nachala                               AS "Дата начала",
            w.data_okonchaniya                           AS "Дата окончания",
            w.status                                     AS "Статус работы",
            m.sys_nomer                                  AS "Системный номер",
            m.naimenovanie                               AS "Наименование материала",
            m.ed_izm                                     AS "Ед.изм",
            CAST(r.potrebnost AS NUMERIC(18,4))          AS "Потребность",
            CAST(r.prognosnaya_tsena AS NUMERIC(18,2))   AS "Прогнозная цена",
            CAST(r.potrebnost * r.prognosnaya_tsena AS NUMERIC(18,2)) AS "Прогнозная стоимость"
        FROM requirements r
        JOIN works     w ON r.work_id     = w.id
        JOIN materials m ON r.material_id = m.id
        ORDER BY
            w.is_emergency DESC,
            w.data_nachala ASC NULLS LAST,
            w.prioritet ASC,
            w.kod_raboty,
            m.sys_nomer
    """)
    rows = session.execute(sql).fetchall()
    df = _to_df(rows)
    if df.empty:
        return df
    df["Аварийная"] = df["Аварийная"].apply(lambda x: "Да" if x else "")
    for col in ["Дата начала", "Дата окончания"]:
        if col in df.columns:
            df[col] = df[col].apply(_fmt_date)
    df["Потребность"] = df["Потребность"].apply(lambda v: _fmt_num(v, 4))
    df["Прогнозная цена"] = df["Прогнозная цена"].apply(lambda v: _fmt_num(v, 2))
    df["Прогнозная стоимость"] = df["Прогнозная стоимость"].apply(lambda v: _fmt_num(v, 2))
    return df


def _get_source_stock_df(session) -> pd.DataFrame:
    """
    Лист «Исх: Остатки складов» — начальные остатки до распределения.

    Показывает kolichestvo (полный исходный остаток, не изменяется алгоритмом).
    В отличие от листа «Остатки складов после распределения», здесь нет уменьшений.
    """
    sql = text("""
        SELECT
            wh.kod_sklada                               AS "Склад",
            wh.filial                                   AS "Филиал склада",
            wh.zavod                                    AS "Завод склада",
            m.sys_nomer                                 AS "Системный номер",
            m.naimenovanie                              AS "Наименование материала",
            m.ed_izm                                    AS "Ед.изм",
            m.gruppa                                    AS "Группа",
            sb.nomer_partii                             AS "Номер партии",
            sb.data_postupleniya                        AS "Дата поступления",
            CAST(sb.kolichestvo AS NUMERIC(18,4))       AS "Кол-во (исходное)",
            CAST(sb.stoimost_za_ed AS NUMERIC(18,2))    AS "Цена за ед.",
            CAST(sb.kolichestvo * sb.stoimost_za_ed AS NUMERIC(18,2)) AS "Сумма"
        FROM stock_batches sb
        JOIN warehouses wh ON sb.warehouse_id = wh.id
        JOIN materials   m ON sb.material_id  = m.id
        ORDER BY
            wh.kod_sklada,
            m.sys_nomer,
            sb.data_postupleniya
    """)
    rows = session.execute(sql).fetchall()
    df = _to_df(rows)
    if df.empty:
        return df
    if "Дата поступления" in df.columns:
        df["Дата поступления"] = df["Дата поступления"].apply(_fmt_date)
    df["Кол-во (исходное)"] = df["Кол-во (исходное)"].apply(lambda v: _fmt_num(v, 4))
    df["Цена за ед."] = df["Цена за ед."].apply(lambda v: _fmt_num(v, 2))
    df["Сумма"] = df["Сумма"].apply(lambda v: _fmt_num(v, 2))
    return df


def _get_source_supplies_df(session) -> pd.DataFrame:
    """
    Лист «Исх: Поставки» — все строки поставок, загруженные в систему.

    Показывает полный реестр материалов «в пути» по договорам.
    """
    sql = text("""
        SELECT
            s.dogovor                                   AS "Договор",
            s.postavshchik                              AS "Поставщик",
            s.data_postavki                             AS "Дата поставки",
            s.filial                                    AS "Филиал",
            s.zavod                                     AS "Завод",
            s.status                                    AS "Статус поставки",
            m.sys_nomer                                 AS "Системный номер",
            m.naimenovanie                              AS "Наименование материала",
            m.ed_izm                                    AS "Ед.изм",
            CAST(sl.kolichestvo AS NUMERIC(18,4))       AS "Кол-во",
            CAST(sl.stoimost_za_ed AS NUMERIC(18,2))    AS "Цена за ед.",
            CAST(sl.kolichestvo * sl.stoimost_za_ed AS NUMERIC(18,2)) AS "Сумма"
        FROM supply_lines sl
        JOIN supplies  s ON s.id = sl.supply_id
        JOIN materials m ON m.id = sl.material_id
        ORDER BY
            s.data_postavki ASC NULLS LAST,
            s.dogovor,
            m.sys_nomer
    """)
    rows = session.execute(sql).fetchall()
    df = _to_df(rows)
    if df.empty:
        return df
    if "Дата поставки" in df.columns:
        df["Дата поставки"] = df["Дата поставки"].apply(_fmt_date)
    df["Кол-во"] = df["Кол-во"].apply(lambda v: _fmt_num(v, 4))
    df["Цена за ед."] = df["Цена за ед."].apply(lambda v: _fmt_num(v, 2))
    df["Сумма"] = df["Сумма"].apply(lambda v: _fmt_num(v, 2))
    return df


# =============================================================================
# Вспомогательные функции
# =============================================================================

def _to_df(rows) -> pd.DataFrame:
    """
    Преобразовать результат SQL-запроса (список Row-объектов) в DataFrame.

    Зачем нужна эта функция?
        SQLAlchemy возвращает список объектов Row.
        pandas.DataFrame не умеет напрямую читать Row-объекты.
        Эта функция выступает адаптером: Row._fields → имена колонок.

    Args:
        rows: Результат session.execute(...).fetchall()

    Returns:
        pandas.DataFrame или пустой DataFrame если rows пустой
    """
    if not rows:
        return pd.DataFrame()
    # rows[0]._fields содержит кортеж имён колонок из SQL-запроса
    return pd.DataFrame(rows, columns=list(rows[0]._fields))


def _write_sheet(
    writer: pd.ExcelWriter,
    df: pd.DataFrame,
    sheet_name: str,
) -> None:
    """
    Записать DataFrame в лист Excel с форматированием.

    Что делает функция:
        1. Если DataFrame пустой — создаёт лист с сообщением «Нет данных»
        2. Если есть данные — записывает в Excel
        3. Автоматически подбирает ширину каждой колонки

    Автоподбор ширины:
        Берём максимальную длину строки в колонке (включая заголовок).
        Ограничиваем диапазоном [10, 65] символов — не слишком узко и не слишком широко.

    Args:
        writer:     Открытый pd.ExcelWriter (файл Excel)
        df:         Данные для записи
        sheet_name: Название листа в Excel
    """
    if df.empty:
        # Создаём лист с информационным сообщением
        pd.DataFrame({"Информация": ["Нет данных для этой сессии"]}).to_excel(
            writer, sheet_name=sheet_name, index=False
        )
        return

    # Записываем данные без индекса (индекс — это 0, 1, 2... — не нужен в отчёте)
    df.to_excel(writer, sheet_name=sheet_name, index=False)

    # Автоподбор ширины колонок через openpyxl API
    ws = writer.sheets[sheet_name]
    for col_idx, col_name in enumerate(df.columns, 1):
        # Максимальная длина: заголовок vs. самое длинное значение в колонке
        max_len = max(
            len(str(col_name)),
            df[col_name].astype(str).str.len().max() if not df.empty else 0,
        )
        # Ограничиваем разумным диапазоном: не уже 10 и не шире 65 символов
        col_width = min(max(int(max_len) + 2, 10), 65)
        # Устанавливаем ширину через letter (A, B, C...) из номера колонки
        ws.column_dimensions[ws.cell(1, col_idx).column_letter].width = col_width
