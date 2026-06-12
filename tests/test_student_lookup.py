"""Tests for student form lookup queries."""

from __future__ import annotations

import sqlite3

from app.services.assignments import create_assignment
from app.services.student_lookup import (
    list_eligible_assignments,
    list_eligible_dates,
    list_eligible_students,
    list_periods_with_assignments,
)


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


def test_list_eligible_students_filters_allowable_codes(
    db_conn: sqlite3.Connection,
) -> None:
    _add_assignment(db_conn, periods=[0], assigned_date="2025-09-29", title="Week 1")
    _add_assignment(db_conn, periods=[3], assigned_date="2025-09-02", title="Week 0")

    assert list_eligible_students(db_conn, 0) == ["Test Student A"]
    assert list_eligible_students(db_conn, 3) == []


def test_list_eligible_dates(db_conn: sqlite3.Connection) -> None:
    _add_assignment(db_conn, periods=[0], assigned_date="2025-09-29", title="A")
    _add_assignment(db_conn, periods=[0], assigned_date="2025-10-07", title="B")

    dates = list_eligible_dates(db_conn, 0, "Test Student A")
    assert dates == ["2025-10-07", "2025-09-29"]


def test_list_eligible_assignments(db_conn: sqlite3.Connection) -> None:
    _add_assignment(db_conn, periods=[0], assigned_date="2025-09-29", title="Packet A")
    _add_assignment(db_conn, periods=[0], assigned_date="2025-09-29", title="Packet B")

    options = list_eligible_assignments(
        db_conn, 0, "Test Student A", "2025-09-29"
    )
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
    assert list_eligible_students(db_conn, 1) == ["Test Student A"]
    assert list_eligible_students(db_conn, 3) == []
    options = list_eligible_assignments(
        db_conn, 1, "Test Student A", "2025-10-20"
    )
    assert len(options) == 1
    assert options[0].title == "Shared reading"


def test_list_eligible_assignments_not_eligible(
    db_conn: sqlite3.Connection,
) -> None:
    _add_assignment(db_conn, periods=[3], assigned_date="2025-09-02", title="Quiz")

    assert (
        list_eligible_assignments(db_conn, 3, "Test Student A", "2025-09-02")
        == []
    )

