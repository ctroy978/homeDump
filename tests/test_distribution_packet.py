"""Tests for GitHub worksheet print packet generation."""

from __future__ import annotations

import types
from io import BytesIO

import fitz
import pytest
from pypdf import PdfReader, PdfWriter
from starlette.requests import Request

from app import config
import app.public_url as public_url
from app.services.distribution_packet import (
    DistributionPacketError,
    build_distribute_url,
    build_print_packet_pdf,
    render_cover_pdf,
)


def _request(host: str = "classroom.test:8000") -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/admin/distribute/prep",
        "headers": [(b"host", host.encode())],
        "scheme": "http",
        "server": ("testserver", 8000),
        "client": ("127.0.0.1", 12345),
    }
    return Request(scope)


def _worksheet_pdf(page_count: int = 1) -> bytes:
    document = fitz.open()
    try:
        for _ in range(page_count):
            document.new_page(width=612, height=792)
        return document.tobytes()
    finally:
        document.close()


def _encrypted_worksheet_pdf() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(612, 792)
    writer.encrypt("secret")
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def test_build_distribute_url_encodes_repo_and_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    test_settings = types.SimpleNamespace(
        public_base_url="http://homework.local:8000"
    )
    monkeypatch.setattr(config, "settings", test_settings)
    monkeypatch.setattr(public_url, "settings", test_settings)

    url = build_distribute_url(
        _request(),
        "scope_tenth",
        "unit2/ch04.pdf",
    )
    assert (
        url
        == "http://homework.local:8000/admin/distribute?repo=scope_tenth&path=unit2%2Fch04.pdf"
    )


def test_render_cover_pdf_includes_title_and_repo_path() -> None:
    cover = render_cover_pdf(
        display_title="Chapter 4 Practice",
        distribute_url="http://homework.local:8000/admin/distribute?repo=scope_tenth&path=unit2%2Fch04.pdf",
        github_repo="scope_tenth",
        github_path="unit2/ch04.pdf",
    )
    document = fitz.open(stream=cover, filetype="pdf")
    try:
        assert document.page_count == 1
        text = document[0].get_text()
        assert "Chapter 4 Practice" in text
        assert "scope_tenth / unit2/ch04.pdf" in text
        assert "Scan this QR" in text
    finally:
        document.close()


def test_build_print_packet_pdf_merges_cover_and_worksheet() -> None:
    packet = build_print_packet_pdf(
        display_title="Chapter 4 Practice",
        distribute_url="http://homework.local:8000/admin/distribute?repo=scope_tenth&path=unit2%2Fch04.pdf",
        github_repo="scope_tenth",
        github_path="unit2/ch04.pdf",
        worksheet_pdf_bytes=_worksheet_pdf(page_count=2),
    )

    reader = PdfReader(BytesIO(packet))
    assert len(reader.pages) == 3


def test_build_print_packet_pdf_rejects_encrypted_worksheet() -> None:
    with pytest.raises(
        DistributionPacketError,
        match="password-protected",
    ):
        build_print_packet_pdf(
            display_title="Protected Worksheet",
            distribute_url="http://homework.local:8000/admin/distribute?repo=scope_tenth&path=unit2%2Fsecret.pdf",
            github_repo="scope_tenth",
            github_path="unit2/secret.pdf",
            worksheet_pdf_bytes=_encrypted_worksheet_pdf(),
        )


def test_build_print_packet_pdf_rejects_empty_worksheet() -> None:
    writer = PdfWriter()
    buffer = BytesIO()
    writer.write(buffer)

    with pytest.raises(DistributionPacketError, match="no pages"):
        build_print_packet_pdf(
            display_title="Empty Worksheet",
            distribute_url="http://homework.local:8000/admin/distribute?repo=scope_tenth&path=empty.pdf",
            github_repo="scope_tenth",
            github_path="empty.pdf",
            worksheet_pdf_bytes=buffer.getvalue(),
        )