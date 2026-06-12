"""Tests for assignment creation with multiple periods."""

from __future__ import annotations

import sqlite3

import pytest

from app.database import init_schema
from app.services.assignments import (
    create_assignment,
    delete_assignment,
    format_period_list,
    list_assignments,
)


@pytest.fixture
def db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    init_schema(conn)
    yield conn
    conn.close()


def test_create_assignment_with_multiple_periods(db_conn: sqlite3.Connection) -> None:
    assignment_id = create_assignment(
        db_conn,
        periods=[1, 3, 5],
        assigned_date="2025-09-10",
        title="Aristotle packet",
        description=None,
        pdf_bytes=b"%PDF-1.4 test",
        original_filename="aristotle.pdf",
    )

    rows = db_conn.execute(
        """
        SELECT period
        FROM assignment_periods
        WHERE assignment_id = ?
        ORDER BY period
        """,
        (assignment_id,),
    ).fetchall()
    assert [row["period"] for row in rows] == [1, 3, 5]


def test_create_assignment_requires_at_least_one_period(
    db_conn: sqlite3.Connection,
) -> None:
    with pytest.raises(ValueError, match="at least one"):
        create_assignment(
            db_conn,
            periods=[],
            assigned_date="2025-09-10",
            title="Missing periods",
            description=None,
            pdf_bytes=b"%PDF-1.4 test",
            original_filename="test.pdf",
        )


def test_format_period_list() -> None:
    assert format_period_list([5, 1, 3]) == "1, 3, 5"


def test_list_assignments_filter_by_title(db_conn: sqlite3.Connection) -> None:
    create_assignment(
        db_conn,
        periods=[1],
        assigned_date="2025-09-10",
        title="Aristotle packet",
        description=None,
        pdf_bytes=b"%PDF-1.4 test",
        original_filename="aristotle.pdf",
    )
    create_assignment(
        db_conn,
        periods=[2],
        assigned_date="2025-09-11",
        title="Plato reading",
        description=None,
        pdf_bytes=b"%PDF-1.4 test",
        original_filename="plato.pdf",
    )

    results = list_assignments(db_conn, title_query="arist")
    assert len(results) == 1
    assert results[0].title == "Aristotle packet"


def test_list_assignments_filter_by_date(db_conn: sqlite3.Connection) -> None:
    create_assignment(
        db_conn,
        periods=[1],
        assigned_date="2025-09-10",
        title="Aristotle packet",
        description=None,
        pdf_bytes=b"%PDF-1.4 test",
        original_filename="aristotle.pdf",
    )
    create_assignment(
        db_conn,
        periods=[2],
        assigned_date="2025-09-11",
        title="Plato reading",
        description=None,
        pdf_bytes=b"%PDF-1.4 test",
        original_filename="plato.pdf",
    )

    results = list_assignments(db_conn, assigned_date="2025-09-11")
    assert len(results) == 1
    assert results[0].title == "Plato reading"


def test_delete_assignment_removes_row_and_periods(db_conn: sqlite3.Connection) -> None:
    assignment_id = create_assignment(
        db_conn,
        periods=[1, 3],
        assigned_date="2025-09-10",
        title="To delete",
        description=None,
        pdf_bytes=b"%PDF-1.4 test",
        original_filename="delete-me.pdf",
    )

    delete_assignment(db_conn, assignment_id)

    assert (
        db_conn.execute(
            "SELECT COUNT(*) FROM assignments WHERE id = ?",
            (assignment_id,),
        ).fetchone()[0]
        == 0
    )
    assert (
        db_conn.execute(
            "SELECT COUNT(*) FROM assignment_periods WHERE assignment_id = ?",
            (assignment_id,),
        ).fetchone()[0]
        == 0
    )