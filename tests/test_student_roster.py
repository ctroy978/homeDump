"""Tests for period-tagged class rosters."""

from __future__ import annotations

import sqlite3

import pytest

from app.services.attendance_parser import upsert_student
from app.services.student_roster import (
    list_active_period_counts,
    list_active_roster,
    resolve_selected_roster,
)


def _enroll(conn: sqlite3.Connection, name: str, sis: str, period: int, *, active: int = 1) -> int:
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


def test_list_active_roster_sorts_by_name_and_skips_inactive(
    db_conn: sqlite3.Connection,
) -> None:
    _enroll(db_conn, "Zebra, Ann", "20001", 6)
    _enroll(db_conn, "Able, Pat", "20002", 6)
    _enroll(db_conn, "Gone, Sam", "20003", 6, active=0)
    _enroll(db_conn, "Other, Period", "20004", 7)

    roster = list_active_roster(db_conn, 6)
    assert [student.name for student in roster] == ["Able, Pat", "Zebra, Ann"]


def test_list_active_period_counts(db_conn: sqlite3.Connection) -> None:
    _enroll(db_conn, "Able, Pat", "20002", 6)
    _enroll(db_conn, "Zebra, Ann", "20001", 6)
    _enroll(db_conn, "Gone, Sam", "20003", 6, active=0)
    _enroll(db_conn, "Other, Period", "20004", 7)

    counts = list_active_period_counts(db_conn)
    assert counts[6] == 2
    assert counts[7] == 1
    assert counts[0] == 1  # fixture Test Student A
    assert counts[5] == 0


def test_resolve_selected_roster_keeps_name_order(
    db_conn: sqlite3.Connection,
) -> None:
    zebra = _enroll(db_conn, "Zebra, Ann", "20001", 6)
    able = _enroll(db_conn, "Able, Pat", "20002", 6)
    _enroll(db_conn, "Gone, Sam", "20003", 6, active=0)

    selected = resolve_selected_roster(db_conn, 6, [zebra, able, zebra])
    assert [student.name for student in selected] == ["Able, Pat", "Zebra, Ann"]


def test_resolve_selected_roster_rejects_empty(db_conn: sqlite3.Connection) -> None:
    _enroll(db_conn, "Able, Pat", "20002", 6)
    with pytest.raises(ValueError, match="at least one student"):
        resolve_selected_roster(db_conn, 6, [])


def test_resolve_selected_roster_rejects_other_period(
    db_conn: sqlite3.Connection,
) -> None:
    able = _enroll(db_conn, "Able, Pat", "20002", 6)
    other = _enroll(db_conn, "Other, Period", "20004", 7)
    with pytest.raises(ValueError, match="not in this period"):
        resolve_selected_roster(db_conn, 6, [able, other])
