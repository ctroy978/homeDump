"""Admin class roster page: add or remove one student."""

from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient

from app.dependencies import ADMIN_COOKIE_NAME, _expected_admin_token
from app.services.attendance_parser import upsert_student
from app.services.student_roster import list_active_roster


def _login(client: TestClient) -> None:
    client.cookies.set(ADMIN_COOKIE_NAME, _expected_admin_token())


def _enroll(
    conn: sqlite3.Connection, name: str, sis: str, period: int, *, active: int = 1
) -> int:
    student_id = upsert_student(conn, name, "10", sis_number=sis)
    conn.execute(
        """
        INSERT INTO student_class_periods (student_id, period, active)
        VALUES (?, ?, ?)
        ON CONFLICT(student_id, period) DO UPDATE SET active = excluded.active
        """,
        (student_id, period, active),
    )
    conn.commit()
    return student_id


def test_roster_page_requires_login(client: TestClient) -> None:
    response = client.get("/admin/roster", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/admin/login")


def test_roster_page_shows_period_picker(client: TestClient) -> None:
    _login(client)
    response = client.get("/admin/roster")
    assert response.status_code == 200
    assert "Class roster" in response.text
    assert 'action="/admin/roster"' in response.text
    assert "Add a student" not in response.text
    assert "href=\"/admin/roster\"" in response.text


def test_roster_page_lists_period_with_perm_id(
    client: TestClient, db_conn: sqlite3.Connection
) -> None:
    _login(client)
    _enroll(db_conn, "Zebra, Ann", "20001", 6)
    _enroll(db_conn, "Able, Pat", "20002", 6)
    _enroll(db_conn, "Gone, Sam", "20003", 6, active=0)

    response = client.get("/admin/roster?period=6")
    assert response.status_code == 200
    assert "Able, Pat" in response.text
    assert "20002" in response.text
    assert "Zebra, Ann" in response.text
    assert "20001" in response.text
    assert "Gone, Sam" not in response.text
    assert "Add a student to period 6" in response.text
    assert "Remove" in response.text


def test_add_student_then_see_them_on_roster(
    client: TestClient, db_conn: sqlite3.Connection
) -> None:
    _login(client)
    _enroll(db_conn, "Able, Pat", "20002", 6)

    response = client.post(
        "/admin/roster/add",
        data={"period": "6", "sis_number": "20005", "name": "New, Kid"},
    )
    assert response.status_code == 200
    assert "Added New, Kid to period 6." in response.text
    assert "New, Kid" in response.text
    assert "20005" in response.text
    assert "Able, Pat" in response.text
    names = [student.name for student in list_active_roster(db_conn, 6)]
    assert names == ["Able, Pat", "New, Kid"]


def test_add_student_reactivates_and_warns_other_period(
    client: TestClient, db_conn: sqlite3.Connection
) -> None:
    _login(client)
    _enroll(db_conn, "Able, Pat", "20002", 6)
    _enroll(db_conn, "Able, Pat", "20002", 7, active=0)

    response = client.post(
        "/admin/roster/add",
        data={"period": "7", "sis_number": "20002", "name": "Able, Patricia"},
    )
    assert response.status_code == 200
    assert "Able, Patricia is back on the period 7 roster." in response.text
    assert "Able, Patricia is also on period 6." in response.text
    assert [student.name for student in list_active_roster(db_conn, 7)] == [
        "Able, Patricia"
    ]
    assert [student.name for student in list_active_roster(db_conn, 6)] == [
        "Able, Patricia"
    ]


def test_add_student_already_on_roster(client: TestClient, db_conn: sqlite3.Connection) -> None:
    _login(client)
    _enroll(db_conn, "Able, Pat", "20002", 6)

    response = client.post(
        "/admin/roster/add",
        data={"period": "6", "sis_number": "20002", "name": "Able, Pat"},
    )
    assert response.status_code == 200
    assert "Able, Pat is already on the period 6 roster." in response.text


def test_add_student_validation_keeps_form(client: TestClient) -> None:
    _login(client)
    response = client.post(
        "/admin/roster/add",
        data={"period": "3", "sis_number": "", "name": "Able, Pat"},
    )
    assert response.status_code == 400
    assert "Missing student ID (Perm ID)" in response.text
    assert 'value="Able, Pat"' in response.text

    response = client.post(
        "/admin/roster/add",
        data={"period": "3", "sis_number": "20002", "name": "  "},
    )
    assert response.status_code == 400
    assert "Missing student name" in response.text
    assert 'value="20002"' in response.text


def test_add_student_requires_period(client: TestClient) -> None:
    _login(client)
    response = client.post(
        "/admin/roster/add",
        data={"sis_number": "20002", "name": "Able, Pat"},
    )
    assert response.status_code == 400
    assert "class period" in response.text.lower()


def test_add_student_requires_login(client: TestClient) -> None:
    response = client.post(
        "/admin/roster/add",
        data={"period": "3", "sis_number": "20002", "name": "Able, Pat"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "/admin/login" in response.headers["location"]


def test_remove_student_from_roster(
    client: TestClient, db_conn: sqlite3.Connection
) -> None:
    _login(client)
    student_id = _enroll(db_conn, "Able, Pat", "20002", 6)
    _enroll(db_conn, "Baker, Quinn", "20006", 6)
    db_conn.execute(
        """
        INSERT INTO attendance_records (
            student_id, absence_date, period, absence_code
        ) VALUES (?, ?, ?, ?)
        """,
        (student_id, "2026-09-08", 6, "Illness"),
    )
    db_conn.commit()

    page = client.get("/admin/roster?period=6")
    assert f'action="/admin/roster/{student_id}/remove"' in page.text
    assert "They keep old absences for leftover makeup." in page.text

    response = client.post(
        f"/admin/roster/{student_id}/remove",
        data={"period": "6"},
    )
    assert response.status_code == 200
    assert "Removed Able, Pat from period 6." in response.text
    assert "Baker, Quinn" in response.text
    assert "Able, Pat" not in response.text.split("<table")[-1]
    assert [student.name for student in list_active_roster(db_conn, 6)] == [
        "Baker, Quinn"
    ]
    remaining = db_conn.execute(
        "SELECT COUNT(*) AS n FROM attendance_records WHERE student_id = ?",
        (student_id,),
    ).fetchone()
    assert remaining["n"] == 1


def test_remove_rejects_student_not_on_period(
    client: TestClient, db_conn: sqlite3.Connection
) -> None:
    _login(client)
    other = _enroll(db_conn, "Other, Period", "20004", 7)
    response = client.post(
        f"/admin/roster/{other}/remove",
        data={"period": "6"},
    )
    assert response.status_code == 400
    assert "not on this period" in response.text


def test_remove_requires_login(client: TestClient, db_conn: sqlite3.Connection) -> None:
    student_id = _enroll(db_conn, "Able, Pat", "20002", 6)
    response = client.post(
        f"/admin/roster/{student_id}/remove",
        data={"period": "6"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "/admin/login" in response.headers["location"]


def test_attendance_and_dashboard_link_to_roster(client: TestClient) -> None:
    _login(client)
    attendance = client.get("/admin/attendance")
    assert attendance.status_code == 200
    assert 'href="/admin/roster"' in attendance.text
    dashboard = client.get("/admin")
    assert dashboard.status_code == 200
    assert 'href="/admin/roster"' in dashboard.text
