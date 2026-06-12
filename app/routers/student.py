"""Student-facing HTMX endpoints for the homework request form."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.database import get_db
from app.public_url import PublicUrlError, resolve_public_base_url
from app.services.claims import ClaimError, ClaimResult, process_claim
from app.services.print_queue import enqueue_token, is_already_printed
from app.services.student_lookup import (
    LOOKUP_FAILURE_MESSAGE,
    list_eligible_assignments_by_sis,
    list_eligible_dates_by_sis,
)

router = APIRouter(prefix="/student", tags=["student"])
templates = Jinja2Templates(directory=str(settings.project_root / "templates"))


def _client_ip(request: Request) -> str | None:
    if request.client is None:
        return None
    return request.client.host


def _lookup_failure_response(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="student/_lookup_failure.html",
        context={"message": LOOKUP_FAILURE_MESSAGE},
    )


@router.get("/sis-field", response_class=HTMLResponse)
def student_sis_field(
    request: Request,
    period: int = Query(..., ge=0, le=7),
) -> HTMLResponse:
    """Show the SIS number field after a period is selected."""
    return templates.TemplateResponse(
        request=request,
        name="student/_sis_field.html",
        context={"period": period},
    )


@router.post("/lookup", response_class=HTMLResponse)
def student_lookup(
    request: Request,
    period: int = Form(..., ge=0, le=7),
    sis_number: str = Form(..., min_length=1),
    db=Depends(get_db),
) -> HTMLResponse:
    """Resolve a student by SIS and reveal only their eligible absence dates."""
    student, dates = list_eligible_dates_by_sis(db, period, sis_number)
    if student is None or not dates:
        return _lookup_failure_response(request)

    return templates.TemplateResponse(
        request=request,
        name="student/_date_select.html",
        context={
            "period": period,
            "sis_number": student.sis_number,
            "student_name": student.name,
            "dates": dates,
        },
    )


@router.post("/assignments", response_class=HTMLResponse)
def student_assignments(
    request: Request,
    period: int = Form(..., ge=0, le=7),
    sis_number: str = Form(..., min_length=1),
    date: str = Form(..., min_length=10, max_length=10),
    db=Depends(get_db),
) -> HTMLResponse:
    student, assignments = list_eligible_assignments_by_sis(
        db, period, sis_number, date
    )
    if student is None or not assignments:
        return _lookup_failure_response(request)

    return templates.TemplateResponse(
        request=request,
        name="student/_assignments_list.html",
        context={
            "period": period,
            "sis_number": student.sis_number,
            "student_name": student.name,
            "date": date.strip(),
            "assignments": assignments,
        },
    )


@router.post("/confirm", response_class=HTMLResponse)
def student_confirm(
    request: Request,
    assignment_id: int = Form(...),
    period: int = Form(..., ge=0, le=7),
    sis_number: str = Form(..., min_length=1),
    date: str = Form(..., min_length=10, max_length=10),
    db=Depends(get_db),
) -> HTMLResponse:
    """Prepare homework and add it to the teacher print queue."""
    try:
        public_base_url = resolve_public_base_url(request)
        result = process_claim(
            db,
            sis_number=sis_number,
            assignment_id=assignment_id,
            period=period,
            absence_date=date,
            public_base_url=public_base_url,
            client_ip=_client_ip(request),
            user_agent=request.headers.get("user-agent"),
        )
        if is_already_printed(db, result.token):
            confirm_result = ClaimResult(
                token=result.token,
                student_name=result.student_name,
                assignment_id=result.assignment_id,
                assignment_title=result.assignment_title,
                period=result.period,
                absence_date=result.absence_date,
                already_queued=True,
            )
            return templates.TemplateResponse(
                request=request,
                name="student/_confirm_result.html",
                context={"claim": confirm_result, "already_printed": True},
            )

        newly_queued = enqueue_token(db, result.token)
    except PublicUrlError as exc:
        return templates.TemplateResponse(
            request=request,
            name="student/_claim_error.html",
            context={"message": str(exc)},
            status_code=400,
        )
    except ClaimError as exc:
        return templates.TemplateResponse(
            request=request,
            name="student/_claim_error.html",
            context={"message": str(exc)},
            status_code=400,
        )

    confirm_result = ClaimResult(
        token=result.token,
        student_name=result.student_name,
        assignment_id=result.assignment_id,
        assignment_title=result.assignment_title,
        period=result.period,
        absence_date=result.absence_date,
        already_queued=not newly_queued,
    )
    return templates.TemplateResponse(
        request=request,
        name="student/_confirm_result.html",
        context={"claim": confirm_result, "already_printed": False},
    )