"""Determine whether a student qualifies for makeup homework."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from app.config import settings


@dataclass(frozen=True)
class EligibilityResult:
    """Outcome of an eligibility check for one student/period/date."""

    eligible: bool
    student_name: str
    period: int
    absence_date: str
    absence_code: str | None = None
    reason: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "eligible": self.eligible,
            "student_name": self.student_name,
            "period": self.period,
            "absence_date": self.absence_date,
            "absence_code": self.absence_code,
            "reason": self.reason,
        }


def normalize_text(value: str) -> str:
    """Normalize strings for comparison (trim, unify apostrophe variants)."""
    text = value.strip()
    return text.replace("\u2019", "'").replace("\u2018", "'")


def is_allowable_code(
    code: str,
    allowable_codes: tuple[str, ...] | None = None,
) -> bool:
    """Return True when an absence code is in the configured allowable list."""
    codes = allowable_codes if allowable_codes is not None else settings.allowable_codes
    normalized_code = normalize_text(code)
    allowable_normalized = {normalize_text(item) for item in codes}
    return normalized_code in allowable_normalized


def check_eligibility(
    conn: sqlite3.Connection,
    student_name: str,
    period: int,
    absence_date: str,
) -> EligibilityResult:
    """
    Check whether a student had an allowable absence on a given date and period.

    Requires an exact match on student name, period (0-7), and absence date
    (YYYY-MM-DD).
    """
    name = student_name.strip()
    date = absence_date.strip()

    row = conn.execute(
        """
        SELECT ar.absence_code
        FROM attendance_records ar
        JOIN students s ON s.id = ar.student_id
        WHERE s.name = ? AND ar.period = ? AND ar.absence_date = ?
        """,
        (name, period, date),
    ).fetchone()

    if row is None:
        return EligibilityResult(
            eligible=False,
            student_name=name,
            period=period,
            absence_date=date,
            reason="No absence record found for this student, period, and date.",
        )

    absence_code = str(row["absence_code"])
    if is_allowable_code(absence_code):
        return EligibilityResult(
            eligible=True,
            student_name=name,
            period=period,
            absence_date=date,
            absence_code=absence_code,
            reason="Allowable absence code.",
        )

    return EligibilityResult(
        eligible=False,
        student_name=name,
        period=period,
        absence_date=date,
        absence_code=absence_code,
        reason=f"Absence code is not allowable: {absence_code}",
    )