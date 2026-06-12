"""Admin routes for teacher maintenance tasks."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.database import get_db
from app.services.attendance_parser import SUPPORTED_EXTENSIONS, ingest_attendance_file

router = APIRouter(prefix="/admin", tags=["admin"])
templates = Jinja2Templates(directory=str(settings.project_root / "templates"))


def _attendance_page_context(db, error: str | None = None) -> dict:
    uploads = db.execute(
        """
        SELECT id, filename, uploaded_at, row_count
        FROM attendance_uploads
        ORDER BY uploaded_at DESC
        LIMIT 10
        """
    ).fetchall()
    stats = db.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM students) AS student_count,
            (SELECT COUNT(*) FROM attendance_records) AS record_count
        """
    ).fetchone()
    return {
        "title": "Upload Attendance",
        "uploads": uploads,
        "student_count": stats["student_count"],
        "record_count": stats["record_count"],
        "error": error,
    }


def _save_upload(upload: UploadFile) -> Path:
    """Persist an uploaded workbook under data/uploads/attendance/."""
    settings.attendance_upload_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name = Path(upload.filename or "attendance.txt").name
    destination = settings.attendance_upload_dir / f"{timestamp}_{safe_name}"
    destination.write_bytes(upload.file.read())
    return destination


@router.get("/attendance", response_class=HTMLResponse)
def attendance_upload_page(request: Request, db=Depends(get_db)) -> HTMLResponse:
    """Show attendance upload form and recent upload history."""
    return templates.TemplateResponse(
        request=request,
        name="admin/attendance.html",
        context=_attendance_page_context(db),
    )


@router.post("/attendance/upload")
async def upload_attendance(
    request: Request,
    file: UploadFile = File(...),
    db=Depends(get_db),
):
    """
    Accept an attendance export (.txt, .xlsx, etc.) and load it into the database.

    Auth will be added in Phase 4; for now this is open on the local network.
    """
    filename = file.filename or ""
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        supported = ", ".join(sorted(SUPPORTED_EXTENSIONS))
        return templates.TemplateResponse(
            request=request,
            name="admin/attendance.html",
            context=_attendance_page_context(
                db, f"Unsupported file type. Please upload one of: {supported}"
            ),
            status_code=400,
        )

    saved_path = _save_upload(file)

    try:
        result = ingest_attendance_file(db, saved_path, filename)
    except Exception as exc:  # noqa: BLE001 — show teacher-friendly message in UI
        return templates.TemplateResponse(
            request=request,
            name="admin/attendance.html",
            context=_attendance_page_context(db, str(exc)),
            status_code=400,
        )

    return RedirectResponse(
        url=(
            f"/admin/attendance?success=1"
            f"&records={result.records_upserted}"
            f"&students={result.students_touched}"
        ),
        status_code=303,
    )