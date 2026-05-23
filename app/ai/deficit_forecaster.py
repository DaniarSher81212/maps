"""
app/ai/deficit_forecaster.py — AI-прогноз дефицита материалов

Что делает этот модуль?
    Анализирует три источника данных и просит Claude оценить риск дефицита:

    1. ИСТОРИЯ — последние N сессий распределения:
       - какие материалы были в дефиците, как часто, на сколько
       - «хронический» дефицит vs разовый

    2. ТЕКУЩИЙ СКЛАД — остатки по всем материалам прямо сейчас

    3. БУДУЩАЯ ПОТРЕБНОСТЬ — активные работы и их неудовлетворённые потребности:
       - что нужно, сколько осталось покрыть, когда дата начала
       - поставки которые ожидаются

    Claude получает структурированный срез и возвращает прогноз с уровнями риска:
    ВЫСОКИЙ / СРЕДНИЙ / НИЗКИЙ — и объяснение для каждого материала.

Почему не чистая математика?
    Математика легко покажет "остаток < потребность = дефицит".
    Claude добавляет контекст: учитывает тренды, сезонность (если есть данные),
    объясняет ПОЧЕМУ риск высокий и что конкретно сделать.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

import anthropic
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.db.models import (
    AllocationSession,
    DeficitRecord,
    Material,
    Requirement,
    StockBatch,
    SupplyLine,
    Work,
)


# =============================================================================
# Структуры данных
# =============================================================================

@dataclass
class MaterialRiskData:
    """
    Все данные по одному материалу для прогноза.

    Собирается из трёх источников: история дефицитов, склад, будущие потребности.
    """
    material_id: int
    sys_nomer: str
    name: str
    ed_izm: str | None

    # --- История дефицитов ---
    deficit_sessions_count: int      # В скольких сессиях был дефицит
    total_sessions_analyzed: int     # Всего сессий в анализе
    avg_deficit_qty: Decimal         # Средний дефицит на сессию (где был)
    max_deficit_qty: Decimal         # Максимальный дефицит

    # --- Текущий склад ---
    current_stock: Decimal           # Суммарный остаток на всех складах
    # По филиалам: {"Алматы": 500, "Актобе": 0}
    stock_by_filial: dict[str, Decimal] = field(default_factory=dict)

    # --- Будущая потребность активных работ ---
    upcoming_need: Decimal = Decimal("0")    # Суммарная потребность активных работ
    earliest_start: date | None = None       # Ближайшая дата начала работы с потребностью
    works_needing: int = 0                   # Сколько активных работ нуждаются

    # --- Поставки в пути ---
    incoming_supply: Decimal = Decimal("0")  # Ожидаемые поставки

    # --- Вычисляемые ---
    @property
    def projected_balance(self) -> Decimal:
        """Прогнозный баланс: склад + поставки - потребность."""
        return self.current_stock + self.incoming_supply - self.upcoming_need

    @property
    def deficit_frequency_pct(self) -> float:
        """Частота дефицита: в каком % сессий был дефицит."""
        if self.total_sessions_analyzed == 0:
            return 0.0
        return round(self.deficit_sessions_count / self.total_sessions_analyzed * 100, 1)


@dataclass
class ForecastContext:
    """Полный контекст для прогноза — передаётся в Claude."""
    analysis_date: str           # Дата анализа (сегодня)
    sessions_analyzed: int       # Сколько сессий взяли в историю
    last_session_id: str | None  # ID последней сессии
    materials: list[MaterialRiskData]


# =============================================================================
# Сбор данных из БД
# =============================================================================

def load_forecast_context(session: Session, last_n_sessions: int = 5) -> ForecastContext:
    """
    Загружает из БД данные для прогноза дефицита.

    last_n_sessions: сколько последних сессий взять для анализа истории.
    Рекомендуется 3-10 — меньше слишком мало данных, больше — старые данные теряют актуальность.
    """
    # --- 1. Последние N сессий ---
    sessions = list(session.scalars(
        select(AllocationSession)
        .where(AllocationSession.status == "completed")
        .order_by(desc(AllocationSession.started_at))
        .limit(last_n_sessions)
    ).all())

    session_ids = [s.id for s in sessions]
    last_session_id = session_ids[0] if session_ids else None

    # --- 2. История дефицитов по материалам ---
    # Группируем DeficitRecord по material_id: считаем в скольких сессиях был дефицит
    deficit_rows = list(session.scalars(
        select(DeficitRecord).where(DeficitRecord.session_id.in_(session_ids))
    ).all()) if session_ids else []

    # material_id → {session_id → total_deficit}
    deficit_by_material: dict[int, dict[str, Decimal]] = {}
    for dr in deficit_rows:
        mid = dr.material_id
        if mid not in deficit_by_material:
            deficit_by_material[mid] = {}
        sid = dr.session_id
        deficit_by_material[mid][sid] = deficit_by_material[mid].get(sid, Decimal("0")) + dr.deficit_qty

    # --- 3. Текущие складские остатки по материалам ---
    stock_rows = list(session.scalars(
        select(StockBatch).where(StockBatch.dostupno > 0)
    ).all())

    # material_id → {filial → qty}
    stock_by_mat: dict[int, dict[str, Decimal]] = {}
    for batch in stock_rows:
        mid = batch.material_id
        if mid not in stock_by_mat:
            stock_by_mat[mid] = {}
        # Получаем филиал склада через Warehouse
        from app.db.models import Warehouse
        wh = session.get(Warehouse, batch.warehouse_id)
        filial = (wh.filial or "Без филиала") if wh else "Неизвестно"
        stock_by_mat[mid][filial] = stock_by_mat[mid].get(filial, Decimal("0")) + batch.dostupno

    # --- 4. Потребности активных работ (ещё не покрыты) ---
    # Берём активные работы и их Requirement
    active_works = list(session.scalars(
        select(Work).where(Work.status == "active")
    ).all())

    active_work_ids = [w.id for w in active_works]
    work_by_id: dict[int, Work] = {w.id: w for w in active_works}

    # material_id → {работы которые нуждаются}
    need_by_mat: dict[int, list[tuple[Decimal, date | None]]] = {}
    if active_work_ids:
        reqs = list(session.scalars(
            select(Requirement).where(Requirement.work_id.in_(active_work_ids))
        ).all())
        for req in reqs:
            mid = req.material_id
            if mid not in need_by_mat:
                need_by_mat[mid] = []
            work = work_by_id.get(req.work_id)
            need_by_mat[mid].append((req.potrebnost, work.data_nachala if work else None))

    # --- 5. Ожидаемые поставки ---
    supply_lines = list(session.scalars(
        select(SupplyLine).where(SupplyLine.dostupno > 0)
    ).all())

    supply_by_mat: dict[int, Decimal] = {}
    for sl in supply_lines:
        supply_by_mat[sl.material_id] = supply_by_mat.get(sl.material_id, Decimal("0")) + sl.dostupno

    # --- 6. Собираем всё по материалам ---
    # Рассматриваем только материалы которые: были в дефиците ИЛИ нужны активным работам
    material_ids = set(deficit_by_material.keys()) | set(need_by_mat.keys())

    materials: list[MaterialRiskData] = []

    for mid in material_ids:
        mat = session.get(Material, mid)
        if not mat:
            continue

        # История дефицитов
        hist = deficit_by_material.get(mid, {})
        deficit_counts = len(hist)
        deficit_values = list(hist.values())
        avg_def = sum(deficit_values, Decimal("0")) / max(len(deficit_values), 1)
        max_def = max(deficit_values, default=Decimal("0"))

        # Склад
        stock_filial = stock_by_mat.get(mid, {})
        current_stock = sum(stock_filial.values(), Decimal("0"))

        # Потребности
        needs = need_by_mat.get(mid, [])
        upcoming_need = sum((n for n, _ in needs), Decimal("0"))
        dates = [d for _, d in needs if d is not None]
        earliest_start = min(dates) if dates else None

        materials.append(MaterialRiskData(
            material_id=mid,
            sys_nomer=mat.sys_nomer,
            name=mat.naimenovanie or mat.sys_nomer,
            ed_izm=mat.ed_izm,
            deficit_sessions_count=deficit_counts,
            total_sessions_analyzed=len(sessions),
            avg_deficit_qty=avg_def,
            max_deficit_qty=max_def,
            current_stock=current_stock,
            stock_by_filial=stock_filial,
            upcoming_need=upcoming_need,
            earliest_start=earliest_start,
            works_needing=len(needs),
            incoming_supply=supply_by_mat.get(mid, Decimal("0")),
        ))

    # Сортируем: сначала хронические дефициты, затем по отрицательному балансу
    materials.sort(key=lambda m: (-m.deficit_sessions_count, m.projected_balance))

    return ForecastContext(
        analysis_date=date.today().isoformat(),
        sessions_analyzed=len(sessions),
        last_session_id=last_session_id,
        materials=materials,
    )


# =============================================================================
# Формирование текста для Claude
# =============================================================================

def _format_forecast_context(ctx: ForecastContext) -> str:
    """Преобразует ForecastContext в читаемый текст для Claude."""
    lines: list[str] = []

    lines.append(f"ДАТА АНАЛИЗА: {ctx.analysis_date}")
    lines.append(f"ПРОАНАЛИЗИРОВАНО СЕССИЙ: {ctx.sessions_analyzed} (последние завершённые)")
    lines.append(f"МАТЕРИАЛОВ ДЛЯ ОЦЕНКИ: {len(ctx.materials)}")
    lines.append("")

    for m in ctx.materials:
        unit = m.ed_izm or "ед."
        lines.append(f"{'='*60}")
        lines.append(f"МАТЕРИАЛ: {m.name} [{m.sys_nomer}]")
        lines.append(f"Единица: {unit}")
        lines.append("")

        # История
        if ctx.sessions_analyzed > 0:
            lines.append(f"  История дефицитов: {m.deficit_sessions_count} из {m.total_sessions_analyzed} сессий ({m.deficit_frequency_pct}%)")
            if m.deficit_sessions_count > 0:
                lines.append(f"  Средний дефицит: {m.avg_deficit_qty} {unit}")
                lines.append(f"  Максимальный дефицит: {m.max_deficit_qty} {unit}")
        else:
            lines.append("  История: нет завершённых сессий")

        # Склад
        lines.append(f"  Текущий остаток: {m.current_stock} {unit}")
        if m.stock_by_filial:
            for filial, qty in sorted(m.stock_by_filial.items(), key=lambda x: x[1], reverse=True):
                lines.append(f"    • {filial}: {qty} {unit}")
        else:
            lines.append("    (складских остатков нет)")

        # Поставки
        lines.append(f"  Ожидаемые поставки: {m.incoming_supply} {unit}")

        # Будущие потребности
        lines.append(f"  Потребность активных работ: {m.upcoming_need} {unit} ({m.works_needing} работ)")
        if m.earliest_start:
            lines.append(f"  Ближайшая дата начала: {m.earliest_start}")

        # Прогноз
        balance = m.projected_balance
        balance_str = f"{balance} {unit}"
        if balance < 0:
            lines.append(f"  Прогнозный баланс: {balance_str} ← ДЕФИЦИТ")
        elif balance == 0:
            lines.append(f"  Прогнозный баланс: {balance_str} ← ВПРИТЫК")
        else:
            lines.append(f"  Прогнозный баланс: {balance_str} ← в наличии")
        lines.append("")

    return "\n".join(lines)


# =============================================================================
# Вызов Claude API
# =============================================================================

FORECAST_SYSTEM_PROMPT = """Ты — аналитик системы MAPS (Material Allocation & Planning System).
Тебе даны данные о материалах строительного проекта: история дефицитов, текущие остатки, предстоящие потребности.

Твоя задача: составить прогноз дефицита — определить какие материалы окажутся в дефиците
при следующем запуске распределения или в ближайшее время.

Для каждого материала укажи:
🔴 ВЫСОКИЙ РИСК — дефицит почти гарантирован (хронический дефицит + нет остатка + срочные работы)
🟡 СРЕДНИЙ РИСК — дефицит возможен (остатка мало или нестабильное покрытие)
🟢 НИЗКИЙ РИСК — достаточно обеспечен

Структура ответа:
1. Сводка (2-3 предложения) — общая картина
2. По каждому материалу: уровень риска, числа, главная причина, рекомендация
3. Приоритетные действия (топ-3 что сделать прямо сейчас)

Правила:
- Пиши на русском, кратко и конкретно
- Используй только цифры из данных, не придумывай
- Учитывай дату начала работ — срочность важна
- Если история одна сессия или нет истории — так и скажи, прогноз менее надёжен"""


def forecast_deficit(
    session: Session,
    last_n_sessions: int = 5,
    api_key: str | None = None,
) -> dict:
    """
    Главная функция — строит прогноз дефицита и возвращает анализ от Claude.

    last_n_sessions: сколько последних сессий взять для анализа истории (по умолчанию 5).

    Возвращает:
        {
            "analysis_date": str,
            "sessions_analyzed": int,
            "materials_count": int,
            "forecast": str,        # Прогноз от Claude (Markdown)
            "context_snapshot": str # Данные которые видел Claude
        }
    """
    ctx = load_forecast_context(session, last_n_sessions=last_n_sessions)

    if not ctx.materials:
        return {
            "analysis_date": ctx.analysis_date,
            "sessions_analyzed": ctx.sessions_analyzed,
            "materials_count": 0,
            "forecast": "Нет данных для прогноза: нет дефицитов в истории и нет активных работ с потребностями.",
            "context_snapshot": "",
        }

    context_text = _format_forecast_context(ctx)

    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise ValueError("ANTHROPIC_API_KEY не задан")

    client = anthropic.Anthropic(api_key=key)

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1500,
        system=FORECAST_SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": f"Составь прогноз дефицита на основе данных:\n\n{context_text}",
            }
        ],
    )

    forecast_text = message.content[0].text if message.content else "Нет ответа от модели"

    return {
        "analysis_date": ctx.analysis_date,
        "sessions_analyzed": ctx.sessions_analyzed,
        "materials_count": len(ctx.materials),
        "forecast": forecast_text,
        "context_snapshot": context_text,
    }
