"""Class roster CSV import: Perm ID + Student Name, period membership."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.database import init_schema
from app.services.attendance_parser import ingest_attendance_file
from app.services.roster_import import (
    EMPTY_ROSTER_MESSAGE,
    HEADER_MISSING_MESSAGE,
    ingest_roster_file,
    parse_roster_rows,
)
from app.services.student_roster import list_active_roster


def _memory_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    return conn


def _write_csv(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def _active_names(conn: sqlite3.Connection, period: int) -> list[str]:
    return [student.name for student in list_active_roster(conn, period)]


def test_parse_quoted_last_first_csv() -> None:
    text = 'Perm ID,Student Name\n970123,"Able, Pat"\n970124,"Baker, Quinn"\n'
    roster, rejections, rows = parse_roster_rows(text, ".csv")
    assert rows == 2
    assert rejections == []
    assert roster["970123"].name == "Able, Pat"
    assert roster["970124"].name == "Baker, Quinn"


def test_parse_accepts_tab_and_sis_header() -> None:
    text = "Sis Number\tStudent name\n1001\tLee, Jordan\n"
    roster, rejections, rows = parse_roster_rows(text, ".txt")
    assert rows == 1
    assert rejections == []
    assert roster["1001"].name == "Lee, Jordan"


def test_duplicate_perm_id_last_name_wins() -> None:
    text = (
        "Perm ID,Student Name\n"
        '970123,"Able, Pat"\n'
        '970123,"Able, Patricia"\n'
    )
    roster, rejections, _ = parse_roster_rows(text, ".csv")
    assert rejections == []
    assert list(roster) == ["970123"]
    assert roster["970123"].name == "Able, Patricia"


def test_missing_perm_id_is_rejected() -> None:
    text = 'Perm ID,Student Name\n,"Able, Pat"\n970124,"Baker, Quinn"\n'
    roster, rejections, _ = parse_roster_rows(text, ".csv")
    assert "970124" in roster
    assert len(rejections) == 1
    assert rejections[0].name == "Able, Pat"
    assert "Perm ID" in rejections[0].reason


def test_missing_header_raises() -> None:
    with pytest.raises(ValueError, match="Perm ID"):
        parse_roster_rows("hello,world\n1,2\n", ".csv")
    with pytest.raises(ValueError, match=HEADER_MISSING_MESSAGE[:20]):
        parse_roster_rows("hello,world\n1,2\n", ".csv")


def test_ingest_enrolls_period_and_leaves_others(tmp_path: Path) -> None:
    conn = _memory_db()
    period1 = _write_csv(
        tmp_path / "period1.csv",
        'Perm ID,Student Name\n970123,"Able, Pat"\n970124,"Baker, Quinn"\n',
    )
    result = ingest_roster_file(conn, period1, period1.name, class_period=1)
    assert result.outcome == "success"
    assert result.students_touched == 2
    assert result.class_period == 1
    assert _active_names(conn, 1) == ["Able, Pat", "Baker, Quinn"]
    assert _active_names(conn, 3) == []


def test_roster_reupload_deactivates_missing_student(tmp_path: Path) -> None:
    conn = _memory_db()
    first = _write_csv(
        tmp_path / "full.csv",
        'Perm ID,Student Name\n114007,"Pat, Example"\n2002,"Sam, Example"\n',
    )
    ingest_roster_file(conn, first, first.name, class_period=1)
    second = _write_csv(
        tmp_path / "without_pat.csv",
        'Perm ID,Student Name\n2002,"Sam, Example"\n',
    )
    result = ingest_roster_file(conn, second, second.name, class_period=1)
    assert result.roster_removed == 1
    assert _active_names(conn, 1) == ["Sam, Example"]
    row = conn.execute(
        """
        SELECT scp.active
        FROM student_class_periods scp
        JOIN students s ON s.id = scp.student_id
        WHERE s.sis_number = '114007' AND scp.period = 1
        """
    ).fetchone()
    assert row["active"] == 0


def test_attendance_reupload_does_not_drop_roster_only_student(
    tmp_path: Path,
) -> None:
    from tests.test_attendance_ingest import _base_row, _write_fixture

    conn = _memory_db()
    roster = _write_csv(
        tmp_path / "roster.csv",
        'Perm ID,Student Name\n1001,"Alice Example"\n2002,"Bob Example"\n',
    )
    ingest_roster_file(conn, roster, roster.name, class_period=3)

    absences = tmp_path / "absences.txt"
    _write_fixture(
        absences,
        [_base_row(name="Alice Example", sis="1001", period3="Illness")],
    )
    result = ingest_attendance_file(conn, absences, absences.name, class_period=3)
    assert result.roster_removed == 0
    assert _active_names(conn, 3) == ["Alice Example", "Bob Example"]


def test_empty_file_does_not_wipe_class(tmp_path: Path) -> None:
    conn = _memory_db()
    first = _write_csv(
        tmp_path / "full.csv",
        'Perm ID,Student Name\n970123,"Able, Pat"\n',
    )
    ingest_roster_file(conn, first, first.name, class_period=1)
    empty = _write_csv(tmp_path / "empty.csv", "Perm ID,Student Name\n")
    with pytest.raises(ValueError, match="No students were imported"):
        ingest_roster_file(conn, empty, empty.name, class_period=1)
    assert EMPTY_ROSTER_MESSAGE
    assert _active_names(conn, 1) == ["Able, Pat"]


def test_roster_upload_page_and_import(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.dependencies import ADMIN_COOKIE_NAME, _expected_admin_token
    from app.routers import admin as admin_router

    def _save_to_tmp(upload) -> Path:
        destination = tmp_path / (upload.filename or "roster.csv")
        destination.write_bytes(upload.file.read())
        return destination

    monkeypatch.setattr(admin_router, "_save_roster_upload", _save_to_tmp)

    client.cookies.set(ADMIN_COOKIE_NAME, _expected_admin_token())
    page = client.get("/admin/attendance")
    assert page.status_code == 200
    assert "Upload class roster" in page.text
    assert 'action="/admin/attendance/roster"' in page.text

    csv_bytes = b'Perm ID,Student Name\n970123,"Able, Pat"\n970124,"Baker, Quinn"\n'
    response = client.post(
        "/admin/attendance/roster",
        data={"class_period": "1"},
        files={"file": ("period1.csv", csv_bytes, "text/csv")},
    )
    assert response.status_code == 200
    assert "Roster loaded for period 1" in response.text
    assert "2 student(s) enrolled" in response.text
    assert "Period 1:" in response.text


def test_roster_upload_requires_period(client: TestClient) -> None:
    from app.dependencies import ADMIN_COOKIE_NAME, _expected_admin_token

    client.cookies.set(ADMIN_COOKIE_NAME, _expected_admin_token())
    response = client.post(
        "/admin/attendance/roster",
        data={},
        files={
            "file": (
                "period1.csv",
                b'Perm ID,Student Name\n970123,"Able, Pat"\n',
                "text/csv",
            )
        },
    )
    assert response.status_code == 400
    assert "class period" in response.text.lower()


def test_roster_upload_requires_login(client: TestClient) -> None:
    response = client.post(
        "/admin/attendance/roster",
        data={"class_period": "1"},
        files={
            "file": (
                "period1.csv",
                b'Perm ID,Student Name\n970123,"Able, Pat"\n',
                "text/csv",
            )
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "/admin/login" in response.headers["location"]
