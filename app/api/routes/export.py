"""
app/api/routes/export.py — Эндпоинт скачивания Excel-отчёта

GET /api/export/{session_id} — сформировать и скачать Excel-отчёт

Это ключевой эндпоинт для использования с рабочего компа:
    1. Запустил распределение дома (POST /api/allocate)
    2. В таблице сессий появилась ссылка:
       http://192.168.1.x:8000/api/export/20260523_143055_a1b2c
    3. Эту ссылку можно отправить себе на email / мессенджер
    4. Открыть с рабочего компа → файл скачается автоматически

Как работает FileResponse в FastAPI:
    FastAPI потоково отдаёт файл браузеру с правильными HTTP-заголовками:
      Content-Disposition: attachment; filename="MAPS_отчёт_..."
      Content-Type: application/vnd.openxmlformats...
    Браузер получает эти заголовки и открывает диалог «Сохранить файл».
"""

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.services.export_service import export_allocation_results

router = APIRouter()


@router.get("/export/{session_id}")
def download_export(session_id: str) -> FileResponse:
    """
    Сформировать Excel-отчёт для сессии и вернуть его браузеру как скачиваемый файл.

    Аргументы:
        session_id: ID сессии распределения (например "20260523_143055_a1b2c")

    Возвращает:
        Excel-файл (.xlsx) с 4 листами: Распределение, Движение склада,
        Остатки складов, Возможное перемещение.

    Ошибки:
        400 — если сессия не найдена или произошла ошибка при формировании отчёта
    """
    try:
        file_path = export_allocation_results(session_id)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Ошибка формирования отчёта: {e}")

    # Имя файла с датой сессии — удобно для сохранения
    filename = f"MAPS_отчёт_{session_id}.xlsx"

    return FileResponse(
        path=str(file_path),
        filename=filename,
        # MIME-тип для .xlsx файлов — браузер знает, что это Excel
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
