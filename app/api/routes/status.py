"""
app/api/routes/status.py — Эндпоинт статуса системы

GET /api/status — возвращает агрегированную статистику:
  - Количество активных работ, материалов, складских партий
  - Информацию о текущей (единственной) сессии распределения
"""

from fastapi import APIRouter
from sqlalchemy import desc, func, select

from app.db.database import get_session
from app.db.models import AllocationSession, Material, StockBatch, Work

router = APIRouter()


@router.get("/status")
def get_status() -> dict:
    """
    Вернуть текущий статус системы: счётчики и текущую сессию распределения.

    В системе всегда не более одной сессии (старая удаляется при новом запуске).
    Используется дашбордом для отображения карточек статуса и текущих результатов.
    """
    with get_session() as session:
        # Количество активных работ (только status='active')
        active_works = session.scalar(
            select(func.count(Work.id)).where(Work.status == "active")
        ) or 0

        # Общее количество материалов в справочнике
        materials = session.scalar(select(func.count(Material.id))) or 0

        # Количество складских партий с ненулевым остатком
        available_batches = session.scalar(
            select(func.count(StockBatch.id)).where(StockBatch.dostupno > 0)
        ) or 0

        # Текущая сессия распределения (всегда одна — самая свежая)
        current = session.scalar(
            select(AllocationSession).order_by(desc(AllocationSession.started_at))
        )

        current_session = None
        if current:
            current_session = {
                "id": current.id,
                "status": current.status,
                "started_at": current.started_at.isoformat() if current.started_at else None,
                "completed_at": current.completed_at.isoformat() if current.completed_at else None,
                "total_requirements": current.total_requirements,
                "total_allocated": current.total_allocated,
                "total_deficit": current.total_deficit,
            }

        return {
            "active_works": active_works,
            "materials": materials,
            "available_batches": available_batches,
            "current_session": current_session,
        }
