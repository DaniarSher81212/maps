"""
app/api/routes/import_.py — Эндпоинты загрузки Excel-файлов

POST /api/import/works         — перечень работ (мастер-данные: наименования, даты, филиал)
POST /api/import/requirements  — потребности в материалах по работам
POST /api/import/emergency     — аварийные работы (наивысший приоритет)
POST /api/import/stock         — складские остатки (партии, Слой 3)
POST /api/import/supplies      — поставки / материалы в пути (Слой 4)
POST /api/import/writeoffs     — фактические списания (Слой 1)
POST /api/import/issued        — выдано не списано (Слой 2)
POST /api/import/transfers     — согласованные межфилиальные перемещения (Слой 3в)

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
    import_approved_transfers,
    import_emergency_works,
    import_issued_not_written_off,
    import_requirements,
    import_stock,
    import_supplies,
    import_works,      # Импорт перечня работ с наименованиями (мастер-данные)
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

    Ожидаемый формат Excel (обязательные колонки):
        Код работы | Системный номер материала | Потребность (кол-во) |
        Приоритет | Филиал | Завод

    Рекомендуется загружать ПОСЛЕ /import/works, чтобы работы уже имели наименования.
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


@router.post("/import/works")
async def upload_works(file: UploadFile = File(...)) -> dict:
    """
    Загрузить перечень работ с наименованиями из Excel.

    Что делает этот эндпоинт?
        Принимает Excel-файл «Перечень работ» и передаёт его в import_works().
        Работы создаются (если новые) или обновляются (если уже есть в БД).
        Это операция upsert по коду работы (kod_raboty).

    Когда вызывать?
        Рекомендуется ДО загрузки потребностей (import/requirements),
        чтобы при создании работ они сразу получили наименования.
        Но можно и ПОСЛЕ — тогда наименования обновятся у существующих работ.

    Не удаляет существующие данные — только добавляет или обновляет.

    Формат файла:
        Код работы | Наименование работы | Дата начала | Дата окончания |
        Филиал | Завод | Приоритет | Статус | Тип работы

    Returns:
        {"status": "ok", "stats": {"created": 30, "updated": 20, "errors": 0}}
    """
    # Сохраняем загруженный файл во временный файл на диске
    tmp_path = await _save_upload(file)
    try:
        # Запускаем импорт (синхронная функция, работает с БД через SQLAlchemy)
        stats = import_works(tmp_path)
        return {"status": "ok", "stats": stats}
    except Exception as e:
        # 422 Unprocessable Entity — данные приняты, но обработать не удалось
        raise HTTPException(status_code=422, detail=str(e))
    finally:
        # Удаляем временный файл в любом случае (даже при ошибке)
        tmp_path.unlink(missing_ok=True)


@router.post("/import/transfers")
async def import_transfers_endpoint(file: UploadFile = File(...)) -> dict:
    """
    Загрузить Excel с согласованными межфилиальными перемещениями (Слой 3в).

    Когда использовать?
        1. Сначала запустите распределение (POST /api/allocate)
        2. Откройте отчёт, лист «Возможное перемещение»
        3. Согласуйте нужные перемещения с другими филиалами
        4. Загрузите файл через этот эндпоинт
        5. Запустите распределение повторно — согласованные перемещения
           будут использованы как реальный источник (остатки склада уменьшатся)

    Ожидаемый формат Excel (обязательные колонки):
        Системный номер    — sys_nomer материала
        Склад-источник     — kod_sklada склада, откуда берём материал
        Филиал-получатель  — to_filial, куда идёт материал (как в works.filial)
        Кол-во согласовано — согласованное количество (> 0)

    Альтернативные заголовки (поддерживаются автоматически):
        «Возможное кол-во» вместо «Кол-во согласовано»
        «Куда (филиал)»    вместо «Филиал-получатель»

    ВАЖНО: каждая загрузка ПОЛНОСТЬЮ ЗАМЕНЯЕТ предыдущие данные.
    Старые записи удаляются перед сохранением новых.

    Returns:
        {"status": "ok", "stats": {"created": N, "errors": K, "skipped": M}}
    """
    tmp_path = await _save_upload(file)
    try:
        stats = import_approved_transfers(tmp_path)
        return {"status": "ok", "file": file.filename, "stats": stats}
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e))
    finally:
        tmp_path.unlink(missing_ok=True)
