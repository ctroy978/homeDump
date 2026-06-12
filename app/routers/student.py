"""Student-facing HTMX endpoints for the claim lookup form."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.database import get_db
from app.services.student_lookup import (
    list_eligible_assignments,
    list_eligible_dates,
    list_eligible_students,
)

router = APIRouter(prefix="/student", tags=["student"])
templates = Jinja2Templates(directory=str(settings.project_root / "templates"))


@router.get("/names", response_class=HTMLResponse)
def student_names(
    request: Request,
    period: int = Query(..., ge=0, le=7),
    db=Depends(get_db),
) -> HTMLResponse:
    students = list_eligible_students(db, period)
    return templates.TemplateResponse(
        request=request,
        name="student/_name_select.html",
        context={"period": period, "students": students},
    )


@router.get("/dates", response_class=HTMLResponse)
def student_dates(
    request: Request,
    period: int = Query(..., ge=0, le=7),
    student: str = Query(..., min_length=1),
    db=Depends(get_db),
) -> HTMLResponse:
    dates = list_eligible_dates(db, period, student)
    return templates.TemplateResponse(
        request=request,
        name="student/_date_select.html",
        context={"period": period, "student": student.strip(), "dates": dates},
    )


@router.get("/assignments", response_class=HTMLResponse)
def student_assignments(
    request: Request,
    period: int = Query(..., ge=0, le=7),
    student: str = Query(..., min_length=1),
    date: str = Query(..., min_length=10, max_length=10),
    db=Depends(get_db),
) -> HTMLResponse:
    assignments = list_eligible_assignments(db, period, student, date)
    return templates.TemplateResponse(
        request=request,
        name="student/_assignments_list.html",
        context={
            "period": period,
            "student": student.strip(),
            "date": date.strip(),
            "assignments": assignments,
        },
    )