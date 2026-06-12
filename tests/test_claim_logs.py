"""Tests for teacher claim log queries."""

from __future__ import annotations

import sqlite3

from app.services.claim_logs import list_claim_logs
from app.services.claims import log_claim


def _insert_log(
    conn: sqlite3.Connection,
    *,
    student_name: str,
    success: bool,
    message: str,
    token: str | None = None,
) -> None:
    log_claim(
        conn,
        student_name=student_name,
        assignment_id=None,
        period=1,
        absence_date="2025-09-10",
        token=token,
        client_ip="127.0.0.1",
        user_agent="pytest",
        success=success,
        message=message,
    )
    conn.commit()


def test_list_claim_logs_returns_recent_entries_first(
    db_conn: sqlite3.Connection,
) -> None:
    _insert_log(db_conn, student_name="Alice Example", success=True, message="ok")
    _insert_log(db_conn, student_name="Bob Example", success=False, message="nope")

    logs = list_claim_logs(db_conn)
    assert len(logs) == 2
    assert logs[0].student_name == "Bob Example"
    assert logs[1].student_name == "Alice Example"


def test_list_claim_logs_filters_by_student_and_status(
    db_conn: sqlite3.Connection,
) -> None:
    _insert_log(
        db_conn,
        student_name="Alice Example",
        success=True,
        message="ok",
        token="ABCD1234",
    )
    _insert_log(db_conn, student_name="Bob Example", success=False, message="nope")

    alice_logs = list_claim_logs(db_conn, student_query="alice")
    assert len(alice_logs) == 1
    assert alice_logs[0].token == "ABCD1234"

    failed_logs = list_claim_logs(db_conn, status="failed")
    assert len(failed_logs) == 1
    assert failed_logs[0].student_name == "Bob Example"

    success_logs = list_claim_logs(db_conn, status="success")
    assert len(success_logs) == 1
    assert success_logs[0].student_name == "Alice Example"