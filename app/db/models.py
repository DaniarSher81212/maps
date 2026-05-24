"""
app/db/models.py — ORM-модели (таблицы базы данных)

Что такое ORM-модель?
    Класс Python, который соответствует таблице в БД.
    Каждый атрибут класса = колонка таблицы.
    Каждый экземпляр класса = строка в таблице.

Пример:
    work = Work(kod_raboty="W-001", filial="Алматы", prioritet=1)
    session.add(work)
    session.commit()
    # В таблице works появится новая строка

Слои распределения (порядок приоритета):
    1. Списание (WriteOff)                    — фактически списано в SAP
    2. Выдано не списано (IssuedNotWrittenOff) — выдано на объект, не проведено
    3. Склад своего завода / филиала          — текущие остатки, FIFO
    4. Поставки (Supply)                      — материалы в пути (по договору, по филиалу)
    5. К закупу (DeficitRecord)               — дефицит, нужно закупить

    Дополнительно (только для анализа, НЕ учитывается в дефиците):
       Возможное перемещение — склады других филиалов (is_possible=True)

Схема данных:
    ┌──────────────┐     ┌──────────────┐     ┌───────────────┐
    │    works     │     │  materials   │     │  warehouses   │
    ├──────────────┤     ├──────────────┤     ├───────────────┤
    │ id           │     │ id           │     │ id            │
    │ kod_raboty   │     │ sys_nomer    │     │ kod_sklada    │
    │ is_emergency │     │ naimenovanie │     │ filial        │
    │ prioritet    │     │ ed_izm       │     │ zavod         │
    └──────┬───────┘     └──────┬───────┘     └──────┬────────┘
           │                   │                     │
           └─────────┐  ┌──────┘              ┌──────┘
                     ▼  ▼                     ▼
              ┌──────────────┐         ┌──────────────────┐
              │ requirements │         │  stock_batches   │
              ├──────────────┤         ├──────────────────┤
              │ work_id (FK) │         │ warehouse_id (FK)│
              │ material_id  │         │ material_id (FK) │
              │ potrebnost   │         │ kolichestvo      │
              │ prognosnaya_ │         │ data_postupleniya│
              │   tsena      │         └──────────────────┘
              │ raspredeleno │
              └──────────────┘
"""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base


# =============================================================================
# ВХОДНЫЕ ТАБЛИЦЫ (данные загружаются из Excel или SAP)
# =============================================================================

class Work(Base):
    """
    Таблица works — строительно-монтажные работы (СМР).

    Каждая строка = одна работа (заявка, наряд-задание и т.д.).
    Работы конкурируют за материалы — порядок распределения:
        1. Аварийные работы (is_emergency=True) — ВСЕГДА первыми, по дате начала
        2. Обычные работы — по дате начала (ГЛАВНЫЙ приоритет), затем prioritet
    """
    __tablename__ = "works"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # Уникальный бизнес-код работы (из SAP PS/PM)
    kod_raboty: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)

    # Наименование работы (описание/название из перечня работ).
    # Загружается через import-works — отдельный файл «Перечень работ».
    # Может быть None если перечень работ ещё не загружался.
    # Используется в листе «Обеспеченность» Excel-отчёта.
    nazvanie: Mapped[str | None] = mapped_column(Text)

    # Тип работы: "ТО", "Ремонт", "Монтаж", "Аварийный" и т.д.
    tip_raboty: Mapped[str | None] = mapped_column(String(100))

    # Аварийная работа: True — загружается из отдельного файла и идёт самой первой.
    # Внутри аварийных работ порядок — только по дате начала (prioritet не учитывается).
    is_emergency: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Организационная структура
    filial: Mapped[str | None] = mapped_column(String(100))         # Филиал компании
    podrazdelenie: Mapped[str | None] = mapped_column(String(100))  # Подразделение
    centr_zatrat: Mapped[str | None] = mapped_column(String(50))    # Центр затрат (CC)
    zavod: Mapped[str | None] = mapped_column(String(50))           # Код завода (Plant в SAP)

    # Временные рамки
    data_nachala: Mapped[date | None] = mapped_column(Date)         # Дата начала — ГЛАВНЫЙ приоритет
    data_okonchaniya: Mapped[date | None] = mapped_column(Date)     # Дата окончания

    # Приоритет для ОБЫЧНЫХ (не аварийных) работ:
    #   1 — высший (критичный путь)
    #   2 — средний (плановые)
    #   3 — низший (резервные)
    # Для аварийных работ (is_emergency=True) это поле НЕ учитывается при сортировке.
    prioritet: Mapped[int] = mapped_column(default=3)

    # Статус: "active", "completed", "cancelled", "on_hold"
    status: Mapped[str] = mapped_column(String(50), default="active")

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    # Связи
    requirements: Mapped[list["Requirement"]] = relationship(back_populates="work")

    def __repr__(self) -> str:
        emerg = " [АВАРИЙНАЯ]" if self.is_emergency else ""
        return f"<Work id={self.id} kod={self.kod_raboty!r} filial={self.filial!r} prio={self.prioritet}{emerg}>"


class Material(Base):
    """
    Таблица materials — справочник материалов (номенклатура).

    Каждый материал имеет уникальный системный номер (sys_nomer),
    по которому он идентифицируется в SAP MM.
    """
    __tablename__ = "materials"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # Системный номер материала в SAP (18-значный код): "000000000010012345"
    sys_nomer: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)

    naimenovanie: Mapped[str | None] = mapped_column(Text)
    ed_izm: Mapped[str | None] = mapped_column(String(20))
    gruppa: Mapped[str | None] = mapped_column(String(100))

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    requirements: Mapped[list["Requirement"]] = relationship(back_populates="material")
    stock_batches: Mapped[list["StockBatch"]] = relationship(back_populates="material")
    supply_lines: Mapped[list["SupplyLine"]] = relationship(back_populates="material")

    def __repr__(self) -> str:
        name_preview = self.naimenovanie[:30] if self.naimenovanie else None
        return f"<Material id={self.id} sys_nomer={self.sys_nomer!r} name={name_preview!r}>"


class Requirement(Base):
    """
    Таблица requirements — потребности работ в материалах.

    Это связующая таблица между Work и Material.
    Показывает: "Работа W-001 требует 50 кг кабеля ВВГ по 1500 тг/кг".

    Поля raspredeleno и deficit обновляются алгоритмом распределения.

    prognosnaya_tsena — прогнозная цена из исходной заявки. Используется для:
      - Расчёта прогнозной стоимости потребности
      - Формирования стоимости «К закупу»
      - Расчёта % обеспечённости (по стоимости)
    """
    __tablename__ = "requirements"
    __table_args__ = (
        Index("ix_requirements_work_material", "work_id", "material_id", unique=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    work_id: Mapped[int] = mapped_column(ForeignKey("works.id"), nullable=False)
    material_id: Mapped[int] = mapped_column(ForeignKey("materials.id"), nullable=False)

    # Количество материала, которое нужно для этой работы
    potrebnost: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)

    # Прогнозная цена за единицу из исходной заявки (загружается при импорте потребностей)
    # Используется для расчёта обеспечённости по стоимости и «К закупу»
    prognosnaya_tsena: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)

    # Количество уже распределённого материала (заполняется алгоритмом)
    raspredeleno: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)

    # Дефицит = potrebnost - raspredeleno (заполняется алгоритмом)
    deficit: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    work: Mapped["Work"] = relationship(back_populates="requirements")
    material: Mapped["Material"] = relationship(back_populates="requirements")

    def __repr__(self) -> str:
        return (
            f"<Requirement work_id={self.work_id} mat_id={self.material_id} "
            f"need={self.potrebnost} alloc={self.raspredeleno}>"
        )


class WriteOff(Base):
    """
    Таблица writeoffs — фактические списания материалов на работы (Слой 1).

    Загружается из Excel-файла выгрузки SAP.
    Это материалы, которые уже физически списаны — самый надёжный источник покрытия.

    В алгоритме: применяется ПЕРВЫМ, уменьшает остаток потребности (remaining).
    """
    __tablename__ = "writeoffs"
    __table_args__ = (
        Index("ix_writeoffs_work_material", "work_id", "material_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    work_id: Mapped[int] = mapped_column(ForeignKey("works.id"), nullable=False)
    material_id: Mapped[int] = mapped_column(ForeignKey("materials.id"), nullable=False)

    # Количество и стоимость списания
    kolichestvo: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)
    stoimost_za_ed: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)
    summa: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)  # = qty * price

    # Реквизиты документа списания (информативно)
    nomer_dokumenta: Mapped[str | None] = mapped_column(String(100))
    data_spisaniya: Mapped[date | None] = mapped_column(Date)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    work: Mapped["Work"] = relationship()
    material: Mapped["Material"] = relationship()

    def __repr__(self) -> str:
        return f"<WriteOff work={self.work_id} mat={self.material_id} qty={self.kolichestvo}>"


class IssuedNotWrittenOff(Base):
    """
    Таблица issued_not_written_off — выдано под работу, но ещё не списано (Слой 2).

    Загружается из Excel-файла.
    Материал уже находится на объекте (выдан со склада), но документ списания
    ещё не оформлен в SAP.

    В алгоритме: применяется ВТОРЫМ, после фактических списаний.
    warehouse_id — информативное поле (не влияет на остатки склада).
    """
    __tablename__ = "issued_not_written_off"
    __table_args__ = (
        Index("ix_issued_work_material", "work_id", "material_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    work_id: Mapped[int] = mapped_column(ForeignKey("works.id"), nullable=False)
    material_id: Mapped[int] = mapped_column(ForeignKey("materials.id"), nullable=False)

    # Склад, с которого выдано — только для информации, остатки НЕ трогаем
    warehouse_id: Mapped[int | None] = mapped_column(ForeignKey("warehouses.id"))

    kolichestvo: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)
    stoimost_za_ed: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)
    summa: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)

    data_vydachi: Mapped[date | None] = mapped_column(Date)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    work: Mapped["Work"] = relationship()
    material: Mapped["Material"] = relationship()

    def __repr__(self) -> str:
        return f"<IssuedNotWrittenOff work={self.work_id} mat={self.material_id} qty={self.kolichestvo}>"


class Warehouse(Base):
    """
    Таблица warehouses — справочник складов.

    Склад имеет привязку к филиалу и заводу (Plant в SAP).
    Приоритеты распределения со склада:
        1. Склад того же завода (zavod == work.zavod)
        2. Склад того же филиала (filial == work.filial, другой завод)
        3. Склады других филиалов — только «возможное» (не фактическое)
    """
    __tablename__ = "warehouses"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    kod_sklada: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
    tip_sklada: Mapped[str | None] = mapped_column(String(50))
    filial: Mapped[str | None] = mapped_column(String(100))
    zavod: Mapped[str | None] = mapped_column(String(50))

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    stock_batches: Mapped[list["StockBatch"]] = relationship(back_populates="warehouse")

    def __repr__(self) -> str:
        return f"<Warehouse id={self.id} kod={self.kod_sklada!r} filial={self.filial!r}>"


class StockBatch(Base):
    """
    Таблица stock_batches — партии материалов на складах.

    Разные партии могут иметь разную цену закупки.
    FIFO (First In First Out) — сначала списываются старые партии.

    При агрегации нескольких партий одного склада считается средняя взвешенная цена:
        avg = sum(qty_i * price_i) / sum(qty_i)
    """
    __tablename__ = "stock_batches"
    __table_args__ = (
        Index("ix_stock_batches_material", "material_id"),
        Index("ix_stock_batches_fifo", "material_id", "data_postupleniya"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    warehouse_id: Mapped[int] = mapped_column(ForeignKey("warehouses.id"), nullable=False)
    material_id: Mapped[int] = mapped_column(ForeignKey("materials.id"), nullable=False)

    nomer_partii: Mapped[str | None] = mapped_column(String(100))
    kolichestvo: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)

    # Доступное количество — обновляется алгоритмом (уменьшается при распределении)
    dostupno: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)

    stoimost_za_ed: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)
    data_postupleniya: Mapped[date | None] = mapped_column(Date)  # Для FIFO-сортировки

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    warehouse: Mapped["Warehouse"] = relationship(back_populates="stock_batches")
    material: Mapped["Material"] = relationship(back_populates="stock_batches")

    def __repr__(self) -> str:
        return (
            f"<StockBatch id={self.id} wh={self.warehouse_id} "
            f"mat={self.material_id} qty={self.kolichestvo} date={self.data_postupleniya}>"
        )


class Supply(Base):
    """
    Таблица supplies — заказы на поставку (материалы «в пути»).

    Поставки идут на ФИЛИАЛ (не на завод).
    Это ключевое отличие от складских остатков: при распределении
    из поставок мы сравниваем filial работы с filial поставки.
    """
    __tablename__ = "supplies"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    dogovor: Mapped[str | None] = mapped_column(String(100))       # Номер договора
    postavshchik: Mapped[str | None] = mapped_column(String(200))  # Наименование поставщика

    warehouse_id: Mapped[int | None] = mapped_column(ForeignKey("warehouses.id"))

    # Поставки привязаны к ФИЛИАЛУ — используется для сопоставления с работой
    filial: Mapped[str | None] = mapped_column(String(100))
    zavod: Mapped[str | None] = mapped_column(String(50))

    data_postavki: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(50), default="confirmed")

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    lines: Mapped[list["SupplyLine"]] = relationship(back_populates="supply")

    def __repr__(self) -> str:
        return f"<Supply id={self.id} dogovor={self.dogovor!r} filial={self.filial!r} date={self.data_postavki}>"


class SupplyLine(Base):
    """
    Таблица supply_lines — строки поставки (конкретные материалы в поставке).

    Одна поставка (Supply) → много материалов (SupplyLine).
    """
    __tablename__ = "supply_lines"
    __table_args__ = (
        Index("ix_supply_lines_material", "material_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    supply_id: Mapped[int] = mapped_column(ForeignKey("supplies.id"), nullable=False)
    material_id: Mapped[int] = mapped_column(ForeignKey("materials.id"), nullable=False)

    kolichestvo: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)
    dostupno: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)
    stoimost_za_ed: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)

    supply: Mapped["Supply"] = relationship(back_populates="lines")
    material: Mapped["Material"] = relationship(back_populates="supply_lines")

    def __repr__(self) -> str:
        return f"<SupplyLine id={self.id} supply={self.supply_id} mat={self.material_id} qty={self.kolichestvo}>"


# =============================================================================
# ВЫХОДНЫЕ ТАБЛИЦЫ (результаты работы алгоритма)
# =============================================================================

class AllocationSession(Base):
    """
    Таблица allocation_sessions — сессии запуска распределения.

    Каждый запуск алгоритма создаёт новую сессию.
    Позволяет хранить несколько вариантов распределения (сценарии).
    """
    __tablename__ = "allocation_sessions"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(50), default="running")

    total_requirements: Mapped[int] = mapped_column(default=0)
    total_allocated: Mapped[int] = mapped_column(default=0)
    total_deficit: Mapped[int] = mapped_column(default=0)

    notes: Mapped[str | None] = mapped_column(Text)

    allocations: Mapped[list["AllocationResult"]] = relationship(back_populates="session")
    movements: Mapped[list["StockMovement"]] = relationship(back_populates="session")
    deficits: Mapped[list["DeficitRecord"]] = relationship(back_populates="session")

    def __repr__(self) -> str:
        return f"<Session id={self.id!r} status={self.status!r}>"


class AllocationResult(Base):
    """
    Таблица allocation_results — результаты распределения по слоям.

    Каждая строка = одно распределение для одной потребности.

    Значения istochnik:
        "spisanie"         — Слой 1: фактически списано (из WriteOff)
        "vydano"           — Слой 2: выдано не списано (из IssuedNotWrittenOff)
        "sklad"            — Слой 3: распределено со склада (того же завода/филиала)
        "vozmozhnoe_sklad" — Анализ: склад другого филиала (is_possible=True)
        "postavka"         — Слой 4: зарезервировано из поставки
        "zakup"            — Слой 5: дефицит, нужно закупить

    Для is_possible=True (возможное движение):
        - Остатки НЕ уменьшаются
        - В дефиците НЕ участвует
        - Используется только для анализа межфилиального переноса
    """
    __tablename__ = "allocation_results"
    __table_args__ = (
        Index("ix_alloc_results_session", "session_id"),
        Index("ix_alloc_results_work", "work_id"),
        Index("ix_alloc_results_material", "material_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("allocation_sessions.id"), nullable=False)

    requirement_id: Mapped[int | None] = mapped_column(ForeignKey("requirements.id"))
    work_id: Mapped[int] = mapped_column(ForeignKey("works.id"), nullable=False)
    material_id: Mapped[int] = mapped_column(ForeignKey("materials.id"), nullable=False)

    istochnik: Mapped[str] = mapped_column(String(50))

    # Откуда взяли (если со склада/поставки)
    warehouse_id: Mapped[int | None] = mapped_column(ForeignKey("warehouses.id"))
    supply_line_id: Mapped[int | None] = mapped_column(ForeignKey("supply_lines.id"))

    # Количество и средняя взвешенная стоимость.
    # Для склада: weighted avg по всем партиям с одного склада.
    # Для слоёв 1, 2, 4: avg по записям в источнике.
    kolichestvo: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)
    srednyaya_stoimost: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)
    summa: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)

    # Уточнение для складского распределения: "zavod" / "filial" / "vozmozhnoe"
    tip_raspredeleniya: Mapped[str | None] = mapped_column(String(50))

    # True = возможное (анализ), False = фактическое (меняет остатки)
    is_possible: Mapped[bool] = mapped_column(default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    session: Mapped["AllocationSession"] = relationship(back_populates="allocations")

    def __repr__(self) -> str:
        return (
            f"<AllocResult work={self.work_id} mat={self.material_id} "
            f"src={self.istochnik!r} qty={self.kolichestvo}>"
        )


class StockMovement(Base):
    """
    Таблица stock_movements — движение по складам (детально, по партиям).

    Фиксирует каждое списание со склада по конкретной партии.
    Используется для детального листа «Движение склада» в Excel-отчёте.
    """
    __tablename__ = "stock_movements"
    __table_args__ = (
        Index("ix_movements_session", "session_id"),
        Index("ix_movements_warehouse_material", "warehouse_id", "material_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("allocation_sessions.id"), nullable=False)

    warehouse_id: Mapped[int] = mapped_column(ForeignKey("warehouses.id"), nullable=False)
    material_id: Mapped[int] = mapped_column(ForeignKey("materials.id"), nullable=False)
    batch_id: Mapped[int | None] = mapped_column(ForeignKey("stock_batches.id"))
    work_id: Mapped[int | None] = mapped_column(ForeignKey("works.id"))

    izmenenie: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)  # Отрицательное = расход
    ostatok: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)    # После движения

    data_dvizheniya: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    session: Mapped["AllocationSession"] = relationship(back_populates="movements")

    def __repr__(self) -> str:
        return (
            f"<Movement wh={self.warehouse_id} mat={self.material_id} "
            f"delta={self.izmenenie} остаток={self.ostatok}>"
        )


class DeficitRecord(Base):
    """
    Таблица deficit_records — записи о дефиците (Слой 5: К закупу).

    Создаётся когда потребность не покрыта ни одним из слоёв 1-4.
    """
    __tablename__ = "deficit_records"
    __table_args__ = (
        Index("ix_deficit_session", "session_id"),
        Index("ix_deficit_material", "material_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("allocation_sessions.id"), nullable=False)

    requirement_id: Mapped[int | None] = mapped_column(ForeignKey("requirements.id"))
    work_id: Mapped[int] = mapped_column(ForeignKey("works.id"), nullable=False)
    material_id: Mapped[int] = mapped_column(ForeignKey("materials.id"), nullable=False)

    deficit_qty: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)

    # Оценочная стоимость дефицита = deficit_qty × prognosnaya_tsena из Requirement
    estimated_cost: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)

    needed_by: Mapped[date | None] = mapped_column(Date)  # Дата окончания работы

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    session: Mapped["AllocationSession"] = relationship(back_populates="deficits")

    def __repr__(self) -> str:
        return (
            f"<Deficit work={self.work_id} mat={self.material_id} "
            f"qty={self.deficit_qty} needed_by={self.needed_by}>"
        )
