"""Shared pytest fixtures."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.database import init_schema
from app.services.attendance_parser import upsert_student

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_TXT = PROJECT_ROOT / "tests" / "fixtures" / "named_attendance.txt"
FIXTURE_XLSX = PROJECT_ROOT / "tests" / "fixtures" / "named_attendance.xlsx"


@pytest.fixture
def db_conn() -> sqlite3.Connection:
    """In-memory database with representative attendance rows for eligibility tests."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    init_schema(conn)

    student_id = upsert_student(conn, "Test Student A", "10", sis_number="10001")
    records = [
        ("2025-09-02", 3, "Unexcused Absence"),
        ("2025-09-02", 4, "Tardy Unexcused"),
        ("2025-09-29", 0, "Family Emergency"),
        ("2025-10-15", 2, "Sports-Athletics"),
        ("2025-10-20", 1, "Illness"),
        ("2025-10-07", 0, "Field Trip/School A"),
    ]

    for absence_date, period, code in records:
        conn.execute(
            """
            INSERT INTO attendance_records (
                student_id, absence_date, period, absence_code
            ) VALUES (?, ?, ?, ?)
            """,
            (student_id, absence_date, period, code),
        )

    conn.commit()
    yield conn
    conn.close()


@pytest.fixture
def fixture_db_path(tmp_path: Path) -> Path | None:
    """
    Load the anonymized named fixture into a temp database when available.

    Skips integration checks when cleanatt-based fixtures have not been built.
    """
    fixture = FIXTURE_TXT if FIXTURE_TXT.exists() else FIXTURE_XLSX
    if not fixture.exists():
        return None

    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    init_schema(conn)

    from app.services.attendance_parser import ingest_attendance_file

    ingest_attendance_file(conn, fixture, fixture.name)
    conn.close()
    return db_path