"""Tests for teacher distribution scan audit log queries."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from app.config import settings
import app.services.assignments as assignments_module
from app.database import init_schema
from app.services import distribution as dist
from app.services.distribution_log import list_distribution_events
from app.services.github_worksheets import RepoInfo

PDF_BYTES = b"%PDF-1.4 distribution log test"
SCOPE_REPO = "scope_tenth"
WORKSHEET_PATH = "unit2/ch04.pdf"


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
    assigned_date: str = "2025-09-10",
) -> dist.DistributionResult:
    return dist.register_distribution(
        conn,
        github_repo=SCOPE_REPO,
        github_path=WORKSHEET_PATH,
        periods=periods,
        client_ip="127.0.0.1",
        assigned_date=assigned_date,
    )


def test_list_distribution_events_returns_recent_entries_first(
    db_conn: sqlite3.Connection,
    github_mocks: None,
) -> None:
    _register(db_conn, [1], assigned_date="2025-09-10")
    _register(db_conn, [2], assigned_date="2025-09-11")

    logs = list_distribution_events(db_conn)
    assert len(logs) == 2
    assert logs[0].assigned_date == "2025-09-11"
    assert logs[1].assigned_date == "2025-09-10"


def test_list_distribution_events_filters_by_repo_date_and_outcome(
    db_conn: sqlite3.Connection,
    github_mocks: None,
) -> None:
    _register(db_conn, [1])
    _register(db_conn, [1, 3])
    _register(db_conn, [1, 3])

    repo_logs = list_distribution_events(db_conn, repo_query="tenth")
    assert len(repo_logs) == 3
    assert all(entry.github_repo == SCOPE_REPO for entry in repo_logs)

    date_logs = list_distribution_events(db_conn, assigned_date="2025-09-10")
    assert len(date_logs) == 3

    duplicate_logs = list_distribution_events(db_conn, outcome="all_duplicate")
    assert len(duplicate_logs) == 1
    assert duplicate_logs[0].periods_skipped == [1, 3]

    success_logs = list_distribution_events(db_conn, outcome="success")
    assert len(success_logs) == 1
    assert success_logs[0].periods_added == [1]

    partial_logs = list_distribution_events(db_conn, outcome="partial")
    assert len(partial_logs) == 1
    assert partial_logs[0].periods_added == [3]
    assert partial_logs[0].periods_skipped == [1]


def test_list_distribution_events_parses_period_json(
    db_conn: sqlite3.Connection,
    github_mocks: None,
) -> None:
    _register(db_conn, [1, 5])

    logs = list_distribution_events(db_conn)
    assert logs[0].periods_added == [1, 5]
    assert logs[0].periods_requested == [1, 5]
    assert logs[0].periods_skipped == []


def test_list_distribution_events_includes_failure_rows(
    db_conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dist, "list_filtered_repos", lambda **kwargs: [])

    dist.register_distribution(
        db_conn,
        github_repo=SCOPE_REPO,
        github_path=WORKSHEET_PATH,
        periods=[2],
        client_ip="127.0.0.1",
        assigned_date="2025-09-10",
    )

    logs = list_distribution_events(db_conn, outcome="failure")
    assert len(logs) == 1
    assert logs[0].assignment_id is None
    assert logs[0].error_message is not None
    assert "not configured" in logs[0].error_message