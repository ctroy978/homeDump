"""Teacher scan and prep workflows for GitHub worksheet distributions."""

from __future__ import annotations

import re
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.database import get_db
from app.dependencies import is_scan_authenticated, pin_matches, require_admin, set_scan_cookie
from app.public_url import PublicUrlError
from app.services.distribution import register_distribution
from app.services.distribution_log import DistributionOutcome, list_distribution_events
from app.services.distribution_packet import (
    DistributionPacketError,
    build_distribute_url,
    build_print_packet_pdf,
)
from app.services.github_worksheets import (
    GitHubWorksheetError,
    browse_pdf_worksheets,
    display_title_from_path,
    fetch_pdf_bytes,
    list_filtered_repos,
    validate_worksheet_locator,
)

router = APIRouter(prefix="/admin", tags=["distribution"])
templates = Jinja2Templates(directory=str(settings.project_root / "templates"))


def _github_enabled_for_templates() -> bool:
    from app.config import settings as current_settings

    return current_settings.github_enabled


templates.env.globals["github_enabled"] = _github_enabled_for_templates

ALL_PERIODS = list(range(8))


def _client_ip(request: Request) -> str | None:
    if request.client is None:
        return None
    return request.client.host


def _locator_context(repo: str, path: str) -> dict[str, str]:
    return {
        "repo": repo,
        "path": path,
        "display_title": display_title_from_path(path),
    }


def _config_error_response(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="admin/distribute_config_error.html",
        context={"title": "Distribution Unavailable"},
        status_code=503,
    )


def _invalid_link_response(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="admin/distribute_config_error.html",
        context={
            "title": "Invalid Install Link",
            "message": "This install link is incomplete. Scan the QR code from your worksheet cover sheet.",
        },
        status_code=400,
    )


def _safe_packet_filename(path: str) -> str:
    stem = display_title_from_path(path)
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-") or "worksheet"
    return f"{slug}-print-packet.pdf"


@router.get("/distribute/prep", response_class=HTMLResponse)
def distribute_prep_page(
    request: Request,
    repo: str | None = Query(default=None),
    _admin: None = Depends(require_admin),
) -> HTMLResponse:
    """Browse GitHub worksheet repos and prepare print packets."""
    if not settings.github_enabled:
        return RedirectResponse(
            url="/admin?prep_error=github_disabled",
            status_code=303,
        )

    repos = list_filtered_repos()
    selected_repo = repo or (repos[0].name if repos else "")
    browse = (
        browse_pdf_worksheets(selected_repo)
        if selected_repo
        else None
    )
    error_code = request.query_params.get("error")
    error_message = None
    if error_code == "packet":
        error_message = request.query_params.get("message", "Could not build print packet.")

    return templates.TemplateResponse(
        request=request,
        name="admin/distribute_prep.html",
        context={
            "title": "GitHub Worksheets",
            "repos": repos,
            "selected_repo": selected_repo,
            "repo": selected_repo,
            "browse": browse,
            "search_query": "",
            "error": error_message,
            "browser_mode": "packet",
            "browse_url": "/admin/distribute/prep/browse",
            "repo_field_id": "repo",
        },
    )


@router.get("/distribute/prep/browse", response_class=HTMLResponse)
def distribute_prep_browse(
    request: Request,
    repo: str = Query(..., min_length=1),
    path: str = "",
    q: str | None = Query(default=None),
    _admin: None = Depends(require_admin),
) -> HTMLResponse:
    """HTMX partial: folder browser for PDF worksheets in the selected repo."""
    if not settings.github_enabled:
        return HTMLResponse(
            "<p class='status-note error'>GitHub integration is not configured.</p>",
            status_code=503,
        )

    try:
        browse = browse_pdf_worksheets(repo, path=path, query=q)
    except ValueError as exc:
        return HTMLResponse(
            f"<p class='status-note error'>{exc}</p>",
            status_code=400,
        )
    return templates.TemplateResponse(
        request=request,
        name="admin/_worksheet_list.html",
        context={
            "repo": repo,
            "browse": browse,
            "search_query": q or "",
            "browser_mode": "packet",
            "browse_url": "/admin/distribute/prep/browse",
            "repo_field_id": "repo",
        },
    )


@router.get("/distribute/prep/print-packet", response_model=None)
def distribute_print_packet(
    request: Request,
    repo: str = Query(..., min_length=1),
    path: str = Query(..., min_length=1),
    _admin: None = Depends(require_admin),
):
    """Download a cover sheet plus worksheet PDF for classroom printing."""
    if not settings.github_enabled:
        return RedirectResponse(
            url="/admin?prep_error=github_disabled",
            status_code=303,
        )

    try:
        validate_worksheet_locator(repo, path)
        worksheet_pdf = fetch_pdf_bytes(repo, path)
        distribute_url = build_distribute_url(request, repo, path)
        display_title = display_title_from_path(path)
        packet_bytes = build_print_packet_pdf(
            display_title=display_title,
            distribute_url=distribute_url,
            github_repo=repo,
            github_path=path,
            worksheet_pdf_bytes=worksheet_pdf,
        )
    except (GitHubWorksheetError, DistributionPacketError, PublicUrlError, ValueError) as exc:
        message = quote(str(exc))
        return RedirectResponse(
            url=f"/admin/distribute/prep?repo={quote(repo)}&error=packet&message={message}",
            status_code=303,
        )

    filename = _safe_packet_filename(path)
    return Response(
        content=packet_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/distribute", response_class=HTMLResponse)
def distribute_scan_page(
    request: Request,
    repo: str | None = Query(default=None),
    path: str | None = Query(default=None),
) -> HTMLResponse:
    """Show the PIN gate or period selection form for a scanned install QR."""
    if not settings.scan_enabled:
        return _config_error_response(request)

    if not repo or not path:
        return _invalid_link_response(request)

    try:
        validate_worksheet_locator(repo, path)
    except ValueError as exc:
        return templates.TemplateResponse(
            request=request,
            name="admin/distribute_config_error.html",
            context={
                "title": "Invalid Install Link",
                "message": str(exc),
            },
            status_code=400,
        )

    context = _locator_context(repo, path)
    if not is_scan_authenticated(request):
        return templates.TemplateResponse(
            request=request,
            name="admin/distribute_pin.html",
            context={
                "title": "Distribution PIN",
                **context,
            },
        )

    return templates.TemplateResponse(
        request=request,
        name="admin/distribute_scan.html",
        context={
            "title": "Register Distribution",
            "periods": ALL_PERIODS,
            **context,
        },
    )


@router.post("/distribute/pin", response_model=None)
def distribute_pin_submit(
    request: Request,
    pin: str = Form(...),
    repo: str = Form(...),
    path: str = Form(...),
):
    """Validate the teacher PIN and open a short-lived scan session."""
    if not settings.scan_enabled:
        return _config_error_response(request)

    try:
        validate_worksheet_locator(repo, path)
    except ValueError as exc:
        return templates.TemplateResponse(
            request=request,
            name="admin/distribute_config_error.html",
            context={
                "title": "Invalid Install Link",
                "message": str(exc),
            },
            status_code=400,
        )

    if not pin_matches(pin):
        return templates.TemplateResponse(
            request=request,
            name="admin/distribute_pin.html",
            context={
                "title": "Distribution PIN",
                "error": "Incorrect PIN. Try again.",
                **_locator_context(repo, path),
            },
            status_code=401,
        )

    query = urlencode({"repo": repo, "path": path})
    response = RedirectResponse(
        url=f"/admin/distribute?{query}",
        status_code=303,
    )
    set_scan_cookie(response)
    return response


@router.post("/distribute", response_class=HTMLResponse)
async def distribute_submit(
    request: Request,
    repo: str = Form(...),
    path: str = Form(...),
    db=Depends(get_db),
) -> HTMLResponse:
    """Register the worksheet for the selected periods on today's date."""
    form = await request.form()
    periods = [int(value) for value in form.getlist("periods")]

    if not settings.scan_enabled:
        return _config_error_response(request)

    try:
        validate_worksheet_locator(repo, path)
    except ValueError as exc:
        return templates.TemplateResponse(
            request=request,
            name="admin/distribute_config_error.html",
            context={
                "title": "Invalid Install Link",
                "message": str(exc),
            },
            status_code=400,
        )

    if not is_scan_authenticated(request):
        return templates.TemplateResponse(
            request=request,
            name="admin/distribute_pin.html",
            context={
                "title": "Distribution PIN",
                "error": "Enter your PIN to register this worksheet.",
                **_locator_context(repo, path),
            },
            status_code=401,
        )

    if not periods:
        return templates.TemplateResponse(
            request=request,
            name="admin/distribute_scan.html",
            context={
                "title": "Register Distribution",
                "periods": ALL_PERIODS,
                "error": "Select at least one class period.",
                **_locator_context(repo, path),
            },
            status_code=400,
        )

    result = register_distribution(
        db,
        github_repo=repo,
        github_path=path,
        periods=periods,
        client_ip=_client_ip(request),
    )
    return templates.TemplateResponse(
        request=request,
        name="admin/distribute_success.html",
        context={
            "title": "Distribution Registered",
            "result": result,
            **_locator_context(repo, path),
        },
    )


@router.get("/distribution-log", response_class=HTMLResponse)
def distribution_log_page(
    request: Request,
    repo: str = "",
    date: str = "",
    outcome: str = "all",
    _admin: None = Depends(require_admin),
    db=Depends(get_db),
) -> HTMLResponse:
    """Review append-only audit entries for worksheet scan registrations."""
    normalized_outcome: DistributionOutcome = (
        outcome
        if outcome in {"all", "success", "partial", "all_duplicate", "failure"}
        else "all"
    )
    repo_query = repo.strip() or None
    assigned_date = date.strip() or None
    logs = list_distribution_events(
        db,
        repo_query=repo_query,
        assigned_date=assigned_date,
        outcome=normalized_outcome,
    )
    return templates.TemplateResponse(
        request=request,
        name="admin/distribution_log.html",
        context={
            "title": "Distribution Log",
            "logs": logs,
            "filters": {
                "repo": repo,
                "date": date,
                "outcome": normalized_outcome,
            },
        },
    )
