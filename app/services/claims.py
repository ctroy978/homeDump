"""Generate traceable makeup homework claims with named PDFs."""

from __future__ import annotations

import secrets
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import fitz

from app.config import settings
from app.services.assignments import get_assignment_pdf_path
from app.services.eligibility import check_eligibility
from app.services.attendance_parser import student_has_class_period
from app.services.student_lookup import LOOKUP_FAILURE_MESSAGE, get_student_by_sis
from app.services.worksheet_name import (
    WorksheetNameError,
    assert_student_name_fields,
    stamp_student_name,
)


class ClaimError(Exception):
    """Raised when a student cannot claim an assignment."""


@dataclass(frozen=True)
class ClaimResult:
    """Successful homework preparation returned to the student UI."""

    token: str
    student_name: str
    assignment_id: int
    assignment_title: str
    period: int
    absence_date: str
    already_queued: bool = False


@dataclass(frozen=True)
class ClaimVerification:
    """Public claim details shown on the verification page."""

    token: str
    student_name: str
    assignment_title: str
    period: int
    absence_date: str
    claimed_at: str


def _generate_token(conn: sqlite3.Connection) -> str:
    for _ in range(10):
        token = secrets.token_hex(4).upper()
        row = conn.execute(
            "SELECT 1 FROM claim_tokens WHERE token = ?",
            (token,),
        ).fetchone()
        if row is None:
            return token
    raise RuntimeError("Failed to generate a unique claim token.")


def _existing_claim_token(
    conn: sqlite3.Connection,
    *,
    student_id: int,
    assignment_id: int,
    period: int,
    absence_date: str,
) -> str | None:
    row = conn.execute(
        """
        SELECT token
        FROM claim_tokens
        WHERE student_id = ? AND assignment_id = ? AND absence_date = ? AND period = ?
        """,
        (student_id, assignment_id, absence_date, period),
    ).fetchone()
    if row is None:
        return None
    return str(row["token"])


def _issue_or_reuse_token(
    conn: sqlite3.Connection,
    *,
    student_id: int,
    assignment_id: int,
    period: int,
    absence_date: str,
) -> str:
    """Insert a claim token, or return the existing one on a double-submit."""
    existing = _existing_claim_token(
        conn,
        student_id=student_id,
        assignment_id=assignment_id,
        period=period,
        absence_date=absence_date,
    )
    if existing is not None:
        return existing

    for _ in range(10):
        token = _generate_token(conn)
        try:
            conn.execute(
                """
                INSERT INTO claim_tokens (
                    token, student_id, assignment_id, period, absence_date
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (token, student_id, assignment_id, period, absence_date),
            )
            conn.commit()
            return token
        except sqlite3.IntegrityError:
            conn.rollback()
            raced = _existing_claim_token(
                conn,
                student_id=student_id,
                assignment_id=assignment_id,
                period=period,
                absence_date=absence_date,
            )
            if raced is not None:
                return raced
    raise RuntimeError("Failed to issue a unique claim token.")


def _assignment_for_period(
    conn: sqlite3.Connection,
    assignment_id: int,
    period: int,
) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT a.id, a.title, a.assigned_date
        FROM assignments a
        JOIN assignment_periods ap ON ap.assignment_id = a.id
        WHERE a.id = ? AND ap.period = ?
        """,
        (assignment_id, period),
    ).fetchone()
    if row is None:
        raise ClaimError("Assignment not found for this period.")
    return row


def log_claim(
    conn: sqlite3.Connection,
    *,
    student_name: str,
    assignment_id: int | None,
    period: int | None,
    absence_date: str | None,
    token: str | None,
    client_ip: str | None,
    user_agent: str | None,
    success: bool,
    message: str,
) -> None:
    conn.execute(
        """
        INSERT INTO claim_logs (
            student_name, assignment_id, period, absence_date, token,
            client_ip, user_agent, success, message
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            student_name,
            assignment_id,
            period,
            absence_date,
            token,
            client_ip,
            user_agent,
            1 if success else 0,
            message,
        ),
    )
    conn.commit()


def prepare_named_claim_pdf(
    source: Path,
    destination: Path,
    student_name: str,
) -> None:
    """Copy the assignment PDF and stamp the student name in the header."""
    if not source.exists():
        raise ClaimError("Original assignment PDF is missing.")

    document = fitz.open(source)
    try:
        if document.page_count == 0:
            raise ClaimError("Original assignment PDF has no pages.")
        try:
            assert_student_name_fields(document)
            for page in document:
                stamp_student_name(page, student_name)
        except WorksheetNameError as exc:
            raise ClaimError(
                "Could not prepare this homework for printing. Ask your teacher."
            ) from exc
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(document.tobytes())
    finally:
        document.close()


def claim_pdf_path(token: str) -> Path:
    return settings.claims_dir / f"{token}.pdf"


def process_claim(
    conn: sqlite3.Connection,
    *,
    sis_number: str,
    assignment_id: int,
    period: int,
    absence_date: str,
    public_base_url: str,
    client_ip: str | None = None,
    user_agent: str | None = None,
) -> ClaimResult:
    """
    Validate eligibility, issue a unique token, and prepare a named PDF.

    Re-requests for the same student/assignment/date return the existing token.
    """
    student = get_student_by_sis(conn, sis_number)
    if student is None:
        log_claim(
            conn,
            student_name="Unknown",
            assignment_id=assignment_id,
            period=period,
            absence_date=absence_date.strip(),
            token=None,
            client_ip=client_ip,
            user_agent=user_agent,
            success=False,
            message="SIS lookup failed during claim.",
        )
        raise ClaimError(LOOKUP_FAILURE_MESSAGE)

    if not student_has_class_period(conn, student.id, period):
        log_claim(
            conn,
            student_name=student.name,
            assignment_id=assignment_id,
            period=period,
            absence_date=absence_date.strip(),
            token=None,
            client_ip=client_ip,
            user_agent=user_agent,
            success=False,
            message="Student is not in this class period.",
        )
        raise ClaimError(LOOKUP_FAILURE_MESSAGE)

    name = student.name
    date = absence_date.strip()
    assignment = _assignment_for_period(conn, assignment_id, period)

    if str(assignment["assigned_date"]) != date:
        log_claim(
            conn,
            student_name=name,
            assignment_id=assignment_id,
            period=period,
            absence_date=date,
            token=None,
            client_ip=client_ip,
            user_agent=user_agent,
            success=False,
            message="Assignment date does not match the selected absence date.",
        )
        raise ClaimError("Assignment date does not match the selected absence date.")

    eligibility = check_eligibility(conn, student.id, period, date)
    if not eligibility.eligible:
        log_claim(
            conn,
            student_name=name,
            assignment_id=assignment_id,
            period=period,
            absence_date=date,
            token=None,
            client_ip=client_ip,
            user_agent=user_agent,
            success=False,
            message=eligibility.reason,
        )
        raise ClaimError(eligibility.reason)

    student_id = student.id
    token = _issue_or_reuse_token(
        conn,
        student_id=student_id,
        assignment_id=assignment_id,
        period=period,
        absence_date=date,
    )

    pdf_destination = claim_pdf_path(token)
    try:
        prepare_named_claim_pdf(
            get_assignment_pdf_path(assignment_id),
            pdf_destination,
            name,
        )
    except ClaimError:
        raise
    except Exception as exc:
        raise ClaimError(
            "Could not prepare this homework for printing. Ask your teacher."
        ) from exc

    log_claim(
        conn,
        student_name=name,
        assignment_id=assignment_id,
        period=period,
        absence_date=date,
        token=token,
        client_ip=client_ip,
        user_agent=user_agent,
        success=True,
        message="Homework prepared for print queue.",
    )

    return ClaimResult(
        token=token,
        student_name=name,
        assignment_id=assignment_id,
        assignment_title=str(assignment["title"]),
        period=period,
        absence_date=date,
    )


def get_claim_by_token(conn: sqlite3.Connection, token: str) -> ClaimVerification | None:
    """Load public verification details for a claim token."""
    row = conn.execute(
        """
        SELECT
            ct.token,
            ct.period,
            ct.absence_date,
            ct.created_at,
            s.name AS student_name,
            a.title AS assignment_title
        FROM claim_tokens ct
        JOIN students s ON s.id = ct.student_id
        JOIN assignments a ON a.id = ct.assignment_id
        WHERE ct.token = ?
        """,
        (token.strip().upper(),),
    ).fetchone()
    if row is None:
        return None

    period_value = row["period"]
    if period_value is None:
        return None

    return ClaimVerification(
        token=str(row["token"]),
        student_name=str(row["student_name"]),
        assignment_title=str(row["assignment_title"]),
        period=int(period_value),
        absence_date=str(row["absence_date"]),
        claimed_at=str(row["created_at"]),
    )