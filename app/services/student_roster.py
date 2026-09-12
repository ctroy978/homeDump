"""Active class rosters from class-list imports and attendance enrollments."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from app.services.attendance_parser import upsert_student, validate_class_period
from app.services.sis import find_student_row_by_sis, normalize_sis_number

MISSING_PERM_ID_MESSAGE = (
    "Missing student ID (Perm ID). Enter their ID to add them to this class."
)
MISSING_NAME_MESSAGE = (
    "Missing student name. Enter their name as Last, First."
)
NOT_ON_ROSTER_MESSAGE = "That student is not on this period's roster."


@dataclass(frozen=True)
class RosterStudent:
    """One currently enrolled student in a class period."""

    id: int
    name: str
    sis_number: str | None = None


@dataclass(frozen=True)
class EnrollResult:
    """Outcome of adding one student to a class period."""

    student: RosterStudent
    created: bool
    reactivated: bool
    already_active: bool
    other_active_periods: tuple[int, ...]


def _student_from_row(row: sqlite3.Row) -> RosterStudent:
    sis = row["sis_number"]
    return RosterStudent(
        id=int(row["id"]),
        name=str(row["name"]),
        sis_number=str(sis) if sis else None,
    )


def list_active_roster(conn: sqlite3.Connection, period: int) -> list[RosterStudent]:
    """Return active students for a period, sorted by name."""
    class_period = validate_class_period(period)
    rows = conn.execute(
        """
        SELECT s.id, s.name, s.sis_number
        FROM student_class_periods scp
        JOIN students s ON s.id = scp.student_id
        WHERE scp.period = ? AND scp.active = 1
        ORDER BY s.name COLLATE NOCASE, s.id
        """,
        (class_period,),
    ).fetchall()
    return [_student_from_row(row) for row in rows]


def list_active_period_counts(conn: sqlite3.Connection) -> dict[int, int]:
    """Return active-student counts for periods 0–7."""
    counts = {period: 0 for period in range(8)}
    rows = conn.execute(
        """
        SELECT period, COUNT(*) AS n
        FROM student_class_periods
        WHERE active = 1
        GROUP BY period
        """
    ).fetchall()
    for row in rows:
        counts[int(row["period"])] = int(row["n"])
    return counts


def list_other_active_periods(
    conn: sqlite3.Connection,
    student_id: int,
    period: int,
) -> tuple[int, ...]:
    """Return other periods where this student is currently active."""
    rows = conn.execute(
        """
        SELECT period
        FROM student_class_periods
        WHERE student_id = ? AND active = 1 AND period != ?
        ORDER BY period
        """,
        (student_id, period),
    ).fetchall()
    return tuple(int(row["period"]) for row in rows)


def resolve_selected_roster(
    conn: sqlite3.Connection,
    period: int,
    student_ids: list[int],
) -> list[RosterStudent]:
    """
    Return the selected active students in roster order.

    Unknown or inactive IDs are rejected so a crafted form cannot pull
    names from another period.
    """
    roster = list_active_roster(conn, period)
    selected_ids: list[int] = []
    seen: set[int] = set()
    for raw in student_ids:
        student_id = int(raw)
        if student_id in seen:
            continue
        seen.add(student_id)
        selected_ids.append(student_id)

    if not selected_ids:
        raise ValueError("Select at least one student.")

    by_id = {student.id: student for student in roster}
    if any(student_id not in by_id for student_id in selected_ids):
        raise ValueError("One or more selected students are not in this period.")

    wanted = set(selected_ids)
    return [student for student in roster if student.id in wanted]


def enroll_student(
    conn: sqlite3.Connection,
    period: int,
    sis_number: str,
    name: str,
) -> EnrollResult:
    """
    Add or reactivate one student on a period roster.

    Identity is SIS only. Other students in the period are left unchanged.
    Other periods for this student are left unchanged.
    """
    class_period = validate_class_period(period)
    display_name = " ".join(str(name or "").split())
    if not display_name:
        raise ValueError(MISSING_NAME_MESSAGE)

    sis = normalize_sis_number(sis_number)
    if not sis:
        raise ValueError(MISSING_PERM_ID_MESSAGE)

    existing = find_student_row_by_sis(conn, sis)
    created = existing is None
    student_id = upsert_student(conn, display_name, None, sis)

    membership = conn.execute(
        """
        SELECT active
        FROM student_class_periods
        WHERE student_id = ? AND period = ?
        """,
        (student_id, class_period),
    ).fetchone()
    already_active = membership is not None and int(membership["active"]) == 1
    reactivated = membership is not None and int(membership["active"]) == 0

    conn.execute(
        """
        INSERT INTO student_class_periods (
            student_id, period, last_upload_id, active
        )
        VALUES (?, ?, NULL, 1)
        ON CONFLICT(student_id, period) DO UPDATE SET
            active = 1
        """,
        (student_id, class_period),
    )
    conn.commit()

    row = conn.execute(
        "SELECT id, name, sis_number FROM students WHERE id = ?",
        (student_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Failed to enroll student SIS {sis}")
    return EnrollResult(
        student=_student_from_row(row),
        created=created,
        reactivated=reactivated,
        already_active=already_active,
        other_active_periods=list_other_active_periods(
            conn, student_id, class_period
        ),
    )


def deactivate_student(
    conn: sqlite3.Connection,
    period: int,
    student_id: int,
) -> RosterStudent:
    """
    Mark one student inactive for a period.

    Keeps the student row, other periods, and absences so leftover makeup
    still works.
    """
    class_period = validate_class_period(period)
    row = conn.execute(
        """
        SELECT s.id, s.name, s.sis_number, scp.active
        FROM student_class_periods scp
        JOIN students s ON s.id = scp.student_id
        WHERE scp.student_id = ? AND scp.period = ?
        """,
        (int(student_id), class_period),
    ).fetchone()
    if row is None or int(row["active"]) != 1:
        raise ValueError(NOT_ON_ROSTER_MESSAGE)

    conn.execute(
        """
        UPDATE student_class_periods
        SET active = 0
        WHERE student_id = ? AND period = ?
        """,
        (int(student_id), class_period),
    )
    conn.commit()
    return _student_from_row(row)
