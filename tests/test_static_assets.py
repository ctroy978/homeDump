"""Classroom pages must serve HTMX locally — Chromebooks may have no CDN access."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import settings

HTMX_SRC = 'src="/static/htmx.min.js"'
CDN_HTMX = "unpkg.com/htmx"


def test_htmx_is_vendored_in_static() -> None:
    path = settings.project_root / "static" / "htmx.min.js"
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert "htmx" in text
    assert path.stat().st_size > 10_000


def test_htmx_static_file_is_served(client: TestClient) -> None:
    response = client.get("/static/htmx.min.js")
    assert response.status_code == 200
    assert "htmx" in response.text
    assert CDN_HTMX not in response.text


def test_student_home_uses_local_htmx(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert HTMX_SRC in response.text
    assert CDN_HTMX not in response.text


def test_templates_do_not_load_htmx_from_cdn() -> None:
    root = settings.project_root / "templates"
    offenders = [
        str(path.relative_to(settings.project_root))
        for path in root.rglob("*.html")
        if CDN_HTMX in path.read_text(encoding="utf-8")
    ]
    assert offenders == []
