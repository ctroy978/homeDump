"""Tests for attendance import and cohort replacement."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd

from app.database import init_schema
from app.services.attendance_parser import (
    find_header_row_index,
    ingest_attendance_file,
    load_attendance_dataframe,
    upsert_student,
)


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


def test_find_header_row_skips_preamble_lines() -> None:
    text = (
        "School Attendance Report\n"
        "Period 3 - Room 204\n"
        "\n"
        "Generated: 2025-09-15\n"
        "Student Name\tGrade\tDate\tPeriod 0\tPeriod 1\tPeriod 2\tPeriod 3\t"
        "Period 4\tPeriod 5\tPeriod 6\tPeriod 7\tNote\n"
        "Alice Example\t10\t2025-09-02\t\t\t\tIllness\t\t\t\t\t\n"
    )
    assert find_header_row_index(text, "\t") == 4


def test_text_export_with_preamble_parses_correctly(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_schema(conn)

    export = tmp_path / "period3_with_preamble.txt"
    export.write_text(
        "Jefferson High School\n"
        "Attendance Detail - Year to Date\n"
        "Teacher: Example Teacher | Period 3\n"
        "\n"
        "Student Name\tSis Number\tGrade\tDate\tPeriod 0\tPeriod 1\tPeriod 2\t"
        "Period 3\tPeriod 4\tPeriod 5\tPeriod 6\tPeriod 7\tNote\n"
        "Alice Example\t1001\t10\t2025-09-02\t\t\t\tIllness\t\t\t\t\t\n",
        encoding="utf-8",
    )

    result = ingest_attendance_file(conn, export, export.name)

    assert result.students_touched == 1
    assert result.records_upserted == 1
    assert _record_code(conn, "Alice Example", "2025-09-02", 3) == "Illness"

    row = conn.execute(
        "SELECT sis_number FROM students WHERE name = ?",
        ("Alice Example",),
    ).fetchone()
    assert row["sis_number"] == "1001"


def test_plain_header_export_still_parses(tmp_path: Path) -> None:
    export = tmp_path / "plain.txt"
    _write_fixture(
        export,
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

    df = load_attendance_dataframe(export)
    assert find_header_row_index(export.read_text(encoding="utf-8"), "\t") == 0
    assert list(df.columns)[:3] == ["Student Name", "Grade", "Date"]
    assert len(df) == 1


def test_missing_header_row_raises_clear_error(tmp_path: Path) -> None:
    export = tmp_path / "no_header.txt"
    export.write_text(
        "School Attendance Report\n"
        "Generated: 2025-09-15\n"
        "Alice Example\t10\t2025-09-02\tIllness\n",
        encoding="utf-8",
    )

    try:
        load_attendance_dataframe(export)
    except ValueError as exc:
        assert "Could not find attendance header row" in str(exc)
    else:
        raise AssertionError("Expected ValueError for missing header row")