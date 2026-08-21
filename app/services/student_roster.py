"""Active class rosters from period-tagged attendance imports."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from app.services.attendance_parser import validate_class_period


@dataclass(frozen=True)
class RosterStudent:
    """One currently enrolled student in a class period."""

    id: int
    name: str


def list_active_roster(conn: sqlite3.Connection, period: int) -> list[RosterStudent]:
    """Return active students for a period, sorted by name."""
    class_period = validate_class_period(period)
    rows = conn.execute(
        """
        SELECT s.id, s.name
        FROM student_class_periods scp
        JOIN students s ON s.id = scp.student_id
        WHERE scp.period = ? AND scp.active = 1
        ORDER BY s.name COLLATE NOCASE, s.id
        """,
        (class_period,),
    ).fetchall()
    return [
        RosterStudent(id=int(row["id"]), name=str(row["name"]))
        for row in rows
    ]


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
