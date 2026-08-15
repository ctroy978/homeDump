"""Tests for the teacher print queue."""

from __future__ import annotations

import sqlite3
import types
from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader, PdfWriter

from app import config
from app.database import init_schema
from app.dependencies import ADMIN_COOKIE_NAME, _expected_admin_token
from app.routers import admin as admin_router
from app.services.assignments import create_assignment
from app.services.attendance_parser import upsert_student
from app.services.claims import claim_pdf_path, process_claim
from app.services.print_queue import (
    PrintQueueError,
    PrintSkip,
    clear_print_queue,
    enqueue_token,
    is_already_printed,
    list_print_queue,
    print_batch_and_clear,
    remove_queue_item,
)


@pytest.fixture
def queue_env(
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
    conn.commit()
    yield conn, test_settings
    conn.close()


def _blank_pdf_bytes() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(200, 200)
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def _prepare_claim(conn: sqlite3.Connection) -> str:
    assignment_id = create_assignment(
        conn,
        periods=[0],
        assigned_date="2025-09-29",
        title="Week 1 packet",
        description=None,
        pdf_bytes=_blank_pdf_bytes(),
        original_filename="week1.pdf",
    )
    result = process_claim(
        conn,
        sis_number="10001",
        assignment_id=assignment_id,
        period=0,
        absence_date="2025-09-29",
        public_base_url="http://classroom.test:8000",
    )
    return result.token


def test_enqueue_and_list_print_queue(queue_env: tuple[sqlite3.Connection, object]) -> None:
    conn, _ = queue_env
    token = _prepare_claim(conn)

    assert enqueue_token(conn, token) is True
    assert enqueue_token(conn, token) is False

    queue = list_print_queue(conn)
    assert len(queue) == 1
    assert queue[0].token == token
    assert queue[0].student_name == "Test Student A"
    assert queue[0].assignment_title == "Week 1 packet"


def test_remove_and_clear_queue(queue_env: tuple[sqlite3.Connection, object]) -> None:
    conn, _ = queue_env
    token = _prepare_claim(conn)
    enqueue_token(conn, token)
    queue = list_print_queue(conn)

    assert remove_queue_item(conn, queue[0].id) is True
    assert list_print_queue(conn) == []

    enqueue_token(conn, token)
    assert clear_print_queue(conn) == 1
    assert list_print_queue(conn) == []


def test_print_batch_marks_printed_and_clears_queue(
    queue_env: tuple[sqlite3.Connection, object],
) -> None:
    conn, _ = queue_env
    token = _prepare_claim(conn)
    enqueue_token(conn, token)

    result = print_batch_and_clear(conn)
    assert result.printed_count == 1
    assert result.skipped == []
    assert result.filename.startswith("makeup-homework-batch-")
    assert result.batch_path.exists()
    assert list_print_queue(conn) == []
    assert is_already_printed(conn, token) is True

    reader = PdfReader(str(result.batch_path))
    assert len(reader.pages) >= 1
    result.batch_path.unlink()


def test_print_batch_rejects_empty_queue(queue_env: tuple[sqlite3.Connection, object]) -> None:
    conn, _ = queue_env
    with pytest.raises(PrintQueueError, match="empty"):
        print_batch_and_clear(conn)


def test_print_batch_skips_missing_pdf_and_prints_the_rest(
    queue_env: tuple[sqlite3.Connection, types.SimpleNamespace],
) -> None:
    conn, _ = queue_env
    good_token = _prepare_claim(conn)
    enqueue_token(conn, good_token)

    other_id = upsert_student(conn, "Test Student B", "10", sis_number="10002")
    conn.execute(
        """
        INSERT INTO attendance_records (
            student_id, absence_date, period, absence_code
        ) VALUES (?, '2025-09-29', 0, 'Family Emergency')
        """,
        (other_id,),
    )
    conn.commit()

    assignment_id = conn.execute("SELECT id FROM assignments").fetchone()["id"]
    bad_result = process_claim(
        conn,
        sis_number="10002",
        assignment_id=int(assignment_id),
        period=0,
        absence_date="2025-09-29",
        public_base_url="http://classroom.test:8000",
    )
    enqueue_token(conn, bad_result.token)
    claim_pdf_path(bad_result.token).unlink()

    result = print_batch_and_clear(conn)
    assert result.printed_count == 1
    assert len(result.skipped) == 1
    assert result.skipped[0].student_name == "Test Student B"
    assert "Missing" in result.skipped[0].reason

    remaining = list_print_queue(conn)
    assert len(remaining) == 1
    assert remaining[0].token == bad_result.token
    assert is_already_printed(conn, good_token) is True
    assert is_already_printed(conn, bad_result.token) is False

    reader = PdfReader(str(result.batch_path))
    cover_text = reader.pages[0].extract_text() or ""
    assert "Test Student B" in cover_text
    assert "not included" in cover_text.lower()
    result.batch_path.unlink()


def test_print_batch_all_unreadable_keeps_queue(
    queue_env: tuple[sqlite3.Connection, types.SimpleNamespace],
) -> None:
    conn, _ = queue_env
    token = _prepare_claim(conn)
    enqueue_token(conn, token)
    claim_pdf_path(token).write_bytes(b"this is not a pdf")

    with pytest.raises(PrintQueueError, match="None of the queued") as exc_info:
        print_batch_and_clear(conn)

    assert exc_info.value.skipped
    assert exc_info.value.skipped[0].student_name == "Test Student A"
    assert list_print_queue(conn)[0].token == token
    assert is_already_printed(conn, token) is False


def test_re_enqueue_after_admin_removes_without_printing(
    queue_env: tuple[sqlite3.Connection, object],
) -> None:
    conn, _ = queue_env
    token = _prepare_claim(conn)
    enqueue_token(conn, token)
    item_id = list_print_queue(conn)[0].id
    remove_queue_item(conn, item_id)

    assert is_already_printed(conn, token) is False
    assert enqueue_token(conn, token) is True


def test_print_route_empty_queue_is_not_confused_with_skipped(
    client: TestClient,
) -> None:
    client.cookies.set(ADMIN_COOKIE_NAME, _expected_admin_token())
    response = client.post("/admin/print-queue/print", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/print-queue?error=empty"


def test_print_route_all_unreadable_uses_skipped_error(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client.cookies.set(ADMIN_COOKIE_NAME, _expected_admin_token())

    def fail_all(_db):
        raise PrintQueueError(
            "None of the queued homework PDFs could be printed.",
            skipped=[
                PrintSkip(
                    student_name="Test Student A",
                    assignment_title="Week 1",
                    reason="Missing PDF",
                )
            ],
        )

    monkeypatch.setattr(admin_router, "print_batch_and_clear", fail_all)
    response = client.post("/admin/print-queue/print", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/print-queue?error=skipped"

    page = client.get("/admin/print-queue?error=skipped")
    assert page.status_code == 200
    assert "None of the queued homework PDFs could be printed" in page.text