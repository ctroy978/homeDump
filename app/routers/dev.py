"""Developer-only routes for debugging (disabled unless DEBUG=true)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from app.config import settings
from app.database import get_db
from app.services.eligibility import check_eligibility
from app.services.student_lookup import get_student_by_sis

router = APIRouter(prefix="/dev", tags=["dev"])


def _require_debug() -> None:
    if not settings.debug:
        raise HTTPException(status_code=404, detail="Not found")


@router.get("/eligibility")
def dev_eligibility_check(
    _debug: None = Depends(_require_debug),
    sis_number: str = Query(..., description="Student SIS / student ID"),
    period: int = Query(..., ge=0, le=7, description="Class period (0-7)"),
    date: str = Query(..., description="Absence date (YYYY-MM-DD)"),
    db=Depends(get_db),
) -> dict[str, object]:
    """Quick eligibility check for local testing. Set DEBUG=true in .env to enable."""
    student = get_student_by_sis(db, sis_number)
    if student is None:
        return {
            "eligible": False,
            "student_name": "",
            "period": period,
            "absence_date": date,
            "absence_code": None,
            "reason": "Student not found for this SIS number.",
        }
    result = check_eligibility(db, student.id, period, date)
    return result.as_dict()
