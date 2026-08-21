"""Generate teacher print packets: cover sheet with install QR + worksheet PDF."""

from __future__ import annotations

from io import BytesIO
from urllib.parse import urlencode

import fitz
import qrcode
from fastapi import Request
from pypdf import PdfReader, PdfWriter
from pypdf.errors import PdfReadError

from app.public_url import resolve_public_base_url
from app.services.worksheet_name import (
    WorksheetNameError,
    assert_student_name_fields,
    stamp_student_name,
)

COVER_WIDTH = 612.0
COVER_HEIGHT = 792.0


class DistributionPacketError(Exception):
    """Raised when a print packet cannot be generated."""


def build_distribute_url(request: Request, repo: str, path: str) -> str:
    """Build the install QR target URL for a worksheet."""
    base = resolve_public_base_url(request)
    query = urlencode({"repo": repo, "path": path})
    return f"{base}/admin/distribute?{query}"


def _qr_png_bytes(url: str) -> bytes:
    image = qrcode.make(url)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def render_cover_pdf(
    *,
    display_title: str,
    distribute_url: str,
    github_repo: str,
    github_path: str,
) -> bytes:
    """Render sheet 1: title, install QR, and repo/path subtitle."""
    document = fitz.open()
    try:
        page = document.new_page(width=COVER_WIDTH, height=COVER_HEIGHT)
        page.insert_text(
            (72, 100),
            display_title,
            fontsize=26,
            fontname="helv",
        )
        page.insert_text(
            (72, 140),
            f"{github_repo} / {github_path}",
            fontsize=11,
            fontname="helv",
            color=(0.35, 0.35, 0.35),
        )
        page.insert_text(
            (72, 170),
            "Scan this QR on the day you distribute the worksheet.",
            fontsize=12,
            fontname="helv",
        )

        qr_rect = fitz.Rect(400, 90, 540, 230)
        page.insert_image(qr_rect, stream=_qr_png_bytes(distribute_url))
        page.insert_text(
            (400, 245),
            "Install QR",
            fontsize=10,
            fontname="helv",
        )
        return document.tobytes()
    finally:
        document.close()


def _read_worksheet_pages(worksheet_pdf_bytes: bytes) -> PdfReader:
    try:
        reader = PdfReader(BytesIO(worksheet_pdf_bytes))
    except PdfReadError as exc:
        raise DistributionPacketError(
            "The worksheet PDF could not be read."
        ) from exc

    if reader.is_encrypted:
        raise DistributionPacketError(
            "This worksheet PDF is password-protected and cannot be distributed."
        )

    if len(reader.pages) == 0:
        raise DistributionPacketError("The worksheet PDF has no pages.")

    return reader


def build_print_packet_pdf(
    *,
    display_title: str,
    distribute_url: str,
    github_repo: str,
    github_path: str,
    worksheet_pdf_bytes: bytes,
) -> bytes:
    """
    Merge a cover sheet and worksheet into one printable PDF.

    Does not write to the database or filesystem.
    """
    cover_bytes = render_cover_pdf(
        display_title=display_title,
        distribute_url=distribute_url,
        github_repo=github_repo,
        github_path=github_path,
    )
    worksheet_reader = _read_worksheet_pages(worksheet_pdf_bytes)

    writer = PdfWriter()
    cover_reader = PdfReader(BytesIO(cover_bytes))
    for page in cover_reader.pages:
        writer.add_page(page)
    for page in worksheet_reader.pages:
        writer.add_page(page)

    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def build_named_class_packet_pdf(
    *,
    worksheet_pdf_bytes: bytes,
    student_names: list[str],
    display_title: str,
    distribute_url: str,
    github_repo: str,
    github_path: str,
) -> bytes:
    """
    Build a collated classroom PDF: install QR cover, named copies, then one blank.

    The cover is the same sheet as the regular print packet, included once.
    Names are written in the OCR ``Student Name:`` blank on worksheet pages.
    """
    names = [name.strip() for name in student_names if name.strip()]
    if not names:
        raise DistributionPacketError("Select at least one student.")

    _read_worksheet_pages(worksheet_pdf_bytes)
    cover_bytes = render_cover_pdf(
        display_title=display_title,
        distribute_url=distribute_url,
        github_repo=github_repo,
        github_path=github_path,
    )
    source = fitz.open(stream=worksheet_pdf_bytes, filetype="pdf")
    try:
        if source.page_count == 0:
            raise DistributionPacketError("The worksheet PDF has no pages.")
        try:
            assert_student_name_fields(source)
            output = fitz.open()
            cover = fitz.open(stream=cover_bytes, filetype="pdf")
            try:
                output.insert_pdf(cover)
                for name in names:
                    start = output.page_count
                    output.insert_pdf(source)
                    for offset in range(source.page_count):
                        stamp_student_name(output[start + offset], name)
                output.insert_pdf(source)
                return output.tobytes()
            finally:
                cover.close()
                output.close()
        except WorksheetNameError as exc:
            raise DistributionPacketError(str(exc)) from exc
    finally:
        source.close()
