"""
app/api/main.py — Главный FastAPI-модуль MAPS

Что такое FastAPI?
    Современный Python-фреймворк для создания REST API.
    Быстрее Flask, автоматически генерирует документацию (/docs),
    поддерживает async/await, типизацию через Pydantic.

Структура приложения:
    GET  /login                     — страница входа
    POST /login                     — проверка логина/пароля
    POST /logout                    — выход из системы
    GET  /                          — HTML-дашборд (главная страница, требует авторизации)
    GET  /api/status                — статус системы + текущая сессия (JSON)
    POST /api/allocate              — запустить распределение (JSON)
    POST /api/import/requirements   — загрузить потребности (multipart)
    POST /api/import/emergency      — загрузить аварийные работы (multipart)
    POST /api/import/stock          — загрузить остатки (multipart)
    POST /api/import/supplies       — загрузить поставки (multipart)
    POST /api/import/writeoffs      — загрузить списания (multipart)
    POST /api/import/issued         — загрузить выдано не списано (multipart)
    GET  /api/export/{session_id}   — скачать Excel-отчёт (file download)
    GET  /api/ai/deficits/{sid}     — список дефицитных материалов (JSON)
    POST /api/ai/explain            — AI-объяснение дефицита (JSON)
    GET  /docs                      — автодокументация Swagger UI

Запуск:
    maps serve                      — через CLI (рекомендуется)
    uvicorn app.api.main:app --reload  — напрямую для разработки
"""

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app.api.routes import ai, allocation, auth, export, import_, status
from app.api.routes import settings as settings_router
from app.core.config import settings

# Путь к папке с HTML-шаблонами (рядом с этим файлом)
BASE_DIR = Path(__file__).parent

# =============================================================================
# Создание FastAPI-приложения
# =============================================================================
__author__    = "Шералиев Даниар Ешалович"
__copyright__ = "© 2024–2026 Шералиев Даниар Ешалович. Все права защищены."

app = FastAPI(
    title="MAPS — Material Allocation & Planning System",
    description="Автоматизированное распределение материалов для строительно-монтажных работ",
    version="3.0.0",
)

# =============================================================================
# Middleware
# =============================================================================
# SessionMiddleware хранит данные сессии в подписанной cookie.
# secret_key — ключ для подписи: знает только сервер, клиент не может подделать.
# https_only=False — разрешаем куки по HTTP (нужно для разработки без SSL).
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    https_only=False,
    session_cookie="maps_session",
    max_age=86400,  # сессия живёт 24 часа
)

# Jinja2Templates — рендерит HTML из шаблонов (как в Django/Flask)
templates = Jinja2Templates(directory=BASE_DIR / "templates")

# =============================================================================
# Подключение роутеров
# =============================================================================
# Роутер авторизации — /login и /logout (без prefix, они на корневом уровне)
app.include_router(auth.router)

# Остальные роутеры — все под /api/...
app.include_router(status.router, prefix="/api", tags=["Статус"])
app.include_router(settings_router.router, prefix="/api", tags=["Настройки"])
app.include_router(allocation.router, prefix="/api", tags=["Распределение"])
app.include_router(import_.router, prefix="/api", tags=["Импорт"])
app.include_router(export.router, prefix="/api", tags=["Экспорт"])
app.include_router(ai.router, prefix="/api", tags=["AI-анализ"])


# =============================================================================
# Главная страница — HTML-дашборд (защищена авторизацией)
# =============================================================================
@app.get("/", response_class=HTMLResponse, response_model=None)
def dashboard(request: Request) -> HTMLResponse | RedirectResponse:
    """
    Отдать HTML-дашборд.

    Сначала проверяем: вошёл ли пользователь? Если нет — редирект на /login.
    Данные для карточек статуса и таблицы сессий загружаются через JavaScript
    (fetch /api/status) — так страница работает быстро.
    """
    if not request.session.get("authenticated"):
        return RedirectResponse(url="/login", status_code=302)
    # Передаём username в шаблон чтобы показать его в навбаре
    username = request.session.get("username", "")
    return templates.TemplateResponse(request, "index.html", {"username": username})
