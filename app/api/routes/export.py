"""
app/api/routes/export.py — Эндпоинты скачивания Excel-отчётов

GET /api/export/{session_id}              — полный отчёт (все листы)
GET /api/export/{session_id}/{sheet_key}  — отдельный лист (deficit, distribution, ...)

Допустимые значения sheet_key:
    distribution, movements, balances, possible, transfers_tmpl, approved,
    coverage, in_transit, in_transit_free, deficit,
    src_works, src_req, src_stock, src_supplies, src_writeoffs, src_issued
"""

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.services.export_service import SHEET_KEYS, export_allocation_results, export_single_sheet

router = APIRouter()

_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@router.get("/export/{session_id}")
def download_export(session_id: str) -> FileResponse:
    """
    Сформировать полный Excel-отчёт (все листы) и скачать.

    Ошибки:
        400 — если сессия не найдена или ошибка при формировании отчёта
    """
    try:
        file_path = export_allocation_results(session_id)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Ошибка формирования отчёта: {e}")

    return FileResponse(
        path=str(file_path),
        filename=f"MAPS_отчёт_{session_id}.xlsx",
        media_type=_XLSX_MIME,
    )


@router.get("/export/{session_id}/{sheet_key}")
def download_sheet(session_id: str, sheet_key: str) -> FileResponse:
    """
    Сформировать Excel-файл с одним листом отчёта и скачать.

    Параметры:
        session_id: ID сессии распределения
        sheet_key:  Ключ листа (deficit, distribution, coverage, src_writeoffs, ...)

    Ошибки:
        400 — неизвестный sheet_key или ошибка формирования
    """
    if sheet_key not in SHEET_KEYS:
        raise HTTPException(
            status_code=400,
            detail=f"Неизвестный лист: {sheet_key!r}. Допустимые: {list(SHEET_KEYS)}",
        )
    try:
        file_path = export_single_sheet(session_id, sheet_key)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Ошибка формирования листа: {e}")

    sheet_name = SHEET_KEYS[sheet_key]
    return FileResponse(
        path=str(file_path),
        filename=f"MAPS_{sheet_name}_{session_id}.xlsx",
        media_type=_XLSX_MIME,
    )
