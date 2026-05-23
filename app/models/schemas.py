"""
app/models/schemas.py — Pydantic-схемы для валидации данных

Что такое Pydantic-схема?
    Это Python-класс, который описывает структуру данных и проверяет их корректность.
    Используется при:
      - Импорте из Excel (проверяем, что данные правильного формата)
      - API запросах/ответах (позже, в Этапе 2)
      - Передаче данных между слоями приложения

Чем схемы отличаются от ORM-моделей (app/db/models.py)?
    ORM-модели  — описывают ТАБЛИЦЫ в базе данных
    Pydantic-схемы — описывают ДАННЫЕ при вводе/выводе (не связаны с БД напрямую)

    Пример: при импорте Excel мы сначала валидируем строку через Pydantic,
    и только если всё правильно — сохраняем в БД через ORM-модель.

Почему Decimal, а не float для количеств и стоимостей?
    float имеет ошибки округления: 0.1 + 0.2 = 0.30000000000000004
    Decimal точный: Decimal("0.1") + Decimal("0.2") = Decimal("0.3")
    В финансовых расчётах и количествах это критично!
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator


# =============================================================================
# Базовый класс — общие настройки для всех схем
# =============================================================================
class MapsBaseModel(BaseModel):
    """
    Базовый класс для всех схем MAPS.

    model_config настраивает поведение Pydantic:
        from_attributes=True  — позволяет создавать схему из ORM-объекта
                                (Work → WorkSchema автоматически)
        str_strip_whitespace  — убирает пробелы в начале/конце строк
                                "  Алматы  " → "Алматы"
    """
    model_config = {
        "from_attributes": True,     # Совместимость с SQLAlchemy ORM
        "str_strip_whitespace": True, # Автоматическая обрезка пробелов
    }


# =============================================================================
# Схемы для работ (Works)
# =============================================================================

class WorkImportRow(MapsBaseModel):
    """
    Схема одной строки Excel при импорте потребностей.

    Используется для валидации каждой строки из файла.
    Если данные некорректны — Pydantic выдаст понятную ошибку.
    """
    # Обязательные поля (нет значения по умолчанию)
    kod_raboty: str = Field(..., description="Код работы", min_length=1)
    sys_nomer_materiala: str = Field(..., description="Системный номер материала")
    potrebnost: Decimal = Field(..., description="Потребность в материале", gt=0)

    # Необязательные поля (None если не заполнено)
    tip_raboty: Optional[str] = None
    filial: Optional[str] = None
    podrazdelenie: Optional[str] = None
    centr_zatrat: Optional[str] = None
    zavod: Optional[str] = None
    data_nachala: Optional[date] = None
    data_okonchaniya: Optional[date] = None
    prioritet: int = Field(default=3, ge=1, le=3, description="Приоритет: 1 (высший) - 3 (низший)")
    status: str = Field(default="active")
    naimenovanie_materiala: Optional[str] = None
    ed_izm: Optional[str] = None

    # Прогнозная цена из исходной заявки — используется для расчёта обеспечённости по стоимости
    prognosnaya_tsena: Decimal = Field(default=Decimal("0"), ge=0, description="Прогнозная цена за ед.")

    @field_validator("prognosnaya_tsena", mode="before")
    @classmethod
    def parse_forecast_price(cls, v: object) -> Decimal:
        if v is None or str(v).strip() in ("", "nan", "None"):
            return Decimal("0")
        try:
            return Decimal(str(v)).quantize(Decimal("0.0001"))
        except Exception:
            return Decimal("0")

    @field_validator("kod_raboty", "sys_nomer_materiala", mode="before")
    @classmethod
    def strip_and_upper(cls, v: object) -> str:
        """
        Нормализация кодов: убираем пробелы, приводим к строке.
        Excel иногда добавляет лишние пробелы или форматирует числа как float.
        Например, SAP-номер 10012345 может прийти как 10012345.0 — исправляем.
        """
        if v is None:
            return ""
        s = str(v).strip()
        # Если SAP-номер пришёл как float "10012345.0" → убираем ".0"
        if s.endswith(".0"):
            s = s[:-2]
        return s

    @field_validator("potrebnost", mode="before")
    @classmethod
    def parse_quantity(cls, v: object) -> Decimal:
        """
        Преобразуем количество из любого числового формата в Decimal.
        Excel может хранить числа как int, float или строку.
        """
        try:
            return Decimal(str(v)).quantize(Decimal("0.0001"))
        except Exception:
            raise ValueError(f"Некорректное количество: {v!r}")

    @model_validator(mode="after")
    def check_dates(self) -> "WorkImportRow":
        """Проверяем, что дата начала не позже даты окончания."""
        if (
            self.data_nachala is not None
            and self.data_okonchaniya is not None
            and self.data_nachala > self.data_okonchaniya
        ):
            raise ValueError(
                f"Дата начала ({self.data_nachala}) не может быть позже "
                f"даты окончания ({self.data_okonchaniya})"
            )
        return self


class WorkOut(MapsBaseModel):
    """Схема для вывода информации о работе (например, в API-ответе)."""
    id: int
    kod_raboty: str
    tip_raboty: Optional[str] = None
    filial: Optional[str] = None
    zavod: Optional[str] = None
    data_nachala: Optional[date] = None
    data_okonchaniya: Optional[date] = None
    prioritet: int
    status: str


# =============================================================================
# Схемы для складских остатков
# =============================================================================

class StockImportRow(MapsBaseModel):
    """
    Схема строки при импорте складских остатков из Excel.

    Каждая строка = одна партия материала на складе.
    """
    kod_sklada: str = Field(..., description="Код склада")
    sys_nomer_materiala: str = Field(..., description="Системный номер материала")
    kolichestvo: Decimal = Field(..., description="Количество в партии", ge=0)

    # Данные партии
    nomer_partii: Optional[str] = None
    stoimost_za_ed: Decimal = Field(default=Decimal("0"), ge=0, description="Стоимость за единицу")
    data_postupleniya: Optional[date] = None

    # Данные склада
    tip_sklada: Optional[str] = None
    filial_sklada: Optional[str] = None
    zavod_sklada: Optional[str] = None

    # Данные материала
    naimenovanie_materiala: Optional[str] = None
    ed_izm: Optional[str] = None
    gruppa_materiala: Optional[str] = None

    @field_validator("kod_sklada", "sys_nomer_materiala", "nomer_partii", mode="before")
    @classmethod
    def clean_string(cls, v: object) -> Optional[str]:
        if v is None:
            return None
        s = str(v).strip()
        if s.endswith(".0"):
            s = s[:-2]
        return s or None

    @field_validator("kolichestvo", "stoimost_za_ed", mode="before")
    @classmethod
    def parse_decimal(cls, v: object) -> Decimal:
        try:
            return Decimal(str(v)).quantize(Decimal("0.0001"))
        except Exception:
            raise ValueError(f"Некорректное числовое значение: {v!r}")


# =============================================================================
# Схемы для поставок
# =============================================================================

class SupplyImportRow(MapsBaseModel):
    """
    Схема строки при импорте поставок ("материалы в пути").
    """
    sys_nomer_materiala: str = Field(..., description="Системный номер материала")
    kolichestvo: Decimal = Field(..., description="Количество в поставке", gt=0)

    dogovor: Optional[str] = None
    postavshchik: Optional[str] = None
    kod_sklada: Optional[str] = None
    filial: Optional[str] = None
    zavod: Optional[str] = None
    data_postavki: Optional[date] = None
    stoimost_za_ed: Decimal = Field(default=Decimal("0"), ge=0)
    status: str = Field(default="confirmed")
    naimenovanie_materiala: Optional[str] = None
    ed_izm: Optional[str] = None

    @field_validator("sys_nomer_materiala", "dogovor", "kod_sklada", mode="before")
    @classmethod
    def clean_string(cls, v: object) -> Optional[str]:
        if v is None:
            return None
        s = str(v).strip()
        if s.endswith(".0"):
            s = s[:-2]
        return s or None

    @field_validator("kolichestvo", "stoimost_za_ed", mode="before")
    @classmethod
    def parse_decimal(cls, v: object) -> Decimal:
        try:
            return Decimal(str(v)).quantize(Decimal("0.0001"))
        except Exception:
            raise ValueError(f"Некорректное числовое значение: {v!r}")


# =============================================================================
# Схемы результатов распределения (для экспорта в Excel / API)
# =============================================================================

# =============================================================================
# Схемы для фактических списаний (Слой 1 распределения)
# =============================================================================

class WriteOffImportRow(MapsBaseModel):
    """
    Схема одной строки Excel при импорте фактических списаний на работы.

    Что такое «фактическое списание»?
        Это операция в SAP, когда материал физически ушёл на объект и
        документально оформлено его списание на конкретную работу.
        Это самый достоверный источник: «Кабель уже лежит на стройплощадке,
        документ проведён, материал с баланса снят».

    Зачем это нужно в MAPS?
        Если материал уже списан на работу, он закрывает часть потребности.
        Алгоритм видит: «Нужно 100 м, уже списано 60 м → остаток к распределению 40 м».

    Колонки в Excel:
        | Код работы | Системный номер | Количество | Стоимость за ед | Сумма | Документ | Дата |
    """
    # --- Обязательные поля ---
    # Код работы — ключ, по которому мы найдём работу в БД
    kod_raboty: str = Field(..., description="Код работы", min_length=1)
    # Системный номер — ключ для поиска материала в БД
    sys_nomer_materiala: str = Field(..., description="Системный номер материала")
    # Количество — сколько списано (обязательно > 0)
    kolichestvo: Decimal = Field(..., description="Количество списания", gt=0)

    # --- Необязательные поля ---
    # Стоимость за единицу — используется для расчёта суммы и % обеспечённости по стоимости
    stoimost_za_ed: Decimal = Field(default=Decimal("0"), ge=0, description="Цена за единицу")
    # Сумма = количество × стоимость за ед (может быть заполнена напрямую из Excel)
    summa: Decimal = Field(default=Decimal("0"), ge=0, description="Сумма списания")

    # Номер документа списания в SAP — для трассировки и аудита
    nomer_dokumenta: Optional[str] = None
    # Дата списания — информативно, для отчётности
    data_spisaniya: Optional[date] = None
    # Наименование и единица измерения — для создания материала если его нет в БД
    naimenovanie_materiala: Optional[str] = None
    ed_izm: Optional[str] = None

    @field_validator("kod_raboty", "sys_nomer_materiala", "nomer_dokumenta", mode="before")
    @classmethod
    def clean_string(cls, v: object) -> Optional[str]:
        """
        Нормализация строковых полей.
        Excel часто добавляет пробелы или форматирует числа как '10012345.0'.
        Убираем лишнее и приводим к чистой строке.
        """
        if v is None:
            return None
        s = str(v).strip()
        # SAP-номер может прийти как '10012345.0' из-за формата Excel → убираем '.0'
        if s.endswith(".0"):
            s = s[:-2]
        return s or None  # Пустую строку превращаем в None

    @field_validator("kolichestvo", "stoimost_za_ed", "summa", mode="before")
    @classmethod
    def parse_decimal(cls, v: object) -> Decimal:
        """
        Преобразование числовых полей из любого формата в Decimal.
        В Excel числа могут быть int, float, строкой или пустыми.
        Decimal точнее float для финансовых вычислений.
        """
        if v is None or str(v).strip() in ("", "nan", "None"):
            return Decimal("0")
        try:
            return Decimal(str(v)).quantize(Decimal("0.0001"))
        except Exception:
            raise ValueError(f"Некорректное числовое значение: {v!r}")


# =============================================================================
# Схемы для «Выдано не списано» (Слой 2 распределения)
# =============================================================================

class IssuedNotWrittenOffImportRow(MapsBaseModel):
    """
    Схема одной строки Excel при импорте «Выдано не списано».

    Что такое «Выдано не списано»?
        Материал уже выдан со склада на объект (подрядчик его получил физически),
        но документ списания в SAP ещё не проведён.
        Это «серая зона»: материал де-факто уже используется, но де-юре ещё числится.

        Пример: Утром выдали 100 м кабеля бригаде, вечером оформят документ.
        В течение дня — это «Выдано не списано».

    Зачем код_склада?
        Указывается информативно — чтобы понять, с какого склада была выдача.
        На остатки склада НЕ влияет (склад уже отдал материал физически),
        и алгоритм тоже не изменяет остатки при обработке этого слоя.

    Колонки в Excel:
        | Код работы | Системный номер | Количество | Стоимость за ед | Сумма | Код склада | Дата выдачи |
    """
    # --- Обязательные поля ---
    kod_raboty: str = Field(..., description="Код работы", min_length=1)
    sys_nomer_materiala: str = Field(..., description="Системный номер материала")
    kolichestvo: Decimal = Field(..., description="Количество выданного", gt=0)

    # --- Необязательные поля ---
    stoimost_za_ed: Decimal = Field(default=Decimal("0"), ge=0)
    summa: Decimal = Field(default=Decimal("0"), ge=0)
    # Склад — только для информации, не влияет на остатки
    kod_sklada: Optional[str] = None
    data_vydachi: Optional[date] = None
    naimenovanie_materiala: Optional[str] = None
    ed_izm: Optional[str] = None

    @field_validator("kod_raboty", "sys_nomer_materiala", "kod_sklada", mode="before")
    @classmethod
    def clean_string(cls, v: object) -> Optional[str]:
        """Нормализация строковых полей: убираем пробелы, исправляем '.0'."""
        if v is None:
            return None
        s = str(v).strip()
        if s.endswith(".0"):
            s = s[:-2]
        return s or None

    @field_validator("kolichestvo", "stoimost_za_ed", "summa", mode="before")
    @classmethod
    def parse_decimal(cls, v: object) -> Decimal:
        """Преобразование числовых полей из любого формата в Decimal."""
        if v is None or str(v).strip() in ("", "nan", "None"):
            return Decimal("0")
        try:
            return Decimal(str(v)).quantize(Decimal("0.0001"))
        except Exception:
            raise ValueError(f"Некорректное числовое значение: {v!r}")


class AllocationResultOut(MapsBaseModel):
    """Одна строка результата распределения."""
    session_id: str
    kod_raboty: str
    filial_raboty: Optional[str] = None
    prioritet: int
    sys_nomer_materiala: str
    naimenovanie_materiala: Optional[str] = None
    ed_izm: Optional[str] = None
    istochnik: str                    # "sklad" | "postavka" | "zakup"
    kod_sklada: Optional[str] = None
    nomer_partii: Optional[str] = None
    tip_raspredeleniya: Optional[str] = None  # "zavod" | "filial" | "prochee"
    kolichestvo: Decimal
    stoimost_za_ed: Decimal
    summa: Decimal


class DeficitRecordOut(MapsBaseModel):
    """Одна строка дефицита."""
    session_id: str
    kod_raboty: str
    filial_raboty: Optional[str] = None
    sys_nomer_materiala: str
    naimenovanie_materiala: Optional[str] = None
    ed_izm: Optional[str] = None
    deficit_qty: Decimal
    estimated_cost: Decimal
    needed_by: Optional[date] = None


class CoverageReport(MapsBaseModel):
    """Отчёт об обеспечённости работ материалами."""
    session_id: str
    generated_at: datetime
    total_requirements: int       # Всего строк потребностей
    fully_covered: int            # Полностью обеспечено
    partially_covered: int        # Частично обеспечено
    not_covered: int              # Не обеспечено совсем
    coverage_pct: float           # Общий % обеспечённости

    # Детализация по филиалам
    by_filial: dict[str, float] = Field(default_factory=dict)
