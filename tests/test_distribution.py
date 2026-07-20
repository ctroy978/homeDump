"""Tests for GitHub worksheet distribution registration."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from app.config import settings
import app.services.assignments as assignments_module
from app.database import init_schema
from app.services import distribution as dist
from app.services.assignments import (
    find_github_assignment,
    get_assignment_pdf_path,
)
from app.services.github_worksheets import GitHubWorksheetError, RepoInfo

PDF_BYTES = b"%PDF-1.4 distribution test"
SCOPE_REPO = "scope_tenth"
WORKSHEET_PATH = "unit2/ch04.pdf"
ASSIGNED_DATE = "2025-09-10"


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


@pytest.fixture
def github_mocks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        dist,
        "list_filtered_repos",
        lambda **kwargs: [RepoInfo(name=SCOPE_REPO, full_name=f"krewten-978/{SCOPE_REPO}")],
    )
    monkeypatch.setattr(
        dist,
        "fetch_pdf_bytes",
        lambda repo, path, **kwargs: PDF_BYTES,
    )


def _register(
    conn: sqlite3.Connection,
    periods: list[int],
    *,
    assigned_date: str = ASSIGNED_DATE,
) -> dist.DistributionResult:
    return dist.register_distribution(
        conn,
        github_repo=SCOPE_REPO,
        github_path=WORKSHEET_PATH,
        periods=periods,
        client_ip="127.0.0.1",
        assigned_date=assigned_date,
    )


def test_first_registration_creates_assignment_and_writes_pdf(
    db_conn: sqlite3.Connection,
    github_mocks: None,
) -> None:
    result = _register(db_conn, [1])

    assert result.outcome == "success"
    assert result.periods_added == [1]
    assert result.periods_skipped == []
    assert result.assignment_id is not None
    assert get_assignment_pdf_path(result.assignment_id).read_bytes() == PDF_BYTES

    assignment_id = find_github_assignment(
        db_conn,
        SCOPE_REPO,
        WORKSHEET_PATH,
        ASSIGNED_DATE,
    )
    assert assignment_id == result.assignment_id

    assignment = db_conn.execute(
        "SELECT title, pdf_filename FROM assignments WHERE id = ?",
        (assignment_id,),
    ).fetchone()
    assert assignment["title"] == "unit2-ch04"
    assert assignment["pdf_filename"] == "ch04.pdf"

    row = db_conn.execute(
        "SELECT outcome, assignment_id FROM distribution_events WHERE id = 1"
    ).fetchone()
    assert row["outcome"] == "success"
    assert row["assignment_id"] == assignment_id


def test_same_day_rescan_adds_periods(
    db_conn: sqlite3.Connection,
    github_mocks: None,
) -> None:
    first = _register(db_conn, [1])
    second = _register(db_conn, [3])

    assert second.outcome == "success"
    assert second.assignment_id == first.assignment_id
    assert second.periods_added == [3]

    periods = db_conn.execute(
        """
        SELECT period
        FROM assignment_periods
        WHERE assignment_id = ?
        ORDER BY period
        """,
        (first.assignment_id,),
    ).fetchall()
    assert [row["period"] for row in periods] == [1, 3]


def test_same_day_duplicate_periods_are_idempotent(
    db_conn: sqlite3.Connection,
    github_mocks: None,
) -> None:
    _register(db_conn, [1, 3])
    result = _register(db_conn, [1, 3])

    assert result.outcome == "all_duplicate"
    assert result.periods_added == []
    assert result.periods_skipped == [1, 3]


def test_partial_outcome_when_mixing_new_and_existing_periods(
    db_conn: sqlite3.Connection,
    github_mocks: None,
) -> None:
    _register(db_conn, [1])
    result = _register(db_conn, [1, 5])

    assert result.outcome == "partial"
    assert result.periods_added == [5]
    assert result.periods_skipped == [1]


def test_different_day_creates_new_assignment(
    db_conn: sqlite3.Connection,
    github_mocks: None,
) -> None:
    first = _register(db_conn, [1], assigned_date="2025-09-10")
    second = _register(db_conn, [1], assigned_date="2025-09-11")

    assert first.assignment_id != second.assignment_id
    assert find_github_assignment(db_conn, SCOPE_REPO, WORKSHEET_PATH, "2025-09-10") == (
        first.assignment_id
    )
    assert find_github_assignment(db_conn, SCOPE_REPO, WORKSHEET_PATH, "2025-09-11") == (
        second.assignment_id
    )


def test_expected_failure_records_audit_row_without_assignment(
    db_conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        dist,
        "list_filtered_repos",
        lambda **kwargs: [],
    )

    result = _register(db_conn, [2])

    assert result.outcome == "failure"
    assert result.assignment_id is None
    assert db_conn.execute("SELECT COUNT(*) FROM assignments").fetchone()[0] == 0

    row = db_conn.execute(
        """
        SELECT outcome, assignment_id, error_message
        FROM distribution_events
        """
    ).fetchone()
    assert row["outcome"] == "failure"
    assert row["assignment_id"] is None
    assert "not configured" in row["error_message"]


def test_expected_failure_from_github_fetch(
    db_conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        dist,
        "list_filtered_repos",
        lambda **kwargs: [RepoInfo(name=SCOPE_REPO, full_name=f"krewten-978/{SCOPE_REPO}")],
    )
    monkeypatch.setattr(
        dist,
        "fetch_pdf_bytes",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            GitHubWorksheetError("Worksheet not found in repo")
        ),
    )

    result = _register(db_conn, [2])

    assert result.outcome == "failure"
    assert db_conn.execute("SELECT COUNT(*) FROM assignments").fetchone()[0] == 0
    row = db_conn.execute(
        "SELECT outcome, error_message FROM distribution_events"
    ).fetchone()
    assert row["outcome"] == "failure"
    assert "not found" in row["error_message"]


def test_unexpected_failure_rolls_back_without_orphan_pdf(
    db_conn: sqlite3.Connection,
    github_mocks: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failing_insert(*args, **kwargs) -> None:
        raise RuntimeError("ledger insert failed")

    monkeypatch.setattr(dist, "_insert_distribution_event", failing_insert)

    with pytest.raises(RuntimeError, match="ledger insert failed"):
        _register(db_conn, [2])

    assert db_conn.execute("SELECT COUNT(*) FROM assignments").fetchone()[0] == 0
    assert db_conn.execute("SELECT COUNT(*) FROM distribution_events").fetchone()[0] == 0
    assert list(assignments_module.settings.assignments_dir.iterdir()) == []


def test_post_commit_pdf_write_failure_recovered_on_rescan(
    db_conn: sqlite3.Connection,
    github_mocks: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_calls = 0

    def failing_write(assignment_id: int, pdf_bytes: bytes) -> Path:
        nonlocal write_calls
        write_calls += 1
        if write_calls == 1:
            raise OSError("disk full")
        return assignments_module.write_assignment_pdf(assignment_id, pdf_bytes)

    monkeypatch.setattr(dist, "write_assignment_pdf", failing_write)

    first = _register(db_conn, [1])
    assert first.outcome == "success"
    assert first.assignment_id is not None
    assert first.message is not None
    assert "could not be saved" in first.message.lower()
    assert not get_assignment_pdf_path(first.assignment_id).exists()
    assert (
        db_conn.execute("SELECT COUNT(*) FROM distribution_events").fetchone()[0] == 1
    )

    second = _register(db_conn, [3])
    assert second.outcome == "success"
    assert second.periods_added == [3]
    assert get_assignment_pdf_path(first.assignment_id).read_bytes() == PDF_BYTES


def test_repair_fetch_when_pdf_missing_on_disk(
    db_conn: sqlite3.Connection,
    github_mocks: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _register(db_conn, [1])
    pdf_path = get_assignment_pdf_path(first.assignment_id)
    pdf_path.unlink()

    fetch_calls: list[tuple[str, str]] = []

    def tracking_fetch(repo: str, path: str, **kwargs) -> bytes:
        fetch_calls.append((repo, path))
        return PDF_BYTES

    monkeypatch.setattr(dist, "fetch_pdf_bytes", tracking_fetch)

    second = _register(db_conn, [3])
    assert second.periods_added == [3]
    assert fetch_calls == [(SCOPE_REPO, WORKSHEET_PATH)]
    assert pdf_path.read_bytes() == PDF_BYTES