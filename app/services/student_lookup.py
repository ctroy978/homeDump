"""Queries that power the student claim form."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from app.services.attendance_parser import student_has_class_period
from app.services.eligibility import check_eligibility, is_allowable_code
from app.services.sis import find_student_row_by_sis, normalize_sis_number

LOOKUP_FAILURE_MESSAGE = (
    "We couldn't find matching makeup homework. "
    "Check your period and student ID, or ask your teacher."
)


@dataclass(frozen=True)
class StudentRecord:
    """A student resolved from their SIS number."""

    id: int
    name: str
    sis_number: str


@dataclass(frozen=True)
class AssignmentOption:
    """An assignment a student may claim after passing eligibility."""

    id: int
    title: str
    description: str | None
    assigned_date: str
    period: int


@dataclass(frozen=True)
class ClaimDiagnosis:
    """Teacher-facing explanation of why a student can or cannot claim work."""

    sis_number: str
    period: int
    absence_date: str | None
    student: StudentRecord | None
    summary: str
    eligible_dates: list[str]
    blocked_dates: list[tuple[str, str]]
    assignments: list[AssignmentOption]


def get_student_by_sis(
    conn: sqlite3.Connection,
    sis_number: str,
) -> StudentRecord | None:
    """Return the student row for a SIS number, if one exists."""
    try:
        normalized = normalize_sis_number(sis_number)
    except ValueError:
        return None
    if not normalized:
        return None

    row = find_student_row_by_sis(conn, normalized)
    if row is None or row["sis_number"] is None:
        return None

    return StudentRecord(
        id=int(row["id"]),
        name=str(row["name"]),
        sis_number=str(row["sis_number"]),
    )


def list_periods_with_assignments(conn: sqlite3.Connection) -> list[int]:
    """Return class periods that have at least one uploaded assignment."""
    rows = conn.execute(
        "SELECT DISTINCT period FROM assignment_periods ORDER BY period"
    ).fetchall()
    return [int(row["period"]) for row in rows]


def list_eligible_dates_for_student(
    conn: sqlite3.Connection,
    period: int,
    student_id: int,
) -> list[str]:
    """
    Absence dates where the student qualifies and homework was assigned.

    Dates are returned newest-first (ISO YYYY-MM-DD sorts correctly).
    """
    rows = conn.execute(
        """
        SELECT DISTINCT ar.absence_date, ar.absence_code
        FROM attendance_records ar
        JOIN assignments a ON a.assigned_date = ar.absence_date
        JOIN assignment_periods ap
            ON ap.assignment_id = a.id AND ap.period = ar.period
        WHERE ar.period = ? AND ar.student_id = ?
        ORDER BY ar.absence_date DESC
        """,
        (period, student_id),
    ).fetchall()

    dates: list[str] = []
    seen: set[str] = set()
    for row in rows:
        absence_date = str(row["absence_date"])
        if absence_date in seen:
            continue
        if is_allowable_code(str(row["absence_code"])):
            dates.append(absence_date)
            seen.add(absence_date)
    return dates


def list_eligible_dates_by_sis(
    conn: sqlite3.Connection,
    period: int,
    sis_number: str,
) -> tuple[StudentRecord | None, list[str]]:
    """Resolve a student by SIS and return their eligible absence dates."""
    student = get_student_by_sis(conn, sis_number)
    if student is None:
        return None, []

    if not student_has_class_period(conn, student.id, period):
        return student, []
    dates = list_eligible_dates_for_student(conn, period, student.id)
    return student, dates


def list_eligible_assignments_for_student(
    conn: sqlite3.Connection,
    period: int,
    student_id: int,
    absence_date: str,
) -> list[AssignmentOption]:
    """Assignments the student can claim for the selected period and date."""
    date = absence_date.strip()

    rows = conn.execute(
        """
        SELECT a.id, a.title, a.description, a.assigned_date, ap.period
        FROM assignments a
        JOIN assignment_periods ap ON ap.assignment_id = a.id
        WHERE ap.period = ? AND a.assigned_date = ?
        ORDER BY a.title, a.id
        """,
        (period, date),
    ).fetchall()

    options: list[AssignmentOption] = []
    for row in rows:
        result = check_eligibility(conn, student_id, period, date)
        if not result.eligible:
            continue
        options.append(
            AssignmentOption(
                id=int(row["id"]),
                title=str(row["title"]),
                description=row["description"],
                assigned_date=str(row["assigned_date"]),
                period=int(row["period"]),
            )
        )
    return options


def list_eligible_assignments_by_sis(
    conn: sqlite3.Connection,
    period: int,
    sis_number: str,
    absence_date: str,
) -> tuple[StudentRecord | None, list[AssignmentOption]]:
    """Resolve a student by SIS and return claimable assignments."""
    student = get_student_by_sis(conn, sis_number)
    if student is None:
        return None, []
    if not student_has_class_period(conn, student.id, period):
        return student, []

    options = list_eligible_assignments_for_student(
        conn,
        period,
        student.id,
        absence_date,
    )
    return student, options


def _period_absence_rows(
    conn: sqlite3.Connection,
    student_id: int,
    period: int,
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT absence_date, absence_code
        FROM attendance_records
        WHERE student_id = ? AND period = ?
        ORDER BY absence_date DESC
        """,
        (student_id, period),
    ).fetchall()


def diagnose_claim(
    conn: sqlite3.Connection,
    sis_number: str,
    period: int,
    absence_date: str | None = None,
) -> ClaimDiagnosis:
    """Explain claim eligibility for a teacher standing next to a student."""
    date = absence_date.strip() if absence_date else None
    if date == "":
        date = None

    empty = ClaimDiagnosis(
        sis_number=str(sis_number).strip(),
        period=period,
        absence_date=date,
        student=None,
        summary="",
        eligible_dates=[],
        blocked_dates=[],
        assignments=[],
    )

    try:
        normalized = normalize_sis_number(sis_number)
    except ValueError as exc:
        return ClaimDiagnosis(**{**empty.__dict__, "summary": str(exc)})
    if not normalized:
        return ClaimDiagnosis(
            **{**empty.__dict__, "summary": "Enter a student ID."}
        )

    student = get_student_by_sis(conn, normalized)
    if student is None:
        return ClaimDiagnosis(
            **{
                **empty.__dict__,
                "sis_number": normalized,
                "summary": (
                    "No student with this ID is in the attendance database. "
                    "Upload the class export that contains them."
                ),
            }
        )

    if not student_has_class_period(conn, student.id, period):
        return ClaimDiagnosis(
            sis_number=student.sis_number,
            period=period,
            absence_date=date,
            student=student,
            summary=(
                f"{student.name} has not been imported as your period {period} "
                "class. Upload that class export and tag it as this period."
            ),
            eligible_dates=[],
            blocked_dates=[],
            assignments=[],
        )

    eligible_dates = list_eligible_dates_for_student(conn, period, student.id)
    rows = _period_absence_rows(conn, student.id, period)
    blocked: list[tuple[str, str]] = []
    for row in rows:
        day = str(row["absence_date"])
        if day in eligible_dates:
            continue
        code = str(row["absence_code"])
        if not is_allowable_code(code):
            blocked.append((day, f"Absence code is not allowable: {code}"))
        else:
            blocked.append(
                (day, "Allowable absence, but no homework is assigned for this date.")
            )

    if date:
        result = check_eligibility(conn, student.id, period, date)
        assignments = (
            list_eligible_assignments_for_student(conn, period, student.id, date)
            if result.eligible
            else []
        )
        if not result.eligible:
            summary = f"{student.name}: {result.reason}"
        elif not assignments:
            summary = (
                f"{student.name} has an allowable absence on {date}, but no "
                "homework is assigned for this period and date."
            )
        else:
            summary = (
                f"{student.name} can claim {len(assignments)} assignment(s) "
                f"for period {period} on {date}."
            )
        return ClaimDiagnosis(
            sis_number=student.sis_number,
            period=period,
            absence_date=date,
            student=student,
            summary=summary,
            eligible_dates=eligible_dates,
            blocked_dates=blocked,
            assignments=assignments,
        )

    if eligible_dates:
        summary = (
            f"{student.name} has {len(eligible_dates)} eligible date(s) "
            f"in period {period}."
        )
    elif not rows:
        summary = (
            f"{student.name} has no attendance records for period {period}."
        )
    elif any(
        is_allowable_code(str(row["absence_code"])) for row in rows
    ):
        summary = (
            f"{student.name} has allowable absences in period {period}, "
            "but no matching homework is uploaded for those dates."
        )
    else:
        summary = (
            f"{student.name} has attendance in period {period}, but none of "
            "the codes qualify for makeup."
        )

    return ClaimDiagnosis(
        sis_number=student.sis_number,
        period=period,
        absence_date=None,
        student=student,
        summary=summary,
        eligible_dates=eligible_dates,
        blocked_dates=blocked,
        assignments=[],
    )