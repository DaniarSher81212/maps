"""
app/api/routes/import_.py — Эндпоинты загрузки Excel-файлов

POST /api/import/requirements  — потребности в материалах
POST /api/import/emergency     — аварийные работы
POST /api/import/stock         — складские остатки
POST /api/import/supplies      — поставки (материалы в пути)
POST /api/import/writeoffs     — фактические списания (Слой 1)
POST /api/import/issued        — выдано не списано (Слой 2)

Как работает загрузка файла:
    1. Браузер отправляет multipart/form-data POST запрос с файлом
    2. FastAPI принимает файл как UploadFile (поток байт)
    3. Мы читаем файл в память и сохраняем во временный .xlsx файл
    4. Передаём путь к временному файлу в соответствующий import-сервис
    5. Удаляем временный файл после обработки

Почему временный файл, а не читать из памяти?
    Импорт-сервисы используют pandas.read_excel(path), который ожидает путь к файлу.
    Переделывать их для работы с BytesIO возможно, но нецелесообразно.
"""

import tempfile
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile

from app.services.import_service import (
    import_emergency_works,
    import_issued_not_written_off,
    import_requirements,
    import_stock,
    import_supplies,
    import_writeoffs,
)

router = APIRouter()


async def _save_upload(file: UploadFile) -> Path:
    """
    Сохранить загруженный файл во временный .xlsx файл на диске.

    Аргументы:
        file: UploadFile — файл из HTTP-запроса (поток байт от браузера)

    Возвращает:
        Path — путь к временному файлу (нужно удалить после использования!)

    ВАЖНО: UploadFile.read() — это async-операция, нельзя вызывать синхронно.
    Поэтому все маршруты импорта объявлены как async def.
    """
    # Читаем содержимое файла из HTTP-потока (асинхронно)
    content = await file.read()

    # Определяем расширение файла (.xlsx или .xls) для корректного tmpfile
    suffix = Path(file.filename or "upload.xlsx").suffix or ".xlsx"

    # delete=False — не удалять автоматически при закрытии (сделаем сами после импорта)
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(content)
        return Path(tmp.name)


@router.post("/import/requirements")
async def import_requirements_endpoint(file: UploadFile = File(...)) -> dict:
    """
    Загрузить Excel с потребностями в материалах.

    Ожидаемый формат Excel: колонки с кодом работы, материалом, количеством, ценой.
    """
    tmp = await _save_upload(file)
    try:
        stats = import_requirements(tmp)
        return {"status": "ok", "file": file.filename, "stats": stats}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        tmp.unlink(missing_ok=True)  # Удаляем временный файл в любом случае


@router.post("/import/emergency")
async def import_emergency_endpoint(file: UploadFile = File(...)) -> dict:
    """
    Загрузить Excel с аварийными работами.

    Аварийные работы получат наивысший приоритет при следующем запуске распределения.
    """
    tmp = await _save_upload(file)
    try:
        stats = import_emergency_works(tmp)
        return {"status": "ok", "file": file.filename, "stats": stats}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        tmp.unlink(missing_ok=True)


@router.post("/import/stock")
async def import_stock_endpoint(file: UploadFile = File(...)) -> dict:
    """Загрузить Excel с текущими складскими остатками (партиями)."""
    tmp = await _save_upload(file)
    try:
        stats = import_stock(tmp)
        return {"status": "ok", "file": file.filename, "stats": stats}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        tmp.unlink(missing_ok=True)


@router.post("/import/supplies")
async def import_supplies_endpoint(file: UploadFile = File(...)) -> dict:
    """Загрузить Excel с поставками (материалы в пути по договорам)."""
    tmp = await _save_upload(file)
    try:
        stats = import_supplies(tmp)
        return {"status": "ok", "file": file.filename, "stats": stats}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        tmp.unlink(missing_ok=True)


@router.post("/import/writeoffs")
async def import_writeoffs_endpoint(file: UploadFile = File(...)) -> dict:
    """Загрузить Excel с фактическими списаниями (Слой 1 распределения)."""
    tmp = await _save_upload(file)
    try:
        stats = import_writeoffs(tmp)
        return {"status": "ok", "file": file.filename, "stats": stats}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        tmp.unlink(missing_ok=True)


@router.post("/import/issued")
async def import_issued_endpoint(file: UploadFile = File(...)) -> dict:
    """Загрузить Excel с «Выдано не списано» (Слой 2 распределения)."""
    tmp = await _save_upload(file)
    try:
        stats = import_issued_not_written_off(tmp)
        return {"status": "ok", "file": file.filename, "stats": stats}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        tmp.unlink(missing_ok=True)
