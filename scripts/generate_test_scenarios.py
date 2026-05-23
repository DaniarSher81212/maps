"""
scripts/generate_test_scenarios.py — Сценарные тестовые данные для MAPS

Создаёт 6 Excel-файлов с детерминированными данными (без random).
Каждый сценарий проверяет конкретное поведение алгоритма.

ОЖИДАЕМЫЕ РЕЗУЛЬТАТЫ (для проверки после прогона):

  SCN-01  Полное покрытие через списание (Слой 1)
          → Сп:100, Вд:0, Сл:0, Пс:0, Дефицит:0, %:100

  SCN-02  Частичное списание + остаток со склада (Слой 1 + Слой 3)
          → Сп:40, Сл:60, Дефицит:0, %:100

  SCN-03  Только выдано не списано (Слой 2)
          → Вд:20, Дефицит:0, %:100

  SCN-04  Все 4 слоя + дефицит
          → Сп:15, Вд:10, Сл:20, Пс:25, Дефицит:30

  SCN-05  Полный дефицит — ничего нет нигде
          → Дефицит:50, %:0

  SCN-06  Аварийная (AV) забирает материал у плановой (WO)
          → AV получает 80м, WO — дефицит 80м

  SCN-07  FIFO — старая партия уходит первой
          → Партия A (2025-01-01) уйдёт раньше партии B (2026-01-01)
          → Средняя цена = (30×3000 + 20×3800) / 50 = 3320.00

  SCN-08  Поставка из другого филиала не учитывается
          → Поставка в Алматы (KZ01) НЕ покрывает работу в Астане (KZ02)
          → Поставка в Астане (KZ02) покрывает 30шт

  SCN-09  Возможное перемещение из другого филиала
          → Дефицит:50, в листе «Возможное перемещение» — склад SKL-ALM-01 (Алматы)

  SCN-10  Прогнозная цена = 0 → обеспечённость по количеству
          → Склад покрывает 60м из 100м → % = 60.0 (по кол-ву, не по стоимости)

  SCN-11  Несколько договоров на один материал
          → Пс: 100м, Договор(а) = «ДОГ-ТЕСТ-А, ДОГ-ТЕСТ-Б, ДОГ-ТЕСТ-В»

  SCN-12  Приоритет 1 забирает ресурс у приоритета 3 (одинаковая дата)
          → P1 получает 50 мешков, P3 — дефицит 50 мешков

Запуск:
    python scripts/generate_test_scenarios.py

Файлы создаются в папке data/test/
"""

from datetime import date, timedelta
from pathlib import Path

import pandas as pd

# Папка для тестовых файлов
TEST_DIR = Path("data/test")
TEST_DIR.mkdir(parents=True, exist_ok=True)

# Сегодняшняя дата — используется как опорная точка для дат работ
TODAY = date.today()
TOMORROW   = TODAY + timedelta(days=1)
IN_30_DAYS = TODAY + timedelta(days=30)
IN_60_DAYS = TODAY + timedelta(days=60)

# =============================================================================
# Справочник материалов
# Используем короткие sys_nomer для читаемости в Excel
# =============================================================================
# sys_nomer → (naimenovanie, ed_izm, prognosnaya_tsena)
MATERIALS = {
    "MAT-001": ("Кабель ВВГ 3х2,5 мм",        "м",     1200.0),
    "MAT-002": ("Труба стальная 57х3,5 мм",     "м",     3500.0),
    "MAT-003": ("Автомат. выключатель АВ 25А",   "шт",    8500.0),
    "MAT-004": ("Клапан шаровой 1 дюйм",         "шт",    5500.0),
    "MAT-005": ("Цемент М400 в мешках 50кг",     "мешок", 3200.0),
}

# Вспомогательная функция — строит словарь строки потребности
def req_row(kod, mat_id, qty, filial, zavod, prioritet, tip,
            data_nach, data_ok, prog_price_override=None):
    """Создать одну строку для файла потребностей/аварийных работ."""
    naim, ed_izm, prog_price = MATERIALS[mat_id]
    if prog_price_override is not None:
        prog_price = prog_price_override
    return {
        "Код работы":             kod,
        "Тип работы":             tip,
        "Филиал":                 filial,
        "Подразделение":          "Цех 1",
        "Центр затрат":           f"CC-{zavod}-001",
        "Завод":                  zavod,
        "Дата начала":            data_nach.strftime("%Y-%m-%d"),
        "Дата окончания":         data_ok.strftime("%Y-%m-%d"),
        "Приоритет":              prioritet,
        "Статус":                 "active",
        "Системный номер":        mat_id,
        "Наименование материала": naim,
        "Ед.изм":                 ed_izm,
        "Потребность":            qty,
        "Прогнозная цена":        prog_price,
    }


# =============================================================================
# ШАГ 1: Потребности обычных работ (test_requirements.xlsx)
# =============================================================================

def build_requirements():
    """
    Строим потребности для сценариев SCN-01..05, SCN-07..12.
    Сценарий SCN-06 (аварийная) — в отдельном файле.

    Все даты и числа — точные, чтобы результат можно было предсказать заранее.
    """
    rows = []

    # --- SCN-01: Полное покрытие через списание ---
    # Потребность 100м, спишем ровно 100м → дефицита нет, склад не нужен
    rows.append(req_row(
        kod="WO-SCN-01", mat_id="MAT-001", qty=100,
        filial="Алматы", zavod="KZ01", prioritet=2,
        tip="SCN-01: Полное списание",
        data_nach=IN_30_DAYS, data_ok=IN_60_DAYS,
    ))

    # --- SCN-02: Частичное списание (40м) + склад (60м) ---
    # Спишем 40м, на складе KZ01 положим ровно 60м
    rows.append(req_row(
        kod="WO-SCN-02", mat_id="MAT-002", qty=100,
        filial="Алматы", zavod="KZ01", prioritet=2,
        tip="SCN-02: Списание + склад",
        data_nach=IN_30_DAYS, data_ok=IN_60_DAYS,
    ))

    # --- SCN-03: Только выдано не списано (20шт) ---
    # Нет ни списаний, ни склада — всё покрыто через «Выдано не списано»
    rows.append(req_row(
        kod="WO-SCN-03", mat_id="MAT-003", qty=20,
        filial="Астана", zavod="KZ02", prioritet=2,
        tip="SCN-03: Только выдано",
        data_nach=IN_30_DAYS, data_ok=IN_60_DAYS,
    ))

    # --- SCN-04: Все 4 слоя + дефицит ---
    # Потребность 100шт:
    #   Слой 1 (список): 15шт
    #   Слой 2 (выдано): 10шт
    #   Слой 3 (склад):  20шт  (KZ03)
    #   Слой 4 (поставка): 25шт (KZ03)
    #   Дефицит:         30шт
    rows.append(req_row(
        kod="WO-SCN-04", mat_id="MAT-004", qty=100,
        filial="Шымкент", zavod="KZ03", prioritet=2,
        tip="SCN-04: Все слои + дефицит",
        data_nach=IN_30_DAYS, data_ok=IN_60_DAYS,
    ))

    # --- SCN-05: Полный дефицит ---
    # Материал MAT-005, нет ничего нигде
    rows.append(req_row(
        kod="WO-SCN-05", mat_id="MAT-005", qty=50,
        filial="Актобе", zavod="KZ04", prioritet=2,
        tip="SCN-05: Полный дефицит",
        data_nach=IN_30_DAYS, data_ok=IN_60_DAYS,
    ))

    # --- SCN-06: Плановая работа — конкурирует с аварийной (файл emergency)
    # Та же дата начала, тот же материал MAT-001, тот же филиал Алматы
    # На складе KZ01 только 80м → аварийная (AV-SCN-06) заберёт всё
    rows.append(req_row(
        kod="WO-SCN-06", mat_id="MAT-001", qty=80,
        filial="Алматы", zavod="KZ01", prioritet=1,
        tip="SCN-06: Плановая vs аварийная (должен быть дефицит)",
        data_nach=TOMORROW, data_ok=IN_30_DAYS,
    ))

    # --- SCN-07: FIFO — старая партия должна уйти первой ---
    # На складе KZ01 две партии MAT-002:
    #   Партия A: 2025-01-01, 30м, цена 3000
    #   Партия B: 2026-01-01, 30м, цена 3800
    # Потребность: 50м → должны взять 30м из A + 20м из B
    # Средняя цена = (30*3000 + 20*3800) / 50 = 3320.00
    rows.append(req_row(
        kod="WO-SCN-07", mat_id="MAT-002", qty=50,
        filial="Алматы", zavod="KZ01", prioritet=2,
        tip="SCN-07: FIFO (старая партия первой)",
        data_nach=IN_30_DAYS, data_ok=IN_60_DAYS,
    ))

    # --- SCN-08: Поставка из другого филиала не используется ---
    # Работа в Астане (KZ02), потребность MAT-003, 30шт
    # Поставка в Алматы (KZ01): 100шт — НЕ должна использоваться
    # Поставка в Астане (KZ02): 30шт  — ДОЛЖНА использоваться
    rows.append(req_row(
        kod="WO-SCN-08", mat_id="MAT-003", qty=30,
        filial="Астана", zavod="KZ02", prioritet=2,
        tip="SCN-08: Поставка только своего филиала",
        data_nach=IN_30_DAYS, data_ok=IN_60_DAYS,
    ))

    # --- SCN-09: Возможное перемещение ---
    # Работа в Астане (KZ02), MAT-004, 50шт
    # Нет поставок и остатков в KZ02
    # На складе KZ01 (Алматы) — 100шт → vozmozhnoe_sklad, в листе «Возможное перемещение»
    rows.append(req_row(
        kod="WO-SCN-09", mat_id="MAT-004", qty=50,
        filial="Астана", zavod="KZ02", prioritet=2,
        tip="SCN-09: Возможное перемещение из Алматы",
        data_nach=IN_30_DAYS, data_ok=IN_60_DAYS,
    ))

    # --- SCN-10: Прогнозная цена = 0, обеспечённость по количеству ---
    # Потребность 100м MAT-002 в АСТАНЕ (KZ02), прогнозная цена НОЛЬ
    # На складе KZ02 — 60м → Сл:60, Деф:40
    # Обеспечённость = 60/100 × 100 = 60.0% (по количеству, т.к. цена = 0)
    # KZ02/Астана — отдельная филиал от KZ01, нет конкуренции с SCN-06
    rows.append(req_row(
        kod="WO-SCN-10", mat_id="MAT-002", qty=100,
        filial="Астана", zavod="KZ02", prioritet=2,
        tip="SCN-10: Прогнозная цена = 0",
        data_nach=IN_30_DAYS, data_ok=IN_60_DAYS,
        prog_price_override=0,   # Специально обнуляем цену
    ))

    # --- SCN-11: Несколько договоров покрывают одну потребность ---
    # Потребность 100м MAT-002 в Шымкенте (KZ03)
    # Поставки: ДОГ-ТЕСТ-А: 40м + ДОГ-ТЕСТ-Б: 40м + ДОГ-ТЕСТ-В: 40м
    # Алгоритм берёт первые 100м, все 3 договора попадают в строку через запятую
    rows.append(req_row(
        kod="WO-SCN-11", mat_id="MAT-002", qty=100,
        filial="Шымкент", zavod="KZ03", prioritet=2,
        tip="SCN-11: Несколько договоров поставки",
        data_nach=IN_30_DAYS, data_ok=IN_60_DAYS,
    ))

    # --- SCN-12: Приоритет 1 vs Приоритет 3 — одинаковая дата начала ---
    # Обе работы в Актобе (KZ04), MAT-005, дата начала = завтра
    # На складе KZ04 — ровно 50 мешков
    # P1 должен получить 50, P3 — дефицит 50
    rows.append(req_row(
        kod="WO-SCN-12-P1", mat_id="MAT-005", qty=50,
        filial="Актобе", zavod="KZ04", prioritet=1,
        tip="SCN-12: Приоритет 1 (должен получить материал)",
        data_nach=TOMORROW, data_ok=IN_30_DAYS,
    ))
    rows.append(req_row(
        kod="WO-SCN-12-P3", mat_id="MAT-005", qty=50,
        filial="Актобе", zavod="KZ04", prioritet=3,
        tip="SCN-12: Приоритет 3 (должен быть дефицит)",
        data_nach=TOMORROW, data_ok=IN_30_DAYS,
    ))

    return pd.DataFrame(rows)


# =============================================================================
# ШАГ 2: Аварийные работы (test_emergency.xlsx)
# =============================================================================

def build_emergency():
    """
    SCN-06: Аварийная работа AV-SCN-06.

    Та же дата начала (TOMORROW), тот же материал MAT-001, тот же филиал Алматы.
    На складе KZ01 только 80м.
    Аварийная обрабатывается ПЕРВОЙ → забирает все 80м.
    Плановая WO-SCN-06 остаётся с дефицитом 80м.
    """
    naim, ed_izm, prog_price = MATERIALS["MAT-001"]
    rows = [{
        "Код работы":             "AV-SCN-06",
        "Тип работы":             "SCN-06: Аварийная (должна забрать материал у плановой)",
        "Филиал":                 "Алматы",
        "Подразделение":          "Цех 1",
        "Центр затрат":           "CC-KZ01-AV",
        "Завод":                  "KZ01",
        "Дата начала":            TOMORROW.strftime("%Y-%m-%d"),
        "Дата окончания":         IN_30_DAYS.strftime("%Y-%m-%d"),
        "Приоритет":              1,
        "Статус":                 "active",
        "Системный номер":        "MAT-001",
        "Наименование материала": naim,
        "Ед.изм":                 ed_izm,
        "Потребность":            80,
        "Прогнозная цена":        prog_price,
    }]
    return pd.DataFrame(rows)


# =============================================================================
# ШАГ 3: Складские остатки (test_stock.xlsx)
# =============================================================================

def build_stock():
    """
    Остатки на складах — строго под каждый сценарий.

    Ключевые моменты:
    - SCN-06: ровно 80м MAT-001 в KZ01 → аварийная заберёт всё
    - SCN-07: две партии MAT-002 в KZ01 с разными датами (FIFO)
    - SCN-09: MAT-004 только в KZ01 (Алматы), но работа в KZ02 (Астана)
    - SCN-10: 60м MAT-001 в KZ01 (прогнозная цена = 0 у работы)
    - SCN-12: ровно 50 мешков MAT-005 в KZ04

    Единица стоимости для каждой партии — своя, чтобы проверить FIFO-цены.
    """
    def stock_row(kod_sklada, filial, zavod, mat_id, qty, price,
                  nomer_partii, data_pост):
        naim, ed_izm, _ = MATERIALS[mat_id]
        return {
            "Код склада":             kod_sklada,
            "Тип склада":             "центральный",
            "Филиал склада":          filial,
            "Завод склада":           zavod,
            "Системный номер":        mat_id,
            "Наименование материала": naim,
            "Ед.изм":                 ed_izm,
            "Группа материала":       "Тест",
            "Номер партии":           nomer_partii,
            "Количество":             qty,
            "Стоимость за ед":        price,
            "Дата поступления":       data_pост,
        }

    rows = []

    # ── SCN-01: MAT-001 на складе KZ01 НЕ добавляем:
    # потребность полностью закрыта списанием (100м). Склад не нужен,
    # а лишние остатки мешали бы SCN-06 (аварийная vs плановая).

    # ── SCN-02: MAT-002 на складе KZ01 — ровно 60м.
    # Дата 2023-01-01 (самая старая) чтобы FIFO брал её ПЕРВОЙ среди MAT-002,
    # оставляя партии P-S07-A и P-S07-B нетронутыми для SCN-07.
    rows.append(stock_row("SKL-ALM-01", "Алматы", "KZ01",
                          "MAT-002", 60, 3400.0, "P-S02-001", "2023-01-01"))

    # ── SCN-04: MAT-004 на складе KZ03 — ровно 20шт
    rows.append(stock_row("SKL-SHY-01", "Шымкент", "KZ03",
                          "MAT-004", 20, 5400.0, "P-S04-001", "2026-01-15"))

    # ── SCN-06: MAT-001 на складе KZ01 — ровно 80м.
    # Аварийная AV-SCN-06 обрабатывается первой и забирает все 80м.
    # Плановая WO-SCN-06 идёт следом — склад уже пуст → дефицит 80м.
    # ВАЖНО: нет других партий MAT-001 в KZ01, иначе WO-SCN-06 получит материал.
    rows.append(stock_row("SKL-ALM-01", "Алматы", "KZ01",
                          "MAT-001", 80, 1200.0, "P-S06-001", "2026-03-01"))

    # ── SCN-07 FIFO: две партии MAT-002 в KZ01 с разными датами поступления.
    # Партия A: 2025-01-01 (30м, цена 3000) — старее P-S02-001? НЕТ!
    #   P-S02-001 имеет дату 2023-01-01 (ещё старее) → WO-SCN-02 возьмёт его первым.
    #   После SCN-02 остатки: P-S07-A и P-S07-B нетронуты.
    # Партия B: 2026-01-01 (30м, цена 3800) — WO-SCN-07 берёт 30м из A + 20м из B.
    # Средняя цена: (30×3000 + 20×3800) / 50 = 166000 / 50 = 3320.00
    rows.append(stock_row("SKL-ALM-01", "Алматы", "KZ01",
                          "MAT-002", 30, 3000.0, "P-S07-A", "2025-01-01"))  # СТАРАЯ
    rows.append(stock_row("SKL-ALM-01", "Алматы", "KZ01",
                          "MAT-002", 30, 3800.0, "P-S07-B", "2026-01-01"))  # НОВАЯ

    # ── SCN-09 «Возможное перемещение»:
    # MAT-004 есть ТОЛЬКО в KZ01 (Алматы), но работа WO-SCN-09 в KZ02 (Астана)
    # Алгоритм не берёт этот склад, но заносит в «Возможное перемещение»
    rows.append(stock_row("SKL-ALM-01", "Алматы", "KZ01",
                          "MAT-004", 100, 5300.0, "P-S09-001", "2026-02-15"))

    # ── SCN-10: MAT-002 в KZ02 (Астана) — ровно 60м (прогнозная цена у работы = 0).
    # Отдельная партия в другом филиале: не конкурирует с SCN-06 (KZ01/Алматы).
    # WO-SCN-10 заберёт 60м → Сл:60, Деф:40, %:60.0 (расчёт по кол-ву, цена=0)
    rows.append(stock_row("SKL-AST-01", "Астана", "KZ02",
                          "MAT-002", 60, 3300.0, "P-S10-001", "2026-01-01"))

    # ── SCN-12: MAT-005 в KZ04 — ровно 50 мешков
    # P1 возьмёт все 50, P3 останется с дефицитом
    rows.append(stock_row("SKL-AKT-01", "Актобе", "KZ04",
                          "MAT-005", 50, 3100.0, "P-S12-001", "2026-01-10"))

    return pd.DataFrame(rows)


# =============================================================================
# ШАГ 4: Поставки (test_supplies.xlsx)
# =============================================================================

def build_supplies():
    """
    Поставки для сценариев SCN-04, SCN-08, SCN-11.

    SCN-04: MAT-004 в KZ03 (Шымкент), 25шт, один договор
    SCN-08: MAT-003 в KZ01 (Алматы) — НЕ должна покрыть работу в Астане (KZ02)
            MAT-003 в KZ02 (Астана)  — ДОЛЖНА покрыть 30шт
    SCN-11: MAT-002 в KZ03 (Шымкент), три разных договора по 40м каждый
    """
    def supply_row(dogovor, postavshchik, kod_sklada, filial, zavod,
                   mat_id, qty, price, data_postavki, status="confirmed"):
        naim, ed_izm, _ = MATERIALS[mat_id]
        return {
            "Договор":                dogovor,
            "Поставщик":              postavshchik,
            "Код склада":             kod_sklada,
            "Филиал":                 filial,
            "Завод":                  zavod,
            "Системный номер":        mat_id,
            "Наименование материала": naim,
            "Ед.изм":                 ed_izm,
            "Дата поставки":          data_postavki,
            "Количество":             qty,
            "Стоимость за ед":        price,
            "Статус":                 status,
        }

    rows = []

    # ── SCN-04: 25шт MAT-004 в Шымкенте (покроет остаток после склада)
    rows.append(supply_row(
        dogovor="ДОГ-SCN04-001", postavshchik="ТОО Тест-Снаб",
        kod_sklada="SKL-SHY-01", filial="Шымкент", zavod="KZ03",
        mat_id="MAT-004", qty=25, price=5450.0,
        data_postavki=(TODAY + timedelta(days=15)).strftime("%Y-%m-%d"),
    ))

    # ── SCN-08а: MAT-003 в АЛМАТЫ (KZ01) — НЕ должна покрыть работу в Астане
    # Это 100шт — специально большое количество, чтобы убедиться что оно не берётся
    rows.append(supply_row(
        dogovor="ДОГ-SCN08-ALM", postavshchik="ТОО Тест-Алматы",
        kod_sklada="SKL-ALM-01", filial="Алматы", zavod="KZ01",
        mat_id="MAT-003", qty=100, price=8400.0,
        data_postavki=(TODAY + timedelta(days=10)).strftime("%Y-%m-%d"),
    ))

    # ── SCN-08б: MAT-003 в АСТАНЕ (KZ02) — ДОЛЖНА покрыть 30шт
    rows.append(supply_row(
        dogovor="ДОГ-SCN08-AST", postavshchik="ТОО Тест-Астана",
        kod_sklada="SKL-AST-01", filial="Астана", zavod="KZ02",
        mat_id="MAT-003", qty=30, price=8600.0,
        data_postavki=(TODAY + timedelta(days=10)).strftime("%Y-%m-%d"),
    ))

    # ── SCN-11: три договора на MAT-002 в Шымкенте (KZ03), по 40м каждый
    # Потребность 100м, алгоритм берёт 40+40+20 = 100м
    # В итоге: Договор(а) = «ДОГ-ТЕСТ-А, ДОГ-ТЕСТ-Б, ДОГ-ТЕСТ-В»
    for letter, qty in [("А", 40), ("Б", 40), ("В", 40)]:
        rows.append(supply_row(
            dogovor=f"ДОГ-ТЕСТ-{letter}", postavshchik=f"ТОО Поставщик-{letter}",
            kod_sklada="SKL-SHY-01", filial="Шымкент", zavod="KZ03",
            mat_id="MAT-002", qty=qty, price=3450.0,
            data_postavki=(TODAY + timedelta(days=20)).strftime("%Y-%m-%d"),
        ))

    return pd.DataFrame(rows)


# =============================================================================
# ШАГ 5: Списания (test_writeoffs.xlsx)
# =============================================================================

def build_writeoffs():
    """
    Списания для сценариев SCN-01, SCN-02, SCN-04.

    SCN-01: спишем ровно 100м MAT-001 → потребность полностью закрыта Слоем 1
    SCN-02: спишем 40м MAT-002 → остаток 60м будет взят со склада
    SCN-04: спишем 15шт MAT-004 → остаток 85шт пойдёт в Слои 2-4-5
    """
    def wo_row(kod_raboty, mat_id, qty, price, nomer_doc, data_sp):
        naim, ed_izm, _ = MATERIALS[mat_id]
        summa = round(qty * price, 2)
        return {
            "Код работы":             kod_raboty,
            "Системный номер":        mat_id,
            "Наименование материала": naim,
            "Ед.изм":                 ed_izm,
            "Количество":             qty,
            "Стоимость за ед":        price,
            "Сумма":                  summa,
            "Номер документа":        nomer_doc,
            "Дата списания":          data_sp,
        }

    yesterday = (TODAY - timedelta(days=1)).strftime("%Y-%m-%d")
    two_weeks_ago = (TODAY - timedelta(days=14)).strftime("%Y-%m-%d")

    rows = [
        # SCN-01: полное списание 100м → никакого дефицита
        wo_row("WO-SCN-01", "MAT-001", qty=100.0, price=1180.0,
               nomer_doc="МОВФ-SCN01", data_sp=two_weeks_ago),

        # SCN-02: частичное списание 40м (остаток 60м → возьмётся со склада)
        wo_row("WO-SCN-02", "MAT-002", qty=40.0, price=3350.0,
               nomer_doc="МОВФ-SCN02", data_sp=two_weeks_ago),

        # SCN-04: списание 15шт (остаток 85шт → Слои 2+3+4+5)
        wo_row("WO-SCN-04", "MAT-004", qty=15.0, price=5380.0,
               nomer_doc="МОВФ-SCN04", data_sp=yesterday),
    ]
    return pd.DataFrame(rows)


# =============================================================================
# ШАГ 6: Выдано не списано (test_issued.xlsx)
# =============================================================================

def build_issued():
    """
    Данные «Выдано не списано» для сценариев SCN-03 и SCN-04.

    SCN-03: полностью выдано 20шт MAT-003 → потребность закрыта Слоем 2
    SCN-04: выдано 10шт MAT-004 → остаток после Слоя 1 был 85шт, после Слоя 2 = 75шт
    """
    def iss_row(kod_raboty, mat_id, qty, price, kod_sklada, data_vyd):
        naim, ed_izm, _ = MATERIALS[mat_id]
        summa = round(qty * price, 2)
        return {
            "Код работы":             kod_raboty,
            "Системный номер":        mat_id,
            "Наименование материала": naim,
            "Ед.изм":                 ed_izm,
            "Количество":             qty,
            "Стоимость за ед":        price,
            "Сумма":                  summa,
            "Код склада":             kod_sklada,  # Информативно
            "Дата выдачи":            data_vyd,
        }

    last_week = (TODAY - timedelta(days=7)).strftime("%Y-%m-%d")

    rows = [
        # SCN-03: полная выдача 20шт — нет ни склада, ни поставки
        iss_row("WO-SCN-03", "MAT-003", qty=20.0, price=8450.0,
                kod_sklada="SKL-AST-01", data_vyd=last_week),

        # SCN-04: выдача 10шт (после Слоя 1 осталось 85шт, после Слоя 2 → 75шт)
        iss_row("WO-SCN-04", "MAT-004", qty=10.0, price=5420.0,
                kod_sklada="SKL-SHY-01", data_vyd=last_week),
    ]
    return pd.DataFrame(rows)


# =============================================================================
# Запуск
# =============================================================================

if __name__ == "__main__":
    print("=" * 60)
    print("Генерация СЦЕНАРНЫХ тестовых данных для MAPS (12 сценариев)")
    print("=" * 60)

    # Потребности
    df = build_requirements()
    path = TEST_DIR / "test_requirements.xlsx"
    df.to_excel(path, index=False)
    print(f"✓ Потребности ({len(df)} строк): {path}")

    # Аварийные
    df = build_emergency()
    path = TEST_DIR / "test_emergency.xlsx"
    df.to_excel(path, index=False)
    print(f"✓ Аварийные ({len(df)} строк): {path}")

    # Склад
    df = build_stock()
    path = TEST_DIR / "test_stock.xlsx"
    df.to_excel(path, index=False)
    print(f"✓ Склад ({len(df)} строк): {path}")

    # Поставки
    df = build_supplies()
    path = TEST_DIR / "test_supplies.xlsx"
    df.to_excel(path, index=False)
    print(f"✓ Поставки ({len(df)} строк): {path}")

    # Списания
    df = build_writeoffs()
    path = TEST_DIR / "test_writeoffs.xlsx"
    df.to_excel(path, index=False)
    print(f"✓ Списания ({len(df)} строк): {path}")

    # Выдано
    df = build_issued()
    path = TEST_DIR / "test_issued.xlsx"
    df.to_excel(path, index=False)
    print(f"✓ Выдано не списано ({len(df)} строк): {path}")

    print()
    print("=" * 60)
    print("Порядок загрузки:")
    print()
    print("  python main.py init")
    print("  python main.py import-requirements data/test/test_requirements.xlsx")
    print("  python main.py import-emergency     data/test/test_emergency.xlsx")
    print("  python main.py import-stock         data/test/test_stock.xlsx")
    print("  python main.py import-supplies      data/test/test_supplies.xlsx")
    print("  python main.py import-writeoffs     data/test/test_writeoffs.xlsx")
    print("  python main.py import-issued        data/test/test_issued.xlsx")
    print("  python main.py status")
    print("  python main.py allocate")
    print("  python main.py export <SESSION_ID>")
    print()
    print("ОЖИДАЕМЫЕ РЕЗУЛЬТАТЫ:")
    print("  SCN-01  Сп:100  Вд:0   Сл:0   Пс:0   Деф:0    %:100")
    print("  SCN-02  Сп:40   Вд:0   Сл:60  Пс:0   Деф:0    %:100")
    print("  SCN-03  Сп:0    Вд:20  Сл:0   Пс:0   Деф:0    %:100")
    print("  SCN-04  Сп:15   Вд:10  Сл:20  Пс:25  Деф:30   %≈64.8")
    print("  SCN-05  Сп:0    Вд:0   Сл:0   Пс:0   Деф:50   %:0")
    print("  SCN-06  AV:80   WO-деф:80 (аварийная забирает материал)")
    print("  SCN-07  Сл:50 (партия A 30м + партия B 20м), ср.цена 3320.00")
    print("  SCN-08  Пс:30 (только KZ02-поставка, KZ01-поставка игнорируется)")
    print("  SCN-09  Деф:50, лист «Возможное перемещение»: SKL-SHY-01 20шт + SKL-ALM-01 30шт")
    print("  SCN-10  Сл:60, Деф:40, %:60.0 (по количеству т.к. прогнозная цена = 0)")
    print("  SCN-11  Пс:100, Договор(а) = ДОГ-ТЕСТ-А, ДОГ-ТЕСТ-Б, ДОГ-ТЕСТ-В")
    print("  SCN-12  P1: Сл:50 %:100 | P3: Деф:50 %:0")
    print("=" * 60)
