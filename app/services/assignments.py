"""Assignment storage helpers."""

from __future__ import annotations

import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from app.config import settings


@dataclass(frozen=True)
class AssignmentRow:
    """Assignment summary for admin list views."""

    id: int
    assigned_date: str
    title: str
    description: str | None
    pdf_filename: str
    created_at: str
    periods: list[int]

    @property
    def periods_display(self) -> str:
        return format_period_list(self.periods)


def _validate_periods(periods: list[int]) -> list[int]:
    if not periods:
        raise ValueError("Select at least one class period.")
    unique = sorted({int(period) for period in periods})
    for period in unique:
        if not 0 <= period <= 7:
            raise ValueError("Period must be between 0 and 7.")
    return unique


def create_assignment(
    conn: sqlite3.Connection,
    periods: list[int],
    assigned_date: str,
    title: str,
    description: str | None,
    pdf_bytes: bytes,
    original_filename: str,
) -> int:
    """
    Insert an assignment row, link it to one or more periods, and store its PDF.

    Returns the new assignment id.
    """
    period_list = _validate_periods(periods)

    assigned_date = assigned_date.strip()
    title = title.strip()
    if not title:
        raise ValueError("Title is required.")

    safe_filename = Path(original_filename or "assignment.pdf").name
    if not safe_filename.lower().endswith(".pdf"):
        raise ValueError("Assignment file must be a PDF.")

    cursor = conn.execute(
        """
        INSERT INTO assignments (assigned_date, title, description, pdf_filename)
        VALUES (?, ?, ?, ?)
        """,
        (assigned_date, title, description, safe_filename),
    )
    assignment_id = int(cursor.lastrowid)

    conn.executemany(
        """
        INSERT INTO assignment_periods (assignment_id, period)
        VALUES (?, ?)
        """,
        [(assignment_id, period) for period in period_list],
    )

    assignment_dir = settings.assignments_dir / str(assignment_id)
    assignment_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = assignment_dir / "original.pdf"
    pdf_path.write_bytes(pdf_bytes)

    conn.commit()
    return assignment_id


def format_period_list(periods: list[int]) -> str:
    """Format period integers for display, e.g. ``[1, 3, 5]`` -> ``1, 3, 5``."""
    return ", ".join(str(period) for period in sorted(periods))


def list_assignments(
    conn: sqlite3.Connection,
    *,
    title_query: str | None = None,
    assigned_date: str | None = None,
) -> list[AssignmentRow]:
    """Return assignments, optionally filtered by title substring or exact date."""
    clauses = ["1 = 1"]
    params: list[str] = []

    if title_query:
        clauses.append("LOWER(a.title) LIKE ?")
        params.append(f"%{title_query.strip().lower()}%")

    if assigned_date:
        clauses.append("a.assigned_date = ?")
        params.append(assigned_date.strip())

    where_sql = " AND ".join(clauses)
    rows = conn.execute(
        f"""
        SELECT
            a.id,
            a.assigned_date,
            a.title,
            a.description,
            a.pdf_filename,
            a.created_at,
            GROUP_CONCAT(ap.period) AS periods
        FROM assignments a
        LEFT JOIN assignment_periods ap ON ap.assignment_id = a.id
        WHERE {where_sql}
        GROUP BY a.id
        ORDER BY a.assigned_date DESC, a.id DESC
        """,
        params,
    ).fetchall()

    results: list[AssignmentRow] = []
    for row in rows:
        period_values = [
            int(value)
            for value in str(row["periods"] or "").split(",")
            if value.strip()
        ]
        results.append(
            AssignmentRow(
                id=int(row["id"]),
                assigned_date=str(row["assigned_date"]),
                title=str(row["title"]),
                description=row["description"],
                pdf_filename=str(row["pdf_filename"]),
                created_at=str(row["created_at"]),
                periods=period_values,
            )
        )
    return results


def delete_assignment(conn: sqlite3.Connection, assignment_id: int) -> None:
    """Delete an assignment, its period links, stored PDF, and claim tokens."""
    row = conn.execute(
        "SELECT id FROM assignments WHERE id = ?",
        (assignment_id,),
    ).fetchone()
    if row is None:
        raise ValueError("Assignment not found.")

    conn.execute("DELETE FROM claim_tokens WHERE assignment_id = ?", (assignment_id,))
    conn.execute(
        "UPDATE claim_logs SET assignment_id = NULL WHERE assignment_id = ?",
        (assignment_id,),
    )
    conn.execute("DELETE FROM assignments WHERE id = ?", (assignment_id,))
    conn.commit()

    assignment_dir = settings.assignments_dir / str(assignment_id)
    if assignment_dir.exists():
        shutil.rmtree(assignment_dir)


def get_assignment_pdf_path(assignment_id: int) -> Path:
    """Return the stored PDF path for an assignment."""
    return settings.assignments_dir / str(assignment_id) / "original.pdf"