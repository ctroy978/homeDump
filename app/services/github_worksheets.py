"""Read-only GitHub API client for scope_* worksheet repositories."""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import httpx

from app.config import settings

GITHUB_API_BASE = "https://api.github.com"
GITHUB_API_VERSION = "2022-11-28"
CONTENTS_BASE64_LIMIT = 1_000_000
TREE_CACHE_TTL_SECONDS = 60

_repo_tree_cache: dict[tuple[str, str], tuple[float, list[dict[str, object]]]] = {}


def clear_repo_tree_cache() -> None:
    """Clear cached repository trees (used in tests)."""
    _repo_tree_cache.clear()


class GitHubWorksheetError(Exception):
    """Teacher-friendly wrapper for GitHub API failures."""


@dataclass(frozen=True)
class RepoInfo:
    """A filtered worksheet repository under the configured owner."""

    name: str
    full_name: str


@dataclass(frozen=True)
class WorksheetEntry:
    """A PDF worksheet discovered in a repository tree."""

    path: str
    name: str
    display_title: str
    size_bytes: int | None


@dataclass(frozen=True)
class WorksheetDirEntry:
    """An immediate subdirectory that contains at least one PDF."""

    name: str
    path: str


@dataclass(frozen=True)
class WorksheetBrowseResult:
    """GitHub-style directory view of PDF worksheets in a repo."""

    current_path: str
    directories: list[WorksheetDirEntry]
    files: list[WorksheetEntry]
    breadcrumbs: list[tuple[str, str]]
    search_active: bool


def display_title_from_path(path: str) -> str:
    stem = Path(path).name.removesuffix(".pdf").removesuffix(".PDF")
    return stem.replace("_", " ").replace("-", " ").strip() or path


def validate_repo_name(repo: str) -> None:
    if not repo or ".." in repo or "/" in repo:
        raise ValueError("Invalid repo name.")


def validate_worksheet_locator(repo: str, path: str) -> None:
    """Syntax checks for repo and path before allowlist lookup."""
    validate_repo_name(repo)
    if not path or ".." in path or path.startswith("/"):
        raise ValueError("Invalid worksheet path.")
    if not path.lower().endswith(".pdf"):
        raise ValueError("Only PDF worksheets are supported.")


def assert_repo_allowed(repo: str, allowed: list[RepoInfo]) -> None:
    if repo not in {item.name for item in allowed}:
        raise GitHubWorksheetError(f"Repo '{repo}' is not an allowed worksheet repo.")


def _api_headers(token: str | None = None) -> dict[str, str]:
    resolved = token or settings.github_token
    if not resolved:
        raise GitHubWorksheetError("GitHub integration is not configured.")
    return {
        "Authorization": f"Bearer {resolved}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
    }


def _parse_next_link(link_header: str | None) -> str | None:
    """Extract the GitHub REST API pagination URL from a Link response header."""
    if not link_header:
        return None
    for part in link_header.split(","):
        section = part.strip()
        if 'rel="next"' not in section:
            continue
        url = section.split(";", 1)[0].strip()
        if url.startswith("<") and url.endswith(">"):
            return url[1:-1]
    return None


def _download_url_bytes(
    download_url: str,
    *,
    token: str | None = None,
    transport: httpx.BaseTransport | None = None,
    timeout: float = 60.0,
) -> bytes:
    """Stream a worksheet PDF from a GitHub contents download_url."""
    with httpx.Client(
        headers=_api_headers(token),
        timeout=timeout,
        follow_redirects=True,
        transport=transport,
    ) as client:
        with client.stream("GET", download_url) as response:
            if response.status_code >= 400:
                raise GitHubWorksheetError(
                    "Failed to download worksheet PDF from GitHub."
                )
            return b"".join(response.iter_bytes())


def _github_request(
    method: str,
    path: str,
    *,
    token: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> httpx.Response:
    url = f"{GITHUB_API_BASE}{path}"
    with httpx.Client(
        headers=_api_headers(token),
        timeout=30.0,
        transport=transport,
    ) as client:
        response = client.request(method, url)
    if response.status_code == 401:
        raise GitHubWorksheetError("GitHub authentication failed. Check GITHUB_TOKEN.")
    if response.status_code == 403:
        raise GitHubWorksheetError("GitHub access denied for this token.")
    if response.status_code == 404:
        raise GitHubWorksheetError("Requested GitHub resource was not found.")
    if response.status_code >= 400:
        raise GitHubWorksheetError(
            f"GitHub request failed with status {response.status_code}."
        )
    return response


def _list_owner_repos(
    owner: str,
    *,
    token: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> list[dict[str, object]]:
    query = "?per_page=100&type=all"
    with httpx.Client(
        headers=_api_headers(token),
        timeout=30.0,
        transport=transport,
    ) as client:
        for endpoint in (f"/orgs/{owner}/repos", f"/users/{owner}/repos"):
            url: str | None = f"{GITHUB_API_BASE}{endpoint}{query}"
            repos: list[dict[str, object]] = []
            while url:
                response = client.get(url)
                if response.status_code == 404 and endpoint.startswith("/orgs/"):
                    break
                if response.status_code == 401:
                    raise GitHubWorksheetError(
                        "GitHub authentication failed. Check GITHUB_TOKEN."
                    )
                if response.status_code == 403:
                    raise GitHubWorksheetError("GitHub access denied for this token.")
                if response.status_code >= 400:
                    raise GitHubWorksheetError(
                        f"GitHub request failed with status {response.status_code}."
                    )
                payload = response.json()
                if not isinstance(payload, list):
                    raise GitHubWorksheetError("Unexpected GitHub repository list.")
                repos.extend(payload)
                url = _parse_next_link(response.headers.get("Link"))
            if repos:
                return repos
    raise GitHubWorksheetError(f"No repositories found for owner '{owner}'.")


def _list_authenticated_repos(
    owner: str,
    *,
    token: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> list[dict[str, object]]:
    """
    Return repos visible to the authenticated token.

    Fine-grained PATs with access to selected private repos often appear here
    but not on the public /users/{owner}/repos listing.
    """
    query = "?per_page=100&affiliation=owner,collaborator,organization_member"
    url: str | None = f"{GITHUB_API_BASE}/user/repos{query}"
    repos: list[dict[str, object]] = []
    with httpx.Client(
        headers=_api_headers(token),
        timeout=30.0,
        transport=transport,
    ) as client:
        while url:
            response = client.get(url)
            if response.status_code == 401:
                raise GitHubWorksheetError(
                    "GitHub authentication failed. Check GITHUB_TOKEN."
                )
            if response.status_code == 403:
                raise GitHubWorksheetError("GitHub access denied for this token.")
            if response.status_code >= 400:
                raise GitHubWorksheetError(
                    f"GitHub request failed with status {response.status_code}."
                )
            payload = response.json()
            if not isinstance(payload, list):
                raise GitHubWorksheetError("Unexpected GitHub repository list.")
            for item in payload:
                if not isinstance(item, dict):
                    continue
                owner_obj = item.get("owner")
                if (
                    isinstance(owner_obj, dict)
                    and str(owner_obj.get("login", "")) == owner
                ):
                    repos.append(item)
            url = _parse_next_link(response.headers.get("Link"))
    return repos


def _filter_repo_infos(
    repos: list[dict[str, object]],
    *,
    owner: str,
    repo_filter: str,
) -> list[RepoInfo]:
    filtered: list[RepoInfo] = []
    for item in repos:
        name = str(item.get("name", ""))
        full_name = str(item.get("full_name", f"{owner}/{name}"))
        if repo_filter in name:
            filtered.append(RepoInfo(name=name, full_name=full_name))
    filtered.sort(key=lambda repo: repo.name)
    return filtered


def list_filtered_repos(
    *,
    owner: str | None = None,
    repo_filter: str | None = None,
    token: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> list[RepoInfo]:
    """Return repos whose names contain the configured filter substring."""
    resolved_owner = owner or settings.github_owner
    resolved_filter = repo_filter if repo_filter is not None else settings.github_repo_filter
    try:
        owner_repos = _list_owner_repos(
            resolved_owner,
            token=token,
            transport=transport,
        )
    except GitHubWorksheetError:
        owner_repos = []

    filtered = _filter_repo_infos(
        owner_repos,
        owner=resolved_owner,
        repo_filter=resolved_filter,
    )

    authenticated_repos = _list_authenticated_repos(
        resolved_owner,
        token=token,
        transport=transport,
    )
    filtered.extend(
        _filter_repo_infos(
            authenticated_repos,
            owner=resolved_owner,
            repo_filter=resolved_filter,
        )
    )

    unique_by_name = {repo.name: repo for repo in filtered}
    return sorted(unique_by_name.values(), key=lambda repo: repo.name)


def validate_browse_path(path: str | None) -> str:
    """Normalize and validate a repo-relative directory path for browsing."""
    if not path or not path.strip():
        return ""
    cleaned = path.strip().strip("/")
    if not cleaned:
        return ""
    for segment in cleaned.split("/"):
        if not segment or segment in {".", ".."}:
            raise ValueError("Invalid browse path.")
    return cleaned


def _fetch_repo_tree(
    repo: str,
    *,
    owner: str | None = None,
    token: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> list[dict[str, object]]:
    validate_repo_name(repo)
    resolved_owner = owner or settings.github_owner
    cache_key = (resolved_owner, repo)
    if transport is None:
        cached = _repo_tree_cache.get(cache_key)
        if cached is not None:
            cached_at, tree = cached
            if time.monotonic() - cached_at < TREE_CACHE_TTL_SECONDS:
                return tree

    response = _github_request(
        "GET",
        f"/repos/{resolved_owner}/{repo}/git/trees/main?recursive=1",
        token=token,
        transport=transport,
    )
    payload = response.json()
    tree = payload.get("tree", [])
    if not isinstance(tree, list):
        raise GitHubWorksheetError("Unexpected GitHub tree response.")
    normalized = [item for item in tree if isinstance(item, dict)]
    if transport is None:
        _repo_tree_cache[cache_key] = (time.monotonic(), normalized)
    return normalized


def _worksheet_entries_from_tree(tree: list[dict[str, object]]) -> list[WorksheetEntry]:
    entries: list[WorksheetEntry] = []
    for item in tree:
        if item.get("type") != "blob":
            continue
        path = str(item.get("path", ""))
        if not path.lower().endswith(".pdf"):
            continue
        size = item.get("size")
        entries.append(
            WorksheetEntry(
                path=path,
                name=Path(path).name,
                display_title=display_title_from_path(path),
                size_bytes=int(size) if size is not None else None,
            )
        )
    entries.sort(key=lambda entry: entry.path)
    return entries


def _filter_worksheet_entries(
    entries: list[WorksheetEntry],
    query: str | None,
) -> list[WorksheetEntry]:
    normalized_query = (query or "").strip().lower()
    if not normalized_query:
        return entries
    filtered: list[WorksheetEntry] = []
    for entry in entries:
        if normalized_query in entry.path.lower():
            filtered.append(entry)
            continue
        if normalized_query in entry.display_title.lower():
            filtered.append(entry)
    return filtered


def _browse_breadcrumbs(current_path: str) -> list[tuple[str, str]]:
    if not current_path:
        return []
    crumbs: list[tuple[str, str]] = []
    accumulated = ""
    for segment in current_path.split("/"):
        accumulated = f"{accumulated}/{segment}".strip("/")
        crumbs.append((segment, accumulated))
    return crumbs


def browse_pdf_worksheets(
    repo: str,
    *,
    path: str | None = None,
    query: str | None = None,
    owner: str | None = None,
    token: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> WorksheetBrowseResult:
    """
    Browse PDF worksheets in a GitHub-style folder view.

    When a search query is set, returns matching PDFs across the whole repo.
    Otherwise lists immediate subdirectories and PDFs in the current folder.
    """
    current_path = validate_browse_path(path)
    tree = _fetch_repo_tree(
        repo,
        owner=owner,
        token=token,
        transport=transport,
    )
    all_entries = _worksheet_entries_from_tree(tree)
    normalized_query = (query or "").strip()
    if normalized_query:
        return WorksheetBrowseResult(
            current_path=current_path,
            directories=[],
            files=_filter_worksheet_entries(all_entries, normalized_query),
            breadcrumbs=_browse_breadcrumbs(""),
            search_active=True,
        )

    prefix = f"{current_path}/" if current_path else ""
    directories: dict[str, WorksheetDirEntry] = {}
    files: list[WorksheetEntry] = []
    for entry in all_entries:
        if prefix and not entry.path.startswith(prefix):
            continue
        remainder = entry.path[len(prefix) :] if prefix else entry.path
        if not remainder:
            continue
        if "/" in remainder:
            directory_name = remainder.split("/", 1)[0]
            directory_path = (
                f"{current_path}/{directory_name}".strip("/")
                if current_path
                else directory_name
            )
            directories[directory_name] = WorksheetDirEntry(
                name=directory_name,
                path=directory_path,
            )
            continue
        files.append(entry)

    return WorksheetBrowseResult(
        current_path=current_path,
        directories=sorted(directories.values(), key=lambda item: item.name),
        files=files,
        breadcrumbs=_browse_breadcrumbs(current_path),
        search_active=False,
    )


def list_pdf_worksheets(
    repo: str,
    *,
    query: str | None = None,
    owner: str | None = None,
    token: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> list[WorksheetEntry]:
    """List PDF blobs from the repository's main branch tree."""
    tree = _fetch_repo_tree(
        repo,
        owner=owner,
        token=token,
        transport=transport,
    )
    return _filter_worksheet_entries(_worksheet_entries_from_tree(tree), query)


def _decode_contents_payload(
    payload: dict[str, object],
    *,
    token: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> bytes:
    if payload.get("encoding") == "base64" and payload.get("content"):
        encoded = str(payload["content"]).replace("\n", "")
        return base64.b64decode(encoded)

    download_url = payload.get("download_url")
    if download_url:
        return _download_url_bytes(
            str(download_url),
            token=token,
            transport=transport,
        )

    raise GitHubWorksheetError("GitHub did not return worksheet PDF content.")


def fetch_pdf_bytes(
    repo: str,
    path: str,
    *,
    owner: str | None = None,
    token: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> bytes:
    """Fetch worksheet PDF bytes from main at the given repo-relative path."""
    validate_worksheet_locator(repo, path)
    resolved_owner = owner or settings.github_owner
    encoded_path = quote(path, safe="/")
    response = _github_request(
        "GET",
        f"/repos/{resolved_owner}/{repo}/contents/{encoded_path}?ref=main",
        token=token,
        transport=transport,
    )
    payload = response.json()
    if not isinstance(payload, dict):
        raise GitHubWorksheetError("Unexpected GitHub contents response.")

    size = payload.get("size")
    if isinstance(size, int) and size > CONTENTS_BASE64_LIMIT:
        download_url = payload.get("download_url")
        if not download_url:
            raise GitHubWorksheetError(
                "Worksheet PDF is too large and has no download URL."
            )
        return _download_url_bytes(
            str(download_url),
            token=token,
            transport=transport,
        )

    return _decode_contents_payload(
        payload,
        token=token,
        transport=transport,
    )


def periods_to_json(periods: list[int]) -> str:
    """Serialize period lists for distribution_events columns."""
    return json.dumps(sorted(periods))