"""Route tests for adding assignments from uploads and GitHub."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config
import app.dependencies as dependencies
import app.routers.admin as admin_router
import app.services.assignments as assignments_module
from app.dependencies import ADMIN_COOKIE_NAME, _expected_admin_token
from app.services.github_worksheets import (
    GitHubWorksheetError,
    RepoInfo,
    WorksheetBrowseResult,
    WorksheetEntry,
)

REPO = "scope_tenth"
PATH = "unit2/ch04-test.pdf"
PDF_BYTES = b"%PDF-1.4 github worksheet"


@pytest.fixture
def github_assignment_settings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    test_settings = replace(
        config.settings,
        data_dir=tmp_path,
        github_token="test-token",
    )
    monkeypatch.setattr(config, "settings", test_settings)
    monkeypatch.setattr(dependencies, "settings", test_settings)
    monkeypatch.setattr(admin_router, "settings", test_settings)
    monkeypatch.setattr(assignments_module, "settings", test_settings)


@pytest.fixture
def admin_client(
    client: TestClient,
    github_assignment_settings: None,
) -> TestClient:
    client.cookies.set(ADMIN_COOKIE_NAME, _expected_admin_token())
    return client


def _repo_info() -> list[RepoInfo]:
    return [RepoInfo(name=REPO, full_name=f"krewten-978/{REPO}")]


def _browse_result(*, search_active: bool = False) -> WorksheetBrowseResult:
    return WorksheetBrowseResult(
        current_path="",
        directories=[],
        files=[
            WorksheetEntry(
                path=PATH,
                name="ch04-test.pdf",
                display_title="unit2-ch04-test",
                size_bytes=len(PDF_BYTES),
            )
        ],
        breadcrumbs=[],
        search_active=search_active,
    )


def _mock_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(admin_router, "list_filtered_repos", _repo_info)
    monkeypatch.setattr(
        admin_router,
        "browse_pdf_worksheets",
        lambda repo, path=None, query=None: _browse_result(
            search_active=bool(query)
        ),
    )


def _github_form(*, periods: list[str] | None = None) -> dict[str, object]:
    return {
        "source": "github",
        "periods": periods or ["1", "3"],
        "assigned_date": "2026-08-05",
        "title": "Chapter 4 Test",
        "description": "Complete both pages.",
        "github_repo": REPO,
        "github_path": PATH,
    }


def test_add_assignment_page_requires_admin(
    client: TestClient,
    github_assignment_settings: None,
) -> None:
    response = client.get("/admin/assignments/new", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/admin/login")


def test_add_assignment_page_offers_upload_and_github(
    admin_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_browser(monkeypatch)

    response = admin_client.get("/admin/assignments/new")

    assert response.status_code == 200
    assert 'src="/static/htmx.min.js"' in response.text
    assert "unpkg.com/htmx" not in response.text
    assert "Upload from this computer" in response.text
    assert "Choose from GitHub" in response.text
    assert REPO in response.text
    assert "Use this PDF" in response.text
    assert PATH in response.text


def test_add_assignment_page_keeps_upload_when_github_is_disabled(
    admin_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    disabled = replace(admin_router.settings, github_token=None)
    monkeypatch.setattr(config, "settings", disabled)
    monkeypatch.setattr(admin_router, "settings", disabled)

    response = admin_client.get("/admin/assignments/new")

    assert response.status_code == 200
    assert "Upload from this computer" in response.text
    assert "Set <code>GITHUB_TOKEN</code>" in response.text
    assert 'value="github"' in response.text
    github_radio = response.text.split('value="github"', 1)[1].split(">", 1)[0]
    assert "disabled" in github_radio


def test_github_browse_returns_selection_partial(
    admin_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[str, str | None, str | None]] = []
    monkeypatch.setattr(admin_router, "list_filtered_repos", _repo_info)

    def browse(repo: str, path=None, query=None):
        captured.append((repo, path, query))
        return _browse_result(search_active=bool(query))

    monkeypatch.setattr(admin_router, "browse_pdf_worksheets", browse)

    response = admin_client.get(
        f"/admin/assignments/new/github-browse?repo={REPO}&q=test"
    )

    assert response.status_code == 200
    assert captured == [(REPO, "", "test")]
    assert "Use this PDF" in response.text
    assert "Prepare print packet" not in response.text
    assert "<html" not in response.text.lower()


def test_local_upload_still_creates_manual_assignment(
    admin_client: TestClient,
    db_conn: sqlite3.Connection,
) -> None:
    response = admin_client.post(
        "/admin/assignments/new",
        data={
            "source": "upload",
            "periods": ["2", "4"],
            "assigned_date": "2026-08-05",
            "title": "Local worksheet",
            "description": "Uploaded normally.",
        },
        files={"pdf": ("local.pdf", b"%PDF-1.4 local", "application/pdf")},
        follow_redirects=False,
    )

    assert response.status_code == 303
    row = db_conn.execute(
        "SELECT id, source, github_repo, github_path FROM assignments"
    ).fetchone()
    assert row["source"] == "manual"
    assert row["github_repo"] is None
    assert row["github_path"] is None
    assert assignments_module.get_assignment_pdf_path(row["id"]).read_bytes() == (
        b"%PDF-1.4 local"
    )


def test_github_selection_creates_assignment_and_saves_pdf(
    admin_client: TestClient,
    db_conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(admin_router, "list_filtered_repos", _repo_info)
    monkeypatch.setattr(
        admin_router,
        "fetch_pdf_bytes",
        lambda repo, path: PDF_BYTES,
    )

    response = admin_client.post(
        "/admin/assignments/new",
        data=_github_form(),
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/assignments?success=1"
    row = db_conn.execute(
        """
        SELECT id, assigned_date, title, description, pdf_filename,
               source, github_repo, github_path
        FROM assignments
        """
    ).fetchone()
    assert dict(row) == {
        "id": row["id"],
        "assigned_date": "2026-08-05",
        "title": "Chapter 4 Test",
        "description": "Complete both pages.",
        "pdf_filename": "ch04-test.pdf",
        "source": "github",
        "github_repo": REPO,
        "github_path": PATH,
    }
    periods = db_conn.execute(
        "SELECT period FROM assignment_periods ORDER BY period"
    ).fetchall()
    assert [item["period"] for item in periods] == [1, 3]
    assert assignments_module.get_assignment_pdf_path(row["id"]).read_bytes() == (
        PDF_BYTES
    )


def test_duplicate_github_assignment_adds_only_missing_periods(
    admin_client: TestClient,
    db_conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(admin_router, "list_filtered_repos", _repo_info)
    fetches: list[tuple[str, str]] = []

    def fetch(repo: str, path: str) -> bytes:
        fetches.append((repo, path))
        return PDF_BYTES

    monkeypatch.setattr(admin_router, "fetch_pdf_bytes", fetch)

    first = admin_client.post(
        "/admin/assignments/new",
        data=_github_form(periods=["1"]),
        follow_redirects=False,
    )
    second = admin_client.post(
        "/admin/assignments/new",
        data=_github_form(periods=["1", "3"]),
        follow_redirects=False,
    )

    assert first.status_code == 303
    assert second.status_code == 303
    assert db_conn.execute("SELECT COUNT(*) FROM assignments").fetchone()[0] == 1
    periods = db_conn.execute(
        "SELECT period FROM assignment_periods ORDER BY period"
    ).fetchall()
    assert [item["period"] for item in periods] == [1, 3]
    assert fetches == [(REPO, PATH)]


def test_tampered_repo_is_rejected_without_creating_assignment(
    admin_client: TestClient,
    db_conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_browser(monkeypatch)
    form = _github_form()
    form["github_repo"] = "not-allowed"

    response = admin_client.post("/admin/assignments/new", data=form)

    assert response.status_code == 400
    assert "not an allowed worksheet repo" in response.text
    assert db_conn.execute("SELECT COUNT(*) FROM assignments").fetchone()[0] == 0


def test_github_download_failure_does_not_create_assignment(
    admin_client: TestClient,
    db_conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(admin_router, "list_filtered_repos", _repo_info)
    monkeypatch.setattr(
        admin_router,
        "fetch_pdf_bytes",
        lambda repo, path: (_ for _ in ()).throw(
            GitHubWorksheetError("Worksheet download failed.")
        ),
    )
    monkeypatch.setattr(
        admin_router,
        "browse_pdf_worksheets",
        lambda repo, path=None, query=None: _browse_result(),
    )

    response = admin_client.post("/admin/assignments/new", data=_github_form())

    assert response.status_code == 400
    assert "Worksheet download failed" in response.text
    assert db_conn.execute("SELECT COUNT(*) FROM assignments").fetchone()[0] == 0


def test_github_file_write_failure_rolls_back_assignment(
    admin_client: TestClient,
    db_conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_browser(monkeypatch)
    monkeypatch.setattr(
        admin_router,
        "fetch_pdf_bytes",
        lambda repo, path: PDF_BYTES,
    )

    def deny_write(path: Path, data: bytes) -> int:
        raise PermissionError("Assignment directory is not writable.")

    monkeypatch.setattr(Path, "write_bytes", deny_write)

    response = admin_client.post("/admin/assignments/new", data=_github_form())

    assert response.status_code == 400
    assert "Assignment directory is not writable" in response.text
    assert db_conn.execute("SELECT COUNT(*) FROM assignments").fetchone()[0] == 0
    assert not (admin_router.settings.assignments_dir / "1").exists()
