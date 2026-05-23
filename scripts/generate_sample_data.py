"""
scripts/generate_sample_data.py — Генерация тестовых Excel файлов

Что делает этот скрипт?
    Создаёт три Excel файла с реалистичными тестовыми данными:
      - data/sample_requirements.xlsx  — потребности работ
      - data/sample_stock.xlsx         — складские остатки
      - data/sample_supplies.xlsx      — поставки (в пути)

Зачем?
    Чтобы можно было сразу протестировать систему без реальных данных из SAP.
    Данные сгенерированы случайно, но структурированы реалистично:
    - Есть дефицитные материалы (их нет на складе)
    - Есть профицитные (их на складе больше, чем нужно)
    - Партии имеют разные даты для проверки FIFO

Запуск:
    python scripts/generate_sample_data.py
"""

import random
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

# Фиксируем случайность для воспроизводимости тестов
random.seed(42)

# Путь для сохранения файлов
DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

# =============================================================================
# Справочные данные (реалистичные названия)
# =============================================================================

FILIALI = ["Алматы", "Астана", "Шымкент", "Актобе"]
ZAVODY = {"Алматы": "KZ01", "Астана": "KZ02", "Шымкент": "KZ03", "Актобе": "KZ04"}
SKLADY = {
    "KZ01": ["SKL-ALM-01", "SKL-ALM-02"],
    "KZ02": ["SKL-AST-01"],
    "KZ03": ["SKL-SHY-01"],
    "KZ04": ["SKL-AKT-01"],
}

MATERIALY = [
    ("000000000010010001", "Кабель ВВГ 3х2,5 мм", "м", "Кабели"),
    ("000000000010010002", "Кабель ВВГ 3х6 мм", "м", "Кабели"),
    ("000000000010010003", "Труба стальная 57х3,5 мм", "м", "Трубы"),
    ("000000000010010004", "Труба ПНД 32 мм", "м", "Трубы"),
    ("000000000010010005", "Автомат. выключатель АВ 25А", "шт", "Электрооборудование"),
    ("000000000010010006", "Счётчик электроэнергии Меркурий", "шт", "Электрооборудование"),
    ("000000000010010007", "Клапан шаровой 1/2 дюйма", "шт", "Арматура"),
    ("000000000010010008", "Клапан шаровой 1 дюйм", "шт", "Арматура"),
    ("000000000010010009", "Цемент М400 в мешках 50кг", "мешок", "Стройматериалы"),
    ("000000000010010010", "Краска фасадная белая 10л", "ведро", "ЛКМ"),
    ("000000000010010011", "Болт М12х50 DIN 933", "шт", "Крепёж"),
    ("000000000010010012", "Гайка М12 DIN 934", "шт", "Крепёж"),
    ("000000000010010013", "Электрод сварочный АНО-21 ф3", "кг", "Сварочные"),
    ("000000000010010014", "Прокладка паронитовая DN50", "шт", "Прокладки"),
    ("000000000010010015", "Лоток кабельный 100х50 мм", "м", "Лотки"),
]

TIP_RABOT = ["Техническое обслуживание", "Ремонт оборудования", "Монтаж нового оборудования", "Замена"]
POSTAVSHCHIKI = ["ТОО Стройснаб-KZ", "АО МетРесурс", "ТОО ЭлектроТрейд", "ООО КазПромСнаб"]


def random_date(start: date, end: date) -> date:
    """Случайная дата в заданном диапазоне."""
    delta = (end - start).days
    return start + timedelta(days=random.randint(0, delta))


# =============================================================================
# Генерация потребностей
# =============================================================================

def generate_requirements(n_works: int = 50) -> pd.DataFrame:
    """
    Сгенерировать потребности работ.

    Args:
        n_works: Количество работ

    Returns:
        DataFrame с потребностями
    """
    rows = []
    today = date.today()

    for i in range(1, n_works + 1):
        filial = random.choice(FILIALI)
        zavod = ZAVODY[filial]
        kod_raboty = f"WO-{i:04d}"
        prioritet = random.choices([1, 2, 3], weights=[10, 30, 60])[0]  # 10% важных

        # Даты
        days_from_now = random.randint(-30, 120)  # от прошлого до 4 месяцев вперёд
        data_nachala = today + timedelta(days=days_from_now)
        data_okonchaniya = data_nachala + timedelta(days=random.randint(7, 60))

        # Каждая работа требует 2-6 материалов
        n_materials = random.randint(2, 6)
        selected_materials = random.sample(MATERIALY, n_materials)

        for sys_nomer, naim, ed_izm, gruppa in selected_materials:
            # Количество зависит от единицы измерения
            if ed_izm in ("м",):
                qty = round(random.uniform(10, 500), 1)
            elif ed_izm in ("шт",):
                qty = round(random.uniform(1, 50), 0)
            elif ed_izm in ("кг",):
                qty = round(random.uniform(5, 100), 1)
            else:
                qty = round(random.uniform(1, 20), 1)

            rows.append({
                "Код работы": kod_raboty,
                "Тип работы": random.choice(TIP_RABOT),
                "Филиал": filial,
                "Подразделение": f"Цех {random.randint(1, 5)}",
                "Центр затрат": f"CC-{zavod}-{random.randint(100, 999)}",
                "Завод": zavod,
                "Дата начала": data_nachala.strftime("%Y-%m-%d"),
                "Дата окончания": data_okonchaniya.strftime("%Y-%m-%d"),
                "Приоритет": prioritet,
                "Статус": "active",
                "Системный номер": sys_nomer,
                "Наименование материала": naim,
                "Ед.изм": ed_izm,
                "Потребность": qty,
            })

    df = pd.DataFrame(rows)
    print(f"Сгенерировано потребностей: {len(df)} строк ({n_works} работ)")
    return df


# =============================================================================
# Генерация складских остатков
# =============================================================================

def generate_stock() -> pd.DataFrame:
    """
    Сгенерировать складские остатки.

    Специально создаём дефицит для некоторых материалов
    (чтобы алгоритм мог это обнаружить).
    """
    rows = []

    # Некоторые материалы намеренно отсутствуют на складах
    # (последние 3 из списка — дефицитные)
    available_materials = MATERIALY[:-3]

    for filial, zavod in ZAVODY.items():
        for kod_sklada in SKLADY[zavod]:
            for sys_nomer, naim, ed_izm, gruppa in available_materials:
                # Не все материалы есть на каждом складе
                if random.random() < 0.6:  # 60% вероятность наличия
                    # Генерируем 1-3 партии для разных дат (FIFO)
                    n_partii = random.randint(1, 3)
                    for j in range(1, n_partii + 1):
                        # Дата поступления: от 6 месяцев назад до вчера
                        date_receipt = random_date(
                            date.today() - timedelta(days=180),
                            date.today() - timedelta(days=1),
                        )
                        if ed_izm in ("м",):
                            qty = round(random.uniform(50, 1000), 1)
                            price = round(random.uniform(800, 5000), 2)
                        elif ed_izm in ("шт",):
                            qty = round(random.uniform(5, 200), 0)
                            price = round(random.uniform(1000, 50000), 2)
                        elif ed_izm in ("кг",):
                            qty = round(random.uniform(20, 500), 1)
                            price = round(random.uniform(500, 3000), 2)
                        else:
                            qty = round(random.uniform(5, 100), 1)
                            price = round(random.uniform(500, 10000), 2)

                        rows.append({
                            "Код склада": kod_sklada,
                            "Тип склада": "центральный" if "01" in kod_sklada else "филиальный",
                            "Филиал склада": filial,
                            "Завод склада": zavod,
                            "Системный номер": sys_nomer,
                            "Наименование материала": naim,
                            "Ед.изм": ed_izm,
                            "Группа материала": gruppa,
                            "Номер партии": f"P-{kod_sklada}-{sys_nomer[-4:]}-{j:02d}",
                            "Количество": qty,
                            "Стоимость за ед": price,
                            "Дата поступления": date_receipt.strftime("%Y-%m-%d"),
                        })

    df = pd.DataFrame(rows)
    print(f"Сгенерировано складских партий: {len(df)} строк")
    return df


# =============================================================================
# Генерация поставок
# =============================================================================

def generate_supplies() -> pd.DataFrame:
    """Сгенерировать поставки (материалы в пути)."""
    rows = []

    # Поставляем в том числе дефицитные материалы (последние 3)
    supply_materials = MATERIALY[-5:]  # последние 5 включают 3 дефицитных

    for i in range(1, 21):  # 20 поставок
        filial = random.choice(FILIALI)
        zavod = ZAVODY[filial]
        kod_sklada = random.choice(SKLADY[zavod])

        dogovor = f"ДОГ-2026-{i:03d}"
        postavshchik = random.choice(POSTAVSHCHIKI)

        # Дата поставки: от завтра до 3 месяцев вперёд
        data_postavki = random_date(
            date.today() + timedelta(days=1),
            date.today() + timedelta(days=90),
        )

        status = random.choices(
            ["confirmed", "in_transit"],
            weights=[40, 60],
        )[0]

        # 1-4 материала в поставке
        n_mat = random.randint(1, 4)
        selected = random.sample(supply_materials, min(n_mat, len(supply_materials)))

        for sys_nomer, naim, ed_izm, _ in selected:
            if ed_izm in ("м",):
                qty = round(random.uniform(100, 2000), 1)
                price = round(random.uniform(800, 5000), 2)
            elif ed_izm in ("шт",):
                qty = round(random.uniform(10, 500), 0)
                price = round(random.uniform(1000, 50000), 2)
            else:
                qty = round(random.uniform(20, 500), 1)
                price = round(random.uniform(500, 10000), 2)

            rows.append({
                "Договор": dogovor,
                "Поставщик": postavshchik,
                "Код склада": kod_sklada,
                "Филиал": filial,
                "Завод": zavod,
                "Системный номер": sys_nomer,
                "Наименование материала": naim,
                "Ед.изм": ed_izm,
                "Дата поставки": data_postavki.strftime("%Y-%m-%d"),
                "Количество": qty,
                "Стоимость за ед": price,
                "Статус": status,
            })

    df = pd.DataFrame(rows)
    print(f"Сгенерировано строк поставок: {len(df)} строк")
    return df


# =============================================================================
# Запуск
# =============================================================================

if __name__ == "__main__":
    print("Генерация тестовых данных для MAPS...")
    print("-" * 50)

    # Потребности
    df_req = generate_requirements(n_works=50)
    req_path = DATA_DIR / "sample_requirements.xlsx"
    df_req.to_excel(req_path, index=False)
    print(f"✓ Потребности сохранены: {req_path}")

    # Остатки
    df_stock = generate_stock()
    stock_path = DATA_DIR / "sample_stock.xlsx"
    df_stock.to_excel(stock_path, index=False)
    print(f"✓ Остатки сохранены: {stock_path}")

    # Поставки
    df_supply = generate_supplies()
    supply_path = DATA_DIR / "sample_supplies.xlsx"
    df_supply.to_excel(supply_path, index=False)
    print(f"✓ Поставки сохранены: {supply_path}")

    print("-" * 50)
    print("Готово! Используйте команды:")
    print("  maps import-requirements data/sample_requirements.xlsx")
    print("  maps import-stock data/sample_stock.xlsx")
    print("  maps import-supplies data/sample_supplies.xlsx")
    print("  maps allocate")
