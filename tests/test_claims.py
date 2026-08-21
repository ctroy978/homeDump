"""Tests for the claim flow, named PDFs, and verification."""

from __future__ import annotations

import sqlite3
import types
from io import BytesIO
from pathlib import Path

import fitz
import pytest
from pypdf import PdfWriter

from app import config
from app.database import init_schema
from app.services.assignments import create_assignment
from app.services.attendance_parser import upsert_student
from app.services.claims import (
    ClaimError,
    claim_pdf_path,
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
        public_base_url=None,
    )
    for path in (
        data_dir,
        test_settings.assignments_dir,
        test_settings.claims_dir,
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
    conn.execute(
        """
        INSERT INTO student_class_periods (student_id, period, active)
        VALUES (?, 0, 1)
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


def _grader_header_pdf(page_count: int = 1) -> bytes:
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


def _seed_assignment(
    conn: sqlite3.Connection,
    test_settings: types.SimpleNamespace,
    *,
    pdf_bytes: bytes | None = None,
) -> int:
    return create_assignment(
        conn,
        periods=[0],
        assigned_date="2025-09-29",
        title="Week 1 packet",
        description=None,
        pdf_bytes=pdf_bytes if pdf_bytes is not None else _grader_header_pdf(),
        original_filename="week1.pdf",
    )


def test_claim_pdf_stamps_name_in_header_and_has_no_watermark(
    claim_env: tuple[sqlite3.Connection, types.SimpleNamespace],
) -> None:
    conn, test_settings = claim_env
    assignment_id = _seed_assignment(
        conn, test_settings, pdf_bytes=_grader_header_pdf(page_count=2)
    )

    result = process_claim(
        conn,
        sis_number="10001",
        assignment_id=assignment_id,
        period=0,
        absence_date="2025-09-29",
        public_base_url="http://classroom.test:8000",
    )

    document = fitz.open(claim_pdf_path(result.token))
    try:
        assert document.page_count == 2
        for page in document:
            text = page.get_text()
            assert "Test Student A" in text
            assert "Student Name:" in text
            assert "Makeup Homework" not in text
            assert "Code:" not in text
            label = page.search_for("Student Name:")[0]
            name_hits = page.search_for("Test Student A")
            assert name_hits
            name = name_hits[0]
            assert name.x0 >= label.x1
            assert name.x1 <= label.x1 + 4 + (1.85 * 72) + 8
            assert abs(name.y0 - label.y0) < 8
            id_label = page.search_for("Student ID:")[0]
            assert name.y1 <= id_label.y0 + 2
    finally:
        document.close()


def test_process_claim_rejects_pdf_without_name_field(
    claim_env: tuple[sqlite3.Connection, types.SimpleNamespace],
) -> None:
    conn, test_settings = claim_env
    assignment_id = _seed_assignment(
        conn, test_settings, pdf_bytes=_blank_pdf_bytes()
    )

    with pytest.raises(ClaimError, match="Could not prepare this homework"):
        process_claim(
            conn,
            sis_number="10001",
            assignment_id=assignment_id,
            period=0,
            absence_date="2025-09-29",
            public_base_url="http://classroom.test:8000",
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
    assert not (test_settings.data_dir / "qrcodes" / f"{result.token}.png").exists()

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


def test_duplicate_claim_identity_is_rejected_by_schema(
    claim_env: tuple[sqlite3.Connection, types.SimpleNamespace],
) -> None:
    conn, test_settings = claim_env
    assignment_id = _seed_assignment(conn, test_settings)
    process_claim(
        conn,
        sis_number="10001",
        assignment_id=assignment_id,
        period=0,
        absence_date="2025-09-29",
        public_base_url="http://classroom.test:8000",
    )
    student_id = conn.execute(
        "SELECT id FROM students WHERE sis_number = '10001'"
    ).fetchone()["id"]
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO claim_tokens (
                token, student_id, assignment_id, period, absence_date
            ) VALUES ('DEADBEEF', ?, ?, 0, '2025-09-29')
            """,
            (student_id, assignment_id),
        )


def test_issue_or_reuse_token_returns_existing_after_integrity_error(
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
    from app.services.claims import _issue_or_reuse_token

    student_id = int(
        conn.execute("SELECT id FROM students WHERE sis_number = '10001'").fetchone()[
            "id"
        ]
    )
    reused = _issue_or_reuse_token(
        conn,
        student_id=student_id,
        assignment_id=assignment_id,
        period=0,
        absence_date="2025-09-29",
    )
    assert reused == first.token
    assert conn.execute("SELECT COUNT(*) FROM claim_tokens").fetchone()[0] == 1


def test_init_schema_dedupes_duplicate_claim_tokens(tmp_path: Path) -> None:
    db_path = tmp_path / "dupes.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(
        """
        CREATE TABLE students (
            id INTEGER PRIMARY KEY,
            sis_number TEXT,
            name TEXT NOT NULL
        );
        CREATE TABLE assignments (
            id INTEGER PRIMARY KEY,
            assigned_date TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT,
            pdf_filename TEXT NOT NULL
        );
        CREATE TABLE claim_tokens (
            id INTEGER PRIMARY KEY,
            token TEXT NOT NULL UNIQUE,
            student_id INTEGER NOT NULL,
            assignment_id INTEGER NOT NULL,
            period INTEGER,
            absence_date TEXT NOT NULL
        );
        CREATE TABLE print_queue (
            id INTEGER PRIMARY KEY,
            token TEXT NOT NULL UNIQUE,
            queued_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        """
    )
    conn.execute("INSERT INTO students (id, sis_number, name) VALUES (1, '1', 'A')")
    conn.execute(
        """
        INSERT INTO assignments (id, assigned_date, title, pdf_filename)
        VALUES (1, '2025-09-29', 'W', 'w.pdf')
        """
    )
    conn.execute(
        """
        INSERT INTO claim_tokens (token, student_id, assignment_id, period, absence_date)
        VALUES ('AAAA1111', 1, 1, 0, '2025-09-29')
        """
    )
    conn.execute(
        """
        INSERT INTO claim_tokens (token, student_id, assignment_id, period, absence_date)
        VALUES ('BBBB2222', 1, 1, 0, '2025-09-29')
        """
    )
    conn.execute("INSERT INTO print_queue (token) VALUES ('BBBB2222')")
    conn.commit()
    conn.close()

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    init_schema(conn)
    tokens = [
        row["token"]
        for row in conn.execute("SELECT token FROM claim_tokens ORDER BY id")
    ]
    assert tokens == ["AAAA1111"]
    assert conn.execute("SELECT COUNT(*) FROM print_queue").fetchone()[0] == 0
    conn.close()


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


def test_process_claim_does_not_generate_qr_assets(
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
    assert not (test_settings.data_dir / "qrcodes" / f"{first.token}.png").exists()


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


def test_process_claim_wraps_corrupt_pdf_as_claim_error(
    claim_env: tuple[sqlite3.Connection, types.SimpleNamespace],
) -> None:
    conn, test_settings = claim_env
    assignment_id = _seed_assignment(conn, test_settings)
    pdf_path = test_settings.assignments_dir / str(assignment_id) / "original.pdf"
    pdf_path.write_bytes(b"not a pdf")

    with pytest.raises(ClaimError, match="Could not prepare this homework"):
        process_claim(
            conn,
            sis_number="10001",
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