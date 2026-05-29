"""
app/api/routes/auth.py — Маршруты аутентификации

GET  /login           — страница входа
POST /login           — проверка логина/пароля, установка сессии
POST /logout          — выход, очистка сессии
"""

import hmac

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.core.config import settings
from app.core.logging_config import get_logger

router = APIRouter(tags=["Авторизация"])
logger = get_logger(__name__)

BASE_DIR = __import__("pathlib").Path(__file__).parent.parent
templates = Jinja2Templates(directory=BASE_DIR / "templates")


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request) -> HTMLResponse:
    """Страница входа. Если уже авторизован — редиректим на дашборд."""
    if request.session.get("authenticated"):
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login", response_model=None)
async def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
) -> HTMLResponse | RedirectResponse:
    """
    Проверяем логин и пароль.

    hmac.compare_digest — безопасное сравнение строк без timing-атак:
    обычное == может завершиться быстрее если первые символы не совпадают,
    compare_digest всегда тратит одинаковое время.
    """
    username_ok = hmac.compare_digest(username.strip(), settings.maps_username)
    password_ok = hmac.compare_digest(password, settings.maps_password)

    if username_ok and password_ok:
        request.session["authenticated"] = True
        request.session["username"] = username.strip()
        logger.info("Успешный вход: %s", username.strip())
        return RedirectResponse(url="/", status_code=302)

    logger.warning("Неудачная попытка входа: логин=%r", username.strip())
    return templates.TemplateResponse(
        request,
        "login.html",
        {"error": "Неверный логин или пароль"},
        status_code=401,
    )


@router.post("/logout")
def logout(request: Request) -> RedirectResponse:
    """Выход: очищаем сессию и редиректим на страницу входа."""
    username = request.session.get("username", "—")
    request.session.clear()
    logger.info("Выход из системы: %s", username)
    return RedirectResponse(url="/login", status_code=302)
