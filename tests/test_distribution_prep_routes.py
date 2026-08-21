"""Route tests for the GitHub worksheet prep workflow."""

from __future__ import annotations

from dataclasses import replace
from io import BytesIO

import fitz
import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader

from app import config
import app.dependencies as dependencies
import app.routers.distribution as distribution_router
from app.dependencies import ADMIN_COOKIE_NAME, _expected_admin_token
from app.services.github_worksheets import (
    RepoInfo,
    WorksheetBrowseResult,
    WorksheetDirEntry,
    WorksheetEntry,
)

REPO = "scope_tenth"
PATH = "unit2/ch04.pdf"


@pytest.fixture
def github_settings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    test_settings = replace(
        config.settings,
        data_dir=tmp_path,
        github_token="test-token",
        scan_pin="1234",
    )
    monkeypatch.setattr(config, "settings", test_settings)
    monkeypatch.setattr(dependencies, "settings", test_settings)
    monkeypatch.setattr(distribution_router, "settings", test_settings)


@pytest.fixture
def admin_client(client: TestClient, github_settings: None) -> TestClient:
    client.cookies.set(ADMIN_COOKIE_NAME, _expected_admin_token())
    return client


def test_prep_requires_admin_login(client: TestClient, github_settings: None) -> None:
    response = client.get("/admin/distribute/prep", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/admin/login")


def test_prep_redirects_when_github_disabled(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    test_settings = replace(config.settings, github_token=None, scan_pin=None)
    monkeypatch.setattr(config, "settings", test_settings)
    monkeypatch.setattr(dependencies, "settings", test_settings)
    monkeypatch.setattr(distribution_router, "settings", test_settings)
    client.cookies.set(ADMIN_COOKIE_NAME, _expected_admin_token())

    response = client.get("/admin/distribute/prep", follow_redirects=False)
    assert response.status_code == 303
    assert "prep_error=github_disabled" in response.headers["location"]


def test_prep_page_survives_github_api_failure(
    admin_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.github_worksheets import GitHubWorksheetError

    def boom(**kwargs):
        raise GitHubWorksheetError("GitHub API rate limit exceeded.")

    monkeypatch.setattr(distribution_router, "list_filtered_repos", boom)
    response = admin_client.get("/admin/distribute/prep")
    assert response.status_code == 200
    assert "GitHub worksheets" in response.text
    assert "rate limit" in response.text
    assert "Internal Server Error" not in response.text


def test_prep_browse_survives_github_api_failure(
    admin_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.github_worksheets import GitHubWorksheetError

    def boom(*args, **kwargs):
        raise GitHubWorksheetError("GitHub is unavailable.")

    monkeypatch.setattr(distribution_router, "browse_pdf_worksheets", boom)
    response = admin_client.get(f"/admin/distribute/prep/browse?repo={REPO}")
    assert response.status_code == 400
    assert "unavailable" in response.text
    assert "Internal Server Error" not in response.text


def test_prep_page_lists_worksheets(
    admin_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        distribution_router,
        "list_filtered_repos",
        lambda **kwargs: [RepoInfo(name=REPO, full_name=f"krewten-978/{REPO}")],
    )
    monkeypatch.setattr(
        distribution_router,
        "browse_pdf_worksheets",
        lambda repo, path=None, query=None, **kwargs: WorksheetBrowseResult(
            current_path="",
            directories=[WorksheetDirEntry(name="unit2", path="unit2")],
            files=[
                WorksheetEntry(
                    path=PATH,
                    name="ch04.pdf",
                    display_title="ch04",
                    size_bytes=1200,
                )
            ],
            breadcrumbs=[],
            search_active=False,
        ),
    )

    response = admin_client.get("/admin/distribute/prep")
    assert response.status_code == 200
    assert 'src="/static/htmx.min.js"' in response.text
    assert "unpkg.com/htmx" not in response.text
    assert "GitHub worksheets" in response.text
    assert REPO in response.text
    assert "unit2" in response.text
    assert "Prepare print packet" in response.text
    assert "Print with names" in response.text


def test_prep_browse_partial(
    admin_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        distribution_router,
        "browse_pdf_worksheets",
        lambda repo, path=None, query=None, **kwargs: WorksheetBrowseResult(
            current_path="",
            directories=[],
            files=[
                WorksheetEntry(
                    path=PATH,
                    name="ch04.pdf",
                    display_title="ch04",
                    size_bytes=None,
                )
            ],
            breadcrumbs=[],
            search_active=True,
        ),
    )

    response = admin_client.get(
        f"/admin/distribute/prep/browse?repo={REPO}&q=ch04"
    )
    assert response.status_code == 200
    assert "Search results" in response.text
    assert "ch04" in response.text
    assert "Prepare print packet" in response.text
    assert "<html" not in response.text.lower()


def test_prep_browse_folder_ignores_search_query_param(
    admin_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, str | None]] = []

    def capture_browse(repo, path=None, query=None, **kwargs):
        captured.append({"path": path, "query": query})
        return WorksheetBrowseResult(
            current_path=path or "",
            directories=[],
            files=[],
            breadcrumbs=[],
            search_active=bool(query),
        )

    monkeypatch.setattr(distribution_router, "browse_pdf_worksheets", capture_browse)

    response = admin_client.get(
        f"/admin/distribute/prep/browse?repo={REPO}&path=unit2"
    )
    assert response.status_code == 200
    assert captured == [{"path": "unit2", "query": None}]


def test_prep_browse_drills_into_folder(
    admin_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        distribution_router,
        "browse_pdf_worksheets",
        lambda repo, path=None, query=None, **kwargs: WorksheetBrowseResult(
            current_path="unit2",
            directories=[],
            files=[
                WorksheetEntry(
                    path=PATH,
                    name="ch04.pdf",
                    display_title="ch04",
                    size_bytes=None,
                )
            ],
            breadcrumbs=[("unit2", "unit2")],
            search_active=False,
        ),
    )

    response = admin_client.get(
        f"/admin/distribute/prep/browse?repo={REPO}&path=unit2"
    )
    assert response.status_code == 200
    assert "unit2" in response.text
    assert "Prepare print packet" in response.text


def _minimal_pdf_bytes() -> bytes:
    document = fitz.open()
    try:
        document.new_page(width=612, height=792)
        return document.tobytes()
    finally:
        document.close()


def test_print_packet_download(
    admin_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        distribution_router,
        "fetch_pdf_bytes",
        lambda repo, path, **kwargs: _minimal_pdf_bytes(),
    )
    monkeypatch.setattr(
        distribution_router,
        "build_distribute_url",
        lambda request, repo, path: (
            "http://homework.local:8000/admin/distribute?"
            f"repo={repo}&path=unit2%2Fch04.pdf"
        ),
    )

    response = admin_client.get(
        f"/admin/distribute/prep/print-packet?repo={REPO}&path={PATH}",
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert "attachment" in response.headers.get("content-disposition", "")

    reader = PdfReader(BytesIO(response.content))
    assert len(reader.pages) == 2


def _grader_header_pdf(page_count: int = 2) -> bytes:
    document = fitz.open()
    try:
        for index in range(page_count):
            page = document.new_page(width=612, height=792)
            page.insert_text((43.2, 68.5), "Student Name:", fontsize=10, fontname="helv")
            page.insert_text((43.2, 83.3), "Student ID:", fontsize=10, fontname="helv")
            page.insert_text(
                (72, 200),
                f"Question body {index + 1}",
                fontsize=12,
                fontname="helv",
            )
        return document.tobytes()
    finally:
        document.close()


def _period_one_student_id(db_conn) -> int:
    row = db_conn.execute(
        """
        SELECT s.id
        FROM students s
        JOIN student_class_periods scp ON scp.student_id = s.id
        WHERE scp.period = 1 AND scp.active = 1
        """
    ).fetchone()
    assert row is not None
    return int(row["id"])


def test_named_copies_requires_admin_login(
    client: TestClient, github_settings: None
) -> None:
    response = client.get(
        f"/admin/distribute/prep/named-copies?repo={REPO}&path={PATH}",
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/admin/login")


def test_named_copies_page_lists_period_one_roster(
    admin_client: TestClient,
) -> None:
    response = admin_client.get(
        f"/admin/distribute/prep/named-copies?repo={REPO}&path={PATH}"
    )
    assert response.status_code == 200
    assert 'src="/static/htmx.min.js"' in response.text
    assert "unpkg.com/htmx" not in response.text
    assert "Print with student names" in response.text
    assert "Test Student A" in response.text
    assert "Period 1" in response.text
    assert "Download named copies" in response.text
    assert "install QR cover" in response.text


def test_named_copies_roster_partial(
    admin_client: TestClient,
) -> None:
    response = admin_client.get(
        "/admin/distribute/prep/named-copies/roster?period=1"
    )
    assert response.status_code == 200
    assert "Test Student A" in response.text
    assert "<html" not in response.text.lower()


def test_named_copies_download(
    admin_client: TestClient,
    db_conn,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    student_id = _period_one_student_id(db_conn)
    monkeypatch.setattr(
        distribution_router,
        "fetch_pdf_bytes",
        lambda repo, path, **kwargs: _grader_header_pdf(page_count=2),
    )
    monkeypatch.setattr(
        distribution_router,
        "build_distribute_url",
        lambda request, repo, path: (
            "http://homework.local:8000/admin/distribute?"
            f"repo={repo}&path=unit2%2Fch04.pdf"
        ),
    )

    response = admin_client.post(
        "/admin/distribute/prep/named-copies",
        data={
            "repo": REPO,
            "path": PATH,
            "period": "1",
            "student_ids": str(student_id),
        },
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    disposition = response.headers.get("content-disposition", "")
    assert "attachment" in disposition
    assert "period-1-named.pdf" in disposition

    document = fitz.open(stream=response.content, filetype="pdf")
    try:
        assert document.page_count == 5
        cover = document[0].get_text()
        assert "Install QR" in cover
        assert "Test Student A" not in cover
        assert "Test Student A" in document[1].get_text()
        assert "Test Student A" in document[2].get_text()
        assert "Test Student A" not in document[3].get_text()
        assert "Student Name:" in document[3].get_text()
    finally:
        document.close()


def test_named_copies_requires_a_student(
    admin_client: TestClient,
) -> None:
    response = admin_client.post(
        "/admin/distribute/prep/named-copies",
        data={"repo": REPO, "path": PATH, "period": "1"},
    )
    assert response.status_code == 400
    assert "at least one student" in response.text