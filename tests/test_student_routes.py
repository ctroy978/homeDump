"""Student HTMX routes must return HTML, never a raw 500/422 JSON page."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.routers import student as student_router
from app.services.claims import ClaimError
from app.services.student_lookup import LOOKUP_FAILURE_MESSAGE


def test_sis_field_does_not_force_numeric_keyboard(client: TestClient) -> None:
    response = client.get("/student/sis-field", params={"period": "3"})
    assert response.status_code == 200
    assert 'name="sis_number"' in response.text
    assert "inputmode" not in response.text


def test_lookup_unknown_sis_returns_html(client: TestClient) -> None:
    response = client.post(
        "/student/lookup",
        data={"period": "0", "sis_number": "missing"},
    )
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "matching makeup homework" in response.text
    assert "Traceback" not in response.text


def test_lookup_unexpected_error_returns_html_not_500(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*args, **kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(student_router, "list_eligible_dates_by_sis", boom)
    response = client.post(
        "/student/lookup",
        data={"period": "0", "sis_number": "10001"},
    )
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert student_router.STUDENT_UNEXPECTED_MESSAGE in response.text
    assert "database is locked" not in response.text
    assert "Traceback" not in response.text


def test_assignments_unexpected_error_returns_html(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(student_router, "list_eligible_assignments_by_sis", boom)
    response = client.post(
        "/student/assignments",
        data={"period": "0", "sis_number": "10001", "date": "2025-09-29"},
    )
    assert response.status_code == 200
    assert student_router.STUDENT_UNEXPECTED_MESSAGE in response.text


def test_confirm_claim_error_stays_html(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        student_router,
        "resolve_public_base_url",
        lambda request: "http://classroom.test:8000",
    )

    def fail_claim(*args, **kwargs):
        raise ClaimError("Absence code is not allowable: Unexcused Absence")

    monkeypatch.setattr(student_router, "process_claim", fail_claim)
    response = client.post(
        "/student/confirm",
        data={
            "assignment_id": "1",
            "period": "0",
            "sis_number": "10001",
            "date": "2025-09-29",
        },
    )
    assert response.status_code == 200
    assert "Absence code is not allowable" in response.text
    assert "Traceback" not in response.text


def test_confirm_unexpected_error_returns_html_not_500(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        student_router,
        "resolve_public_base_url",
        lambda request: "http://classroom.test:8000",
    )

    def boom(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(student_router, "process_claim", boom)
    response = client.post(
        "/student/confirm",
        data={
            "assignment_id": "1",
            "period": "0",
            "sis_number": "10001",
            "date": "2025-09-29",
        },
    )
    assert response.status_code == 200
    assert student_router.STUDENT_UNEXPECTED_MESSAGE in response.text
    assert "disk full" not in response.text


def test_student_validation_error_is_html_not_json(client: TestClient) -> None:
    response = client.post(
        "/student/lookup",
        data={"period": "99", "sis_number": "10001"},
    )
    assert response.status_code == 200
    assert "application/json" not in response.headers["content-type"]
    assert "Check your period and student ID" in response.text


def test_admin_eligibility_requires_login(client: TestClient) -> None:
    response = client.get("/admin/eligibility", follow_redirects=False)
    assert response.status_code == 303
    assert "/admin/login" in response.headers["location"]


def test_admin_eligibility_explains_unexcused(
    client: TestClient,
) -> None:
    from app.dependencies import ADMIN_COOKIE_NAME, _expected_admin_token

    client.cookies.set(ADMIN_COOKIE_NAME, _expected_admin_token())
    response = client.get(
        "/admin/eligibility",
        params={"sis_number": "10001", "period": "3", "date": "2025-09-02"},
    )
    assert response.status_code == 200
    assert "not allowable" in response.text
    assert "Students only see a generic message" in response.text


def test_non_student_validation_still_returns_json(client: TestClient) -> None:
    response = client.post("/admin/login", data={})
    assert response.status_code == 422
    assert "application/json" in response.headers["content-type"]
