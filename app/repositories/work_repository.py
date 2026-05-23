"""
app/repositories/work_repository.py — Репозиторий работ и потребностей

Что такое репозиторий?
    Слой, который отвечает за работу с базой данных.
    Скрывает детали SQL-запросов от бизнес-логики.
    Бизнес-логика вызывает repo.get_for_allocation() и получает готовые объекты,
    не зная, как именно был составлен запрос.

Порядок распределения (ГЛАВНЫЙ приоритет — дата начала работ):
    ┌─────────────────────────────────────────────────────┐
    │ 1. Аварийные работы (is_emergency=True)             │
    │    → Сортировка ТОЛЬКО по дате начала (ASC)        │
    │    → Приоритет (prioritet) НЕ учитывается          │
    ├─────────────────────────────────────────────────────┤
    │ 2. Обычные работы (is_emergency=False)              │
    │    → Дата начала (ASC) — ГЛАВНЫЙ критерий          │
    │    → Затем приоритет (1→2→3)                       │
    │    → Затем филиал (алфавитно)                      │
    │    → Затем код работы (алфавитно)                  │
    └─────────────────────────────────────────────────────┘

    Пример очереди распределения:
        АВ-001 (аварийная, дата: 01.06)   ← первая
        АВ-002 (аварийная, дата: 03.06)   ← вторая
        РАБ-010 (обычная, дата: 01.06, приоритет: 1)
        РАБ-005 (обычная, дата: 01.06, приоритет: 2)
        РАБ-020 (обычная, дата: 05.06, приоритет: 1)
        ...
"""

from decimal import Decimal
from typing import Optional

from sqlalchemy import case, select, update
from sqlalchemy.orm import Session

from app.core.logging_config import get_logger
from app.db.models import Material, Requirement, Work
from app.repositories.base import BaseRepository

logger = get_logger(__name__)


class WorkRepository(BaseRepository[Work]):
    """
    Репозиторий для работы с таблицей works.

    Наследует от BaseRepository[Work] — получает методы add(), get_by_id() и т.д.
    Добавляет специфичные для работ методы.
    """

    def __init__(self, session: Session) -> None:
        # Передаём в родительский класс тип модели Work
        super().__init__(session, Work)

    def get_by_kod(self, kod_raboty: str) -> Optional[Work]:
        """
        Найти работу по бизнес-коду.

        Args:
            kod_raboty: Уникальный код работы, например "WO-12345"

        Returns:
            Объект Work или None если не найдено
        """
        stmt = select(Work).where(Work.kod_raboty == kod_raboty)
        return self.session.scalar(stmt)

    def get_or_create(
        self,
        kod_raboty: str,
        tip_raboty: Optional[str] = None,
        filial: Optional[str] = None,
        podrazdelenie: Optional[str] = None,
        centr_zatrat: Optional[str] = None,
        zavod: Optional[str] = None,
        data_nachala=None,
        data_okonchaniya=None,
        prioritet: int = 3,
        status: str = "active",
        is_emergency: bool = False,  # НОВЫЙ параметр: аварийная ли работа
    ) -> Work:
        """
        Получить работу из БД или создать если не существует.

        Параметр is_emergency=True используется при импорте аварийных работ
        из отдельного Excel-файла. Аварийные работы имеют наивысший приоритет
        в очереди распределения материалов.

        Args:
            kod_raboty:      Уникальный код работы (ключ поиска)
            tip_raboty:      Тип: "ТО", "Ремонт", "Монтаж" и т.д.
            filial:          Филиал компании
            podrazdelenie:   Подразделение
            centr_zatrat:    Центр затрат
            zavod:           Код завода в SAP (Plant)
            data_nachala:    Дата начала работ (ГЛАВНЫЙ приоритет распределения)
            data_okonchaniya: Дата окончания работ
            prioritet:       1 (высший) / 2 (средний) / 3 (низший) — для обычных работ
            status:          "active" / "completed" / "cancelled"
            is_emergency:    True = аварийная работа (идёт перед всеми обычными)

        Returns:
            Существующий или новый объект Work
        """
        # Пытаемся найти работу по коду
        work = self.get_by_kod(kod_raboty)
        if work is None:
            # Работы нет в БД — создаём новую
            work = Work(
                kod_raboty=kod_raboty,
                tip_raboty=tip_raboty,
                filial=filial,
                podrazdelenie=podrazdelenie,
                centr_zatrat=centr_zatrat,
                zavod=zavod,
                data_nachala=data_nachala,
                data_okonchaniya=data_okonchaniya,
                prioritet=prioritet,
                status=status,
                is_emergency=is_emergency,
            )
            self.add(work)
            marker = " [АВАРИЙНАЯ]" if is_emergency else ""
            logger.debug("Создана работа: %s%s", kod_raboty, marker)
        else:
            # Работа уже есть. Если при повторном импорте передаём is_emergency=True —
            # обновляем флаг (аварийный статус может быть добавлен позже)
            if is_emergency and not work.is_emergency:
                work.is_emergency = True
                logger.debug("Работа помечена как аварийная: %s", kod_raboty)
        return work

    def get_active_sorted(self) -> list[Work]:
        """
        Получить все активные работы, отсортированные для распределения.

        Используется для вывода статуса. Для распределения используется
        get_for_allocation() в RequirementRepository.

        Порядок сортировки:
            1. Аварийные первыми (is_emergency DESC: True > False в PostgreSQL)
            2. Дата начала (ASC: ранние работы — первые)
            3. Для обычных: приоритет (ASC: 1 → 2 → 3)
            4. Филиал (алфавитно)
            5. Код работы (алфавитно, для детерминированности)
        """
        stmt = (
            select(Work)
            .where(Work.status == "active")
            .order_by(
                # Аварийные работы (True) идут перед обычными (False)
                # В PostgreSQL: TRUE > FALSE при DESC-сортировке
                Work.is_emergency.desc(),

                # ГЛАВНЫЙ приоритет — дата начала работ (для всех групп)
                Work.data_nachala.asc().nulls_last(),

                # Для ОБЫЧНЫХ работ — дополнительно по полю prioritet.
                # Для аварийных — это поле игнорируем (ставим 0 через CASE).
                # Конструкция CASE (SQL):
                #   CASE WHEN NOT is_emergency THEN prioritet ELSE 0 END ASC
                # Это означает: если работа НЕ аварийная → используем prioritet,
                # если аварийная → используем 0 (и все аварийные сортируются одинаково по этому полю).
                case(
                    (Work.is_emergency == False, Work.prioritet),  # noqa: E712
                    else_=0,
                ).asc(),

                # Дополнительные поля для стабильной сортировки при совпадении
                Work.filial.asc().nulls_last(),
                Work.kod_raboty.asc(),
            )
        )
        return list(self.session.scalars(stmt).all())

    def get_kod_map(self) -> dict[str, int]:
        """
        Вернуть словарь {kod_raboty: id} для быстрого поиска при импорте.

        Зачем это нужно?
            При импорте Excel (тысячи строк) нам нужно знать ID работы.
            Если делать SELECT для каждой строки — N+1 проблема, медленно.
            Лучше один раз загрузить весь словарь в память и искать за O(1).
        """
        stmt = select(Work.kod_raboty, Work.id)
        rows = self.session.execute(stmt).all()
        return {row.kod_raboty: row.id for row in rows}


class RequirementRepository(BaseRepository[Requirement]):
    """
    Репозиторий для работы с потребностями в материалах.

    Requirement — это связь между Work и Material:
    «Работа W-001 требует 100 м кабеля ВВГ по прогнозной цене 1500 тг/м».
    """

    def __init__(self, session: Session) -> None:
        super().__init__(session, Requirement)

    def get_by_work_and_material(self, work_id: int, material_id: int) -> Optional[Requirement]:
        """
        Найти потребность по паре (work_id, material_id).

        Используется перед upsert, чтобы определить: создавать новую или обновлять.
        """
        stmt = select(Requirement).where(
            Requirement.work_id == work_id,
            Requirement.material_id == material_id,
        )
        return self.session.scalar(stmt)

    def upsert(
        self,
        work_id: int,
        material_id: int,
        potrebnost: Decimal,
        prognosnaya_tsena: Decimal = Decimal("0"),  # НОВЫЙ параметр
    ) -> Requirement:
        """
        Создать или обновить потребность (Upsert = Update + Insert).

        Что такое upsert?
            - Если потребность «work_id + material_id» УЖЕ ЕСТЬ:
                → Добавляем количество (суммируем, не заменяем)
                → Прогнозную цену берём первую ненулевую
            - Если потребности НЕТ:
                → Создаём новую запись

        Зачем суммировать, а не заменять?
            Иногда один материал встречается в Excel в нескольких строках
            (например, разные подрядчики, разные секции объекта).
            Бизнес-правило: суммируем все строки в одну потребность.

        Args:
            work_id:          ID работы в БД
            material_id:      ID материала в БД
            potrebnost:       Количество (будет суммировано при дублях)
            prognosnaya_tsena: Прогнозная цена за единицу (первая ненулевая сохраняется)

        Returns:
            Объект Requirement (новый или обновлённый)
        """
        req = self.get_by_work_and_material(work_id, material_id)
        if req is None:
            # Потребности нет — создаём первую
            req = Requirement(
                work_id=work_id,
                material_id=material_id,
                potrebnost=potrebnost,
                prognosnaya_tsena=prognosnaya_tsena,
                raspredeleno=Decimal("0"),
                deficit=Decimal("0"),
            )
            self.add(req)
        else:
            # Дубль — суммируем количество
            req.potrebnost += potrebnost
            # Прогнозную цену обновляем только если она ещё не установлена
            # (берём первую ненулевую встреченную в Excel)
            if prognosnaya_tsena > 0 and req.prognosnaya_tsena == 0:
                req.prognosnaya_tsena = prognosnaya_tsena
        return req

    def get_for_allocation(self) -> list[tuple[Requirement, Work, Material]]:
        """
        Получить все потребности, отсортированные для распределения.

        Это ГЛАВНЫЙ запрос, который питает алгоритм AllocationEngine.
        Возвращает тройки (Requirement, Work, Material) в порядке распределения.

        Порядок сортировки (бизнес-правило из ТЗ):
            ╔══════════════════════════════════════════════════════╗
            ║ АВАРИЙНЫЕ РАБОТЫ (is_emergency = TRUE)              ║
            ║   → только по дате начала (ASC)                    ║
            ╠══════════════════════════════════════════════════════╣
            ║ ОБЫЧНЫЕ РАБОТЫ (is_emergency = FALSE)               ║
            ║   → дата начала (ASC) — ГЛАВНЫЙ критерий           ║
            ║   → приоритет (1→2→3)                              ║
            ║   → филиал (алфавитно)                             ║
            ║   → код работы (алфавитно)                         ║
            ╚══════════════════════════════════════════════════════╝

        Зачем разный порядок для аварийных и обычных?
            Аварийные работы — это ЧП: упал агрегат, нет электричества.
            Они должны получить материалы немедленно, не конкурируя по приоритету.
            Внутри группы аварийных — кто начинается раньше, тот и первый.

        Returns:
            Список кортежей [(Requirement, Work, Material), ...]
            в порядке, в котором алгоритм будет их обрабатывать.
        """
        stmt = (
            select(Requirement, Work, Material)
            .join(Work, Requirement.work_id == Work.id)           # Работа через FK work_id
            .join(Material, Requirement.material_id == Material.id)  # Материал через FK material_id
            .where(Work.status == "active")  # Только активные работы (не cancelled, не completed)
            .order_by(
                # ── Критерий 1: Аварийные работы ПЕРЕД всеми обычными ──
                # В PostgreSQL: TRUE > FALSE, DESC → True идёт первым
                Work.is_emergency.desc(),

                # ── Критерий 2 (ГЛАВНЫЙ): дата начала, ранние работы первые ──
                # NULLS LAST: работы без даты — в самом конце
                Work.data_nachala.asc().nulls_last(),

                # ── Критерий 3: только для ОБЫЧНЫХ работ — поле prioritet ──
                # Для аварийных используем 0 (нейтральное значение, они уже выше по критерию 1)
                # CASE SQL:
                #   Если НЕ аварийная → берём реальный prioritet (1, 2 или 3)
                #   Если аварийная   → берём 0 (не влияет, они уже отсортированы по дате)
                case(
                    (Work.is_emergency == False, Work.prioritet),  # noqa: E712
                    else_=0,
                ).asc(),

                # ── Критерии 4-5: для стабильности при одинаковых значениях ──
                Work.filial.asc().nulls_last(),   # Алфавитно по филиалу
                Work.kod_raboty.asc(),             # Алфавитно по коду работы
                Material.sys_nomer.asc(),          # Алфавитно по номеру материала
            )
        )
        rows = self.session.execute(stmt).all()
        # Извлекаем из строки результата три объекта: Requirement, Work, Material
        return [(row.Requirement, row.Work, row.Material) for row in rows]

    def reset_allocation(self) -> None:
        """
        Сбросить результаты предыдущего распределения перед новым запуском.

        Обнуляет поля raspredeleno и deficit во ВСЕХ потребностях.
        Вызывается в начале каждого запуска AllocationEngine.run().

        Зачем нужен сброс?
            Алгоритм работает с накопительным полем req.raspredeleno.
            Без сброса новый запуск продолжил бы с предыдущих значений,
            что привело бы к двойному счёту.
        """
        stmt = (
            update(Requirement)
            .values(raspredeleno=Decimal("0"), deficit=Decimal("0"))
        )
        self.session.execute(stmt)
        logger.info("Сброшены результаты предыдущего распределения (raspredeleno и deficit → 0)")
