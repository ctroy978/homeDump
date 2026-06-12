"""Password-protected admin routes for teachers."""

from __future__ import annotations

import hmac
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.database import get_db
from app.dependencies import (
    ADMIN_COOKIE_MAX_AGE,
    ADMIN_COOKIE_NAME,
    _expected_admin_token,
    require_admin,
)
from app.services.assignments import create_assignment
from app.services.attendance_parser import SUPPORTED_EXTENSIONS, ingest_attendance_file

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
        context={"title": "Admin Dashboard", **_admin_summary(db)},
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
            f"&students={result.students_touched}"
        ),
        status_code=303,
    )


@router.get("/assignments", response_class=HTMLResponse)
def assignments_list(
    request: Request,
    _admin: None = Depends(require_admin),
    db=Depends(get_db),
) -> HTMLResponse:
    assignments = db.execute(
        """
        SELECT id, period, assigned_date, title, description, pdf_filename, created_at
        FROM assignments
        ORDER BY assigned_date DESC, period ASC, id DESC
        """
    ).fetchall()
    return templates.TemplateResponse(
        request=request,
        name="admin/assignments_list.html",
        context={"title": "Assignments", "assignments": assignments},
    )


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
            "form": {"period": 0, "assigned_date": "", "title": "", "description": ""},
        },
    )


@router.post("/assignments/new")
async def assignment_new_submit(
    request: Request,
    period: int = Form(...),
    assigned_date: str = Form(...),
    title: str = Form(...),
    description: str = Form(""),
    pdf: UploadFile = File(...),
    _admin: None = Depends(require_admin),
    db=Depends(get_db),
):
    form = {
        "period": period,
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
            period=period,
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