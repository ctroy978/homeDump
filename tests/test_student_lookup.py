"""Tests for student form lookup queries."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from app.config import settings
import app.services.assignments as assignments_module
from app.services.assignments import create_assignment
from app.services.student_lookup import (
    LOOKUP_FAILURE_MESSAGE,
    diagnose_claim,
    get_student_by_sis,
    list_eligible_assignments_by_sis,
    list_eligible_assignments_for_student,
    list_eligible_dates_by_sis,
    list_eligible_dates_for_student,
    list_periods_with_assignments,
)


@pytest.fixture(autouse=True)
def isolated_assignment_storage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep assignment PDFs created by lookup tests out of runtime data/."""
    test_settings = replace(settings, data_dir=tmp_path / "data")
    monkeypatch.setattr(assignments_module, "settings", test_settings)


def _add_assignment(
    conn: sqlite3.Connection,
    *,
    periods: list[int],
    assigned_date: str,
    title: str,
) -> None:
    create_assignment(
        conn,
        periods=periods,
        assigned_date=assigned_date,
        title=title,
        description=None,
        pdf_bytes=b"%PDF-1.4 test",
        original_filename="test.pdf",
    )


def test_list_periods_with_assignments_empty(db_conn: sqlite3.Connection) -> None:
    assert list_periods_with_assignments(db_conn) == []


def test_list_periods_with_assignments(db_conn: sqlite3.Connection) -> None:
    _add_assignment(db_conn, periods=[0], assigned_date="2025-09-29", title="Week 1")
    _add_assignment(db_conn, periods=[2], assigned_date="2025-10-15", title="Week 2")

    assert list_periods_with_assignments(db_conn) == [0, 2]


def test_get_student_by_sis(db_conn: sqlite3.Connection) -> None:
    student = get_student_by_sis(db_conn, " 10001 ")
    assert student is not None
    assert student.name == "Test Student A"
    assert student.sis_number == "10001"
    assert get_student_by_sis(db_conn, "missing") is None
    padded = get_student_by_sis(db_conn, "010001")
    assert padded is not None
    assert padded.id == student.id
    dotted = get_student_by_sis(db_conn, "10001.0")
    assert dotted is not None
    assert dotted.id == student.id
    assert get_student_by_sis(db_conn, "12.34") is None


def test_list_eligible_dates_by_sis_includes_unexcused_absences(
    db_conn: sqlite3.Connection,
) -> None:
    _add_assignment(db_conn, periods=[0], assigned_date="2025-09-29", title="Week 1")
    _add_assignment(db_conn, periods=[0], assigned_date="2025-10-07", title="Week 2")
    _add_assignment(db_conn, periods=[3], assigned_date="2025-09-02", title="Week 0")

    student, dates = list_eligible_dates_by_sis(db_conn, 0, "10001")
    assert student is not None
    assert dates == ["2025-10-07", "2025-09-29"]

    unexcused_student, period_three_dates = list_eligible_dates_by_sis(
        db_conn, 3, "10001"
    )
    assert unexcused_student is not None
    assert period_three_dates == ["2025-09-02"]


def test_list_eligible_dates_for_student(db_conn: sqlite3.Connection) -> None:
    _add_assignment(db_conn, periods=[0], assigned_date="2025-09-29", title="A")
    _add_assignment(db_conn, periods=[0], assigned_date="2025-10-07", title="B")

    student = get_student_by_sis(db_conn, "10001")
    assert student is not None
    dates = list_eligible_dates_for_student(db_conn, 0, student.id)
    assert dates == ["2025-10-07", "2025-09-29"]


def test_list_eligible_assignments_by_sis(db_conn: sqlite3.Connection) -> None:
    _add_assignment(db_conn, periods=[0], assigned_date="2025-09-29", title="Packet A")
    _add_assignment(db_conn, periods=[0], assigned_date="2025-09-29", title="Packet B")

    student, options = list_eligible_assignments_by_sis(
        db_conn, 0, "10001", "2025-09-29"
    )
    assert student is not None
    assert len(options) == 2
    assert [item.title for item in options] == ["Packet A", "Packet B"]


def test_multi_period_assignment_visible_in_each_period(
    db_conn: sqlite3.Connection,
) -> None:
    _add_assignment(
        db_conn,
        periods=[1, 3, 5],
        assigned_date="2025-10-20",
        title="Shared reading",
    )

    assert list_periods_with_assignments(db_conn) == [1, 3, 5]

    student, dates = list_eligible_dates_by_sis(db_conn, 1, "10001")
    assert student is not None
    assert dates == ["2025-10-20"]

    _, period_three_dates = list_eligible_dates_by_sis(db_conn, 3, "10001")
    assert period_three_dates == []

    _, options = list_eligible_assignments_by_sis(
        db_conn, 1, "10001", "2025-10-20"
    )
    assert len(options) == 1
    assert options[0].title == "Shared reading"


def test_list_eligible_dates_requires_absence_on_assignment_day(
    db_conn: sqlite3.Connection,
) -> None:
    """An absence on a different day does not unlock that day's homework."""
    _add_assignment(db_conn, periods=[0], assigned_date="2025-10-15", title="Later")

    student, dates = list_eligible_dates_by_sis(db_conn, 0, "10001")
    assert student is not None
    assert dates == []

    _, options = list_eligible_assignments_by_sis(
        db_conn, 0, "10001", "2025-09-29"
    )
    assert options == []


def test_list_eligible_assignments_without_absence(
    db_conn: sqlite3.Connection,
) -> None:
    _add_assignment(db_conn, periods=[3], assigned_date="2025-01-01", title="Quiz")

    student = get_student_by_sis(db_conn, "10001")
    assert student is not None
    assert (
        list_eligible_assignments_for_student(
            db_conn, 3, student.id, "2025-01-01"
        )
        == []
    )


def test_list_eligible_assignments_unexcused_absence(
    db_conn: sqlite3.Connection,
) -> None:
    _add_assignment(db_conn, periods=[3], assigned_date="2025-09-02", title="Quiz")

    student, options = list_eligible_assignments_by_sis(
        db_conn, 3, "10001", "2025-09-02"
    )
    assert student is not None
    assert len(options) == 1
    assert options[0].title == "Quiz"


def test_diagnose_claim_unknown_sis(db_conn: sqlite3.Connection) -> None:
    result = diagnose_claim(db_conn, "99999", 0)
    assert result.student is None
    assert "attendance database" in result.summary


def test_diagnose_claim_unexcused_date_without_assignment(
    db_conn: sqlite3.Connection,
) -> None:
    result = diagnose_claim(db_conn, "10001", 3, "2025-09-02")
    assert result.student is not None
    assert "no homework is assigned" in result.summary
    assert result.assignments == []


def test_diagnose_claim_unexcused_date_with_assignment(
    db_conn: sqlite3.Connection,
) -> None:
    _add_assignment(db_conn, periods=[3], assigned_date="2025-09-02", title="Quiz")
    result = diagnose_claim(db_conn, "10001", 3, "2025-09-02")
    assert result.student is not None
    assert "can claim" in result.summary
    assert len(result.assignments) == 1
    assert result.eligible_dates == ["2025-09-02"]


def test_diagnose_claim_absence_without_assignment(
    db_conn: sqlite3.Connection,
) -> None:
    result = diagnose_claim(db_conn, "10001", 0, "2025-09-29")
    assert result.student is not None
    assert "no homework is assigned" in result.summary


def test_diagnose_claim_eligible_with_assignment(
    db_conn: sqlite3.Connection,
) -> None:
    _add_assignment(db_conn, periods=[0], assigned_date="2025-09-29", title="Week 1")
    result = diagnose_claim(db_conn, "10001", 0, "2025-09-29")
    assert "can claim" in result.summary
    assert len(result.assignments) == 1
    assert result.eligible_dates == ["2025-09-29"]


def test_diagnose_claim_period_with_no_records(db_conn: sqlite3.Connection) -> None:
    result = diagnose_claim(db_conn, "10001", 7)
    assert "has not been imported as your period 7" in result.summary


def test_lookup_failure_message_is_generic() -> None:
    assert "student ID" in LOOKUP_FAILURE_MESSAGE
    assert "teacher" in LOOKUP_FAILURE_MESSAGE


def test_leading_zero_lookup_is_not_used_when_two_digit_equivalents_exist(
    db_conn: sqlite3.Connection,
) -> None:
    # Bypass upsert so we can seed a split that import itself will not create.
    db_conn.execute(
        "INSERT INTO students (sis_number, name, grade) VALUES ('010001', 'Other', '10')"
    )
    db_conn.commit()

    exact = get_student_by_sis(db_conn, "10001")
    assert exact is not None
    assert exact.sis_number == "10001"

    padded = get_student_by_sis(db_conn, "010001")
    assert padded is not None
    assert padded.sis_number == "010001"

    assert get_student_by_sis(db_conn, "00010001") is None
