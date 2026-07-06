"""Route tests for the distribution scan workflow."""

from __future__ import annotations

from dataclasses import replace
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient

from app import config
import app.dependencies as dependencies
import app.routers.distribution as distribution_router
from app.dependencies import SCAN_COOKIE_NAME
from app.services.distribution import DistributionResult

REPO = "scope_tenth"
PATH = "unit2/ch04.pdf"
DISTRIBUTE_URL = f"/admin/distribute?repo={REPO}&path={PATH}"


@pytest.fixture
def scan_settings(
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
def disabled_scan_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    test_settings = replace(
        config.settings,
        github_token=None,
        scan_pin=None,
    )
    monkeypatch.setattr(config, "settings", test_settings)
    monkeypatch.setattr(dependencies, "settings", test_settings)
    monkeypatch.setattr(distribution_router, "settings", test_settings)


def test_distribute_get_disabled_shows_config_error(
    client: TestClient,
    disabled_scan_settings: None,
) -> None:
    response = client.get(DISTRIBUTE_URL)
    assert response.status_code == 503
    assert "not configured" in response.text


def test_distribute_get_shows_pin_form_when_enabled(
    client: TestClient,
    scan_settings: None,
) -> None:
    response = client.get(DISTRIBUTE_URL)
    assert response.status_code == 200
    assert "Enter distribution PIN" in response.text
    assert "ch04" in response.text


def test_distribute_pin_rejects_incorrect_pin(
    client: TestClient,
    scan_settings: None,
) -> None:
    response = client.post(
        "/admin/distribute/pin",
        data={"pin": "0000", "repo": REPO, "path": PATH},
    )
    assert response.status_code == 401
    assert "Incorrect PIN" in response.text


def test_distribute_pin_accepts_correct_pin_and_sets_cookie(
    client: TestClient,
    scan_settings: None,
) -> None:
    response = client.post(
        "/admin/distribute/pin",
        data={"pin": "1234", "repo": REPO, "path": PATH},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/admin/distribute?")
    assert "repo=scope_tenth" in response.headers["location"]
    assert "path=unit2" in response.headers["location"]
    assert SCAN_COOKIE_NAME in response.cookies


def test_distribute_post_requires_pin_session(
    client: TestClient,
    scan_settings: None,
) -> None:
    response = client.post(
        "/admin/distribute",
        data={"repo": REPO, "path": PATH, "periods": "1"},
    )
    assert response.status_code == 401
    assert "Enter your PIN" in response.text


def test_distribute_post_registers_periods(
    client: TestClient,
    scan_settings: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[int]] = []

    def fake_register(db, *, github_repo, github_path, periods, client_ip, assigned_date=None):
        calls.append(periods)
        return DistributionResult(
            assignment_id=42,
            assigned_date="2025-09-10",
            display_title="ch04",
            periods_added=periods,
            periods_skipped=[],
            outcome="success",
        )

    monkeypatch.setattr(distribution_router, "register_distribution", fake_register)

    client.post(
        "/admin/distribute/pin",
        data={"pin": "1234", "repo": REPO, "path": PATH},
    )
    response = client.post(
        "/admin/distribute",
        content=urlencode(
            [
                ("repo", REPO),
                ("path", PATH),
                ("periods", "1"),
                ("periods", "3"),
            ]
        ),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    assert response.status_code == 200
    assert "Distribution registered" in response.text
    assert "Periods added: 1, 3" in response.text
    assert calls == [[1, 3]]


def test_distribute_post_shows_pdf_write_warning(
    client: TestClient,
    scan_settings: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        distribution_router,
        "register_distribution",
        lambda *args, **kwargs: DistributionResult(
            assignment_id=42,
            assigned_date="2025-09-10",
            display_title="ch04",
            periods_added=[1],
            periods_skipped=[],
            outcome="success",
            message="Assignment registered, but the worksheet PDF could not be saved.",
        ),
    )

    client.post(
        "/admin/distribute/pin",
        data={"pin": "1234", "repo": REPO, "path": PATH},
    )
    response = client.post(
        "/admin/distribute",
        data={"repo": REPO, "path": PATH, "periods": "1"},
    )

    assert response.status_code == 200
    assert "could not be saved" in response.text
    assert "Distribution log" in response.text


def test_distribute_post_requires_at_least_one_period(
    client: TestClient,
    scan_settings: None,
) -> None:
    client.post(
        "/admin/distribute/pin",
        data={"pin": "1234", "repo": REPO, "path": PATH},
    )
    response = client.post(
        "/admin/distribute",
        data={"repo": REPO, "path": PATH},
    )
    assert response.status_code == 400
    assert "Select at least one class period" in response.text


def test_distribute_get_rejects_invalid_path(
    client: TestClient,
    scan_settings: None,
) -> None:
    response = client.get(f"/admin/distribute?repo={REPO}&path=notes.txt")
    assert response.status_code == 400
    assert "Only PDF worksheets" in response.text