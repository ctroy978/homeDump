"""Password-protected admin routes for teachers."""

from __future__ import annotations

import hmac
import tempfile
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.background import BackgroundTask

from app.config import settings
from app.database import get_db
from app.public_url import hostname_url_hints, suggest_public_base_url
from app.dependencies import (
    ADMIN_COOKIE_MAX_AGE,
    ADMIN_COOKIE_NAME,
    _expected_admin_token,
    require_admin,
)
from app.services.assignments import create_assignment, delete_assignment, list_assignments
from app.services.attendance_parser import SUPPORTED_EXTENSIONS, ingest_attendance_file
from app.services.claim_logs import ClaimLogStatus, list_claim_logs
from app.services.data_backup import (
    BackupError,
    backup_archive_name,
    data_dir_has_backup_content,
    write_data_backup,
)
from app.services.print_queue import (
    PrintQueueError,
    clear_print_queue,
    list_print_queue,
    print_batch_and_clear,
    remove_queue_item,
)

router = APIRouter(prefix="/admin", tags=["admin"])
templates = Jinja2Templates(directory=str(settings.project_root / "templates"))


def _safe_next_path(next_path: str | None) -> str:
    if not next_path or not next_path.startswith("/admin"):
        return "/admin"
    return next_path


def _admin_summary(db) -> dict[str, int]:
    row = db.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM students) AS student_count,
            (SELECT COUNT(*) FROM attendance_records) AS record_count,
            (SELECT COUNT(*) FROM assignments) AS assignment_count
        """
    ).fetchone()
    return {
        "student_count": row["student_count"],
        "record_count": row["record_count"],
        "assignment_count": row["assignment_count"],
    }


def _attendance_page_context(db, error: str | None = None) -> dict:
    uploads = db.execute(
        """
        SELECT id, filename, uploaded_at, row_count
        FROM attendance_uploads
        ORDER BY uploaded_at DESC
        LIMIT 10
        """
    ).fetchall()
    summary = _admin_summary(db)
    return {
        "title": "Upload Attendance",
        "uploads": uploads,
        "error": error,
        **summary,
    }


def _set_admin_cookie(response: RedirectResponse) -> None:
    response.set_cookie(
        ADMIN_COOKIE_NAME,
        _expected_admin_token(),
        httponly=True,
        max_age=ADMIN_COOKIE_MAX_AGE,
        samesite="lax",
    )


def _save_attendance_upload(upload: UploadFile) -> Path:
    settings.attendance_upload_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name = Path(upload.filename or "attendance.txt").name
    destination = settings.attendance_upload_dir / f"{timestamp}_{safe_name}"
    destination.write_bytes(upload.file.read())
    return destination


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/admin") -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="admin/login.html",
        context={"next_path": _safe_next_path(next)},
    )


@router.post("/login", response_model=None)
def login_submit(
    request: Request,
    password: str = Form(...),
    next: str = Form("/admin"),
):
    if not hmac.compare_digest(password, settings.admin_password):
        return templates.TemplateResponse(
            request=request,
            name="admin/login.html",
            context={
                "next_path": _safe_next_path(next),
                "error": "Incorrect password.",
            },
            status_code=401,
        )

    response = RedirectResponse(url=_safe_next_path(next), status_code=303)
    _set_admin_cookie(response)
    return response


@router.post("/logout")
def logout() -> RedirectResponse:
    response = RedirectResponse(url="/", status_code=303)
    response.delete_cookie(ADMIN_COOKIE_NAME)
    return response


@router.get("", response_class=HTMLResponse)
def admin_dashboard(
    request: Request,
    _admin: None = Depends(require_admin),
    db=Depends(get_db),
) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="admin/dashboard.html",
        context={
            "title": "Admin Dashboard",
            "student_url": suggest_public_base_url(request),
            "hostname_hints": hostname_url_hints(),
            "public_base_url_set": bool(settings.public_base_url),
            **_admin_summary(db),
        },
    )


def _remove_temp_file(path: Path) -> None:
    path.unlink(missing_ok=True)


@router.get("/backup/download", response_model=None)
def download_backup(
    _admin: None = Depends(require_admin),
):
    """Build a classroom data archive and send it to the teacher's browser."""
    if not data_dir_has_backup_content(settings.data_dir):
        return RedirectResponse(url="/admin?backup_error=empty", status_code=303)

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".tar.gz")
    tmp_path = Path(tmp.name)
    tmp.close()

    try:
        write_data_backup(settings.data_dir, tmp_path)
    except BackupError:
        _remove_temp_file(tmp_path)
        return RedirectResponse(url="/admin?backup_error=empty", status_code=303)
    except OSError:
        _remove_temp_file(tmp_path)
        return RedirectResponse(url="/admin?backup_error=failed", status_code=303)

    return FileResponse(
        path=tmp_path,
        filename=backup_archive_name(),
        media_type="application/gzip",
        background=BackgroundTask(_remove_temp_file, tmp_path),
    )


@router.get("/attendance", response_class=HTMLResponse)
def attendance_upload_page(
    request: Request,
    _admin: None = Depends(require_admin),
    db=Depends(get_db),
) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="admin/attendance.html",
        context=_attendance_page_context(db),
    )


@router.post("/attendance/upload")
async def upload_attendance(
    request: Request,
    file: UploadFile = File(...),
    _admin: None = Depends(require_admin),
    db=Depends(get_db),
):
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

    saved_path = _save_attendance_upload(file)

    try:
        result = ingest_attendance_file(db, saved_path, filename)
    except Exception as exc:  # noqa: BLE001 — teacher-friendly UI message
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
            f"&cleared={result.records_cleared}"
            f"&students={result.students_touched}"
        ),
        status_code=303,
    )


@router.get("/claims", response_class=HTMLResponse)
def claim_logs_page(
    request: Request,
    q: str = "",
    status: str = "all",
    _admin: None = Depends(require_admin),
    db=Depends(get_db),
) -> HTMLResponse:
    normalized_status: ClaimLogStatus = (
        status if status in {"all", "success", "failed"} else "all"
    )
    student_query = q.strip() or None
    logs = list_claim_logs(
        db,
        student_query=student_query,
        status=normalized_status,
    )
    return templates.TemplateResponse(
        request=request,
        name="admin/claim_logs.html",
        context={
            "title": "Claim Logs",
            "logs": logs,
            "filters": {"q": q, "status": normalized_status},
        },
    )


@router.get("/print-queue", response_class=HTMLResponse)
def print_queue_page(
    request: Request,
    _admin: None = Depends(require_admin),
    db=Depends(get_db),
) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="admin/print_queue.html",
        context={
            "title": "Print Queue",
            "queue": list_print_queue(db),
        },
    )


@router.post("/print-queue/print", response_model=None)
def print_queue_batch(
    _admin: None = Depends(require_admin),
    db=Depends(get_db),
):
    """Merge queued PDFs into one file for printing, then empty the queue."""
    try:
        batch_path, filename, printed_count = print_batch_and_clear(db)
    except PrintQueueError:
        return RedirectResponse(url="/admin/print-queue?error=empty", status_code=303)
    except OSError:
        return RedirectResponse(url="/admin/print-queue?error=failed", status_code=303)

    return FileResponse(
        path=batch_path,
        filename=filename,
        media_type="application/pdf",
        background=BackgroundTask(_remove_temp_file, batch_path),
    )


@router.post("/print-queue/{item_id}/delete")
def print_queue_delete_item(
    item_id: int,
    _admin: None = Depends(require_admin),
    db=Depends(get_db),
) -> RedirectResponse:
    remove_queue_item(db, item_id)
    return RedirectResponse(url="/admin/print-queue?deleted=1", status_code=303)


@router.post("/print-queue/clear")
def print_queue_clear(
    _admin: None = Depends(require_admin),
    db=Depends(get_db),
) -> RedirectResponse:
    clear_print_queue(db)
    return RedirectResponse(url="/admin/print-queue?cleared=1", status_code=303)


@router.get("/assignments", response_class=HTMLResponse)
def assignments_list(
    request: Request,
    q: str = "",
    date: str = "",
    _admin: None = Depends(require_admin),
    db=Depends(get_db),
) -> HTMLResponse:
    title_query = q.strip() or None
    assigned_date = date.strip() or None
    assignments = list_assignments(
        db,
        title_query=title_query,
        assigned_date=assigned_date,
    )
    return templates.TemplateResponse(
        request=request,
        name="admin/assignments_list.html",
        context={
            "title": "Assignments",
            "assignments": assignments,
            "filters": {"q": q, "date": date},
        },
    )


@router.post("/assignments/{assignment_id}/delete")
def assignment_delete(
    assignment_id: int,
    q: str = Form(""),
    date: str = Form(""),
    _admin: None = Depends(require_admin),
    db=Depends(get_db),
) -> RedirectResponse:
    try:
        delete_assignment(db, assignment_id)
    except ValueError:
        pass

    params = []
    if q.strip():
        params.append(f"q={q.strip()}")
    if date.strip():
        params.append(f"date={date.strip()}")
    params.append("deleted=1")
    query = "&".join(params)
    return RedirectResponse(url=f"/admin/assignments?{query}", status_code=303)


@router.get("/assignments/new", response_class=HTMLResponse)
def assignment_new_page(
    request: Request,
    _admin: None = Depends(require_admin),
) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="admin/assignment_new.html",
        context={
            "title": "Add Assignment",
            "form": {
                "periods": [],
                "assigned_date": "",
                "title": "",
                "description": "",
            },
        },
    )


@router.post("/assignments/new")
async def assignment_new_submit(
    request: Request,
    periods: list[int] = Form(default=[]),
    assigned_date: str = Form(...),
    title: str = Form(...),
    description: str = Form(""),
    pdf: UploadFile = File(...),
    _admin: None = Depends(require_admin),
    db=Depends(get_db),
):
    form = {
        "periods": periods,
        "assigned_date": assigned_date,
        "title": title,
        "description": description,
    }

    try:
        pdf_bytes = await pdf.read()
        if not pdf_bytes:
            raise ValueError("PDF file is empty.")
        create_assignment(
            db,
            periods=periods,
            assigned_date=assigned_date,
            title=title,
            description=description.strip() or None,
            pdf_bytes=pdf_bytes,
            original_filename=pdf.filename or "assignment.pdf",
        )
    except Exception as exc:  # noqa: BLE001 — teacher-friendly UI message
        return templates.TemplateResponse(
            request=request,
            name="admin/assignment_new.html",
            context={"title": "Add Assignment", "form": form, "error": str(exc)},
            status_code=400,
        )

    return RedirectResponse(url="/admin/assignments?success=1", status_code=303)