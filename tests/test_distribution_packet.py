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
    DUPLEX_SPACER_NOTE,
    DistributionPacketError,
    build_distribute_url,
    build_named_class_packet_pdf,
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
    assert len(reader.pages) == 4
    cover = reader.pages[0].extract_text() or ""
    spacer = reader.pages[1].extract_text() or ""
    assert "Chapter 4 Practice" in cover
    assert "Install QR" not in spacer
    assert DUPLEX_SPACER_NOTE in spacer


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


def _grader_header_pdf(page_count: int = 2) -> bytes:
    document = fitz.open()
    try:
        for index in range(page_count):
            page = document.new_page(width=612, height=792)
            page.insert_text((43.2, 68.5), "Student Name:", fontsize=10, fontname="helv")
            page.insert_text((43.2, 83.3), "Student ID:", fontsize=10, fontname="helv")
            page.insert_text(
                (43.2, 98.0),
                f"Assignment ID: TEST  Page {index + 1}",
                fontsize=10,
                fontname="helv",
            )
            page.insert_text(
                (72, 200),
                f"Question body {index + 1}",
                fontsize=12,
                fontname="helv",
            )
        return document.tobytes()
    finally:
        document.close()


COVER_KWARGS = {
    "display_title": "Chapter 4 Practice",
    "distribute_url": (
        "http://homework.local:8000/admin/distribute?"
        "repo=scope_tenth&path=unit2%2Fch04.pdf"
    ),
    "github_repo": "scope_tenth",
    "github_path": "unit2/ch04.pdf",
}


def test_named_class_packet_collates_names_and_blank_extra() -> None:
    packet = build_named_class_packet_pdf(
        worksheet_pdf_bytes=_grader_header_pdf(page_count=2),
        student_names=["Able, Pat", "Zebra, Ann"],
        **COVER_KWARGS,
    )
    document = fitz.open(stream=packet, filetype="pdf")
    try:
        # Cover + spacer, two even-length named copies, unlabeled extra.
        assert document.page_count == 8
        cover = document[0].get_text()
        assert "Chapter 4 Practice" in cover
        assert "Install QR" in cover
        assert "Able, Pat" not in cover
        assert "Student Name:" not in cover
        assert DUPLEX_SPACER_NOTE in document[1].get_text()
        for index in (2, 3):
            text = document[index].get_text()
            assert "Able, Pat" in text
            assert "Zebra, Ann" not in text
            assert "Student Name:" in text
        for index in (4, 5):
            text = document[index].get_text()
            assert "Zebra, Ann" in text
            assert "Able, Pat" not in text
        for index in (6, 7):
            text = document[index].get_text()
            assert "Able, Pat" not in text
            assert "Zebra, Ann" not in text
            assert "Student Name:" in text
            assert "Question body" in text
    finally:
        document.close()


def test_named_class_packet_writes_name_in_header_slot() -> None:
    packet = build_named_class_packet_pdf(
        worksheet_pdf_bytes=_grader_header_pdf(page_count=1),
        student_names=["Able, Pat"],
        **COVER_KWARGS,
    )
    document = fitz.open(stream=packet, filetype="pdf")
    try:
        page = document[2]
        label = page.search_for("Student Name:")[0]
        name_hits = page.search_for("Able, Pat")
        assert name_hits
        name = name_hits[0]
        assert name.x0 >= label.x1
        assert name.x1 <= label.x1 + 4 + (1.85 * 72) + 8
        assert abs(name.y0 - label.y0) < 8
        id_label = page.search_for("Student ID:")[0]
        assert name.y1 <= id_label.y0 + 2
    finally:
        document.close()


def test_named_class_packet_rejects_missing_name_field() -> None:
    with pytest.raises(DistributionPacketError, match="Student Name"):
        build_named_class_packet_pdf(
            worksheet_pdf_bytes=_worksheet_pdf(page_count=1),
            student_names=["Able, Pat"],
            **COVER_KWARGS,
        )


def test_named_class_packet_rejects_empty_roster() -> None:
    with pytest.raises(DistributionPacketError, match="at least one student"):
        build_named_class_packet_pdf(
            worksheet_pdf_bytes=_grader_header_pdf(),
            student_names=["  "],
            **COVER_KWARGS,
        )


def test_named_class_packet_rejects_encrypted_worksheet() -> None:
    with pytest.raises(DistributionPacketError, match="password-protected"):
        build_named_class_packet_pdf(
            worksheet_pdf_bytes=_encrypted_worksheet_pdf(),
            student_names=["Able, Pat"],
            **COVER_KWARGS,
        )


def _page_names(text: str) -> set[str]:
    found = set()
    for name in ("Able, Pat", "Zebra, Ann"):
        if name in text:
            found.add(name)
    return found


def test_named_class_packet_starts_each_copy_on_a_new_duplex_sheet() -> None:
    packet = build_named_class_packet_pdf(
        worksheet_pdf_bytes=_grader_header_pdf(page_count=1),
        student_names=["Able, Pat", "Zebra, Ann"],
        **COVER_KWARGS,
    )
    document = fitz.open(stream=packet, filetype="pdf")
    try:
        # Cover/spacer, Able/spacer, Zebra/spacer, extra/spacer.
        assert document.page_count == 8
        assert "Install QR" in document[0].get_text()
        assert DUPLEX_SPACER_NOTE in document[1].get_text()
        assert "Able, Pat" in document[2].get_text()
        assert DUPLEX_SPACER_NOTE in document[3].get_text()
        assert "Zebra, Ann" in document[4].get_text()
        assert DUPLEX_SPACER_NOTE in document[5].get_text()
        extra = document[6].get_text()
        assert "Student Name:" in extra
        assert "Able, Pat" not in extra
        assert "Zebra, Ann" not in extra
        assert DUPLEX_SPACER_NOTE in document[7].get_text()

        for front in range(0, document.page_count, 2):
            back = front + 1
            names = _page_names(document[front].get_text()) | _page_names(
                document[back].get_text()
            )
            assert len(names) <= 1
            if front == 0:
                assert not names
    finally:
        document.close()


def test_named_class_packet_pads_odd_length_worksheets_between_students() -> None:
    packet = build_named_class_packet_pdf(
        worksheet_pdf_bytes=_grader_header_pdf(page_count=3),
        student_names=["Able, Pat", "Zebra, Ann"],
        **COVER_KWARGS,
    )
    document = fitz.open(stream=packet, filetype="pdf")
    try:
        # Cover/spacer + (3 pages + spacer) * 3 copies.
        assert document.page_count == 14
        able_pages = [index for index in range(document.page_count) if "Able, Pat" in document[index].get_text()]
        zebra_pages = [
            index
            for index in range(document.page_count)
            if "Zebra, Ann" in document[index].get_text()
        ]
        assert able_pages == [2, 3, 4]
        assert zebra_pages == [6, 7, 8]
        assert DUPLEX_SPACER_NOTE in document[5].get_text()
        assert able_pages[0] % 2 == 0
        assert zebra_pages[0] % 2 == 0
    finally:
        document.close()


def test_print_packet_keeps_worksheet_off_the_cover_back() -> None:
    packet = build_print_packet_pdf(
        display_title="Chapter 4 Practice",
        distribute_url="http://homework.local:8000/admin/distribute?repo=scope_tenth&path=unit2%2Fch04.pdf",
        github_repo="scope_tenth",
        github_path="unit2/ch04.pdf",
        worksheet_pdf_bytes=_worksheet_pdf(page_count=1),
    )
    document = fitz.open(stream=packet, filetype="pdf")
    try:
        assert document.page_count == 4
        assert "Install QR" in document[0].get_text()
        assert DUPLEX_SPACER_NOTE in document[1].get_text()
        assert DUPLEX_SPACER_NOTE in document[3].get_text()
    finally:
        document.close()