"""Tests for the eligibility engine."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.database import init_schema
from app.services.eligibility import (
    EligibilityResult,
    check_eligibility,
    is_allowable_code,
    normalize_text,
)


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("Sports-Athletics", True),
        ("Illness", True),
        ("Field Trip/School A", True),
        ("Family Emergency", True),
        ("In-School Absence", True),
        ("Tardy Excused", True),
        ("Nurse\u2019s Office", True),  # curly apostrophe from real exports
        ("Unexcused Absence", False),
        ("Tardy Unexcused", False),
        ("Early Check Out", False),
        ("Out-School Suspensio", False),
    ],
)
def test_is_allowable_code(code: str, expected: bool) -> None:
    assert is_allowable_code(code) is expected


def test_normalize_text_apostrophes() -> None:
    assert normalize_text("Nurse\u2019s Office") == "Nurse's Office"


@pytest.mark.parametrize(
    ("period", "absence_date", "eligible", "code"),
    [
        (0, "2025-09-29", True, "Family Emergency"),
        (2, "2025-10-15", True, "Sports-Athletics"),
        (1, "2025-10-20", True, "Illness"),
        (0, "2025-10-07", True, "Field Trip/School A"),
        (3, "2025-09-02", False, "Unexcused Absence"),
        (4, "2025-09-02", False, "Tardy Unexcused"),
    ],
)
def test_check_eligibility(
    db_conn: sqlite3.Connection,
    period: int,
    absence_date: str,
    eligible: bool,
    code: str,
) -> None:
    result = check_eligibility(db_conn, "Test Student A", period, absence_date)
    assert isinstance(result, EligibilityResult)
    assert result.eligible is eligible
    assert result.absence_code == code


def test_check_eligibility_missing_record(db_conn: sqlite3.Connection) -> None:
    result = check_eligibility(db_conn, "Test Student A", 5, "2025-01-01")
    assert result.eligible is False
    assert result.absence_code is None
    assert "No absence record" in result.reason


def test_check_eligibility_unknown_student(db_conn: sqlite3.Connection) -> None:
    result = check_eligibility(db_conn, "Nobody Here", 1, "2025-10-20")
    assert result.eligible is False


def test_fixture_sparse_period_mapping(fixture_db_path: Path | None) -> None:
    """Integration check: first sample row maps Unexcused Absence to period 3 only."""
    if fixture_db_path is None:
        pytest.skip("Run scripts/build_test_fixture.py to create local fixtures.")

    conn = sqlite3.connect(fixture_db_path)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        """
        SELECT ar.period, ar.absence_code
        FROM attendance_records ar
        JOIN students s ON s.id = ar.student_id
        WHERE s.name = 'Test Student A' AND ar.absence_date = '2025-09-02'
        ORDER BY ar.period
        """
    ).fetchall()
    conn.close()

    assert [(row["period"], row["absence_code"]) for row in rows] == [
        (3, "Unexcused Absence"),
        (4, "Tardy Unexcused"),
    ]

    conn = sqlite3.connect(fixture_db_path)
    conn.row_factory = sqlite3.Row
    result = check_eligibility(conn, "Test Student A", 3, "2025-09-02")
    conn.close()
    assert result.eligible is False
    assert result.absence_code == "Unexcused Absence"