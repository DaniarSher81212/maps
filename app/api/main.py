"""
app/api/main.py — Главный FastAPI-модуль MAPS

Что такое FastAPI?
    Современный Python-фреймворк для создания REST API.
    Быстрее Flask, автоматически генерирует документацию (/docs),
    поддерживает async/await, типизацию через Pydantic.

Структура приложения:
    GET  /                          — HTML-дашборд (главная страница)
    GET  /api/status                — статус системы (JSON)
    POST /api/allocate              — запустить распределение (JSON)
    GET  /api/sessions              — список сессий (JSON)
    GET  /api/sessions/{id}         — детали сессии (JSON)
    POST /api/import/requirements   — загрузить потребности (multipart)
    POST /api/import/emergency      — загрузить аварийные работы (multipart)
    POST /api/import/stock          — загрузить остатки (multipart)
    POST /api/import/supplies       — загрузить поставки (multipart)
    POST /api/import/writeoffs      — загрузить списания (multipart)
    POST /api/import/issued         — загрузить выдано не списано (multipart)
    GET  /api/export/{session_id}   — скачать Excel-отчёт (file download)
    GET  /docs                      — автодокументация Swagger UI

Запуск:
    maps serve                      — через CLI (рекомендуется)
    uvicorn app.api.main:app --reload  — напрямую для разработки
"""

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.api.routes import ai, allocation, export, import_, status

# Путь к папке с HTML-шаблонами (рядом с этим файлом)
BASE_DIR = Path(__file__).parent

# =============================================================================
# Создание FastAPI-приложения
# =============================================================================
app = FastAPI(
    title="MAPS — Material Allocation & Planning System",
    description="Автоматизированное распределение материалов для строительно-монтажных работ",
    version="2.0.0",
)

# Jinja2Templates — рендерит HTML из шаблонов (как в Django/Flask)
templates = Jinja2Templates(directory=BASE_DIR / "templates")

# =============================================================================
# Подключение роутеров
# =============================================================================
# Каждый роутер — это отдельный файл с группой эндпоинтов.
# prefix="/api" означает что все URL в роутере начинаются с /api/...
app.include_router(status.router, prefix="/api", tags=["Статус"])
app.include_router(allocation.router, prefix="/api", tags=["Распределение"])
app.include_router(import_.router, prefix="/api", tags=["Импорт"])
app.include_router(export.router, prefix="/api", tags=["Экспорт"])
app.include_router(ai.router, prefix="/api", tags=["AI-анализ"])


# =============================================================================
# Главная страница — HTML-дашборд
# =============================================================================
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    """
    Отдать HTML-дашборд.

    Request нужен Jinja2 для рендеринга шаблона (передаётся внутрь как контекст).
    Данные для карточек статуса и таблицы сессий загружаются через JavaScript
    (fetch /api/status и /api/sessions) — так страница работает быстро.
    """
    # Starlette 1.0+: request передаётся первым аргументом, не в context dict
    return templates.TemplateResponse(request, "index.html")
