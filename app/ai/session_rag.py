"""
app/ai/session_rag.py — RAG по истории сессий распределения

Что такое RAG (Retrieval-Augmented Generation)?
    Вместо того чтобы передавать Claude ВСЕ данные (их может быть очень много),
    мы сначала ИЩЕМ релевантные фрагменты (Retrieval), а потом отдаём только их
    в качестве контекста для генерации ответа (Generation).

    Схема работы:
        Вопрос пользователя
              ↓
        Retrieval: находим релевантные сессии/материалы
              ↓
        Augmentation: формируем контекст из найденного
              ↓
        Generation: Claude отвечает, опираясь на контекст

Как устроен поиск в нашем случае?
    У нас нет векторной базы данных (это добавило бы сложности).
    Вместо этого используем двухуровневый подход:

    1. СТРУКТУРИРОВАННЫЕ СУММАРИ — для каждой сессии строим компактное
       текстовое описание: дата, дефициты по материалам, филиалы, слои покрытия.
       Это «документы» нашей базы знаний.

    2. КЛЮЧЕВЫЕ СЛОВА — при поиске извлекаем из вопроса ключевые термины
       (имена материалов, филиалы, даты) и находим сессии где они упоминаются.
       Если точных совпадений нет — передаём все суммари (их немного).

    3. CLAUDE ДЕЛАЕТ ОСТАЛЬНОЕ — получает контекст и вопрос, и отвечает
       своими словами, ссылаясь на конкретные данные.

Почему не векторный поиск?
    Для типичного MAPS (десятки сессий) keyword-поиск достаточно точен.
    Векторный поиск нужен при тысячах документов — добавим в Этапе 4.

Примеры вопросов которые понимает RAG:
    - "Когда последний раз был дефицит по кабелю ВВГ?"
    - "Какой материал чаще всего в дефиците?"
    - "Сравни сессии — стало лучше или хуже?"
    - "Какой максимальный дефицит цемента был за всё время?"
    - "По каким работам Актобе есть дефицит?"
    - "Сколько всего материалов не было покрыто?"
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from decimal import Decimal

import anthropic
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.db.models import (
    AllocationResult,
    AllocationSession,
    DeficitRecord,
    Material,
    Requirement,
    Work,
)


# =============================================================================
# Структуры
# =============================================================================

@dataclass
class MaterialDeficitSummary:
    """Дефицит одного материала в одной сессии."""
    material_id: int
    sys_nomer: str
    name: str
    ed_izm: str | None
    total_deficit: Decimal
    works_count: int
    filials: list[str]   # Уникальные филиалы работ в дефиците


@dataclass
class LayerSummary:
    """Сколько покрыто каждым слоем в сессии."""
    spisanie: Decimal = Decimal("0")    # Слой 1: списания
    vydano: Decimal = Decimal("0")      # Слой 2: выдано не списано
    sklad: Decimal = Decimal("0")       # Слой 3: склад
    postavka: Decimal = Decimal("0")    # Слой 4: поставки


@dataclass
class SessionSummary:
    """
    Компактное описание одной сессии распределения.

    Это «документ» в нашей базе знаний RAG.
    Содержит всё важное для ответа на аналитические вопросы.
    """
    session_id: str
    started_at: str          # ISO-дата, например "2026-05-23 18:46"
    status: str

    # Общая статистика
    total_requirements: int   # Всего строк потребностей
    total_deficit_records: int  # Строк с дефицитом
    unique_deficit_materials: int  # Уникальных материалов в дефиците

    # Детали дефицита
    deficits: list[MaterialDeficitSummary]
    # Какие источники покрытия использовались
    layers: LayerSummary

    # Уникальные материалы и филиалы (для быстрого поиска)
    material_names: list[str]  # naimenovanie всех дефицитных материалов
    material_sysnomers: list[str]  # sys_nomer
    deficit_filials: list[str]  # Филиалы работ с дефицитом

    def as_text(self) -> str:
        """
        Преобразует суммари в читаемый текст — передаётся в Claude как контекст.
        """
        lines: list[str] = []
        lines.append(f"=== Сессия: {self.session_id} ===")
        lines.append(f"Дата: {self.started_at}  |  Статус: {self.status}")
        lines.append(f"Потребностей: {self.total_requirements}  |  "
                     f"Строк с дефицитом: {self.total_deficit_records}  |  "
                     f"Материалов в дефиците: {self.unique_deficit_materials}")

        # Покрытие по слоям
        lay = self.layers
        layer_parts = []
        if lay.spisanie > 0:
            layer_parts.append(f"списания={lay.spisanie}")
        if lay.vydano > 0:
            layer_parts.append(f"выдано={lay.vydano}")
        if lay.sklad > 0:
            layer_parts.append(f"склад={lay.sklad}")
        if lay.postavka > 0:
            layer_parts.append(f"поставки={lay.postavka}")
        if layer_parts:
            lines.append(f"Покрыто по слоям: {', '.join(layer_parts)}")

        # Дефициты
        if self.deficits:
            lines.append("Дефицитные материалы:")
            for d in sorted(self.deficits, key=lambda x: x.total_deficit, reverse=True):
                unit = d.ed_izm or "ед."
                filials_str = ", ".join(d.filials) if d.filials else "—"
                lines.append(
                    f"  • {d.name} [{d.sys_nomer}]: дефицит {d.total_deficit} {unit}, "
                    f"{d.works_count} раб., филиалы: {filials_str}"
                )
        else:
            lines.append("Дефицитов нет.")

        return "\n".join(lines)


# =============================================================================
# Сбор данных из БД
# =============================================================================

def build_all_session_summaries(session: Session, limit: int = 50) -> list[SessionSummary]:
    """
    Строит суммари для всех завершённых сессий.

    limit: максимум сессий (по умолчанию 50 — достаточно для RAG без векторного поиска).
    Берём самые свежие.
    """
    db_sessions = list(session.scalars(
        select(AllocationSession)
        .where(AllocationSession.status == "completed")
        .order_by(desc(AllocationSession.started_at))
        .limit(limit)
    ).all())

    summaries: list[SessionSummary] = []

    for s in db_sessions:
        summary = _build_session_summary(session, s)
        summaries.append(summary)

    return summaries


def _build_session_summary(session: Session, alloc_session: AllocationSession) -> SessionSummary:
    """Строит суммари для одной сессии."""
    sid = alloc_session.id

    # --- Дефициты ---
    deficit_rows = list(session.scalars(
        select(DeficitRecord).where(DeficitRecord.session_id == sid)
    ).all())

    # Группируем по материалу
    mat_deficits: dict[int, dict] = {}
    for dr in deficit_rows:
        mid = dr.material_id
        if mid not in mat_deficits:
            mat = session.get(Material, mid)
            mat_deficits[mid] = {
                "material_id": mid,
                "sys_nomer": mat.sys_nomer if mat else str(mid),
                "name": (mat.naimenovanie or mat.sys_nomer) if mat else str(mid),
                "ed_izm": mat.ed_izm if mat else None,
                "total_deficit": Decimal("0"),
                "works_set": set(),
                "filials_set": set(),
            }
        mat_deficits[mid]["total_deficit"] += dr.deficit_qty
        mat_deficits[mid]["works_set"].add(dr.work_id)
        # Добавляем филиал работы
        work = session.get(Work, dr.work_id)
        if work and work.filial:
            mat_deficits[mid]["filials_set"].add(work.filial)

    deficit_summaries = [
        MaterialDeficitSummary(
            material_id=d["material_id"],
            sys_nomer=d["sys_nomer"],
            name=d["name"],
            ed_izm=d["ed_izm"],
            total_deficit=d["total_deficit"],
            works_count=len(d["works_set"]),
            filials=sorted(d["filials_set"]),
        )
        for d in mat_deficits.values()
    ]

    # --- Слои покрытия ---
    alloc_rows = list(session.scalars(
        select(AllocationResult).where(
            AllocationResult.session_id == sid,
            AllocationResult.is_possible == False,  # noqa: E712
        )
    ).all())

    layers = LayerSummary()
    for ar in alloc_rows:
        if ar.istochnik == "spisanie":
            layers.spisanie += ar.kolichestvo
        elif ar.istochnik == "vydano":
            layers.vydano += ar.kolichestvo
        elif ar.istochnik == "sklad":
            layers.sklad += ar.kolichestvo
        elif ar.istochnik == "postavka":
            layers.postavka += ar.kolichestvo

    # --- Индексы для поиска ---
    material_names = [d.name.lower() for d in deficit_summaries]
    material_sysnomers = [d.sys_nomer.lower() for d in deficit_summaries]
    deficit_filials = sorted({f for d in deficit_summaries for f in d.filials})

    # Форматируем дату
    dt_str = alloc_session.started_at.strftime("%Y-%m-%d %H:%M") if alloc_session.started_at else "—"

    # Считаем уникальные Requirement в сессии
    total_req = len(list(session.scalars(
        select(Requirement.id)
        .join(Work, Requirement.work_id == Work.id)
        .where(Work.status == "active")
    ).all()))

    return SessionSummary(
        session_id=sid,
        started_at=dt_str,
        status=alloc_session.status,
        total_requirements=total_req,
        total_deficit_records=len(deficit_rows),
        unique_deficit_materials=len(mat_deficits),
        deficits=deficit_summaries,
        layers=layers,
        material_names=material_names,
        material_sysnomers=material_sysnomers,
        deficit_filials=deficit_filials,
    )


# =============================================================================
# Retrieval — поиск релевантных суммари
# =============================================================================

def retrieve_relevant_summaries(
    summaries: list[SessionSummary],
    question: str,
    top_k: int = 10,
) -> list[SessionSummary]:
    """
    Находит суммари наиболее релевантные к вопросу.

    Алгоритм:
        1. Нормализуем вопрос (нижний регистр, убираем знаки препинания)
        2. Для каждого суммари считаем score — сколько ключевых слов совпало
        3. Возвращаем top_k суммари с ненулевым score
        4. Если score=0 для всех — возвращаем все суммари (общий вопрос)

    Что ищем:
        - Названия материалов (sys_nomer, naimenovanie)
        - Названия филиалов
        - Конкретные session_id
        - Числа (могут быть кодами работ)
    """
    q = question.lower()
    # Убираем лишние знаки для надёжного сравнения
    q_tokens = set(re.split(r"[\s,\.;:!?«»\"'()\-]+", q))
    q_tokens.discard("")

    scored: list[tuple[int, SessionSummary]] = []

    for s in summaries:
        score = 0

        # Прямое совпадение session_id
        if s.session_id.lower() in q:
            score += 10

        # Совпадение названий материалов (проверяем по частичному вхождению)
        for name in s.material_names:
            # Разбиваем имя материала на слова и ищем пересечение с вопросом
            name_tokens = set(re.split(r"[\s,\.;:×Ø⌀]+", name))
            name_tokens.discard("")
            # Даём балл за каждое слово имени найденное в вопросе
            for token in name_tokens:
                if len(token) >= 3 and token in q:
                    score += 2

        # Совпадение sys_nomer
        for sn in s.material_sysnomers:
            if sn in q_tokens:
                score += 5

        # Совпадение филиалов
        for filial in s.deficit_filials:
            if filial.lower() in q:
                score += 3

        scored.append((score, s))

    # Сортируем по score (убывание), затем по дате (свежие первее)
    scored.sort(key=lambda x: (-x[0], x[1].started_at), reverse=False)
    # Исправляем: сортируем по убыванию score и убыванию даты
    scored.sort(key=lambda x: x[0], reverse=True)

    nonzero = [(score, s) for score, s in scored if score > 0]

    if nonzero:
        return [s for _, s in nonzero[:top_k]]
    else:
        # Ничего не нашли — возвращаем все (их немного)
        return [s for _, s in scored[:top_k]]


# =============================================================================
# Формирование контекста и вызов Claude
# =============================================================================

RAG_SYSTEM_PROMPT = """Ты — аналитик системы MAPS (Material Allocation & Planning System).
MAPS управляет распределением строительных материалов на работы. Каждый запуск системы — «сессия».

Тебе дан контекст: суммари сессий распределения из истории системы.
Отвечай на вопрос пользователя, используя ТОЛЬКО данные из контекста.

Правила:
- Отвечай на русском языке
- Ссылайся на конкретные сессии (ID и дата) когда это уместно
- Используй конкретные числа из данных
- Если данных недостаточно для точного ответа — честно скажи об этом
- Если в контексте несколько сессий — сравни их, найди тренды
- Не придумывай данных которых нет в контексте
- Форматируй ответ читаемо: используй списки, выделения

Структура суммари сессии:
  Сессия: <ID>  |  Дата  |  Статус
  Потребностей / Строк с дефицитом / Материалов в дефиците
  Покрыто по слоям: списания / выдано / склад / поставки
  Дефицитные материалы: материал [код]: дефицит X ед., N раб., филиалы"""


def answer_question(
    session: Session,
    question: str,
    api_key: str | None = None,
    history: list[dict] | None = None,
) -> dict:
    """
    Отвечает на вопрос пользователя по истории сессий.

    Параметры:
        question:  вопрос на русском языке
        history:   предыдущие сообщения чата [{"role": "user"/"assistant", "content": "..."}]
                   позволяет задавать уточняющие вопросы в контексте беседы
        api_key:   ключ Anthropic

    Возвращает:
        {
            "answer": str,              # Ответ Claude
            "sessions_used": int,       # Сколько сессий вошло в контекст
            "total_sessions": int,      # Всего сессий в истории
            "retrieved_ids": list[str]  # ID сессий которые вошли в контекст
        }
    """
    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise ValueError("ANTHROPIC_API_KEY не задан")

    # --- Строим суммари всех сессий ---
    all_summaries = build_all_session_summaries(session)

    if not all_summaries:
        return {
            "answer": "В системе нет завершённых сессий распределения. Запустите распределение сначала.",
            "sessions_used": 0,
            "total_sessions": 0,
            "retrieved_ids": [],
        }

    # --- Retrieval: находим релевантные суммари ---
    relevant = retrieve_relevant_summaries(all_summaries, question)

    # --- Формируем контекст ---
    context_parts = [
        f"ИСТОРИЯ СЕССИЙ РАСПРЕДЕЛЕНИЯ (всего в системе: {len(all_summaries)}, "
        f"выбрано для ответа: {len(relevant)}):\n"
    ]
    for s in relevant:
        context_parts.append(s.as_text())

    context_text = "\n\n".join(context_parts)

    # --- Строим сообщения для Claude ---
    # Включаем историю чата если она есть (для многоходовых вопросов)
    messages: list[dict] = []

    if history:
        # Предыдущие сообщения беседы
        for msg in history[-10:]:  # Берём последние 10 чтобы не перегружать контекст
            messages.append({"role": msg["role"], "content": msg["content"]})

    # Текущий вопрос с контекстом
    messages.append({
        "role": "user",
        "content": f"КОНТЕКСТ:\n{context_text}\n\nВОПРОС: {question}",
    })

    # --- Вызов Claude ---
    client = anthropic.Anthropic(api_key=key)
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=RAG_SYSTEM_PROMPT,
        messages=messages,
    )

    answer = response.content[0].text if response.content else "Нет ответа от модели"

    return {
        "answer": answer,
        "sessions_used": len(relevant),
        "total_sessions": len(all_summaries),
        "retrieved_ids": [s.session_id for s in relevant],
    }
