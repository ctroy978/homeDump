"""Generate traceable makeup homework claims with watermarked PDFs and QR codes."""

from __future__ import annotations

import secrets
import sqlite3
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

import qrcode
from PIL import Image, ImageDraw
from pypdf import PdfReader, PdfWriter

from app.config import settings
from app.services.assignments import get_assignment_pdf_path
from app.services.eligibility import check_eligibility
from app.services.student_lookup import LOOKUP_FAILURE_MESSAGE, get_student_by_sis


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


def _watermark_lines(
    student_name: str,
    token: str,
    period: int,
    absence_date: str,
    assignment_title: str,
) -> list[str]:
    return [
        "Makeup Homework",
        student_name,
        f"Code: {token}",
        f"Period {period} · {absence_date}",
        assignment_title,
    ]


def _build_watermark_page(width: float, height: float, lines: list[str]) -> bytes:
    img = Image.new("RGBA", (int(width), int(height)), (255, 255, 255, 0))
    draw = ImageDraw.Draw(img)
    y = height * 0.3
    for line in lines:
        draw.text((width * 0.08, y), line, fill=(90, 90, 90, 150))
        y += 28
    img = img.rotate(45, expand=False)
    buffer = BytesIO()
    img.save(buffer, "PDF", resolution=72.0)
    return buffer.getvalue()


def _build_qr_overlay_page(
    width: float,
    height: float,
    qr_image_path: Path,
    token: str,
) -> bytes:
    """Place a scannable verification QR in the top-right of the first page."""
    qr_size = int(min(width, height) * 0.18)
    margin = int(width * 0.04)

    qr_img = Image.open(qr_image_path).convert("RGBA")
    qr_img = qr_img.resize((qr_size, qr_size), Image.Resampling.LANCZOS)

    overlay = Image.new("RGBA", (int(width), int(height)), (255, 255, 255, 0))
    x = int(width) - qr_size - margin
    y = margin
    overlay.paste(qr_img, (x, y), qr_img)

    draw = ImageDraw.Draw(overlay)
    draw.text((x, y + qr_size + 4), f"Verify {token}", fill=(40, 40, 40, 255))

    buffer = BytesIO()
    overlay.save(buffer, "PDF", resolution=72.0)
    return buffer.getvalue()


def watermark_pdf(
    source: Path,
    destination: Path,
    lines: list[str],
    *,
    qr_image_path: Path | None = None,
    token: str | None = None,
) -> None:
    """Overlay traceability text on every page and a QR code on the first page."""
    if not source.exists():
        raise ClaimError("Original assignment PDF is missing.")
    if qr_image_path is not None and token is None:
        raise ValueError("token is required when qr_image_path is provided.")

    reader = PdfReader(str(source))
    writer = PdfWriter()
    for index, page in enumerate(reader.pages):
        width = float(page.mediabox.width)
        height = float(page.mediabox.height)
        watermark_page = PdfReader(
            BytesIO(_build_watermark_page(width, height, lines))
        ).pages[0]
        page.merge_page(watermark_page)
        if index == 0 and qr_image_path is not None and token is not None:
            qr_page = PdfReader(
                BytesIO(_build_qr_overlay_page(width, height, qr_image_path, token))
            ).pages[0]
            page.merge_page(qr_page)
        writer.add_page(page)

    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("wb") as handle:
        writer.write(handle)


def generate_qr_image(token: str, verify_url: str) -> Path:
    """Create or refresh a QR code PNG that links to the verification page."""
    settings.qrcodes_dir.mkdir(parents=True, exist_ok=True)
    destination = settings.qrcodes_dir / f"{token}.png"
    image = qrcode.make(verify_url)
    image.save(destination)
    return destination


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
    Validate eligibility, issue a unique token, and prepare a watermarked PDF.

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

    eligibility = check_eligibility(conn, name, period, date)
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
    existing = conn.execute(
        """
        SELECT token
        FROM claim_tokens
        WHERE student_id = ? AND assignment_id = ? AND absence_date = ? AND period = ?
        """,
        (student_id, assignment_id, date, period),
    ).fetchone()

    if existing is None:
        token = _generate_token(conn)
        conn.execute(
            """
            INSERT INTO claim_tokens (
                token, student_id, assignment_id, period, absence_date
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (token, student_id, assignment_id, period, date),
        )
        conn.commit()
    else:
        token = str(existing["token"])

    base = public_base_url.rstrip("/")
    verify_url = f"{base}/verify/{token}"
    lines = _watermark_lines(
        name,
        token,
        period,
        date,
        str(assignment["title"]),
    )

    qr_path = generate_qr_image(token, verify_url)
    pdf_destination = claim_pdf_path(token)
    watermark_pdf(
        get_assignment_pdf_path(assignment_id),
        pdf_destination,
        lines,
        qr_image_path=qr_path,
        token=token,
    )

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