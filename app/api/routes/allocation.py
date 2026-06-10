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

Защита от одновременных запусков:
    _allocation_lock — threading.Lock, который захватывается на всё время работы
    движка. Если второй пользователь нажмёт «Рассчитать» пока первый ещё
    выполняется — эндпоинт вернёт HTTP 409 Conflict.

    Двойная проверка:
      1. В start_allocation: _allocation_lock.locked() → 409 до старта задачи.
      2. В _run_allocation: acquire(blocking=False) → тихий выход если всё же
         два запроса проскочили одновременно (крайне редкое состояние гонки).
"""

import threading

from fastapi import APIRouter, BackgroundTasks, HTTPException

from app.allocation.engine import AllocationEngine
from app.api.routes.settings import get_or_create_settings
from app.db.database import get_session

router = APIRouter()

# Глобальный замок: захватывается на время работы _run_allocation().
# Так как сервер работает в одном процессе (без --workers),
# threading.Lock надёжно защищает от параллельных запусков.
_allocation_lock = threading.Lock()


def _run_allocation() -> None:
    """
    Функция, запускаемая в фоновой задаче FastAPI.

    Сначала пытается захватить _allocation_lock без ожидания.
    Если замок уже занят — значит другой запуск уже активен; тихо выходим
    (эндпоинт уже отклонил такой запрос через 409, но на случай гонки).

    После захвата замка читает год планирования и запускает движок.
    Замок гарантированно освобождается в блоке finally — даже при ошибке.
    """
    # acquire(blocking=False): вернёт False если замок уже занят, не будет ждать
    if not _allocation_lock.acquire(blocking=False):
        return  # другой запуск уже работает — выходим без действий

    try:
        with get_session() as session:
            planning_year = get_or_create_settings(session).planning_year
            engine = AllocationEngine(session, planning_year=planning_year)
            engine.run()
    finally:
        # Освобождаем замок в любом случае — иначе система намертво заблокируется
        _allocation_lock.release()


@router.post("/allocate")
def start_allocation(background_tasks: BackgroundTasks) -> dict:
    """
    Запустить распределение материалов в фоне.

    Возвращает HTTP 409, если расчёт уже выполняется.
    Иначе немедленно возвращает {"status": "started"}.
    Прогресс отслеживается через GET /api/status.
    """
    if _allocation_lock.locked():
        raise HTTPException(
            status_code=409,
            detail="Расчёт уже выполняется. Дождитесь завершения текущего запуска.",
        )
    background_tasks.add_task(_run_allocation)
    return {"status": "started", "message": "Распределение запущено"}
