"""Tests for attendance import and cohort replacement."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd

from app.database import init_schema
from app.services.attendance_parser import ingest_attendance_file, upsert_student


def _write_fixture(path: Path, rows: list[dict[str, object]]) -> None:
    df = pd.DataFrame(rows)
    if path.suffix == ".txt":
        df.to_csv(path, sep="\t", index=False)
    else:
        df.to_csv(path, index=False)


def _count_records(conn: sqlite3.Connection, student_name: str) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS total
        FROM attendance_records ar
        JOIN students s ON s.id = ar.student_id
        WHERE s.name = ?
        """,
        (student_name,),
    ).fetchone()
    return int(row["total"])


def _record_code(
    conn: sqlite3.Connection,
    student_name: str,
    absence_date: str,
    period: int,
) -> str | None:
    row = conn.execute(
        """
        SELECT ar.absence_code
        FROM attendance_records ar
        JOIN students s ON s.id = ar.student_id
        WHERE s.name = ? AND ar.absence_date = ? AND ar.period = ?
        """,
        (student_name, absence_date, period),
    ).fetchone()
    return None if row is None else str(row["absence_code"])


def test_cohort_replace_updates_late_excused_note(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_schema(conn)

    first = tmp_path / "period3.txt"
    _write_fixture(
        first,
        [
            {
                "Student Name": "Alice Example",
                "Grade": 10,
                "Date": "2025-09-02",
                "Period 0": "",
                "Period 1": "",
                "Period 2": "",
                "Period 3": "Unexcused Absence",
                "Period 4": "",
                "Period 5": "",
                "Period 6": "",
                "Period 7": "",
                "Note": "",
            }
        ],
    )

    second = tmp_path / "period3_updated.txt"
    _write_fixture(
        second,
        [
            {
                "Student Name": "Alice Example",
                "Grade": 10,
                "Date": "2025-09-02",
                "Period 0": "",
                "Period 1": "",
                "Period 2": "",
                "Period 3": "Excused Absence",
                "Period 4": "",
                "Period 5": "",
                "Period 6": "",
                "Period 7": "",
                "Note": "Parent note received",
            }
        ],
    )

    first_result = ingest_attendance_file(conn, first, first.name)
    assert first_result.records_cleared == 0
    assert _record_code(conn, "Alice Example", "2025-09-02", 3) == "Unexcused Absence"

    second_result = ingest_attendance_file(conn, second, second.name)
    assert second_result.records_cleared == 1
    assert _record_code(conn, "Alice Example", "2025-09-02", 3) == "Excused Absence"
    assert _count_records(conn, "Alice Example") == 1


def test_class_uploads_do_not_wipe_other_classes(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_schema(conn)

    period3 = tmp_path / "period3.txt"
    _write_fixture(
        period3,
        [
            {
                "Student Name": "Alice Example",
                "Grade": 10,
                "Date": "2025-09-02",
                "Period 0": "",
                "Period 1": "",
                "Period 2": "",
                "Period 3": "Illness",
                "Period 4": "",
                "Period 5": "",
                "Period 6": "",
                "Period 7": "",
                "Note": "",
            }
        ],
    )

    period5 = tmp_path / "period5.txt"
    _write_fixture(
        period5,
        [
            {
                "Student Name": "Bob Example",
                "Grade": 10,
                "Date": "2025-09-10",
                "Period 0": "",
                "Period 1": "",
                "Period 2": "",
                "Period 3": "",
                "Period 4": "",
                "Period 5": "Sports-Athletics",
                "Period 6": "",
                "Period 7": "",
                "Note": "",
            }
        ],
    )

    ingest_attendance_file(conn, period3, period3.name)
    ingest_attendance_file(conn, period5, period5.name)

    assert _count_records(conn, "Alice Example") == 1
    assert _count_records(conn, "Bob Example") == 1

    updated_period3 = tmp_path / "period3_refresh.txt"
    _write_fixture(
        updated_period3,
        [
            {
                "Student Name": "Alice Example",
                "Grade": 10,
                "Date": "2025-09-02",
                "Period 0": "",
                "Period 1": "",
                "Period 2": "",
                "Period 3": "Excused Absence",
                "Period 4": "",
                "Period 5": "",
                "Period 6": "",
                "Period 7": "",
                "Note": "",
            }
        ],
    )
    ingest_attendance_file(conn, updated_period3, updated_period3.name)

    assert _record_code(conn, "Alice Example", "2025-09-02", 3) == "Excused Absence"
    assert _record_code(conn, "Bob Example", "2025-09-10", 5) == "Sports-Athletics"


def test_removed_absence_is_cleared_on_reupload(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_schema(conn)

    first = tmp_path / "with_absence.txt"
    _write_fixture(
        first,
        [
            {
                "Student Name": "Alice Example",
                "Grade": 10,
                "Date": "2025-09-02",
                "Period 0": "",
                "Period 1": "",
                "Period 2": "",
                "Period 3": "Illness",
                "Period 4": "",
                "Period 5": "",
                "Period 6": "",
                "Period 7": "",
                "Note": "",
            },
            {
                "Student Name": "Alice Example",
                "Grade": 10,
                "Date": "2025-09-03",
                "Period 0": "",
                "Period 1": "",
                "Period 2": "",
                "Period 3": "Illness",
                "Period 4": "",
                "Period 5": "",
                "Period 6": "",
                "Period 7": "",
                "Note": "",
            },
        ],
    )

    second = tmp_path / "one_day_only.txt"
    _write_fixture(
        second,
        [
            {
                "Student Name": "Alice Example",
                "Grade": 10,
                "Date": "2025-09-02",
                "Period 0": "",
                "Period 1": "",
                "Period 2": "",
                "Period 3": "Illness",
                "Period 4": "",
                "Period 5": "",
                "Period 6": "",
                "Period 7": "",
                "Note": "",
            }
        ],
    )

    ingest_attendance_file(conn, first, first.name)
    assert _count_records(conn, "Alice Example") == 2

    ingest_attendance_file(conn, second, second.name)
    assert _count_records(conn, "Alice Example") == 1
    assert _record_code(conn, "Alice Example", "2025-09-03", 3) is None


def test_student_move_periods_refreshes_from_new_class_export(tmp_path: Path) -> None:
    """A student who leaves Period 3 is refreshed when they appear in Period 5."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_schema(conn)

    period3 = tmp_path / "period3.txt"
    _write_fixture(
        period3,
        [
            {
                "Sis Number": "1001",
                "Student Name": "Alice Example",
                "Grade": 10,
                "Date": "2025-09-02",
                "Period 0": "",
                "Period 1": "",
                "Period 2": "",
                "Period 3": "Illness",
                "Period 4": "",
                "Period 5": "",
                "Period 6": "",
                "Period 7": "",
                "Note": "",
            }
        ],
    )

    period5 = tmp_path / "period5.txt"
    _write_fixture(
        period5,
        [
            {
                "Sis Number": "1001",
                "Student Name": "Alice Example",
                "Grade": 10,
                "Date": "2025-09-02",
                "Period 0": "",
                "Period 1": "",
                "Period 2": "",
                "Period 3": "Excused Absence",
                "Period 4": "",
                "Period 5": "Sports-Athletics",
                "Period 6": "",
                "Period 7": "",
                "Note": "Late parent note",
            }
        ],
    )

    ingest_attendance_file(conn, period3, period3.name)
    assert _record_code(conn, "Alice Example", "2025-09-02", 3) == "Illness"
    assert _record_code(conn, "Alice Example", "2025-09-02", 5) is None

    ingest_attendance_file(conn, period5, period5.name)
    assert _record_code(conn, "Alice Example", "2025-09-02", 3) == "Excused Absence"
    assert _record_code(conn, "Alice Example", "2025-09-02", 5) == "Sports-Athletics"

    row = conn.execute(
        "SELECT sis_number, last_attendance_upload_id FROM students WHERE name = ?",
        ("Alice Example",),
    ).fetchone()
    assert row["sis_number"] == "1001"
    assert row["last_attendance_upload_id"] == 2


def test_student_with_no_codes_still_clears_old_records(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_schema(conn)

    student_id = upsert_student(conn, "Alice Example", "10")
    conn.execute(
        """
        INSERT INTO attendance_records (
            student_id, absence_date, period, absence_code
        ) VALUES (?, '2025-09-02', 3, 'Illness')
        """,
        (student_id,),
    )
    conn.commit()

    empty_export = tmp_path / "empty_year.txt"
    _write_fixture(
        empty_export,
        [
            {
                "Student Name": "Alice Example",
                "Grade": 10,
                "Date": "2025-09-02",
                "Period 0": "",
                "Period 1": "",
                "Period 2": "",
                "Period 3": "",
                "Period 4": "",
                "Period 5": "",
                "Period 6": "",
                "Period 7": "",
                "Note": "",
            }
        ],
    )

    result = ingest_attendance_file(conn, empty_export, empty_export.name)
    assert result.records_cleared == 1
    assert _count_records(conn, "Alice Example") == 0