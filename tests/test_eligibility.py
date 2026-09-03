"""Tests for the eligibility engine."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.services.eligibility import (
    EligibilityResult,
    check_eligibility,
    normalize_absence_code,
    normalize_text,
    unique_absence_codes,
)


@pytest.mark.parametrize(
    "code",
    [
        "Sports-Athletics",
        "Illness",
        "Field Trip/School A",
        "Family Emergency",
        "In-School Absence",
        "Tardy Excused",
        "Nurse\u2019s Office",  # curly apostrophe from real exports
        "illness",
        "EXCUSED ABSENCE",
        "Excused  Absence",
        "Unexcused Absence",
        "Tardy Unexcused",
        "Early Check Out",
        "Out-School Suspensio",
    ],
)
def test_any_recorded_absence_code_is_eligible(
    db_conn: sqlite3.Connection, code: str
) -> None:
    student_id = _test_student_id(db_conn)
    db_conn.execute(
        """
        INSERT INTO attendance_records (
            student_id, absence_date, period, absence_code
        ) VALUES (?, '2026-01-15', 6, ?)
        """,
        (student_id, code),
    )
    db_conn.commit()
    result = check_eligibility(db_conn, student_id, 6, "2026-01-15")
    assert result.eligible is True
    assert result.absence_code == code


def test_normalize_text_apostrophes() -> None:
    assert normalize_text("Nurse\u2019s Office") == "Nurse's Office"


def test_normalize_absence_code_folds_case_and_spaces() -> None:
    assert normalize_absence_code("Excused  Absence") == normalize_absence_code(
        "excused absence"
    )
    assert normalize_absence_code("Nurse\u2019s Office") == normalize_absence_code(
        "Nurse's Office"
    )


def test_unique_absence_codes_dedupes_and_sorts() -> None:
    codes = unique_absence_codes(
        ["Illness", "illness", "Unexcused Absence", "  Tardy Unexcused "]
    )
    assert codes == ["Illness", "Tardy Unexcused", "Unexcused Absence"]


def _test_student_id(db_conn: sqlite3.Connection) -> int:
    row = db_conn.execute(
        "SELECT id FROM students WHERE sis_number = ?",
        ("10001",),
    ).fetchone()
    assert row is not None
    return int(row["id"])


@pytest.mark.parametrize(
    ("period", "absence_date", "code"),
    [
        (0, "2025-09-29", "Family Emergency"),
        (2, "2025-10-15", "Sports-Athletics"),
        (1, "2025-10-20", "Illness"),
        (0, "2025-10-07", "Field Trip/School A"),
        (3, "2025-09-02", "Unexcused Absence"),
        (4, "2025-09-02", "Tardy Unexcused"),
    ],
)
def test_check_eligibility(
    db_conn: sqlite3.Connection,
    period: int,
    absence_date: str,
    code: str,
) -> None:
    result = check_eligibility(
        db_conn, _test_student_id(db_conn), period, absence_date
    )
    assert isinstance(result, EligibilityResult)
    assert result.eligible is True
    assert result.absence_code == code
    assert result.student_name == "Test Student A"


def test_check_eligibility_missing_record(db_conn: sqlite3.Connection) -> None:
    result = check_eligibility(db_conn, _test_student_id(db_conn), 5, "2025-01-01")
    assert result.eligible is False
    assert result.absence_code is None
    assert "No absence record" in result.reason


def test_check_eligibility_unknown_student(db_conn: sqlite3.Connection) -> None:
    result = check_eligibility(db_conn, 999_999, 1, "2025-10-20")
    assert result.eligible is False
    assert "not found" in result.reason.lower()


def test_check_eligibility_ignores_code_case(
    db_conn: sqlite3.Connection,
) -> None:
    student_id = _test_student_id(db_conn)
    db_conn.execute(
        """
        UPDATE attendance_records
        SET absence_code = 'illness'
        WHERE student_id = ? AND absence_date = '2025-10-20' AND period = 1
        """,
        (student_id,),
    )
    db_conn.commit()
    result = check_eligibility(db_conn, student_id, 1, "2025-10-20")
    assert result.eligible is True
    assert result.absence_code == "illness"


def test_check_eligibility_same_name_different_sis(
    db_conn: sqlite3.Connection,
) -> None:
    """Eligibility is tied to student_id, not display name."""
    from app.services.attendance_parser import upsert_student

    other_id = upsert_student(db_conn, "Test Student A", "10", "20002")
    db_conn.execute(
        """
        INSERT INTO attendance_records (
            student_id, absence_date, period, absence_code
        ) VALUES (?, '2025-09-29', 0, 'Unexcused Absence')
        """,
        (other_id,),
    )
    db_conn.commit()

    original = check_eligibility(
        db_conn, _test_student_id(db_conn), 0, "2025-09-29"
    )
    assert original.eligible is True
    assert original.absence_code == "Family Emergency"

    other = check_eligibility(db_conn, other_id, 0, "2025-09-29")
    assert other.eligible is True
    assert other.absence_code == "Unexcused Absence"


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
    student = conn.execute(
        "SELECT id FROM students WHERE name = 'Test Student A'"
    ).fetchone()
    assert student is not None
    result = check_eligibility(conn, int(student["id"]), 3, "2025-09-02")
    conn.close()
    assert result.eligible is True
    assert result.absence_code == "Unexcused Absence"
