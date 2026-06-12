"""Student-facing HTMX endpoints for the claim lookup form."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.database import get_db
from app.public_url import PublicUrlError, resolve_public_base_url
from app.services.claims import ClaimError, claim_pdf_path, process_claim
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


@router.post("/claim", response_class=HTMLResponse)
def student_claim(
    request: Request,
    assignment_id: int = Form(...),
    period: int = Form(..., ge=0, le=7),
    sis_number: str = Form(..., min_length=1),
    date: str = Form(..., min_length=10, max_length=10),
    db=Depends(get_db),
) -> HTMLResponse:
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