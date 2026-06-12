"""Teacher print queue for student homework requests."""

from __future__ import annotations

import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pypdf import PdfReader, PdfWriter

from app.services.claims import claim_pdf_path


class PrintQueueError(Exception):
    """Raised when a print queue operation cannot complete."""


@dataclass(frozen=True)
class PrintQueueEntry:
    """One homework request waiting for teacher printing."""

    id: int
    token: str
    student_name: str
    assignment_id: int
    assignment_title: str
    period: int
    absence_date: str
    queued_at: str


def is_already_printed(conn: sqlite3.Connection, token: str) -> bool:
    """Return whether this homework was included in a completed print batch."""
    row = conn.execute(
        "SELECT printed_at FROM claim_tokens WHERE token = ?",
        (token.strip().upper(),),
    ).fetchone()
    return row is not None and row["printed_at"] is not None


def enqueue_token(conn: sqlite3.Connection, token: str) -> bool:
    """
    Add a prepared claim to the print queue.

    Returns True when newly queued, False when the token was already waiting.
    """
    normalized = token.strip().upper()
    existing = conn.execute(
        "SELECT 1 FROM print_queue WHERE token = ?",
        (normalized,),
    ).fetchone()
    if existing is not None:
        return False

    conn.execute(
        "INSERT INTO print_queue (token) VALUES (?)",
        (normalized,),
    )
    conn.commit()
    return True


def list_print_queue(conn: sqlite3.Connection) -> list[PrintQueueEntry]:
    """Return queued homework oldest-first."""
    rows = conn.execute(
        """
        SELECT
            pq.id,
            pq.token,
            pq.queued_at,
            s.name AS student_name,
            ct.assignment_id,
            a.title AS assignment_title,
            ct.period,
            ct.absence_date
        FROM print_queue pq
        JOIN claim_tokens ct ON ct.token = pq.token
        JOIN students s ON s.id = ct.student_id
        JOIN assignments a ON a.id = ct.assignment_id
        ORDER BY pq.queued_at ASC, pq.id ASC
        """
    ).fetchall()

    return [
        PrintQueueEntry(
            id=int(row["id"]),
            token=str(row["token"]),
            student_name=str(row["student_name"]),
            assignment_id=int(row["assignment_id"]),
            assignment_title=str(row["assignment_title"]),
            period=int(row["period"]),
            absence_date=str(row["absence_date"]),
            queued_at=str(row["queued_at"]),
        )
        for row in rows
    ]


def remove_queue_item(conn: sqlite3.Connection, item_id: int) -> bool:
    """Remove one queue entry. Returns True when a row was deleted."""
    cursor = conn.execute("DELETE FROM print_queue WHERE id = ?", (item_id,))
    conn.commit()
    return cursor.rowcount > 0


def clear_print_queue(conn: sqlite3.Connection) -> int:
    """Remove every item from the queue without printing."""
    cursor = conn.execute("DELETE FROM print_queue")
    conn.commit()
    return cursor.rowcount


def _batch_filename() -> str:
    return f"makeup-homework-batch-{datetime.now().strftime('%Y%m%d-%H%M%S')}.pdf"


def build_batch_pdf(conn: sqlite3.Connection) -> tuple[Path, int]:
    """
    Merge all queued watermarked PDFs into one file for printing.

    Returns the temp file path and number of documents merged.
    """
    entries = list_print_queue(conn)
    if not entries:
        raise PrintQueueError("The print queue is empty.")

    writer = PdfWriter()
    merged_count = 0

    for entry in entries:
        pdf_path = claim_pdf_path(entry.token)
        if not pdf_path.exists():
            raise PrintQueueError(
                f"Missing PDF for {entry.student_name} ({entry.assignment_title})."
            )
        reader = PdfReader(str(pdf_path))
        for page in reader.pages:
            writer.add_page(page)
        merged_count += 1

    if merged_count == 0:
        raise PrintQueueError("The print queue is empty.")

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
    tmp_path = Path(tmp.name)
    tmp.close()

    with tmp_path.open("wb") as handle:
        writer.write(handle)

    return tmp_path, merged_count


def print_batch_and_clear(conn: sqlite3.Connection) -> tuple[Path, str, int]:
    """
    Build a merged PDF for the current queue and then empty the queue.

    Returns temp file path, download filename, and merged document count.
    """
    entries = list_print_queue(conn)
    batch_path, merged_count = build_batch_pdf(conn)
    for entry in entries:
        conn.execute(
            """
            UPDATE claim_tokens
            SET printed_at = datetime('now')
            WHERE token = ?
            """,
            (entry.token,),
        )
    clear_print_queue(conn)
    conn.commit()
    return batch_path, _batch_filename(), merged_count