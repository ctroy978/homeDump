"""Tests for GitHub worksheet API client."""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from app.database import init_schema
from app.services import github_worksheets as gh


def _transport(handlers: dict[tuple[str, str], httpx.Response]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        key = (request.method, str(request.url))
        if key not in handlers:
            raise AssertionError(f"Unexpected request: {key}")
        return handlers[key]

    return httpx.MockTransport(handler)


def test_display_title_from_path_uses_parent_folder_and_pdf_stem() -> None:
    assert gh.display_title_from_path("unit2/ch04_practice.pdf") == "unit2-ch04-practice"
    assert gh.display_title_from_path("unit1/lesson2/worksheet.PDF") == "lesson2-worksheet"
    assert gh.display_title_from_path("worksheet.pdf") == "worksheet"


@pytest.mark.parametrize(
    ("repo", "path", "message"),
    [
        ("", "a.pdf", "Invalid repo name"),
        ("scope_tenth", "../secret.pdf", "Invalid worksheet path"),
        ("scope_tenth", "notes.txt", "Only PDF worksheets"),
    ],
)
def test_validate_worksheet_locator_rejects_invalid(
    repo: str,
    path: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        gh.validate_worksheet_locator(repo, path)


def test_assert_repo_allowed() -> None:
    allowed = [gh.RepoInfo(name="scope_tenth", full_name="krewten-978/scope_tenth")]
    gh.assert_repo_allowed("scope_tenth", allowed)
    with pytest.raises(gh.GitHubWorksheetError, match="not an allowed"):
        gh.assert_repo_allowed("other_repo", allowed)


def test_parse_next_link() -> None:
    header = (
        '<https://api.github.com/orgs/acme/repos?per_page=100&page=2>; rel="next", '
        '<https://api.github.com/orgs/acme/repos?per_page=100&page=5>; rel="last"'
    )
    assert (
        gh._parse_next_link(header)
        == "https://api.github.com/orgs/acme/repos?per_page=100&page=2"
    )
    assert gh._parse_next_link(None) is None


def test_list_filtered_repos_uses_org_endpoint_when_available() -> None:
    transport = _transport(
        {
            (
                "GET",
                "https://api.github.com/orgs/krewten-978/repos?per_page=100&type=all",
            ): httpx.Response(
                200,
                json=[
                    {"name": "scope_ninth", "full_name": "krewten-978/scope_ninth"},
                    {"name": "notes", "full_name": "krewten-978/notes"},
                ],
            ),
            (
                "GET",
                "https://api.github.com/user/repos?per_page=100"
                "&affiliation=owner,collaborator,organization_member",
            ): httpx.Response(200, json=[]),
        }
    )

    repos = gh.list_filtered_repos(
        owner="krewten-978",
        repo_filter="scope",
        token="test-token",
        transport=transport,
    )
    assert [repo.name for repo in repos] == ["scope_ninth"]


def test_list_filtered_repos_uses_org_then_user_fallback() -> None:
    transport = _transport(
        {
            (
                "GET",
                "https://api.github.com/orgs/krewten-978/repos?per_page=100&type=all",
            ): httpx.Response(404),
            (
                "GET",
                "https://api.github.com/users/krewten-978/repos?per_page=100&type=all",
            ): httpx.Response(
                200,
                json=[
                    {"name": "scope_tenth", "full_name": "krewten-978/scope_tenth"},
                    {"name": "other", "full_name": "krewten-978/other"},
                ],
            ),
            (
                "GET",
                "https://api.github.com/user/repos?per_page=100"
                "&affiliation=owner,collaborator,organization_member",
            ): httpx.Response(200, json=[]),
        }
    )

    repos = gh.list_filtered_repos(
        owner="krewten-978",
        repo_filter="scope",
        token="test-token",
        transport=transport,
    )
    assert [repo.name for repo in repos] == ["scope_tenth"]


def test_list_filtered_repos_falls_back_to_authenticated_user_repos() -> None:
    transport = _transport(
        {
            (
                "GET",
                "https://api.github.com/orgs/krewten-978/repos?per_page=100&type=all",
            ): httpx.Response(404),
            (
                "GET",
                "https://api.github.com/users/krewten-978/repos?per_page=100&type=all",
            ): httpx.Response(
                200,
                json=[
                    {"name": "congo", "full_name": "krewten-978/congo"},
                ],
            ),
            (
                "GET",
                "https://api.github.com/user/repos?per_page=100"
                "&affiliation=owner,collaborator,organization_member",
            ): httpx.Response(
                200,
                json=[
                    {
                        "name": "scope_tenth",
                        "full_name": "krewten-978/scope_tenth",
                        "owner": {"login": "krewten-978"},
                    },
                    {
                        "name": "scope_other",
                        "full_name": "other-user/scope_other",
                        "owner": {"login": "other-user"},
                    },
                ],
            ),
        }
    )

    repos = gh.list_filtered_repos(
        owner="krewten-978",
        repo_filter="scope",
        token="test-token",
        transport=transport,
    )
    assert [repo.name for repo in repos] == ["scope_tenth"]


def test_list_filtered_repos_merges_owner_and_authenticated_repos() -> None:
    transport = _transport(
        {
            (
                "GET",
                "https://api.github.com/orgs/krewten-978/repos?per_page=100&type=all",
            ): httpx.Response(404),
            (
                "GET",
                "https://api.github.com/users/krewten-978/repos?per_page=100&type=all",
            ): httpx.Response(
                200,
                json=[
                    {"name": "scope_wr121", "full_name": "krewten-978/scope_wr121"},
                ],
            ),
            (
                "GET",
                "https://api.github.com/user/repos?per_page=100"
                "&affiliation=owner,collaborator,organization_member",
            ): httpx.Response(
                200,
                json=[
                    {
                        "name": "scope_tenth",
                        "full_name": "krewten-978/scope_tenth",
                        "owner": {"login": "krewten-978"},
                    },
                    {
                        "name": "scope_twelfth",
                        "full_name": "krewten-978/scope_twelfth",
                        "owner": {"login": "krewten-978"},
                    },
                    {
                        "name": "scope_wr121",
                        "full_name": "krewten-978/scope_wr121",
                        "owner": {"login": "krewten-978"},
                    },
                ],
            ),
        }
    )

    repos = gh.list_filtered_repos(
        owner="krewten-978",
        repo_filter="scope",
        token="test-token",
        transport=transport,
    )
    assert [repo.name for repo in repos] == [
        "scope_tenth",
        "scope_twelfth",
        "scope_wr121",
    ]

def test_fetch_repo_tree_uses_short_lived_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gh.clear_repo_tree_cache()
    calls = 0
    tree_payload = {
        "tree": [
            {"type": "blob", "path": "unit1/intro.pdf", "size": 1200},
        ]
    }

    def counting_request(
        method: str,
        path: str,
        *,
        token: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=tree_payload)

    monkeypatch.setattr(gh, "_github_request", counting_request)

    gh.browse_pdf_worksheets("scope_tenth", path="")
    gh.browse_pdf_worksheets("scope_tenth", path="unit1")
    assert calls == 1

    gh.clear_repo_tree_cache()


def test_browse_pdf_worksheets_lists_root_directories_and_files() -> None:
    tree_payload = {
        "tree": [
            {"type": "blob", "path": "root.pdf", "size": 100},
            {"type": "blob", "path": "unit1/intro.pdf", "size": 1200},
            {"type": "blob", "path": "unit2/ch04.pdf", "size": 2400},
            {"type": "blob", "path": "unit2/notes.txt", "size": 50},
        ]
    }
    transport = _transport(
        {
            (
                "GET",
                "https://api.github.com/repos/krewten-978/scope_tenth/git/trees/main?recursive=1",
            ): httpx.Response(200, json=tree_payload),
        }
    )

    browse = gh.browse_pdf_worksheets(
        "scope_tenth",
        token="test-token",
        transport=transport,
    )
    assert browse.current_path == ""
    assert [directory.name for directory in browse.directories] == ["unit1", "unit2"]
    assert [worksheet.name for worksheet in browse.files] == ["root.pdf"]


def test_browse_pdf_worksheets_drills_into_subdirectory() -> None:
    tree_payload = {
        "tree": [
            {"type": "blob", "path": "unit2/ch04.pdf", "size": 2400},
            {"type": "blob", "path": "unit2/extra/guide.pdf", "size": 900},
        ]
    }
    transport = _transport(
        {
            (
                "GET",
                "https://api.github.com/repos/krewten-978/scope_tenth/git/trees/main?recursive=1",
            ): httpx.Response(200, json=tree_payload),
        }
    )

    browse = gh.browse_pdf_worksheets(
        "scope_tenth",
        path="unit2",
        token="test-token",
        transport=transport,
    )
    assert browse.current_path == "unit2"
    assert [directory.name for directory in browse.directories] == ["extra"]
    assert [worksheet.name for worksheet in browse.files] == ["ch04.pdf"]
    assert browse.breadcrumbs == [("unit2", "unit2")]


def test_browse_pdf_worksheets_search_returns_flat_matches() -> None:
    tree_payload = {
        "tree": [
            {"type": "blob", "path": "unit1/intro.pdf", "size": 1200},
            {"type": "blob", "path": "unit2/ch04.pdf", "size": 2400},
        ]
    }
    transport = _transport(
        {
            (
                "GET",
                "https://api.github.com/repos/krewten-978/scope_tenth/git/trees/main?recursive=1",
            ): httpx.Response(200, json=tree_payload),
        }
    )

    browse = gh.browse_pdf_worksheets(
        "scope_tenth",
        query="ch04",
        token="test-token",
        transport=transport,
    )
    assert browse.search_active is True
    assert browse.directories == []
    assert [worksheet.path for worksheet in browse.files] == ["unit2/ch04.pdf"]


def test_validate_browse_path_rejects_traversal() -> None:
    with pytest.raises(ValueError, match="Invalid browse path"):
        gh.validate_browse_path("unit2/../secret")


def test_list_pdf_worksheets_filters_and_searches() -> None:
    tree_payload = {
        "tree": [
            {"type": "blob", "path": "unit1/intro.pdf", "size": 1200},
            {"type": "blob", "path": "unit1/notes.txt", "size": 50},
            {"type": "tree", "path": "unit2"},
            {"type": "blob", "path": "unit2/ch04.pdf", "size": 2400},
        ]
    }
    transport = _transport(
        {
            (
                "GET",
                "https://api.github.com/repos/krewten-978/scope_tenth/git/trees/main?recursive=1",
            ): httpx.Response(200, json=tree_payload),
        }
    )

    all_pdfs = gh.list_pdf_worksheets(
        "scope_tenth",
        token="test-token",
        transport=transport,
    )
    assert [entry.path for entry in all_pdfs] == ["unit1/intro.pdf", "unit2/ch04.pdf"]

    transport = _transport(
        {
            (
                "GET",
                "https://api.github.com/repos/krewten-978/scope_tenth/git/trees/main?recursive=1",
            ): httpx.Response(200, json=tree_payload),
        }
    )
    filtered = gh.list_pdf_worksheets(
        "scope_tenth",
        query="ch04",
        token="test-token",
        transport=transport,
    )
    assert [entry.path for entry in filtered] == ["unit2/ch04.pdf"]


def test_list_filtered_repos_follows_pagination() -> None:
    page_one = [
        {"name": f"scope_{index}", "full_name": f"krewten-978/scope_{index}"}
        for index in range(100)
    ]
    transport = _transport(
        {
            (
                "GET",
                "https://api.github.com/orgs/krewten-978/repos?per_page=100&type=all",
            ): httpx.Response(
                200,
                json=page_one,
                headers={
                    "Link": (
                        '<https://api.github.com/orgs/krewten-978/repos?per_page=100'
                        '&page=2&type=all>; rel="next"'
                    )
                },
            ),
            (
                "GET",
                "https://api.github.com/orgs/krewten-978/repos?per_page=100&page=2&type=all",
            ): httpx.Response(
                200,
                json=[
                    {
                        "name": "scope_extra",
                        "full_name": "krewten-978/scope_extra",
                    }
                ],
            ),
            (
                "GET",
                "https://api.github.com/user/repos?per_page=100"
                "&affiliation=owner,collaborator,organization_member",
            ): httpx.Response(200, json=[]),
        }
    )

    repos = gh.list_filtered_repos(
        owner="krewten-978",
        repo_filter="scope",
        token="test-token",
        transport=transport,
    )
    assert len(repos) == 101
    assert repos[-1].name == "scope_extra"


def test_fetch_pdf_bytes_decodes_base64_contents() -> None:
    pdf_bytes = b"%PDF-1.4 test"
    encoded = base64.b64encode(pdf_bytes).decode("ascii")
    transport = _transport(
        {
            (
                "GET",
                "https://api.github.com/repos/krewten-978/scope_tenth/contents/unit2/ch04.pdf?ref=main",
            ): httpx.Response(
                200,
                json={
                    "encoding": "base64",
                    "content": encoded,
                    "size": len(pdf_bytes),
                },
            ),
        }
    )

    result = gh.fetch_pdf_bytes(
        "scope_tenth",
        "unit2/ch04.pdf",
        token="test-token",
        transport=transport,
    )
    assert result == pdf_bytes


def test_fetch_pdf_bytes_follows_download_url_for_large_files() -> None:
    pdf_bytes = b"%PDF-1.4 large worksheet"
    download_url = "https://raw.githubusercontent.com/krewten-978/scope_tenth/main/unit2/big.pdf"
    transport = _transport(
        {
            (
                "GET",
                "https://api.github.com/repos/krewten-978/scope_tenth/contents/unit2/big.pdf?ref=main",
            ): httpx.Response(
                200,
                json={
                    "size": gh.CONTENTS_BASE64_LIMIT + 1,
                    "download_url": download_url,
                },
            ),
            ("GET", download_url): httpx.Response(200, content=pdf_bytes),
        }
    )

    result = gh.fetch_pdf_bytes(
        "scope_tenth",
        "unit2/big.pdf",
        token="test-token",
        transport=transport,
    )
    assert result == pdf_bytes


def test_fetch_pdf_bytes_uses_download_url_when_contents_omitted() -> None:
    pdf_bytes = b"%PDF-1.4 streamed worksheet"
    download_url = "https://raw.githubusercontent.com/krewten-978/scope_tenth/main/unit2/ch04.pdf"
    transport = _transport(
        {
            (
                "GET",
                "https://api.github.com/repos/krewten-978/scope_tenth/contents/unit2/ch04.pdf?ref=main",
            ): httpx.Response(
                200,
                json={
                    "size": len(pdf_bytes),
                    "download_url": download_url,
                },
            ),
            ("GET", download_url): httpx.Response(200, content=pdf_bytes),
        }
    )

    result = gh.fetch_pdf_bytes(
        "scope_tenth",
        "unit2/ch04.pdf",
        token="test-token",
        transport=transport,
    )
    assert result == pdf_bytes


def test_fetch_pdf_bytes_maps_404_to_worksheet_error() -> None:
    transport = _transport(
        {
            (
                "GET",
                "https://api.github.com/repos/krewten-978/scope_tenth/contents/missing.pdf?ref=main",
            ): httpx.Response(404),
        }
    )

    with pytest.raises(gh.GitHubWorksheetError, match="not found"):
        gh.fetch_pdf_bytes(
            "scope_tenth",
            "missing.pdf",
            token="test-token",
            transport=transport,
        )


def test_periods_to_json() -> None:
    assert gh.periods_to_json([3, 1, 5]) == json.dumps([1, 3, 5])


def test_init_schema_creates_distribution_tables(db_conn) -> None:
    tables = {
        row[0]
        for row in db_conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    assert "distribution_events" in tables

    columns = {
        row[1]
        for row in db_conn.execute("PRAGMA table_info(assignments)").fetchall()
    }
    assert {"source", "github_repo", "github_path"}.issubset(columns)


def test_fresh_database_migration_adds_phase8_columns(tmp_path) -> None:
    import sqlite3

    db_path = tmp_path / "phase8.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    init_schema(conn)

    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(assignments)").fetchall()
    }
    assert "source" in columns

    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    assert "distribution_events" in tables
    conn.close()