"""
app/api/routes/status.py — Эндпоинт статуса системы

GET /api/status — возвращает агрегированную статистику:
  - Количество активных работ, материалов, складских партий
  - Информацию о последней сессии распределения
"""

from fastapi import APIRouter
from sqlalchemy import desc, func, select

from app.db.database import get_session
from app.db.models import AllocationSession, Material, StockBatch, Work

router = APIRouter()


@router.get("/status")
def get_status() -> dict:
    """
    Вернуть текущий статус системы: счётчики и последнюю сессию распределения.

    Используется дашбордом для отображения карточек статуса.
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

        # Последняя сессия распределения (самая свежая по времени начала)
        last = session.scalar(
            select(AllocationSession).order_by(desc(AllocationSession.started_at))
        )

        last_session = None
        if last:
            last_session = {
                "id": last.id,
                "status": last.status,
                "started_at": last.started_at.isoformat() if last.started_at else None,
                "completed_at": last.completed_at.isoformat() if last.completed_at else None,
                "total_requirements": last.total_requirements,
                "total_allocated": last.total_allocated,
                "total_deficit": last.total_deficit,
            }

        return {
            "active_works": active_works,
            "materials": materials,
            "available_batches": available_batches,
            "last_session": last_session,
        }
