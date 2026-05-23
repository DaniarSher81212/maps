"""
app/api/routes/allocation.py — Эндпоинты запуска и мониторинга распределения

POST /api/allocate          — запустить распределение в фоне (не блокирует браузер)
GET  /api/sessions          — список последних сессий с результатами
GET  /api/sessions/{id}     — детали одной сессии

Почему «в фоне» (BackgroundTasks)?
    Распределение 10 000+ потребностей занимает 10-60 секунд.
    Если запускать синхронно — браузер будет ждать ответа всё это время
    (и может сбросить соединение по таймауту).

    BackgroundTasks: FastAPI сразу возвращает ответ {"status": "started"},
    а функция _run_allocation() продолжает работу в фоне.
    Клиент периодически опрашивает GET /api/sessions чтобы узнать статус.
"""

from fastapi import APIRouter, BackgroundTasks, HTTPException
from sqlalchemy import desc, select

from app.allocation.engine import AllocationEngine
from app.db.database import get_session
from app.db.models import AllocationSession

router = APIRouter()


def _run_allocation() -> None:
    """
    Функция, запускаемая в фоновой задаче FastAPI.

    Создаёт собственную сессию БД (не использует сессию HTTP-запроса,
    т.к. HTTP-запрос уже завершён к этому моменту).

    get_session() автоматически делает commit при успехе
    и rollback при ошибке — сессия корректно закрывается в любом случае.
    """
    with get_session() as session:
        engine = AllocationEngine(session)
        engine.run()


@router.post("/allocate")
def start_allocation(background_tasks: BackgroundTasks) -> dict:
    """
    Запустить распределение материалов в фоне.

    Немедленно возвращает {"status": "started"}.
    Прогресс отслеживается через GET /api/sessions.
    """
    background_tasks.add_task(_run_allocation)
    return {"status": "started", "message": "Распределение запущено"}


@router.get("/sessions")
def list_sessions(limit: int = 20) -> list[dict]:
    """
    Вернуть последние сессии распределения (по умолчанию — 20 штук).

    Каждая сессия содержит:
      - id, status (running / completed / failed)
      - Время начала и завершения
      - Статистику: сколько потребностей, сколько покрыто, дефицит
    """
    with get_session() as session:
        stmt = (
            select(AllocationSession)
            .order_by(desc(AllocationSession.started_at))
            .limit(limit)
        )
        sessions = list(session.scalars(stmt).all())

        return [
            {
                "id": s.id,
                "status": s.status,
                "started_at": s.started_at.isoformat() if s.started_at else None,
                "completed_at": s.completed_at.isoformat() if s.completed_at else None,
                "total_requirements": s.total_requirements,
                "total_allocated": s.total_allocated,
                "total_deficit": s.total_deficit,
                "notes": s.notes,
            }
            for s in sessions
        ]


@router.get("/sessions/{session_id}")
def get_session_detail(session_id: str) -> dict:
    """Вернуть детали одной сессии по её ID."""
    with get_session() as sess:
        alloc_session = sess.get(AllocationSession, session_id)
        if not alloc_session:
            raise HTTPException(status_code=404, detail="Сессия не найдена")

        return {
            "id": alloc_session.id,
            "status": alloc_session.status,
            "started_at": alloc_session.started_at.isoformat() if alloc_session.started_at else None,
            "completed_at": alloc_session.completed_at.isoformat() if alloc_session.completed_at else None,
            "total_requirements": alloc_session.total_requirements,
            "total_allocated": alloc_session.total_allocated,
            "total_deficit": alloc_session.total_deficit,
            "notes": alloc_session.notes,
        }
