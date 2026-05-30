"""
app/api/routes/settings.py — Эндпоинты системных настроек

GET  /api/settings — получить текущие настройки (год планирования)
POST /api/settings — обновить год планирования

Как устроено хранение настроек?
    В таблице system_settings всегда одна строка (id=1 — синглтон).
    При первом запросе создаётся автоматически с годом по умолчанию.
    При POST значение обновляется и сразу используется при следующем распределении.
"""

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.db.database import get_session
from app.db.models import SystemSettings

router = APIRouter()


def get_or_create_settings(session) -> SystemSettings:
    """
    Получить единственную строку настроек или создать её при первом обращении.

    Паттерн синглтон: всегда id=1. Если строки нет — создаём с дефолтами.
    """
    settings = session.scalar(select(SystemSettings).where(SystemSettings.id == 1))
    if settings is None:
        settings = SystemSettings(id=1, planning_year=2025)
        session.add(settings)
        session.flush()
    return settings


@router.get("/settings")
def get_settings() -> dict:
    """
    Вернуть текущие системные настройки.

    Используется дашбордом при загрузке страницы — заполняет поле «Год планирования».
    """
    with get_session() as session:
        s = get_or_create_settings(session)
        return {"planning_year": s.planning_year}


class SettingsUpdate(BaseModel):
    """Схема запроса на обновление настроек."""
    planning_year: int = Field(..., ge=2000, le=2099, description="Год планирования")


@router.post("/settings")
def update_settings(body: SettingsUpdate) -> dict:
    """
    Обновить год планирования.

    Год сохраняется в БД и сразу учитывается при следующем запуске распределения.
    Текущую сессию распределения (если она уже завершена) изменение не затрагивает —
    нужно запустить распределение заново.
    """
    with get_session() as session:
        s = get_or_create_settings(session)
        s.planning_year = body.planning_year
    return {"planning_year": body.planning_year}
