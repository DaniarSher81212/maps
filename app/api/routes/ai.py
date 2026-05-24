"""
app/api/routes/ai.py — FastAPI-маршруты для AI-анализа дефицита

Эндпоинты:
    GET  /api/ai/deficits/{session_id}          — список материалов с дефицитом
    POST /api/ai/explain                        — AI-объяснение причины дефицита

Как это работает:
    1. Фронтенд запрашивает список материалов с дефицитом для текущей сессии
    2. Пользователь выбирает материал из выпадающего списка
    3. Фронтенд отправляет POST /api/ai/explain с session_id и material_id
    4. Бэкенд загружает данные из БД и вызывает Claude API
    5. Возвращает объяснение и статистику

Почему POST для объяснения, а не GET?
    GET с параметрами (session_id + material_id) тоже работало бы,
    но POST семантически правильнее: мы создаём (генерируем) новый анализ.
    Кроме того, POST не кэшируется браузером — каждый запрос свежий.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.ai.deficit_analyzer import explain_deficit, list_deficit_materials
from app.core.config import settings
from app.db.database import get_session

router = APIRouter()


# =============================================================================
# Схемы запросов/ответов (Pydantic)
# =============================================================================

class ExplainRequest(BaseModel):
    """Тело POST /api/ai/explain."""
    session_id: str      # ID сессии распределения
    material_id: int     # ID материала в таблице materials


class ExplainResponse(BaseModel):
    """Ответ с AI-объяснением дефицита."""
    material_name: str
    sys_nomer: str
    ed_izm: str | None
    total_deficit: str      # "350.0000 кг"
    total_needed: str       # "1000.0000 кг"
    total_allocated: str    # "650.0000 кг"
    works_count: int        # Сколько работ затронуто
    explanation: str        # Текст от Claude (Markdown)
    context_snapshot: str   # Данные которые видел Claude (для отладки)


# =============================================================================
# Эндпоинты
# =============================================================================

@router.get("/ai/deficits/{session_id}")
def get_deficit_materials(session_id: str) -> list[dict]:
    """
    Возвращает список материалов с дефицитом в данной сессии.

    Используется для заполнения выпадающего списка в UI.
    Отсортирован по убыванию дефицита (самые критичные — первые).

    Пример ответа:
        [
            {"material_id": 42, "sys_nomer": "10012345",
             "name": "Арматура А500С ⌀12", "ed_izm": "кг",
             "total_deficit": "350.0000", "works_count": 3},
            ...
        ]
    """
    with get_session() as session:
        materials = list_deficit_materials(session, session_id)

    if not materials:
        raise HTTPException(
            status_code=404,
            detail=f"В сессии {session_id!r} нет дефицита или сессия не существует",
        )

    return materials


@router.post("/ai/explain", response_model=ExplainResponse)
def explain_deficit_endpoint(body: ExplainRequest) -> ExplainResponse:
    """
    AI-анализ причины дефицита для конкретного материала в сессии.

    Вызывает Claude API — может занять 5-15 секунд.

    Требует переменную окружения ANTHROPIC_API_KEY.

    Тело запроса:
        {"session_id": "session_20260523_143022", "material_id": 42}

    Ответ содержит:
        - explanation: объяснение на русском языке от Claude
        - context_snapshot: данные которые видел Claude (для проверки)
        - статистику: дефицит, потребность, покрытие, число работ
    """
    api_key = settings.anthropic_api_key
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail="AI-анализ недоступен: ANTHROPIC_API_KEY не задан в .env",
        )

    try:
        with get_session() as session:
            result = explain_deficit(
                session=session,
                allocation_session_id=body.session_id,
                material_id=body.material_id,
                api_key=api_key,
            )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail=f"Ошибка AI-анализа: {e}",
        )

    return ExplainResponse(**result)
