"""Password-protected admin routes for teachers."""

from __future__ import annotations

import hmac
import tempfile
from datetime import datetime
from html import escape
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
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
from app.services.assignments import (
    add_periods_to_assignment,
    create_assignment,
    delete_assignment,
    find_github_assignment,
    get_assignment_pdf_path,
    list_assignments,
    write_assignment_pdf,
)
from app.services.attendance_parser import SUPPORTED_EXTENSIONS, ingest_attendance_file
from app.services.claim_logs import ClaimLogStatus, list_claim_logs
from app.services.data_backup import (
    BackupError,
    backup_archive_name,
    data_dir_has_backup_content,
    write_data_backup,
)
from app.services.github_worksheets import (
    GitHubWorksheetError,
    assert_repo_allowed,
    browse_pdf_worksheets,
    fetch_pdf_bytes,
    list_filtered_repos,
    validate_worksheet_locator,
)
from app.services.print_queue import (
    PrintQueueError,
    clear_print_queue,
    list_print_queue,
    print_batch_and_clear,
    remove_queue_item,
)
from app.services.student_lookup import diagnose_claim

router = APIRouter(prefix="/admin", tags=["admin"])
templates = Jinja2Templates(directory=str(settings.project_root / "templates"))


def _github_enabled_for_templates() -> bool:
    from app.config import settings as current_settings

    return current_settings.github_enabled


templates.env.globals["github_enabled"] = _github_enabled_for_templates


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


def _attendance_page_context(
    db,
    error: str | None = None,
    *,
    import_result=None,
) -> dict:
    uploads = db.execute(
        """
        SELECT id, filename, uploaded_at, row_count
        FROM attendance_uploads
        ORDER BY uploaded_at DESC
        LIMIT 10
        """
    ).fetchall()
    summary = _admin_summary(db)
    context = {
        "title": "Upload Attendance",
        "uploads": uploads,
        "error": error,
        "import_result": import_result,
        **summary,
    }
    return context


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


def _assignment_form_context(
    form: dict,
    *,
    error: str | None = None,
) -> dict:
    """Build Add Assignment context without letting GitHub failures block uploads."""
    context = {
        "title": "Add Assignment",
        "form": form,
        "repos": [],
        "selected_repo": "",
        "repo": "",
        "browse": None,
        "search_query": "",
        "github_error": None,
        "browser_mode": "assignment",
        "browse_url": "/admin/assignments/new/github-browse",
        "repo_field_id": "assignment-github-repo",
    }
    if error:
        context["error"] = error
    if not settings.github_enabled:
        return context

    try:
        repos = list_filtered_repos()
        requested_repo = str(form.get("github_repo") or "")
        repo_names = {item.name for item in repos}
        selected_repo = (
            requested_repo
            if requested_repo in repo_names
            else (repos[0].name if repos else "")
        )
        context.update(
            {
                "repos": repos,
                "selected_repo": selected_repo,
                "repo": selected_repo,
                "browse": (
                    browse_pdf_worksheets(selected_repo)
                    if selected_repo
                    else None
                ),
            }
        )
    except (GitHubWorksheetError, ValueError) as exc:
        context["github_error"] = str(exc)
    return context


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


@router.get("/eligibility", response_class=HTMLResponse)
def eligibility_lookup_page(
    request: Request,
    sis_number: str = "",
    period: str = "",
    date: str = "",
    _admin: None = Depends(require_admin),
    db=Depends(get_db),
) -> HTMLResponse:
    """Teacher diagnostic: why a student ID can or cannot claim makeup work."""
    diagnosis = None
    error = None
    period_value: int | None = None
    if sis_number.strip() or period.strip() or date.strip():
        try:
            period_value = int(period)
            if not 0 <= period_value <= 7:
                raise ValueError("Period must be between 0 and 7.")
        except ValueError:
            error = "Choose a class period (0–7)."
        else:
            diagnosis = diagnose_claim(
                db,
                sis_number,
                period_value,
                date.strip() or None,
            )

    return templates.TemplateResponse(
        request=request,
        name="admin/eligibility.html",
        context={
            "title": "Student lookup",
            "form": {
                "sis_number": sis_number,
                "period": period if period_value is None else str(period_value),
                "date": date.strip(),
            },
            "diagnosis": diagnosis,
            "error": error,
            "periods": list(range(8)),
        },
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

    # Render result in-page so rejection details (names / reasons) are visible.
    return templates.TemplateResponse(
        request=request,
        name="admin/attendance.html",
        context=_attendance_page_context(db, import_result=result),
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
    """Merge printable PDFs; skip missing/corrupt files so the rest still print."""
    try:
        result = print_batch_and_clear(db)
    except PrintQueueError as exc:
        if exc.skipped:
            return RedirectResponse(
                url="/admin/print-queue?error=skipped",
                status_code=303,
            )
        return RedirectResponse(url="/admin/print-queue?error=empty", status_code=303)
    except OSError:
        return RedirectResponse(url="/admin/print-queue?error=failed", status_code=303)

    return FileResponse(
        path=result.batch_path,
        filename=result.filename,
        media_type="application/pdf",
        background=BackgroundTask(_remove_temp_file, result.batch_path),
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
        context=_assignment_form_context(
            {
                "source": "upload",
                "periods": [],
                "assigned_date": "",
                "title": "",
                "description": "",
                "github_repo": "",
                "github_path": "",
            }
        ),
    )


@router.get("/assignments/new/github-browse", response_class=HTMLResponse)
def assignment_github_browse(
    request: Request,
    repo: str = Query(..., min_length=1),
    path: str = "",
    q: str | None = Query(default=None),
    _admin: None = Depends(require_admin),
) -> HTMLResponse:
    """HTMX partial: choose a GitHub PDF for the Add Assignment form."""
    if not settings.github_enabled:
        return HTMLResponse(
            "<p class='status-note error'>GitHub integration is not configured.</p>",
            status_code=503,
        )

    try:
        allowed = list_filtered_repos()
        assert_repo_allowed(repo, allowed)
        browse = browse_pdf_worksheets(repo, path=path, query=q)
    except (GitHubWorksheetError, ValueError) as exc:
        return HTMLResponse(
            f"<p class='status-note error'>{escape(str(exc))}</p>",
            status_code=400,
        )

    return templates.TemplateResponse(
        request=request,
        name="admin/_worksheet_list.html",
        context={
            "repo": repo,
            "browse": browse,
            "search_query": q or "",
            "browser_mode": "assignment",
            "browse_url": "/admin/assignments/new/github-browse",
            "repo_field_id": "assignment-github-repo",
        },
    )


@router.post("/assignments/new")
async def assignment_new_submit(
    request: Request,
    source: str = Form("upload"),
    periods: list[int] = Form(default=[]),
    assigned_date: str = Form(...),
    title: str = Form(...),
    description: str = Form(""),
    pdf: UploadFile | None = File(default=None),
    github_repo: str = Form(""),
    github_path: str = Form(""),
    _admin: None = Depends(require_admin),
    db=Depends(get_db),
):
    form = {
        "source": source,
        "periods": periods,
        "assigned_date": assigned_date,
        "title": title,
        "description": description,
        "github_repo": github_repo,
        "github_path": github_path,
    }

    try:
        if not title.strip():
            raise ValueError("Title is required.")
        if source == "upload":
            if pdf is None or not pdf.filename:
                raise ValueError("Choose a PDF file from this computer.")
            pdf_bytes = await pdf.read()
            create_assignment(
                db,
                periods=periods,
                assigned_date=assigned_date,
                title=title,
                description=description.strip() or None,
                pdf_bytes=pdf_bytes,
                original_filename=pdf.filename,
            )
        elif source == "github":
            if not settings.github_enabled:
                raise ValueError("GitHub integration is not configured.")
            validate_worksheet_locator(github_repo, github_path)
            allowed = list_filtered_repos()
            assert_repo_allowed(github_repo, allowed)

            existing_id = find_github_assignment(
                db,
                github_repo,
                github_path,
                assigned_date.strip(),
            )
            if existing_id is not None:
                add_periods_to_assignment(db, existing_id, periods)
                existing_pdf = get_assignment_pdf_path(existing_id)
                if not existing_pdf.exists():
                    pdf_bytes = fetch_pdf_bytes(github_repo, github_path)
                    if not pdf_bytes:
                        raise ValueError("GitHub returned an empty PDF file.")
                    write_assignment_pdf(existing_id, pdf_bytes)
                db.commit()
            else:
                pdf_bytes = fetch_pdf_bytes(github_repo, github_path)
                if not pdf_bytes:
                    raise ValueError("GitHub returned an empty PDF file.")
                create_assignment(
                    db,
                    periods=periods,
                    assigned_date=assigned_date,
                    title=title,
                    description=description.strip() or None,
                    pdf_bytes=pdf_bytes,
                    original_filename=Path(github_path).name,
                    source="github",
                    github_repo=github_repo,
                    github_path=github_path,
                )
        else:
            raise ValueError("Choose either a local upload or a GitHub worksheet.")
    except Exception as exc:  # noqa: BLE001 — teacher-friendly UI message
        db.rollback()
        return templates.TemplateResponse(
            request=request,
            name="admin/assignment_new.html",
            context=_assignment_form_context(form, error=str(exc)),
            status_code=400,
        )

    return RedirectResponse(url="/admin/assignments?success=1", status_code=303)
