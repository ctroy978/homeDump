"""Teacher print queue for student homework requests."""

from __future__ import annotations

import sqlite3
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from io import BytesIO
from pathlib import Path

import fitz
from pypdf import PdfReader, PdfWriter

from app.services.claims import claim_pdf_path


class PrintQueueError(Exception):
    """Raised when a print queue operation cannot complete."""

    def __init__(self, message: str, *, skipped: list[PrintSkip] | None = None) -> None:
        super().__init__(message)
        self.skipped = list(skipped or [])


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


@dataclass(frozen=True)
class PrintSkip:
    """A queued request that could not be included in the printed batch."""

    student_name: str
    assignment_title: str
    reason: str

    def display(self) -> str:
        return f"{self.student_name} ({self.assignment_title}) — {self.reason}"


@dataclass(frozen=True)
class PrintBatchResult:
    """Outcome of merging the print queue."""

    batch_path: Path
    filename: str
    printed_count: int
    skipped: list[PrintSkip] = field(default_factory=list)


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


def _skipped_cover_pdf(skipped: list[PrintSkip]) -> bytes:
    """Build a first page listing requests that were left in the queue."""
    lines = [
        "Some requests were not included in this batch:",
        "",
    ]
    for item in skipped:
        lines.append(f"- {item.display()}")
    lines.extend(["", "Those requests are still in the print queue."])

    document = fitz.open()
    try:
        page = document.new_page(width=612, height=792)
        page.insert_text(
            (72, 72),
            "\n".join(lines),
            fontsize=12,
            fontname="helv",
        )
        return document.tobytes()
    finally:
        document.close()


def _append_claim_pdf(
    writer: PdfWriter,
    entry: PrintQueueEntry,
) -> PrintSkip | None:
    pdf_path = claim_pdf_path(entry.token)
    if not pdf_path.exists():
        return PrintSkip(
            student_name=entry.student_name,
            assignment_title=entry.assignment_title,
            reason="Missing PDF",
        )
    try:
        reader = PdfReader(str(pdf_path))
        if len(reader.pages) == 0:
            return PrintSkip(
                student_name=entry.student_name,
                assignment_title=entry.assignment_title,
                reason="PDF has no pages",
            )
        for page in reader.pages:
            writer.add_page(page)
    except Exception:  # noqa: BLE001 — isolate one bad file
        return PrintSkip(
            student_name=entry.student_name,
            assignment_title=entry.assignment_title,
            reason="Could not read PDF",
        )
    return None


def build_batch_pdf(
    conn: sqlite3.Connection,
) -> tuple[Path, list[PrintQueueEntry], list[PrintSkip]]:
    """
    Merge readable queued PDFs into one file.

    Unreadable or missing files are skipped so the rest of the class still prints.
    """
    entries = list_print_queue(conn)
    if not entries:
        raise PrintQueueError("The print queue is empty.")

    writer = PdfWriter()
    printed: list[PrintQueueEntry] = []
    skipped: list[PrintSkip] = []

    for entry in entries:
        skip = _append_claim_pdf(writer, entry)
        if skip is None:
            printed.append(entry)
        else:
            skipped.append(skip)

    if not printed:
        raise PrintQueueError(
            "None of the queued homework PDFs could be printed.",
            skipped=skipped,
        )

    if skipped:
        cover = PdfReader(BytesIO(_skipped_cover_pdf(skipped)))
        merged = PdfWriter()
        merged.add_page(cover.pages[0])
        for page in writer.pages:
            merged.add_page(page)
        writer = merged

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
    tmp_path = Path(tmp.name)
    tmp.close()

    with tmp_path.open("wb") as handle:
        writer.write(handle)

    return tmp_path, printed, skipped


def print_batch_and_clear(conn: sqlite3.Connection) -> PrintBatchResult:
    """
    Build a merged PDF for printable queue items and remove only those items.

    Requests whose PDFs are missing or unreadable stay in the queue.
    """
    batch_path, printed, skipped = build_batch_pdf(conn)
    for entry in printed:
        conn.execute(
            """
            UPDATE claim_tokens
            SET printed_at = datetime('now')
            WHERE token = ?
            """,
            (entry.token,),
        )
        conn.execute("DELETE FROM print_queue WHERE token = ?", (entry.token,))
    conn.commit()
    return PrintBatchResult(
        batch_path=batch_path,
        filename=_batch_filename(),
        printed_count=len(printed),
        skipped=skipped,
    )