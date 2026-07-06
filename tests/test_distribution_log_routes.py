"""Route tests for the distribution audit log page."""

from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app import config
import app.dependencies as dependencies
import app.routers.distribution as distribution_router
from app.dependencies import ADMIN_COOKIE_NAME, _expected_admin_token

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


def test_distribution_log_requires_admin_login(
    client: TestClient,
    github_settings: None,
) -> None:
    response = client.get("/admin/distribution-log", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/admin/login")


def test_distribution_log_page_renders(
    admin_client: TestClient,
) -> None:
    response = admin_client.get("/admin/distribution-log")
    assert response.status_code == 200
    assert "Distribution log" in response.text
    assert 'name="repo"' in response.text
    assert 'name="outcome"' in response.text