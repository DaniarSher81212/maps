"""
app/api/routes/allocation.py — Эндпоинты запуска распределения

POST /api/allocate — запустить распределение в фоне (не блокирует браузер)

Почему «в фоне» (BackgroundTasks)?
    Распределение 10 000+ потребностей занимает 10-60 секунд.
    Если запускать синхронно — браузер будет ждать ответа всё это время
    (и может сбросить соединение по таймауту).

    BackgroundTasks: FastAPI сразу возвращает ответ {"status": "started"},
    а функция _run_allocation() продолжает работу в фоне.
    Клиент периодически опрашивает GET /api/status чтобы узнать статус.
"""

from fastapi import APIRouter, BackgroundTasks

from app.allocation.engine import AllocationEngine
from app.db.database import get_session

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
    Прогресс отслеживается через GET /api/status.
    """
    background_tasks.add_task(_run_allocation)
    return {"status": "started", "message": "Распределение запущено"}
