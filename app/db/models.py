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

Схема данных:
    ┌──────────────┐     ┌──────────────┐     ┌───────────────┐
    │    works     │     │  materials   │     │  warehouses   │
    ├──────────────┤     ├──────────────┤     ├───────────────┤
    │ id           │     │ id           │     │ id            │
    │ kod_raboty   │     │ sys_nomer    │     │ kod_sklada    │
    │ filial       │     │ naimenovanie │     │ filial        │
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
              │ raspredeleno │         │ data_postupleniya│
              └──────────────┘         └──────────────────┘
"""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
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
    Работы конкурируют за материалы — побеждает тот, у кого выше приоритет
    и раньше дата начала.
    """
    __tablename__ = "works"

    # Первичный ключ — уникальный ID, генерируется автоматически
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # Уникальный бизнес-код работы (из SAP PS/PM)
    # Например: "P-2026-001", "WO-12345"
    kod_raboty: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)

    # Тип работы: "ТО" (техническое обслуживание), "Ремонт", "Монтаж" и т.д.
    tip_raboty: Mapped[str | None] = mapped_column(String(100))

    # Организационная структура
    filial: Mapped[str | None] = mapped_column(String(100))         # Филиал компании
    podrazdelenie: Mapped[str | None] = mapped_column(String(100))  # Подразделение
    centr_zatrat: Mapped[str | None] = mapped_column(String(50))    # Центр затрат (CC)
    zavod: Mapped[str | None] = mapped_column(String(50))           # Код завода (Plant в SAP)

    # Временные рамки
    data_nachala: Mapped[date | None] = mapped_column(Date)         # Дата начала работ
    data_okonchaniya: Mapped[date | None] = mapped_column(Date)     # Дата окончания

    # Приоритет определяет порядок распределения материалов:
    #   1 — высший (аварийные работы, критичный путь)
    #   2 — средний (плановые работы)
    #   3 — низший (резервные работы)
    prioritet: Mapped[int] = mapped_column(default=3)

    # Статус: "active", "completed", "cancelled", "on_hold"
    status: Mapped[str] = mapped_column(String(50), default="active")

    # Дата и время создания записи (заполняется автоматически)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    # Связь с потребностями (один ко многим: одна работа → много потребностей)
    requirements: Mapped[list["Requirement"]] = relationship(back_populates="work")

    def __repr__(self) -> str:
        return f"<Work id={self.id} kod={self.kod_raboty!r} filial={self.filial!r} prio={self.prioritet}>"


class Material(Base):
    """
    Таблица materials — справочник материалов (номенклатура).

    Каждый материал имеет уникальный системный номер (sys_nomer),
    по которому он идентифицируется в SAP MM.
    """
    __tablename__ = "materials"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # Системный номер материала в SAP (18-значный код)
    # Например: "000000000010012345"
    sys_nomer: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)

    # Понятное название: "Кабель ВВГ 3х2,5 мм"
    naimenovanie: Mapped[str | None] = mapped_column(Text)

    # Единица измерения: "шт", "м", "кг", "л" и т.д.
    ed_izm: Mapped[str | None] = mapped_column(String(20))

    # Группа материалов: "Кабели", "Трубы", "Арматура" и т.д.
    gruppa: Mapped[str | None] = mapped_column(String(100))

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    # Связи
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
    Показывает: "Работа W-001 требует 50 кг кабеля ВВГ".

    Поля raspredeleno и deficit обновляются алгоритмом распределения.
    """
    __tablename__ = "requirements"
    __table_args__ = (
        # Составной уникальный индекс: одна работа не может дважды требовать
        # один и тот же материал (дубли объединяются на этапе импорта)
        Index("ix_requirements_work_material", "work_id", "material_id", unique=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # Внешние ключи (FK — Foreign Key): ссылаются на строки других таблиц
    work_id: Mapped[int] = mapped_column(ForeignKey("works.id"), nullable=False)
    material_id: Mapped[int] = mapped_column(ForeignKey("materials.id"), nullable=False)

    # Количество материала, которое нужно для этой работы
    potrebnost: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)

    # Количество уже распределённого материала (заполняется алгоритмом)
    raspredeleno: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)

    # Дефицит = potrebnost - raspredeleno (заполняется алгоритмом)
    deficit: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    # Связи для удобного доступа к связанным объектам
    work: Mapped["Work"] = relationship(back_populates="requirements")
    material: Mapped["Material"] = relationship(back_populates="requirements")

    def __repr__(self) -> str:
        return (
            f"<Requirement work_id={self.work_id} mat_id={self.material_id} "
            f"need={self.potrebnost} alloc={self.raspredeleno}>"
        )


class Warehouse(Base):
    """
    Таблица warehouses — справочник складов.

    Склад имеет привязку к филиалу и заводу (Plant в SAP).
    Эта привязка используется в алгоритме приоритетов:
    материал сначала берётся со склада того же завода, что и работа.
    """
    __tablename__ = "warehouses"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # Код склада в SAP: например "0001", "W-ALM-01"
    kod_sklada: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)

    # Тип: "центральный", "филиальный", "подрядчика"
    tip_sklada: Mapped[str | None] = mapped_column(String(50))

    filial: Mapped[str | None] = mapped_column(String(100))  # Филиал
    zavod: Mapped[str | None] = mapped_column(String(50))    # Код завода (Plant)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    # Связи
    stock_batches: Mapped[list["StockBatch"]] = relationship(back_populates="warehouse")

    def __repr__(self) -> str:
        return f"<Warehouse id={self.id} kod={self.kod_sklada!r} filial={self.filial!r}>"


class StockBatch(Base):
    """
    Таблица stock_batches — партии материалов на складах.

    Почему партии, а не просто остаток?
        Разные партии могут иметь разную стоимость (цену закупки).
        При списании важно знать, по какой цене списывается материал.
        FIFO (First In First Out) — сначала списываются более старые партии.

    Пример:
        Склад W-001, Материал "Кабель ВВГ":
          Партия 001: 100 м, 500 тг/м, поступила 01.01.2026
          Партия 002: 200 м, 520 тг/м, поступила 15.02.2026

        По FIFO сначала будем списывать Партию 001.
    """
    __tablename__ = "stock_batches"
    __table_args__ = (
        # Индекс для быстрого поиска партий по материалу
        Index("ix_stock_batches_material", "material_id"),
        # Индекс для FIFO-сортировки по дате поступления
        Index("ix_stock_batches_fifo", "material_id", "data_postupleniya"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    warehouse_id: Mapped[int] = mapped_column(ForeignKey("warehouses.id"), nullable=False)
    material_id: Mapped[int] = mapped_column(ForeignKey("materials.id"), nullable=False)

    # Номер партии (из SAP или присвоенный при импорте)
    nomer_partii: Mapped[str | None] = mapped_column(String(100))

    # Текущее количество в партии
    kolichestvo: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)

    # Доступное количество (kolichestvo минус уже зарезервированное)
    # Обновляется алгоритмом распределения
    dostupno: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)

    # Стоимость за единицу в этой партии (в тенге или другой валюте)
    stoimost_za_ed: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)

    # Дата поступления партии на склад — используется для FIFO-сортировки
    # Чем раньше дата → тем раньше используется партия
    data_postupleniya: Mapped[date | None] = mapped_column(Date)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    # Связи
    warehouse: Mapped["Warehouse"] = relationship(back_populates="stock_batches")
    material: Mapped["Material"] = relationship(back_populates="stock_batches")

    def __repr__(self) -> str:
        return (
            f"<StockBatch id={self.id} wh={self.warehouse_id} "
            f"mat={self.material_id} qty={self.kolichestvo} date={self.data_postupleniya}>"
        )


class Supply(Base):
    """
    Таблица supplies — заказы на поставку (материалы "в пути").

    Это материалы, которые уже заказаны, но ещё не поступили на склад.
    Алгоритм распределения может резервировать их для работ.

    Статусы поставки:
        "confirmed" — подтверждено поставщиком
        "in_transit" — груз отправлен
        "arrived"   — прибыло, ожидает оприходования
        "cancelled" — отменено
    """
    __tablename__ = "supplies"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # Реквизиты поставки
    dogovor: Mapped[str | None] = mapped_column(String(100))      # Номер договора
    postavshchik: Mapped[str | None] = mapped_column(String(200)) # Наименование поставщика

    # На какой склад ожидается поставка
    warehouse_id: Mapped[int | None] = mapped_column(ForeignKey("warehouses.id"))

    # Организационная привязка (если склад не указан)
    filial: Mapped[str | None] = mapped_column(String(100))
    zavod: Mapped[str | None] = mapped_column(String(50))

    # Ожидаемая дата поставки (используется для анализа рисков)
    data_postavki: Mapped[date | None] = mapped_column(Date)

    # Статус поставки
    status: Mapped[str] = mapped_column(String(50), default="confirmed")

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    # Связи
    lines: Mapped[list["SupplyLine"]] = relationship(back_populates="supply")

    def __repr__(self) -> str:
        return f"<Supply id={self.id} dogovor={self.dogovor!r} date={self.data_postavki}>"


class SupplyLine(Base):
    """
    Таблица supply_lines — строки поставки (конкретные материалы в поставке).

    Одна поставка (Supply) может содержать несколько материалов.
    Каждый материал — это отдельная строка (SupplyLine).
    """
    __tablename__ = "supply_lines"
    __table_args__ = (
        Index("ix_supply_lines_material", "material_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    supply_id: Mapped[int] = mapped_column(ForeignKey("supplies.id"), nullable=False)
    material_id: Mapped[int] = mapped_column(ForeignKey("materials.id"), nullable=False)

    # Количество в поставке
    kolichestvo: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)

    # Доступное количество (обновляется алгоритмом)
    dostupno: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)

    # Стоимость за единицу в этой поставке
    stoimost_za_ed: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)

    # Связи
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

    Зачем нужна сессия?
        Каждый запуск алгоритма создаёт новую сессию.
        Это позволяет:
          - Хранить несколько вариантов распределения (сценарии)
          - Сравнивать результаты разных запусков
          - Откатиться к предыдущему варианту
    """
    __tablename__ = "allocation_sessions"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)  # UUID или "2026-05-23_run1"
    started_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)

    # Статус: "running", "completed", "failed"
    status: Mapped[str] = mapped_column(String(50), default="running")

    # Статистика сессии
    total_requirements: Mapped[int] = mapped_column(default=0)  # Всего потребностей
    total_allocated: Mapped[int] = mapped_column(default=0)     # Распределено строк
    total_deficit: Mapped[int] = mapped_column(default=0)       # Строк с дефицитом

    notes: Mapped[str | None] = mapped_column(Text)  # Произвольные заметки

    # Связи с результатами
    allocations: Mapped[list["AllocationResult"]] = relationship(back_populates="session")
    movements: Mapped[list["StockMovement"]] = relationship(back_populates="session")
    deficits: Mapped[list["DeficitRecord"]] = relationship(back_populates="session")

    def __repr__(self) -> str:
        return f"<Session id={self.id!r} status={self.status!r}>"


class AllocationResult(Base):
    """
    Таблица allocation_results — результаты распределения.

    Каждая строка = одно распределение:
    "Для работы W-001, материала 'Кабель ВВГ', выделено 50 м
     из склада SKL-01, партии 002, по цене 520 тг/м".

    Источники (istochnik):
        "sklad"   — распределено со склада
        "postavka" — зарезервировано из поставки "в пути"
        "zakup"   — дефицит, нужно закупить
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

    # Источник материала:
    #   "sklad"            — фактически распределено со склада (тот же завод или филиал)
    #   "vozmozhnoe_sklad" — возможное распределение (другой филиал, требует согласования)
    #   "postavka"         — зарезервировано из поставки "в пути"
    #   "zakup"            — дефицит, нужно закупить
    istochnik: Mapped[str] = mapped_column(String(50))

    # Откуда взяли (если со склада/поставки)
    warehouse_id: Mapped[int | None] = mapped_column(ForeignKey("warehouses.id"))
    supply_line_id: Mapped[int | None] = mapped_column(ForeignKey("supply_lines.id"))

    # Сколько и по какой СРЕДНЕЙ цене.
    # Средняя взвешенная = sum(qty_i * price_i) / sum(qty_i) по всем партиям с одного склада.
    # Почему средняя, а не цена конкретной партии?
    #   На одном складе может быть несколько партий с разными ценами.
    #   Для работы важна итоговая стоимость, а не разбивка по партиям.
    kolichestvo: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)
    srednyaya_stoimost: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)
    summa: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)  # = kolichestvo * srednyaya_stoimost

    # Приоритет склада: "zavod" (тот же завод), "filial" (тот же филиал), "vozmozhnoe" (другой филиал)
    tip_raspredeleniya: Mapped[str | None] = mapped_column(String(50))

    # True = возможное (не фактическое) распределение — материал другого филиала
    # False = фактическое распределение (остатки реально уменьшены)
    is_possible: Mapped[bool] = mapped_column(default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    # Связи
    session: Mapped["AllocationSession"] = relationship(back_populates="allocations")

    def __repr__(self) -> str:
        return (
            f"<AllocResult work={self.work_id} mat={self.material_id} "
            f"src={self.istochnik!r} qty={self.kolichestvo}>"
        )


class StockMovement(Base):
    """
    Таблица stock_movements — движение по складам.

    Фиксирует каждое списание со склада:
    "Со склада SKL-01, партии 002, списано 50 м кабеля для работы W-001.
     Остаток партии стал 150 м."
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

    # Изменение количества (отрицательное = списание)
    izmenenie: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)

    # Остаток партии ПОСЛЕ этого движения
    ostatok: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)

    data_dvizheniya: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    # Связь
    session: Mapped["AllocationSession"] = relationship(back_populates="movements")

    def __repr__(self) -> str:
        return (
            f"<Movement wh={self.warehouse_id} mat={self.material_id} "
            f"delta={self.izmenenie} остаток={self.ostatok}>"
        )


class DeficitRecord(Base):
    """
    Таблица deficit_records — записи о дефиците.

    Создаётся когда потребность работы не покрыта ни складом, ни поставками.
    Используется для формирования плана закупок.
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

    # Размер дефицита
    deficit_qty: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)

    # Ориентировочная стоимость (если известна цена)
    estimated_cost: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)

    # Когда нужен материал (дата окончания работы)
    needed_by: Mapped[date | None] = mapped_column(Date)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    # Связь
    session: Mapped["AllocationSession"] = relationship(back_populates="deficits")

    def __repr__(self) -> str:
        return (
            f"<Deficit work={self.work_id} mat={self.material_id} "
            f"qty={self.deficit_qty} needed_by={self.needed_by}>"
        )
