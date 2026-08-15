"""Tests for assignment creation with multiple periods."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from app.config import settings
import app.services.assignments as assignments_module
from app.database import init_schema
from app.services.assignments import (
    add_periods_to_assignment,
    create_assignment,
    create_github_assignment,
    delete_assignment,
    find_github_assignment,
    format_period_list,
    get_assignment_pdf_path,
    list_assignments,
    write_assignment_pdf,
)
from app.services.github_worksheets import periods_to_json


@pytest.fixture
def db_conn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    test_settings = replace(settings, data_dir=tmp_path)
    monkeypatch.setattr("app.config.settings", test_settings)
    monkeypatch.setattr(assignments_module, "settings", test_settings)
    test_settings.assignments_dir.mkdir(parents=True, exist_ok=True)

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


def test_create_github_assignment_is_db_only(db_conn: sqlite3.Connection) -> None:
    assignment_id = create_github_assignment(
        db_conn,
        periods=[2, 4],
        assigned_date="2025-09-10",
        title="Chapter 4 Practice",
        github_repo="scope_tenth",
        github_path="unit2/ch04.pdf",
        pdf_filename="ch04.pdf",
    )
    db_conn.commit()

    assert not get_assignment_pdf_path(assignment_id).exists()
    row = db_conn.execute(
        "SELECT source, github_repo, github_path FROM assignments WHERE id = ?",
        (assignment_id,),
    ).fetchone()
    assert row["source"] == "github"
    assert row["github_repo"] == "scope_tenth"
    assert row["github_path"] == "unit2/ch04.pdf"


def test_find_github_assignment(db_conn: sqlite3.Connection) -> None:
    assignment_id = create_github_assignment(
        db_conn,
        periods=[1],
        assigned_date="2025-09-10",
        title="Worksheet",
        github_repo="scope_tenth",
        github_path="unit2/ch04.pdf",
        pdf_filename="ch04.pdf",
    )
    db_conn.commit()

    assert (
        find_github_assignment(db_conn, "scope_tenth", "unit2/ch04.pdf", "2025-09-10")
        == assignment_id
    )
    assert (
        find_github_assignment(db_conn, "scope_tenth", "unit2/ch04.pdf", "2025-09-11")
        is None
    )


def test_add_periods_to_assignment_reports_added_and_skipped(
    db_conn: sqlite3.Connection,
) -> None:
    assignment_id = create_github_assignment(
        db_conn,
        periods=[1],
        assigned_date="2025-09-10",
        title="Worksheet",
        github_repo="scope_tenth",
        github_path="unit2/ch04.pdf",
        pdf_filename="ch04.pdf",
    )

    added, skipped = add_periods_to_assignment(db_conn, assignment_id, [1, 3, 5])
    assert added == [3, 5]
    assert skipped == [1]


def test_write_assignment_pdf_after_commit(db_conn: sqlite3.Connection) -> None:
    assignment_id = create_github_assignment(
        db_conn,
        periods=[1],
        assigned_date="2025-09-10",
        title="Worksheet",
        github_repo="scope_tenth",
        github_path="unit2/ch04.pdf",
        pdf_filename="ch04.pdf",
    )
    db_conn.commit()
    write_assignment_pdf(assignment_id, b"%PDF-1.4 test")
    assert get_assignment_pdf_path(assignment_id).read_bytes() == b"%PDF-1.4 test"


def test_list_assignments_includes_github_source_fields(
    db_conn: sqlite3.Connection,
) -> None:
    assignment_id = create_github_assignment(
        db_conn,
        periods=[1],
        assigned_date="2025-09-10",
        title="GitHub Worksheet",
        github_repo="scope_tenth",
        github_path="unit2/ch04.pdf",
        pdf_filename="ch04.pdf",
    )
    db_conn.commit()

    results = list_assignments(db_conn)
    assert len(results) == 1
    assert results[0].id == assignment_id
    assert results[0].source == "github"
    assert results[0].github_repo == "scope_tenth"
    assert results[0].github_path == "unit2/ch04.pdf"


def test_delete_assignment_clears_print_queue_rows(
    db_conn: sqlite3.Connection,
) -> None:
    assignment_id = create_assignment(
        db_conn,
        periods=[0],
        assigned_date="2025-09-29",
        title="Week 1",
        description=None,
        pdf_bytes=b"%PDF-1.4 test",
        original_filename="week1.pdf",
    )
    db_conn.execute(
        "INSERT INTO students (sis_number, name) VALUES ('10001', 'Test Student A')"
    )
    student_id = int(db_conn.execute("SELECT id FROM students").fetchone()["id"])
    db_conn.execute(
        """
        INSERT INTO claim_tokens (
            token, student_id, assignment_id, period, absence_date
        ) VALUES ('ABCD1234', ?, ?, 0, '2025-09-29')
        """,
        (student_id, assignment_id),
    )
    db_conn.execute("INSERT INTO print_queue (token) VALUES ('ABCD1234')")
    db_conn.commit()

    delete_assignment(db_conn, assignment_id)

    assert db_conn.execute("SELECT COUNT(*) FROM assignments").fetchone()[0] == 0
    assert db_conn.execute("SELECT COUNT(*) FROM claim_tokens").fetchone()[0] == 0
    assert db_conn.execute("SELECT COUNT(*) FROM print_queue").fetchone()[0] == 0


def test_delete_assignment_nullifies_distribution_events_fk(
    db_conn: sqlite3.Connection,
) -> None:
    assignment_id = create_github_assignment(
        db_conn,
        periods=[1],
        assigned_date="2025-09-10",
        title="Worksheet",
        github_repo="scope_tenth",
        github_path="unit2/ch04.pdf",
        pdf_filename="ch04.pdf",
    )
    db_conn.execute(
        """
        INSERT INTO distribution_events (
            assigned_date, github_repo, github_path, display_title,
            periods_requested, periods_added, periods_skipped,
            assignment_id, outcome
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "2025-09-10",
            "scope_tenth",
            "unit2/ch04.pdf",
            "Worksheet",
            periods_to_json([1]),
            periods_to_json([1]),
            periods_to_json([]),
            assignment_id,
            "success",
        ),
    )
    db_conn.commit()

    delete_assignment(db_conn, assignment_id)

    row = db_conn.execute(
        "SELECT assignment_id FROM distribution_events"
    ).fetchone()
    assert row["assignment_id"] is None
    assert (
        db_conn.execute("SELECT COUNT(*) FROM distribution_events").fetchone()[0] == 1
    )


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