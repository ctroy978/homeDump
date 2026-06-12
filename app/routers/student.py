"""Student-facing HTMX endpoints for the claim lookup form."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.database import get_db
from app.services.claims import ClaimError, claim_pdf_path, process_claim
from app.services.student_lookup import (
    list_eligible_assignments,
    list_eligible_dates,
    list_eligible_students,
)

router = APIRouter(prefix="/student", tags=["student"])
templates = Jinja2Templates(directory=str(settings.project_root / "templates"))


def _public_base_url(request: Request) -> str:
    if settings.public_base_url:
        return settings.public_base_url.rstrip("/")
    return str(request.base_url).rstrip("/")


def _client_ip(request: Request) -> str | None:
    if request.client is None:
        return None
    return request.client.host


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


@router.post("/claim", response_class=HTMLResponse)
def student_claim(
    request: Request,
    assignment_id: int = Form(...),
    period: int = Form(..., ge=0, le=7),
    student: str = Form(..., min_length=1),
    date: str = Form(..., min_length=10, max_length=10),
    db=Depends(get_db),
) -> HTMLResponse:
    try:
        result = process_claim(
            db,
            student_name=student,
            assignment_id=assignment_id,
            period=period,
            absence_date=date,
            public_base_url=_public_base_url(request),
            client_ip=_client_ip(request),
            user_agent=request.headers.get("user-agent"),
        )
    except ClaimError as exc:
        return templates.TemplateResponse(
            request=request,
            name="student/_claim_error.html",
            context={"message": str(exc)},
            status_code=400,
        )

    return templates.TemplateResponse(
        request=request,
        name="student/_claim_result.html",
        context={"claim": result},
    )


@router.get("/claim/{token}/download")
def download_claimed_pdf(
    token: str,
    db=Depends(get_db),
) -> FileResponse:
    normalized = token.strip().upper()
    row = db.execute(
        "SELECT 1 FROM claim_tokens WHERE token = ?",
        (normalized,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Claim not found.")

    pdf_path = claim_pdf_path(normalized)
    if not pdf_path.exists():
        raise HTTPException(status_code=404, detail="Claim PDF not found.")

    return FileResponse(
        path=pdf_path,
        media_type="application/pdf",
        filename=f"makeup-homework-{normalized}.pdf",
    )


@router.get("/claim/{token}/qr.png")
def claim_qr_image(
    token: str,
    db=Depends(get_db),
) -> FileResponse:
    normalized = token.strip().upper()
    row = db.execute(
        "SELECT 1 FROM claim_tokens WHERE token = ?",
        (normalized,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Claim not found.")

    qr_path = settings.qrcodes_dir / f"{normalized}.png"
    if not qr_path.exists():
        raise HTTPException(status_code=404, detail="QR code not found.")

    return FileResponse(path=qr_path, media_type="image/png")