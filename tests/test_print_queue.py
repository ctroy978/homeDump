"""Tests for the teacher print queue."""

from __future__ import annotations

import sqlite3
import types
from io import BytesIO
from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter

from app import config
from app.database import init_schema
from app.services.assignments import create_assignment
from app.services.attendance_parser import upsert_student
from app.services.claims import claim_pdf_path, process_claim
from app.services.print_queue import (
    PrintQueueError,
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

    batch_path, filename, count = print_batch_and_clear(conn)
    assert count == 1
    assert filename.startswith("makeup-homework-batch-")
    assert batch_path.exists()
    assert list_print_queue(conn) == []
    assert is_already_printed(conn, token) is True

    reader = PdfReader(str(batch_path))
    assert len(reader.pages) >= 1
    batch_path.unlink()


def test_print_batch_rejects_empty_queue(queue_env: tuple[sqlite3.Connection, object]) -> None:
    conn, _ = queue_env
    with pytest.raises(PrintQueueError, match="empty"):
        print_batch_and_clear(conn)


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