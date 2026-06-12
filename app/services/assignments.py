"""Assignment storage helpers."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from app.config import settings


def create_assignment(
    conn: sqlite3.Connection,
    period: int,
    assigned_date: str,
    title: str,
    description: str | None,
    pdf_bytes: bytes,
    original_filename: str,
) -> int:
    """
    Insert an assignment row and store its PDF under data/assignments/{id}/.

    Returns the new assignment id.
    """
    if not (0 <= period <= 7):
        raise ValueError("Period must be between 0 and 7.")

    assigned_date = assigned_date.strip()
    title = title.strip()
    if not title:
        raise ValueError("Title is required.")

    safe_filename = Path(original_filename or "assignment.pdf").name
    if not safe_filename.lower().endswith(".pdf"):
        raise ValueError("Assignment file must be a PDF.")

    cursor = conn.execute(
        """
        INSERT INTO assignments (period, assigned_date, title, description, pdf_filename)
        VALUES (?, ?, ?, ?, ?)
        """,
        (period, assigned_date, title, description, safe_filename),
    )
    assignment_id = int(cursor.lastrowid)

    assignment_dir = settings.assignments_dir / str(assignment_id)
    assignment_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = assignment_dir / "original.pdf"
    pdf_path.write_bytes(pdf_bytes)

    conn.commit()
    return assignment_id


def get_assignment_pdf_path(assignment_id: int) -> Path:
    """Return the stored PDF path for an assignment."""
    return settings.assignments_dir / str(assignment_id) / "original.pdf"