"""
app/ai/deficit_analyzer.py — AI-анализ причин дефицита материалов

Что делает этот модуль?
    1. Получает из БД все данные о дефиците по конкретному материалу в сессии:
       - какие работы в дефиците и на сколько
       - что было распределено по каждому слою (списания, склад, поставки)
       - текущие остатки на складах по этому материалу
    2. Формирует структурированный контекст для Claude
    3. Вызывает Claude API и возвращает объяснение на русском языке

Почему Claude, а не GPT или локальная модель?
    MAPS уже работает на инфраструктуре Anthropic (Claude Code).
    Claude хорошо понимает русский язык и аналитические задачи.
    API простой — один вызов, нет необходимости в сложных цепочках.

Структура возвращаемого объяснения:
    - Краткая причина дефицита (главная)
    - Разбивка по слоям: что покрыло, чего не хватило
    - Текущее состояние складских остатков
    - Рекомендации: где взять, когда ожидать поставку
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal

import anthropic
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    AllocationResult,
    DeficitRecord,
    Material,
    Requirement,
    StockBatch,
    Warehouse,
    Work,
)


# =============================================================================
# Структуры данных для передачи контекста в Claude
# =============================================================================

@dataclass
class LayerSummary:
    """Итог по одному слою распределения для одной работы."""
    layer_name: str          # Человекочитаемое название слоя
    istochnik: str           # Код источника: "spisanie", "vydano", "sklad", etc.
    kolichestvo: Decimal     # Сколько покрыто этим слоем
    srednyaya_stoimost: Decimal  # Средняя стоимость за единицу


@dataclass
class WorkDeficitDetail:
    """Полный разбор дефицита для одной работы."""
    kod_raboty: str
    filial: str | None
    zavod: str | None
    is_emergency: bool
    data_nachala: str | None      # Дата начала работы
    potrebnost: Decimal           # Сколько нужно
    allocated_total: Decimal      # Сколько распределено (все слои вместе)
    deficit: Decimal              # Дефицит = potrebnost - allocated_total
    layers: list[LayerSummary]    # Что дал каждый слой
    # Возможное межфилиальное покрытие (is_possible=True, остатки не тронуты)
    vozmozhnoe: Decimal = Decimal("0")


@dataclass
class StockInfo:
    """Остатки материала на складах (сгруппированы для компактности)."""
    # По филиалу: {"Филиал Алматы": Decimal("500.00"), ...}
    by_filial: dict[str, Decimal] = field(default_factory=dict)
    # По группе завода: {"zavod": Decimal("200"), "filial": Decimal("300"), "prochee": Decimal("0")}
    by_group: dict[str, Decimal] = field(default_factory=dict)
    total: Decimal = Decimal("0")


@dataclass
class DeficitContext:
    """Полный контекст для анализа дефицита — передаётся в Claude."""
    session_id: str
    material_sys_nomer: str
    material_name: str
    ed_izm: str | None
    works: list[WorkDeficitDetail]
    stock: StockInfo
    total_deficit: Decimal
    total_needed: Decimal
    total_allocated: Decimal


# =============================================================================
# Загрузка данных из БД
# =============================================================================

# Человекочитаемые названия слоёв (istochnik → название)
LAYER_NAMES: dict[str, str] = {
    "spisanie": "Слой 1: Фактические списания (SAP)",
    "vydano": "Слой 2: Выдано не списано",
    "sklad": "Слой 3: Склад (завод/филиал)",
    "vozmozhnoe_sklad": "Анализ: возможное с другого филиала",
    "postavka": "Слой 4: Поставки (материал в пути)",
}


def load_deficit_context(session: Session, allocation_session_id: str, material_id: int) -> DeficitContext | None:
    """
    Загружает из БД все данные нужные для анализа дефицита.

    Возвращает None если по данному материалу в сессии нет дефицита.

    Алгоритм:
        1. Находим материал (имя, номер, ед.изм.)
        2. Находим все DeficitRecord для этой сессии + материала
        3. Для каждой дефицитной работы:
           - читаем Requirement (сколько было нужно)
           - читаем AllocationResult (что дал каждый слой)
        4. Читаем текущие остатки StockBatch для этого материала
    """
    # --- 1. Материал ---
    material = session.get(Material, material_id)
    if not material:
        return None

    # --- 2. Дефицитные записи ---
    deficit_rows = list(session.scalars(
        select(DeficitRecord)
        .where(
            DeficitRecord.session_id == allocation_session_id,
            DeficitRecord.material_id == material_id,
        )
    ).all())

    if not deficit_rows:
        return None

    # --- 3. По каждой работе с дефицитом ---
    works_detail: list[WorkDeficitDetail] = []

    for dr in deficit_rows:
        work = session.get(Work, dr.work_id)
        if not work:
            continue

        # Потребность (Requirement)
        req = session.scalar(
            select(Requirement).where(
                Requirement.work_id == dr.work_id,
                Requirement.material_id == material_id,
            )
        )
        potrebnost = req.potrebnost if req else Decimal("0")

        # AllocationResult для этой работы + материала + сессии
        alloc_rows = list(session.scalars(
            select(AllocationResult).where(
                AllocationResult.session_id == allocation_session_id,
                AllocationResult.work_id == dr.work_id,
                AllocationResult.material_id == material_id,
            )
        ).all())

        # Группируем по истоникам
        layers: list[LayerSummary] = []
        allocated_total = Decimal("0")
        vozmozhnoe = Decimal("0")

        for ar in alloc_rows:
            if ar.is_possible:
                # Возможное — не учитывается в реальном покрытии
                vozmozhnoe += ar.kolichestvo
            else:
                allocated_total += ar.kolichestvo
                layers.append(LayerSummary(
                    layer_name=LAYER_NAMES.get(ar.istochnik) or ar.istochnik,
                    istochnik=ar.istochnik,
                    kolichestvo=ar.kolichestvo,
                    srednyaya_stoimost=ar.srednyaya_stoimost,
                ))

        works_detail.append(WorkDeficitDetail(
            kod_raboty=work.kod_raboty,
            filial=work.filial,
            zavod=work.zavod,
            is_emergency=work.is_emergency,
            data_nachala=work.data_nachala.isoformat() if work.data_nachala else None,
            potrebnost=potrebnost,
            allocated_total=allocated_total,
            deficit=dr.deficit_qty,
            layers=layers,
            vozmozhnoe=vozmozhnoe,
        ))

    if not works_detail:
        return None

    # --- 4. Текущие остатки на складах ---
    batches = list(session.scalars(
        select(StockBatch).where(
            StockBatch.material_id == material_id,
            StockBatch.dostupno > 0,
        )
    ).all())

    # Группируем по филиалу склада
    by_filial: dict[str, Decimal] = {}
    total_stock = Decimal("0")

    for batch in batches:
        wh = session.get(Warehouse, batch.warehouse_id)
        filial_name = (wh.filial or "Без филиала") if wh else "Неизвестно"
        by_filial[filial_name] = by_filial.get(filial_name, Decimal("0")) + batch.dostupno
        total_stock += batch.dostupno

    # Итоговые суммы
    total_deficit = sum((w.deficit for w in works_detail), Decimal("0"))
    total_needed = sum((w.potrebnost for w in works_detail), Decimal("0"))
    total_allocated = sum((w.allocated_total for w in works_detail), Decimal("0"))

    return DeficitContext(
        session_id=allocation_session_id,
        material_sys_nomer=material.sys_nomer,
        material_name=material.naimenovanie or material.sys_nomer,
        ed_izm=material.ed_izm,
        works=works_detail,
        stock=StockInfo(by_filial=by_filial, total=total_stock),
        total_deficit=total_deficit,
        total_needed=total_needed,
        total_allocated=total_allocated,
    )


# =============================================================================
# Формирование текстового контекста для Claude
# =============================================================================

def _format_context(ctx: DeficitContext) -> str:
    """
    Преобразует DeficitContext в читаемый текст для системного промпта Claude.

    Почему не JSON?
        Claude лучше понимает структурированный текст с заголовками, чем чистый JSON.
        Текст компактнее и нагляднее для аналитической задачи.
    """
    unit = ctx.ed_izm or "ед."
    lines: list[str] = []

    lines.append(f"МАТЕРИАЛ: {ctx.material_name}")
    lines.append(f"Системный номер: {ctx.material_sys_nomer}")
    lines.append(f"Единица измерения: {unit}")
    lines.append(f"Сессия распределения: {ctx.session_id}")
    lines.append("")
    lines.append(f"ИТОГО: нужно {ctx.total_needed} {unit}, "
                 f"распределено {ctx.total_allocated} {unit}, "
                 f"дефицит {ctx.total_deficit} {unit}")
    lines.append("")

    # --- Работы с дефицитом ---
    lines.append(f"РАБОТЫ С ДЕФИЦИТОМ ({len(ctx.works)} шт.):")
    for w in ctx.works:
        tag = " [АВАРИЙНАЯ]" if w.is_emergency else ""
        lines.append(f"\n  Работа: {w.kod_raboty}{tag}")
        if w.filial:
            lines.append(f"  Филиал: {w.filial}")
        if w.zavod:
            lines.append(f"  Завод: {w.zavod}")
        if w.data_nachala:
            lines.append(f"  Дата начала: {w.data_nachala}")
        lines.append(f"  Потребность: {w.potrebnost} {unit}")
        lines.append(f"  Распределено: {w.allocated_total} {unit}")
        lines.append(f"  Дефицит: {w.deficit} {unit}")

        if w.layers:
            lines.append("  Покрыто по слоям:")
            for layer in w.layers:
                lines.append(f"    • {layer.layer_name}: {layer.kolichestvo} {unit}"
                             f" (ср. цена {layer.srednyaya_stoimost})")
        else:
            lines.append("  Покрыто по слоям: ничего (0 из всех слоёв)")

        if w.vozmozhnoe > 0:
            lines.append(f"  Возможное с другого филиала (не реализовано): {w.vozmozhnoe} {unit}")

    # --- Остатки ---
    lines.append("")
    lines.append(f"ТЕКУЩИЕ ОСТАТКИ ЭТОГО МАТЕРИАЛА НА СКЛАДАХ: {ctx.stock.total} {unit}")
    if ctx.stock.by_filial:
        for filial, qty in sorted(ctx.stock.by_filial.items(), key=lambda x: x[1], reverse=True):
            lines.append(f"  • {filial}: {qty} {unit}")
    else:
        lines.append("  (остатков нет)")

    return "\n".join(lines)


# =============================================================================
# Вызов Claude API
# =============================================================================

SYSTEM_PROMPT = """Ты — аналитик системы MAPS (Material Allocation & Planning System).
MAPS автоматически распределяет строительные материалы на работы в 5 слоёв (приоритет по убыванию):

1. Слой 1 — Фактические списания в SAP (материал уже на объекте, документ проведён)
2. Слой 2 — Выдано не списано (выдан со склада, документ ещё не проведён)
3. Слой 3 — Склад (остатки на складе; сначала свой завод, потом свой филиал, потом другие)
4. Слой 4 — Поставки (материал в пути, ожидаемые поставки)
5. Слой 5 — Дефицит (не покрыто ничем → нужно закупать)

Твоя задача: проанализировать данные и объяснить причину дефицита.

Правила ответа:
- Пиши на русском языке
- Кратко и по делу (3-6 абзацев)
- Используй конкретные цифры из данных
- Объясни ГЛАВНУЮ причину (не хватает склада? нет поставок? слишком большая потребность?)
- Дай 1-2 практических рекомендации (где взять, когда ждать, что проверить)
- Если есть возможное межфилиальное покрытие — упомяни его как вариант
- Не придумывай данных которых нет в контексте"""


def explain_deficit(
    session: Session,
    allocation_session_id: str,
    material_id: int,
    api_key: str | None = None,
) -> dict:
    """
    Главная функция модуля — объясняет причину дефицита материала.

    Возвращает словарь:
        {
            "material_name": str,
            "sys_nomer": str,
            "total_deficit": str,
            "explanation": str,    # Текст от Claude
            "context_snapshot": str  # Данные которые видел Claude (для отладки)
        }

    api_key: ключ Anthropic API. Если None — берётся из переменной окружения ANTHROPIC_API_KEY.
    Raises ValueError если дефицита нет или материал не найден.
    """
    # --- Загружаем данные из БД ---
    ctx = load_deficit_context(session, allocation_session_id, material_id)
    if ctx is None:
        raise ValueError(
            f"Дефицит по материалу {material_id} в сессии {allocation_session_id!r} не найден"
        )

    # --- Формируем текстовый контекст ---
    context_text = _format_context(ctx)

    # --- Вызываем Claude API ---
    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise ValueError(
            "ANTHROPIC_API_KEY не задан. "
            "Добавьте в .env: ANTHROPIC_API_KEY=sk-ant-..."
        )

    client = anthropic.Anthropic(api_key=key)

    # claude-sonnet-4-6 — баланс скорости и качества анализа
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": f"Проанализируй дефицит и объясни причину:\n\n{context_text}",
            }
        ],
    )

    explanation = message.content[0].text if message.content else "Нет ответа от модели"

    unit = ctx.ed_izm or "ед."
    return {
        "material_name": ctx.material_name,
        "sys_nomer": ctx.material_sys_nomer,
        "ed_izm": unit,
        "total_deficit": f"{ctx.total_deficit} {unit}",
        "total_needed": f"{ctx.total_needed} {unit}",
        "total_allocated": f"{ctx.total_allocated} {unit}",
        "works_count": len(ctx.works),
        "explanation": explanation,
        "context_snapshot": context_text,  # Для отладки — что видел Claude
    }


# =============================================================================
# Вспомогательная функция: список материалов с дефицитом в сессии
# =============================================================================

def list_deficit_materials(session: Session, allocation_session_id: str) -> list[dict]:
    """
    Возвращает список материалов, у которых есть дефицит в данной сессии.

    Используется для заполнения выпадающего списка в UI.

    Возвращает:
        [
            {"material_id": 42, "sys_nomer": "10012345", "name": "Арматура А500С ⌀12",
             "ed_izm": "кг", "total_deficit": "350.0000", "works_count": 3},
            ...
        ]
    Отсортирован по убыванию дефицита (самые критичные — первые).
    """
    rows = list(session.scalars(
        select(DeficitRecord).where(DeficitRecord.session_id == allocation_session_id)
    ).all())

    if not rows:
        return []

    # Группируем по material_id (один материал может иметь дефицит в нескольких работах)
    aggregated: dict[int, dict] = {}

    for dr in rows:
        mid = dr.material_id
        if mid not in aggregated:
            mat = session.get(Material, mid)
            aggregated[mid] = {
                "material_id": mid,
                "sys_nomer": mat.sys_nomer if mat else str(mid),
                "name": (mat.naimenovanie or mat.sys_nomer) if mat else str(mid),
                "ed_izm": mat.ed_izm if mat else None,
                "total_deficit": Decimal("0"),
                "works_count": 0,
            }
        aggregated[mid]["total_deficit"] += dr.deficit_qty
        aggregated[mid]["works_count"] += 1

    result = sorted(aggregated.values(), key=lambda x: x["total_deficit"], reverse=True)

    # Конвертируем Decimal в строку для JSON-сериализации
    for item in result:
        item["total_deficit"] = str(item["total_deficit"])

    return result
