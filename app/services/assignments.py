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
    source: str = "manual"
    github_repo: str | None = None
    github_path: str | None = None

    @property
    def periods_display(self) -> str:
        return format_period_list(self.periods)


def validate_periods(periods: list[int]) -> list[int]:
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
    *,
    source: str = "manual",
    github_repo: str | None = None,
    github_path: str | None = None,
) -> int:
    """
    Insert an assignment row, link it to one or more periods, and store its PDF.

    Returns the new assignment id.
    """
    period_list = validate_periods(periods)

    assigned_date = assigned_date.strip()
    title = title.strip()
    if not title:
        raise ValueError("Title is required.")

    safe_filename = Path(original_filename or "assignment.pdf").name
    if not safe_filename.lower().endswith(".pdf"):
        raise ValueError("Assignment file must be a PDF.")
    if not pdf_bytes:
        raise ValueError("PDF file is empty.")
    if source not in {"manual", "github"}:
        raise ValueError("Invalid assignment source.")
    if source == "github" and (not github_repo or not github_path):
        raise ValueError("GitHub repository and PDF path are required.")

    assignment_dir: Path | None = None
    created_assignment_dir = False
    try:
        cursor = conn.execute(
            """
            INSERT INTO assignments (
                assigned_date, title, description, pdf_filename,
                source, github_repo, github_path
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                assigned_date,
                title,
                description,
                safe_filename,
                source,
                github_repo,
                github_path,
            ),
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
        created_assignment_dir = not assignment_dir.exists()
        assignment_dir.mkdir(parents=True, exist_ok=True)
        pdf_path = assignment_dir / "original.pdf"
        pdf_path.write_bytes(pdf_bytes)

        conn.commit()
        return assignment_id
    except Exception:
        conn.rollback()
        if (
            created_assignment_dir
            and assignment_dir is not None
            and assignment_dir.exists()
        ):
            shutil.rmtree(assignment_dir)
        raise


def find_github_assignment(
    conn: sqlite3.Connection,
    github_repo: str,
    github_path: str,
    assigned_date: str,
) -> int | None:
    row = conn.execute(
        """
        SELECT id FROM assignments
        WHERE source = 'github'
          AND github_repo = ?
          AND github_path = ?
          AND assigned_date = ?
        """,
        (github_repo, github_path, assigned_date),
    ).fetchone()
    return int(row["id"]) if row else None


def create_github_assignment(
    conn: sqlite3.Connection,
    *,
    periods: list[int],
    assigned_date: str,
    title: str,
    github_repo: str,
    github_path: str,
    pdf_filename: str,
    description: str | None = None,
) -> int:
    """
    Insert a GitHub-sourced assignment and period links without committing.

    Does not write original.pdf — the caller writes post-commit.
    """
    period_list = validate_periods(periods)
    title = title.strip()
    if not title:
        raise ValueError("Title is required.")

    cursor = conn.execute(
        """
        INSERT INTO assignments (
            assigned_date, title, description, pdf_filename,
            source, github_repo, github_path
        )
        VALUES (?, ?, ?, ?, 'github', ?, ?)
        """,
        (
            assigned_date,
            title,
            description,
            pdf_filename,
            github_repo,
            github_path,
        ),
    )
    assignment_id = int(cursor.lastrowid)

    conn.executemany(
        """
        INSERT INTO assignment_periods (assignment_id, period)
        VALUES (?, ?)
        """,
        [(assignment_id, period) for period in period_list],
    )
    return assignment_id


def _assignment_periods(
    conn: sqlite3.Connection,
    assignment_id: int,
) -> set[int]:
    rows = conn.execute(
        """
        SELECT period
        FROM assignment_periods
        WHERE assignment_id = ?
        """,
        (assignment_id,),
    ).fetchall()
    return {int(row["period"]) for row in rows}


def add_periods_to_assignment(
    conn: sqlite3.Connection,
    assignment_id: int,
    periods: list[int],
) -> tuple[list[int], list[int]]:
    """Add periods to an assignment without committing. Returns (added, skipped)."""
    period_list = validate_periods(periods)
    existing = _assignment_periods(conn, assignment_id)
    added: list[int] = []
    skipped: list[int] = []

    for period in period_list:
        if period in existing:
            skipped.append(period)
            continue
        conn.execute(
            """
            INSERT INTO assignment_periods (assignment_id, period)
            VALUES (?, ?)
            """,
            (assignment_id, period),
        )
        existing.add(period)
        added.append(period)

    return added, skipped


def write_assignment_pdf(assignment_id: int, pdf_bytes: bytes) -> Path:
    """Write original.pdf after a successful commit."""
    assignment_dir = settings.assignments_dir / str(assignment_id)
    assignment_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = assignment_dir / "original.pdf"
    pdf_path.write_bytes(pdf_bytes)
    return pdf_path


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
            a.source,
            a.github_repo,
            a.github_path,
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
                source=str(row["source"] or "manual"),
                github_repo=row["github_repo"],
                github_path=row["github_path"],
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
    conn.execute(
        "UPDATE distribution_events SET assignment_id = NULL WHERE assignment_id = ?",
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
