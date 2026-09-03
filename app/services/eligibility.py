"""Determine whether a student qualifies for makeup homework."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

_WHITESPACE_RE = re.compile(r"\s+")


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


def normalize_absence_code(value: str) -> str:
    """Case, spacing, and apostrophe fold used for absence-code matching."""
    text = normalize_text(value)
    return _WHITESPACE_RE.sub(" ", text).casefold()


def unique_absence_codes(codes: list[str]) -> list[str]:
    """
    Return unique display labels from imported codes.

    First-seen spelling is kept; matching is case/space insensitive.
    """
    seen: dict[str, str] = {}
    for raw in codes:
        text = normalize_text(str(raw))
        if not text:
            continue
        key = normalize_absence_code(text)
        seen.setdefault(key, text)

    return sorted(seen.values(), key=str.casefold)


def check_eligibility(
    conn: sqlite3.Connection,
    student_id: int,
    period: int,
    absence_date: str,
) -> EligibilityResult:
    """
    Check whether a student had an absence on a given date and period.

    Any recorded absence qualifies for makeup work, whether the SIS code is
    excused or unexcused. Identity is by student_id (SIS-backed person), not
    display name.
    """
    date = absence_date.strip()

    student = conn.execute(
        "SELECT name FROM students WHERE id = ?",
        (student_id,),
    ).fetchone()
    if student is None:
        return EligibilityResult(
            eligible=False,
            student_name="",
            period=period,
            absence_date=date,
            reason="Student not found.",
        )

    name = str(student["name"])
    row = conn.execute(
        """
        SELECT absence_code
        FROM attendance_records
        WHERE student_id = ? AND period = ? AND absence_date = ?
        """,
        (student_id, period, date),
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
    return EligibilityResult(
        eligible=True,
        student_name=name,
        period=period,
        absence_date=date,
        absence_code=absence_code,
        reason="Absence record found.",
    )
