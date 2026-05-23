"""
scripts/generate_sample_data.py — Генерация тестовых Excel файлов

Что делает этот скрипт?
    Создаёт шесть Excel файлов с реалистичными тестовыми данными
    для демонстрации и тестирования всех 5 слоёв алгоритма MAPS:

    data/sample_requirements.xlsx     — потребности обычных работ
    data/sample_emergency.xlsx         — аварийные работы (наивысший приоритет)
    data/sample_stock.xlsx             — складские остатки (партии)
    data/sample_supplies.xlsx          — поставки (материалы в пути)
    data/sample_writeoffs.xlsx         — фактические списания (Слой 1)
    data/sample_issued.xlsx            — выдано не списано (Слой 2)

Зачем?
    Чтобы можно было сразу протестировать систему без реальных данных из SAP.

Как данные связаны между собой:
    - В потребностях: работы WO-0001..WO-0050, материалы из MATERIALY
    - В аварийных: работы AV-0001..AV-0005 (отдельные аварийные наряды)
    - На складах: партии материалов (намеренно создан дефицит для тестирования)
    - В поставках: часть дефицитных материалов уже заказана
    - В списаниях: часть потребностей WO-0001..WO-0010 уже списана
    - В выдано: часть потребностей выдана, но не оформлена документально

Запуск:
    python scripts/generate_sample_data.py
"""

import random
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

# Фиксируем случайность — одни и те же данные каждый раз
random.seed(42)

# Папка для сохранения файлов
DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)


# =============================================================================
# Справочные данные (реалистичные названия и структура)
# =============================================================================

# Филиалы компании
FILIALI = ["Алматы", "Астана", "Шымкент", "Актобе"]

# Коды заводов SAP (Plant) для каждого филиала
ZAVODY = {
    "Алматы": "KZ01",
    "Астана": "KZ02",
    "Шымкент": "KZ03",
    "Актобе": "KZ04",
}

# Склады по заводам: {zavod: [kod_sklada, ...]}
SKLADY = {
    "KZ01": ["SKL-ALM-01", "SKL-ALM-02"],
    "KZ02": ["SKL-AST-01"],
    "KZ03": ["SKL-SHY-01"],
    "KZ04": ["SKL-AKT-01"],
}

# Справочник материалов: (sys_nomer, naimenovanie, ed_izm, gruppa)
MATERIALY = [
    ("000000000010010001", "Кабель ВВГ 3х2,5 мм",           "м",     "Кабели"),
    ("000000000010010002", "Кабель ВВГ 3х6 мм",             "м",     "Кабели"),
    ("000000000010010003", "Труба стальная 57х3,5 мм",       "м",     "Трубы"),
    ("000000000010010004", "Труба ПНД 32 мм",               "м",     "Трубы"),
    ("000000000010010005", "Автомат. выключатель АВ 25А",    "шт",    "Электрооборудование"),
    ("000000000010010006", "Счётчик электроэнергии Меркурий","шт",    "Электрооборудование"),
    ("000000000010010007", "Клапан шаровой 1/2 дюйма",       "шт",    "Арматура"),
    ("000000000010010008", "Клапан шаровой 1 дюйм",          "шт",    "Арматура"),
    ("000000000010010009", "Цемент М400 в мешках 50кг",      "мешок", "Стройматериалы"),
    ("000000000010010010", "Краска фасадная белая 10л",       "ведро", "ЛКМ"),
    ("000000000010010011", "Болт М12х50 DIN 933",             "шт",    "Крепёж"),
    ("000000000010010012", "Гайка М12 DIN 934",               "шт",    "Крепёж"),
    ("000000000010010013", "Электрод сварочный АНО-21 ф3",    "кг",    "Сварочные"),
    ("000000000010010014", "Прокладка паронитовая DN50",       "шт",    "Прокладки"),
    ("000000000010010015", "Лоток кабельный 100х50 мм",       "м",     "Лотки"),
]

# Прогнозные цены для материалов (тг/ед.изм)
PROGNOSNYE_TSENY = {
    "000000000010010001": 1200.0,
    "000000000010010002": 2100.0,
    "000000000010010003": 3500.0,
    "000000000010010004": 850.0,
    "000000000010010005": 8500.0,
    "000000000010010006": 45000.0,
    "000000000010010007": 2800.0,
    "000000000010010008": 5500.0,
    "000000000010010009": 3200.0,
    "000000000010010010": 7500.0,
    "000000000010010011": 150.0,
    "000000000010010012": 100.0,
    "000000000010010013": 1800.0,
    "000000000010010014": 650.0,
    "000000000010010015": 1400.0,
}

TIP_RABOT = [
    "Техническое обслуживание",
    "Ремонт оборудования",
    "Монтаж нового оборудования",
    "Замена",
]

POSTAVSHCHIKI = [
    "ТОО Стройснаб-KZ",
    "АО МетРесурс",
    "ТОО ЭлектроТрейд",
    "ООО КазПромСнаб",
]


def random_date(start: date, end: date) -> date:
    """Вернуть случайную дату в диапазоне [start, end]."""
    delta = (end - start).days
    return start + timedelta(days=random.randint(0, delta))


def random_qty(ed_izm: str, scale: float = 1.0) -> float:
    """
    Сгенерировать реалистичное количество в зависимости от единицы измерения.

    Args:
        ed_izm: Единица измерения
        scale:  Масштаб (0.5 = вдвое меньше, 2.0 = вдвое больше)
    """
    if ed_izm in ("м",):
        return round(random.uniform(10, 300) * scale, 1)
    elif ed_izm in ("шт",):
        return round(random.uniform(2, 50) * scale, 0)
    elif ed_izm in ("кг",):
        return round(random.uniform(5, 80) * scale, 1)
    else:
        return round(random.uniform(1, 20) * scale, 1)


# =============================================================================
# Генерация потребностей обычных работ
# =============================================================================

def generate_requirements(n_works: int = 50) -> pd.DataFrame:
    """
    Сгенерировать потребности обычных работ.

    Колонки включают «Прогнозная цена» — цена за единицу из исходной заявки.
    Используется для расчёта обеспечённости по стоимости.

    Args:
        n_works: Количество работ

    Returns:
        DataFrame для импорта через maps import-requirements
    """
    rows = []
    today = date.today()

    for i in range(1, n_works + 1):
        filial = random.choice(FILIALI)
        zavod = ZAVODY[filial]
        kod_raboty = f"WO-{i:04d}"
        prioritet = random.choices([1, 2, 3], weights=[10, 30, 60])[0]

        # Дата начала: от -30 до +120 дней от сегодня
        days_from_now = random.randint(-30, 120)
        data_nachala = today + timedelta(days=days_from_now)
        data_okonchaniya = data_nachala + timedelta(days=random.randint(7, 60))

        # Каждая работа требует 2-6 материалов
        n_mat = random.randint(2, 6)
        selected = random.sample(MATERIALY, n_mat)

        for sys_nomer, naim, ed_izm, gruppa in selected:
            qty = random_qty(ed_izm)
            # Прогнозная цена = базовая цена ± небольшое отклонение (как в реальности)
            base_price = PROGNOSNYE_TSENY[sys_nomer]
            prog_price = round(base_price * random.uniform(0.9, 1.1), 2)

            rows.append({
                "Код работы":           kod_raboty,
                "Тип работы":           random.choice(TIP_RABOT),
                "Филиал":               filial,
                "Подразделение":        f"Цех {random.randint(1, 5)}",
                "Центр затрат":         f"CC-{zavod}-{random.randint(100, 999)}",
                "Завод":                zavod,
                "Дата начала":          data_nachala.strftime("%Y-%m-%d"),
                "Дата окончания":       data_okonchaniya.strftime("%Y-%m-%d"),
                "Приоритет":            prioritet,
                "Статус":               "active",
                "Системный номер":      sys_nomer,
                "Наименование материала": naim,
                "Ед.изм":               ed_izm,
                "Потребность":          qty,
                "Прогнозная цена":      prog_price,  # ← НОВОЕ ПОЛЕ
            })

    df = pd.DataFrame(rows)
    print(f"Сгенерировано потребностей: {len(df)} строк ({n_works} работ)")
    return df


# =============================================================================
# Генерация аварийных работ (отдельный файл)
# =============================================================================

def generate_emergency_works(n_works: int = 5) -> pd.DataFrame:
    """
    Сгенерировать аварийные работы — ОТДЕЛЬНЫЙ ФАЙЛ.

    Аварийные работы имеют наивысший приоритет: они обрабатываются
    ДО всех обычных работ. Сортировка: только по дате начала.

    Реальный пример: авария насосной станции, нет электричества в цехе.
    Такие работы нельзя откладывать — материалы нужны немедленно.

    Формат файла: тот же, что и для обычных потребностей.
    Отличие: все работы будут помечены is_emergency=True.

    Args:
        n_works: Количество аварийных работ

    Returns:
        DataFrame для импорта через maps import-emergency
    """
    rows = []
    today = date.today()

    for i in range(1, n_works + 1):
        filial = random.choice(FILIALI)
        zavod = ZAVODY[filial]
        kod_raboty = f"AV-{i:04d}"

        # Аварийные работы — срочные: начинаются в ближайшие дни
        days_from_now = random.randint(0, 14)
        data_nachala = today + timedelta(days=days_from_now)
        data_okonchaniya = data_nachala + timedelta(days=random.randint(1, 7))

        # Аварийные работы требуют меньше материалов (срочный ремонт)
        n_mat = random.randint(1, 3)
        selected = random.sample(MATERIALY, n_mat)

        for sys_nomer, naim, ed_izm, gruppa in selected:
            qty = random_qty(ed_izm, scale=0.5)  # Меньше чем обычно (срочный ремонт)
            base_price = PROGNOSNYE_TSENY[sys_nomer]
            prog_price = round(base_price * random.uniform(0.95, 1.15), 2)

            rows.append({
                "Код работы":           kod_raboty,
                "Тип работы":           "Аварийный ремонт",
                "Филиал":               filial,
                "Подразделение":        f"Цех {random.randint(1, 5)}",
                "Центр затрат":         f"CC-{zavod}-{random.randint(100, 999)}",
                "Завод":                zavod,
                "Дата начала":          data_nachala.strftime("%Y-%m-%d"),
                "Дата окончания":       data_okonchaniya.strftime("%Y-%m-%d"),
                "Приоритет":            1,  # Формально приоритет 1, но для аварийных не важен
                "Статус":               "active",
                "Системный номер":      sys_nomer,
                "Наименование материала": naim,
                "Ед.изм":               ed_izm,
                "Потребность":          qty,
                "Прогнозная цена":      prog_price,
            })

    df = pd.DataFrame(rows)
    print(f"Сгенерировано аварийных работ: {len(df)} строк ({n_works} работ)")
    return df


# =============================================================================
# Генерация складских остатков
# =============================================================================

def generate_stock() -> pd.DataFrame:
    """
    Сгенерировать складские остатки (партии материалов).

    Специально создаём дефицит:
        - Первые 12 материалов есть на складах (у 60% складов)
        - Последние 3 материала — дефицитные (намеренно отсутствуют)
          → Алгоритм должен найти их в поставках или отметить как «К закупу»

    FIFO-тест: генерируем 1-3 партии с разными датами и ценами.

    Returns:
        DataFrame для импорта через maps import-stock
    """
    rows = []

    # Намеренно оставляем последние 3 материала дефицитными
    available_materials = MATERIALY[:-3]

    for filial, zavod in ZAVODY.items():
        for kod_sklada in SKLADY[zavod]:
            for sys_nomer, naim, ed_izm, gruppa in available_materials:
                # 60% вероятность что этот материал есть на этом складе
                if random.random() < 0.60:
                    # 1-3 партии с разными датами (для проверки FIFO)
                    n_partii = random.randint(1, 3)
                    for j in range(1, n_partii + 1):
                        # Разные даты поступления — для FIFO сортировки
                        date_receipt = random_date(
                            date.today() - timedelta(days=180),
                            date.today() - timedelta(days=1),
                        )
                        # Количество в партии
                        if ed_izm in ("м",):
                            qty = round(random.uniform(50, 800), 1)
                            price = round(PROGNOSNYE_TSENY[sys_nomer] * random.uniform(0.85, 1.05), 2)
                        elif ed_izm in ("шт",):
                            qty = round(random.uniform(5, 200), 0)
                            price = round(PROGNOSNYE_TSENY[sys_nomer] * random.uniform(0.85, 1.05), 2)
                        elif ed_izm in ("кг",):
                            qty = round(random.uniform(20, 300), 1)
                            price = round(PROGNOSNYE_TSENY[sys_nomer] * random.uniform(0.85, 1.05), 2)
                        else:
                            qty = round(random.uniform(5, 80), 1)
                            price = round(PROGNOSNYE_TSENY[sys_nomer] * random.uniform(0.85, 1.05), 2)

                        rows.append({
                            "Код склада":           kod_sklada,
                            "Тип склада":           "центральный" if "01" in kod_sklada else "филиальный",
                            "Филиал склада":        filial,
                            "Завод склада":         zavod,
                            "Системный номер":      sys_nomer,
                            "Наименование материала": naim,
                            "Ед.изм":               ed_izm,
                            "Группа материала":     gruppa,
                            "Номер партии":         f"P-{kod_sklada[-3:]}-{sys_nomer[-4:]}-{j:02d}",
                            "Количество":           qty,
                            "Стоимость за ед":      price,
                            "Дата поступления":     date_receipt.strftime("%Y-%m-%d"),
                        })

    df = pd.DataFrame(rows)
    print(f"Сгенерировано складских партий: {len(df)} строк")
    return df


# =============================================================================
# Генерация поставок
# =============================================================================

def generate_supplies() -> pd.DataFrame:
    """
    Сгенерировать поставки (материалы в пути).

    Поставки покрывают часть дефицитных материалов.
    Поставки привязаны к ФИЛИАЛУ (не к заводу) — это ключевое бизнес-правило.

    Returns:
        DataFrame для импорта через maps import-supplies
    """
    rows = []

    # Поставляем в том числе дефицитные материалы
    supply_materials = MATERIALY[-5:]  # Последние 5 (включая 3 дефицитных)

    for i in range(1, 21):  # 20 договоров на поставку
        filial = random.choice(FILIALI)
        zavod = ZAVODY[filial]
        kod_sklada = random.choice(SKLADY[zavod])

        dogovor = f"ДОГ-2026-{i:03d}"
        postavshchik = random.choice(POSTAVSHCHIKI)

        # Дата поставки — в ближайшие 3 месяца
        data_postavki = random_date(
            date.today() + timedelta(days=1),
            date.today() + timedelta(days=90),
        )
        status = random.choices(["confirmed", "in_transit"], weights=[40, 60])[0]

        # 1-4 материала в поставке
        n_mat = random.randint(1, 4)
        selected = random.sample(supply_materials, min(n_mat, len(supply_materials)))

        for sys_nomer, naim, ed_izm, _ in selected:
            qty = random_qty(ed_izm, scale=3.0)  # Поставки обычно крупными партиями
            price = round(PROGNOSNYE_TSENY[sys_nomer] * random.uniform(0.90, 1.10), 2)

            rows.append({
                "Договор":              dogovor,
                "Поставщик":            postavshchik,
                "Код склада":           kod_sklada,
                "Филиал":               filial,  # Ключевое поле — сопоставление с работой по филиалу
                "Завод":                zavod,
                "Системный номер":      sys_nomer,
                "Наименование материала": naim,
                "Ед.изм":               ed_izm,
                "Дата поставки":        data_postavki.strftime("%Y-%m-%d"),
                "Количество":           qty,
                "Стоимость за ед":      price,
                "Статус":               status,
            })

    df = pd.DataFrame(rows)
    print(f"Сгенерировано строк поставок: {len(df)} строк")
    return df


# =============================================================================
# Генерация фактических списаний (Слой 1)
# =============================================================================

def generate_writeoffs(requirements_df: pd.DataFrame) -> pd.DataFrame:
    """
    Сгенерировать фактические списания для части потребностей.

    Списания создаём только для первых 10 работ (WO-0001..WO-0010),
    и только для части их материалов (примерно 50%).

    Это имитирует реальную ситуацию: часть работ уже в процессе,
    часть материалов уже списана в SAP, остальное ещё нужно распределить.

    Args:
        requirements_df: DataFrame потребностей (берём работы из него)

    Returns:
        DataFrame для импорта через maps import-writeoffs
    """
    rows = []
    today = date.today()

    # Берём только первые 10 работ
    first_10_works = [f"WO-{i:04d}" for i in range(1, 11)]
    subset = requirements_df[requirements_df["Код работы"].isin(first_10_works)]

    for _, row in subset.iterrows():
        # 50% вероятность что этот материал уже списан
        if random.random() < 0.5:
            potrebnost = float(row["Потребность"])
            # Списываем от 20% до 80% потребности
            ratio = random.uniform(0.2, 0.8)
            qty = round(potrebnost * ratio, 4)
            if qty <= 0:
                continue

            price = float(row["Прогнозная цена"])
            # Фактическая цена списания может немного отличаться от прогнозной
            actual_price = round(price * random.uniform(0.90, 1.10), 2)
            summa = round(qty * actual_price, 2)

            # Дата списания — в прошлом (уже произошло)
            data_sp = random_date(
                today - timedelta(days=60),
                today - timedelta(days=1),
            )

            rows.append({
                "Код работы":           row["Код работы"],
                "Системный номер":      row["Системный номер"],
                "Наименование материала": row["Наименование материала"],
                "Ед.изм":               row["Ед.изм"],
                "Количество":           qty,
                "Стоимость за ед":      actual_price,
                "Сумма":                summa,
                "Номер документа":      f"МОВФ-{random.randint(100000, 999999)}",
                "Дата списания":        data_sp.strftime("%Y-%m-%d"),
            })

    df = pd.DataFrame(rows)
    print(f"Сгенерировано фактических списаний: {len(df)} строк")
    return df


# =============================================================================
# Генерация «Выдано не списано» (Слой 2)
# =============================================================================

def generate_issued_not_written_off(requirements_df: pd.DataFrame) -> pd.DataFrame:
    """
    Сгенерировать данные «Выдано не списано» для части потребностей.

    «Выдано не списано» — это следующий период: работы WO-0011..WO-0020.
    Материал выдан со склада, бригада работает, но документ ещё не оформлен.

    Склад указывается информативно (берём случайный склад соответствующего завода).
    На остатки склада это не влияет в алгоритме — материал уже ушёл физически.

    Args:
        requirements_df: DataFrame потребностей

    Returns:
        DataFrame для импорта через maps import-issued
    """
    rows = []
    today = date.today()

    # Работы WO-0011..WO-0020 (следующая партия после списанных)
    next_works = [f"WO-{i:04d}" for i in range(11, 21)]
    subset = requirements_df[requirements_df["Код работы"].isin(next_works)]

    for _, row in subset.iterrows():
        # 40% вероятность что этот материал выдан, но не списан
        if random.random() < 0.4:
            potrebnost = float(row["Потребность"])
            # Выдаём от 30% до 100% потребности
            ratio = random.uniform(0.3, 1.0)
            qty = round(potrebnost * ratio, 4)
            if qty <= 0:
                continue

            price = float(row["Прогнозная цена"])
            actual_price = round(price * random.uniform(0.90, 1.10), 2)
            summa = round(qty * actual_price, 2)

            # Дата выдачи — недавно (1-14 дней назад)
            data_vyd = today - timedelta(days=random.randint(1, 14))

            # Определяем склад по заводу работы
            zavod_raboty = row["Завод"]
            sklady_zavoda = SKLADY.get(zavod_raboty, ["SKL-???-01"])
            kod_sklada = random.choice(sklady_zavoda)

            rows.append({
                "Код работы":           row["Код работы"],
                "Системный номер":      row["Системный номер"],
                "Наименование материала": row["Наименование материала"],
                "Ед.изм":               row["Ед.изм"],
                "Количество":           qty,
                "Стоимость за ед":      actual_price,
                "Сумма":                summa,
                "Код склада":           kod_sklada,  # Информативно, остатки не трогаем
                "Дата выдачи":          data_vyd.strftime("%Y-%m-%d"),
            })

    df = pd.DataFrame(rows)
    print(f"Сгенерировано 'Выдано не списано': {len(df)} строк")
    return df


# =============================================================================
# Запуск генерации
# =============================================================================

if __name__ == "__main__":
    print("=" * 55)
    print("Генерация тестовых данных для MAPS (6 файлов)")
    print("=" * 55)

    # 1. Потребности обычных работ
    df_req = generate_requirements(n_works=50)
    req_path = DATA_DIR / "sample_requirements.xlsx"
    df_req.to_excel(req_path, index=False)
    print(f"  ✓ Потребности: {req_path}")

    # 2. Аварийные работы
    df_emerg = generate_emergency_works(n_works=5)
    emerg_path = DATA_DIR / "sample_emergency.xlsx"
    df_emerg.to_excel(emerg_path, index=False)
    print(f"  ✓ Аварийные работы: {emerg_path}")

    # 3. Складские остатки
    df_stock = generate_stock()
    stock_path = DATA_DIR / "sample_stock.xlsx"
    df_stock.to_excel(stock_path, index=False)
    print(f"  ✓ Остатки: {stock_path}")

    # 4. Поставки
    df_supply = generate_supplies()
    supply_path = DATA_DIR / "sample_supplies.xlsx"
    df_supply.to_excel(supply_path, index=False)
    print(f"  ✓ Поставки: {supply_path}")

    # 5. Фактические списания (используем уже сгенерированные потребности)
    df_wo = generate_writeoffs(df_req)
    wo_path = DATA_DIR / "sample_writeoffs.xlsx"
    df_wo.to_excel(wo_path, index=False)
    print(f"  ✓ Фактические списания: {wo_path}")

    # 6. Выдано не списано
    df_issued = generate_issued_not_written_off(df_req)
    issued_path = DATA_DIR / "sample_issued.xlsx"
    df_issued.to_excel(issued_path, index=False)
    print(f"  ✓ Выдано не списано: {issued_path}")

    print("=" * 55)
    print("Готово! Рекомендуемый порядок загрузки:")
    print()
    print("  maps init")
    print("  maps import-requirements data/sample_requirements.xlsx")
    print("  maps import-emergency     data/sample_emergency.xlsx")
    print("  maps import-stock         data/sample_stock.xlsx")
    print("  maps import-supplies      data/sample_supplies.xlsx")
    print("  maps import-writeoffs     data/sample_writeoffs.xlsx")
    print("  maps import-issued        data/sample_issued.xlsx")
    print("  maps allocate")
    print("  maps export <ID_сессии>")
    print("=" * 55)
