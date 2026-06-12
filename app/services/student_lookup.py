"""Queries that power the student claim form dropdowns."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from app.services.eligibility import check_eligibility, is_allowable_code


@dataclass(frozen=True)
class AssignmentOption:
    """An assignment a student may claim after passing eligibility."""

    id: int
    title: str
    description: str | None
    assigned_date: str
    period: int


def list_periods_with_assignments(conn: sqlite3.Connection) -> list[int]:
    """Return class periods that have at least one uploaded assignment."""
    rows = conn.execute(
        "SELECT DISTINCT period FROM assignments ORDER BY period"
    ).fetchall()
    return [int(row["period"]) for row in rows]


def list_eligible_students(conn: sqlite3.Connection, period: int) -> list[str]:
    """
    Students with an allowable absence in ``period`` on a date that has homework.

    Only includes students where an assignment exists for the same period and
    assigned date as the absence.
    """
    rows = conn.execute(
        """
        SELECT DISTINCT s.name, ar.absence_code
        FROM students s
        JOIN attendance_records ar ON ar.student_id = s.id
        JOIN assignments a
            ON a.period = ar.period AND a.assigned_date = ar.absence_date
        WHERE ar.period = ?
        ORDER BY s.name
        """,
        (period,),
    ).fetchall()

    eligible_names: list[str] = []
    seen: set[str] = set()
    for row in rows:
        name = str(row["name"])
        if name in seen:
            continue
        if is_allowable_code(str(row["absence_code"])):
            eligible_names.append(name)
            seen.add(name)
    return eligible_names


def list_eligible_dates(
    conn: sqlite3.Connection,
    period: int,
    student_name: str,
) -> list[str]:
    """
    Absence dates where the student qualifies and homework was assigned.

    Dates are returned newest-first (ISO YYYY-MM-DD sorts correctly).
    """
    name = student_name.strip()
    rows = conn.execute(
        """
        SELECT DISTINCT ar.absence_date, ar.absence_code
        FROM attendance_records ar
        JOIN students s ON s.id = ar.student_id
        JOIN assignments a
            ON a.period = ar.period AND a.assigned_date = ar.absence_date
        WHERE ar.period = ? AND s.name = ?
        ORDER BY ar.absence_date DESC
        """,
        (period, name),
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


def list_eligible_assignments(
    conn: sqlite3.Connection,
    period: int,
    student_name: str,
    absence_date: str,
) -> list[AssignmentOption]:
    """Assignments the student can claim for the selected period and date."""
    name = student_name.strip()
    date = absence_date.strip()

    rows = conn.execute(
        """
        SELECT id, title, description, assigned_date, period
        FROM assignments
        WHERE period = ? AND assigned_date = ?
        ORDER BY title, id
        """,
        (period, date),
    ).fetchall()

    options: list[AssignmentOption] = []
    for row in rows:
        result = check_eligibility(conn, name, period, date)
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