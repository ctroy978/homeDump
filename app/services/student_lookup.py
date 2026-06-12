"""Queries that power the student claim form."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from app.services.eligibility import check_eligibility, is_allowable_code

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


def normalize_sis_number(sis_number: str) -> str:
    """Normalize user-entered SIS numbers before lookup."""
    return sis_number.strip()


def get_student_by_sis(
    conn: sqlite3.Connection,
    sis_number: str,
) -> StudentRecord | None:
    """Return the student row for a SIS number, if one exists."""
    normalized = normalize_sis_number(sis_number)
    if not normalized:
        return None

    row = conn.execute(
        """
        SELECT id, name, sis_number
        FROM students
        WHERE sis_number = ?
        """,
        (normalized,),
    ).fetchone()
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

    dates = list_eligible_dates_for_student(conn, period, student.id)
    return student, dates


def list_eligible_assignments_for_student(
    conn: sqlite3.Connection,
    period: int,
    student_id: int,
    student_name: str,
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
        result = check_eligibility(conn, student_name, period, date)
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

    options = list_eligible_assignments_for_student(
        conn,
        period,
        student.id,
        student.name,
        absence_date,
    )
    return student, options