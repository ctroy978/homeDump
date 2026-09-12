"""Tests for period-tagged class rosters."""

from __future__ import annotations

import sqlite3

import pytest

from app.services.attendance_parser import upsert_student
from app.services.student_roster import (
    NOT_ON_ROSTER_MESSAGE,
    deactivate_student,
    enroll_student,
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
    assert [student.sis_number for student in roster] == ["20002", "20001"]


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


def test_enroll_student_adds_one_person_without_dropping_class(
    db_conn: sqlite3.Connection,
) -> None:
    existing = _enroll(db_conn, "Able, Pat", "20002", 6)
    result = enroll_student(db_conn, 6, "20005", "New, Kid")

    assert result.created is True
    assert result.already_active is False
    assert result.reactivated is False
    assert result.student.name == "New, Kid"
    assert result.student.sis_number == "20005"
    assert result.other_active_periods == ()
    roster = list_active_roster(db_conn, 6)
    assert [student.id for student in roster] == [existing, result.student.id]


def test_enroll_student_reactivates_and_updates_name(
    db_conn: sqlite3.Connection,
) -> None:
    student_id = _enroll(db_conn, "Gone, Sam", "20003", 6, active=0)
    _enroll(db_conn, "Able, Pat", "20002", 6)
    result = enroll_student(db_conn, 6, "20003", "Sam, Returned")

    assert result.created is False
    assert result.reactivated is True
    assert result.already_active is False
    assert result.student.id == student_id
    assert result.student.name == "Sam, Returned"
    assert [student.name for student in list_active_roster(db_conn, 6)] == [
        "Able, Pat",
        "Sam, Returned",
    ]


def test_enroll_already_active_updates_name(
    db_conn: sqlite3.Connection,
) -> None:
    student_id = _enroll(db_conn, "Able, Pat", "20002", 6)
    result = enroll_student(db_conn, 6, "20002", "Able, Patricia")

    assert result.created is False
    assert result.already_active is True
    assert result.reactivated is False
    assert result.student.id == student_id
    assert result.student.name == "Able, Patricia"
    assert [student.name for student in list_active_roster(db_conn, 6)] == [
        "Able, Patricia"
    ]


def test_enroll_does_not_remove_other_period_membership(
    db_conn: sqlite3.Connection,
) -> None:
    student_id = _enroll(db_conn, "Able, Pat", "20002", 6)
    result = enroll_student(db_conn, 7, "20002", "Able, Pat")

    assert result.student.id == student_id
    assert result.other_active_periods == (6,)
    assert [student.name for student in list_active_roster(db_conn, 6)] == [
        "Able, Pat"
    ]
    assert [student.name for student in list_active_roster(db_conn, 7)] == [
        "Able, Pat"
    ]


def test_enroll_rejects_missing_identity(db_conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="Missing student ID"):
        enroll_student(db_conn, 3, "  ", "Able, Pat")
    with pytest.raises(ValueError, match="Missing student name"):
        enroll_student(db_conn, 3, "20002", "   ")
    with pytest.raises(ValueError, match="decimal point"):
        enroll_student(db_conn, 3, "12.34", "Able, Pat")


def test_deactivate_student_keeps_absences_and_other_periods(
    db_conn: sqlite3.Connection,
) -> None:
    student_id = _enroll(db_conn, "Able, Pat", "20002", 7)
    _enroll(db_conn, "Able, Pat", "20002", 6)
    db_conn.execute(
        """
        INSERT INTO attendance_records (
            student_id, absence_date, period, absence_code
        ) VALUES (?, ?, ?, ?)
        """,
        (student_id, "2026-09-08", 7, "Illness"),
    )
    db_conn.commit()

    removed = deactivate_student(db_conn, 7, student_id)
    assert removed.id == student_id
    assert list_active_roster(db_conn, 7) == []
    assert [student.name for student in list_active_roster(db_conn, 6)] == [
        "Able, Pat"
    ]
    row = db_conn.execute(
        """
        SELECT active FROM student_class_periods
        WHERE student_id = ? AND period = 7
        """,
        (student_id,),
    ).fetchone()
    assert row["active"] == 0
    remaining = db_conn.execute(
        "SELECT COUNT(*) AS n FROM attendance_records WHERE student_id = ?",
        (student_id,),
    ).fetchone()
    assert remaining["n"] == 1
    from app.services.attendance_parser import student_has_class_period

    assert student_has_class_period(db_conn, student_id, 7) is True


def test_deactivate_rejects_inactive_or_other_period(
    db_conn: sqlite3.Connection,
) -> None:
    inactive = _enroll(db_conn, "Gone, Sam", "20003", 6, active=0)
    other = _enroll(db_conn, "Other, Period", "20004", 7)
    with pytest.raises(ValueError, match=NOT_ON_ROSTER_MESSAGE):
        deactivate_student(db_conn, 6, inactive)
    with pytest.raises(ValueError, match=NOT_ON_ROSTER_MESSAGE):
        deactivate_student(db_conn, 6, other)
    with pytest.raises(ValueError, match=NOT_ON_ROSTER_MESSAGE):
        deactivate_student(db_conn, 6, 99999)
