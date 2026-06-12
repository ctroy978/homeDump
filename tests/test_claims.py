"""Tests for the claim flow, watermarking, and verification."""

from __future__ import annotations

import sqlite3
import types
from io import BytesIO
from pathlib import Path

import pytest
from pypdf import PdfWriter

from app import config
from app.database import init_schema
from app.services.assignments import create_assignment
from app.services.attendance_parser import upsert_student
from app.services.claims import (
    ClaimError,
    claim_pdf_path,
    generate_qr_image,
    get_claim_by_token,
    process_claim,
)
from app.services.student_lookup import LOOKUP_FAILURE_MESSAGE


@pytest.fixture
def claim_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[sqlite3.Connection, types.SimpleNamespace]:
    data_dir = tmp_path / "data"
    test_settings = types.SimpleNamespace(
        data_dir=data_dir,
        database_path=data_dir / "app.db",
        assignments_dir=data_dir / "assignments",
        claims_dir=data_dir / "claims",
        qrcodes_dir=data_dir / "qrcodes",
        public_base_url=None,
    )
    for path in (
        data_dir,
        test_settings.assignments_dir,
        test_settings.claims_dir,
        test_settings.qrcodes_dir,
    ):
        path.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(config, "settings", test_settings)
    monkeypatch.setattr("app.services.claims.settings", test_settings)
    monkeypatch.setattr("app.services.assignments.settings", test_settings)

    conn = sqlite3.connect(test_settings.database_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    init_schema(conn)

    student_id = upsert_student(conn, "Test Student A", "10", sis_number="10001")
    conn.execute(
        """
        INSERT INTO attendance_records (
            student_id, absence_date, period, absence_code
        ) VALUES (?, '2025-09-29', 0, 'Family Emergency')
        """,
        (student_id,),
    )
    conn.commit()
    yield conn, test_settings
    conn.close()


def _blank_pdf_bytes() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(200, 200)
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def _seed_assignment(conn: sqlite3.Connection, test_settings: types.SimpleNamespace) -> int:
    return create_assignment(
        conn,
        periods=[0],
        assigned_date="2025-09-29",
        title="Week 1 packet",
        description=None,
        pdf_bytes=_blank_pdf_bytes(),
        original_filename="week1.pdf",
    )


def test_process_claim_issues_token_and_assets(
    claim_env: tuple[sqlite3.Connection, types.SimpleNamespace],
) -> None:
    conn, test_settings = claim_env
    assignment_id = _seed_assignment(conn, test_settings)

    result = process_claim(
        conn,
        sis_number="10001",
        assignment_id=assignment_id,
        period=0,
        absence_date="2025-09-29",
        public_base_url="http://classroom.test:8000",
        client_ip="127.0.0.1",
        user_agent="pytest",
    )

    assert len(result.token) == 8
    pdf_path = claim_pdf_path(result.token)
    assert pdf_path.exists()
    assert pdf_path.stat().st_size > 500
    assert (test_settings.qrcodes_dir / f"{result.token}.png").exists()

    row = conn.execute(
        "SELECT COUNT(*) FROM claim_logs WHERE success = 1 AND token = ?",
        (result.token,),
    ).fetchone()
    assert row[0] == 1


def test_process_claim_is_idempotent(
    claim_env: tuple[sqlite3.Connection, types.SimpleNamespace],
) -> None:
    conn, test_settings = claim_env
    assignment_id = _seed_assignment(conn, test_settings)

    first = process_claim(
        conn,
        sis_number="10001",
        assignment_id=assignment_id,
        period=0,
        absence_date="2025-09-29",
        public_base_url="http://classroom.test:8000",
    )
    second = process_claim(
        conn,
        sis_number="10001",
        assignment_id=assignment_id,
        period=0,
        absence_date="2025-09-29",
        public_base_url="http://classroom.test:8000",
    )

    assert first.token == second.token
    token_count = conn.execute("SELECT COUNT(*) FROM claim_tokens").fetchone()[0]
    assert token_count == 1


def test_process_claim_rejects_ineligible_student(
    claim_env: tuple[sqlite3.Connection, types.SimpleNamespace],
) -> None:
    conn, test_settings = claim_env
    assignment_id = create_assignment(
        conn,
        periods=[3],
        assigned_date="2025-09-02",
        title="Quiz",
        description=None,
        pdf_bytes=_blank_pdf_bytes(),
        original_filename="quiz.pdf",
    )

    with pytest.raises(ClaimError):
        process_claim(
            conn,
            sis_number="10001",
            assignment_id=assignment_id,
            period=3,
            absence_date="2025-09-02",
            public_base_url="http://classroom.test:8000",
        )


def test_generate_qr_image_overwrites_existing_url(
    claim_env: tuple[sqlite3.Connection, types.SimpleNamespace],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, test_settings = claim_env
    calls: list[str] = []

    def _record_make(url: str):
        calls.append(url)
        from PIL import Image

        image = Image.new("RGB", (10, 10), color="white")
        return image

    monkeypatch.setattr("app.services.claims.qrcode.make", _record_make)

    token = "ABCD1234"
    generate_qr_image(token, "http://old-host:8000/verify/ABCD1234")
    generate_qr_image(token, "http://new-host:8000/verify/ABCD1234")

    assert calls == [
        "http://old-host:8000/verify/ABCD1234",
        "http://new-host:8000/verify/ABCD1234",
    ]
    assert (test_settings.qrcodes_dir / f"{token}.png").exists()


def test_process_claim_refreshes_qr_when_public_url_changes(
    claim_env: tuple[sqlite3.Connection, types.SimpleNamespace],
) -> None:
    conn, test_settings = claim_env
    assignment_id = _seed_assignment(conn, test_settings)

    first = process_claim(
        conn,
        sis_number="10001",
        assignment_id=assignment_id,
        period=0,
        absence_date="2025-09-29",
        public_base_url="http://old-host:8000",
    )
    second = process_claim(
        conn,
        sis_number="10001",
        assignment_id=assignment_id,
        period=0,
        absence_date="2025-09-29",
        public_base_url="http://new-host:8000",
    )

    assert first.token == second.token
    assert first.qr_path != second.qr_path
    assert "v=" in second.qr_path


def test_process_claim_rejects_unknown_sis(
    claim_env: tuple[sqlite3.Connection, types.SimpleNamespace],
) -> None:
    conn, test_settings = claim_env
    assignment_id = _seed_assignment(conn, test_settings)

    with pytest.raises(ClaimError, match=LOOKUP_FAILURE_MESSAGE):
        process_claim(
            conn,
            sis_number="999999",
            assignment_id=assignment_id,
            period=0,
            absence_date="2025-09-29",
            public_base_url="http://classroom.test:8000",
        )


def test_get_claim_by_token(
    claim_env: tuple[sqlite3.Connection, types.SimpleNamespace],
) -> None:
    conn, test_settings = claim_env
    assignment_id = _seed_assignment(conn, test_settings)

    result = process_claim(
        conn,
        sis_number="10001",
        assignment_id=assignment_id,
        period=0,
        absence_date="2025-09-29",
        public_base_url="http://classroom.test:8000",
    )

    verification = get_claim_by_token(conn, result.token)
    assert verification is not None
    assert verification.student_name == "Test Student A"
    assert verification.assignment_title == "Week 1 packet"
    assert verification.period == 0