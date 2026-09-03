"""Tests for attendance import: SIS identity, isolation, rejections."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd

from app.database import init_schema
from app.services.attendance_parser import (
    HEADER_MISSING_MESSAGE,
    LEGACY_XLS_MESSAGE,
    MISSING_NAME_MESSAGE,
    MISSING_SIS_MESSAGE,
    find_excel_header_row_index,
    find_header_row_index,
    ingest_attendance_file,
    load_attendance_dataframe,
    upsert_student,
)


def _base_row(
    *,
    name: str,
    sis: str,
    date: str = "2025-09-02",
    period3: str = "",
    period5: str = "",
    grade: object = 10,
    note: str = "",
) -> dict[str, object]:
    return {
        "Student Name": name,
        "Sis Number": sis,
        "Grade": grade,
        "Date": date,
        "Period 0": "",
        "Period 1": "",
        "Period 2": "",
        "Period 3": period3,
        "Period 4": "",
        "Period 5": period5,
        "Period 6": "",
        "Period 7": "",
        "Note": note,
    }


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


def _count_by_sis(conn: sqlite3.Connection, sis: str) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS total
        FROM attendance_records ar
        JOIN students s ON s.id = ar.student_id
        WHERE s.sis_number = ?
        """,
        (sis,),
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


def _memory_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    return conn


def test_cohort_replace_updates_late_excused_note(tmp_path: Path) -> None:
    conn = _memory_db()
    first = tmp_path / "period3.txt"
    _write_fixture(
        first,
        [_base_row(name="Alice Example", sis="1001", period3="Unexcused Absence")],
    )
    second = tmp_path / "period3_updated.txt"
    _write_fixture(
        second,
        [
            _base_row(
                name="Alice Example",
                sis="1001",
                period3="Excused Absence",
                note="Parent note received",
            )
        ],
    )

    first_result = ingest_attendance_file(conn, first, first.name, class_period=3)
    assert first_result.records_cleared == 0
    assert _record_code(conn, "Alice Example", "2025-09-02", 3) == "Unexcused Absence"

    second_result = ingest_attendance_file(conn, second, second.name, class_period=3)
    assert second_result.records_cleared == 1
    assert _record_code(conn, "Alice Example", "2025-09-02", 3) == "Excused Absence"
    assert _count_records(conn, "Alice Example") == 1


def test_class_uploads_do_not_wipe_other_classes(tmp_path: Path) -> None:
    conn = _memory_db()
    period3 = tmp_path / "period3.txt"
    _write_fixture(
        period3,
        [_base_row(name="Alice Example", sis="1001", period3="Illness")],
    )
    period5 = tmp_path / "period5.txt"
    _write_fixture(
        period5,
        [
            _base_row(
                name="Bob Example",
                sis="2002",
                date="2025-09-10",
                period5="Sports-Athletics",
            )
        ],
    )

    ingest_attendance_file(conn, period3, period3.name, class_period=3)
    ingest_attendance_file(conn, period5, period5.name, class_period=5)

    assert _count_records(conn, "Alice Example") == 1
    assert _count_records(conn, "Bob Example") == 1

    updated_period3 = tmp_path / "period3_refresh.txt"
    _write_fixture(
        updated_period3,
        [_base_row(name="Alice Example", sis="1001", period3="Excused Absence")],
    )
    ingest_attendance_file(conn, updated_period3, updated_period3.name, class_period=3)

    assert _record_code(conn, "Alice Example", "2025-09-02", 3) == "Excused Absence"
    assert _record_code(conn, "Bob Example", "2025-09-10", 5) == "Sports-Athletics"


def test_removed_absence_is_cleared_on_reupload(tmp_path: Path) -> None:
    conn = _memory_db()
    first = tmp_path / "with_absence.txt"
    _write_fixture(
        first,
        [
            _base_row(name="Alice Example", sis="1001", period3="Illness"),
            _base_row(
                name="Alice Example",
                sis="1001",
                date="2025-09-03",
                period3="Illness",
            ),
        ],
    )
    second = tmp_path / "one_day_only.txt"
    _write_fixture(
        second,
        [_base_row(name="Alice Example", sis="1001", period3="Illness")],
    )

    ingest_attendance_file(conn, first, first.name, class_period=3)
    assert _count_records(conn, "Alice Example") == 2

    ingest_attendance_file(conn, second, second.name, class_period=3)
    assert _count_records(conn, "Alice Example") == 1
    assert _record_code(conn, "Alice Example", "2025-09-03", 3) is None


def test_student_move_periods_refreshes_from_new_class_export(tmp_path: Path) -> None:
    """A student who leaves Period 3 is refreshed when they appear in Period 5."""
    conn = _memory_db()
    period3 = tmp_path / "period3.txt"
    _write_fixture(
        period3,
        [_base_row(name="Alice Example", sis="1001", period3="Illness")],
    )
    period5 = tmp_path / "period5.txt"
    _write_fixture(
        period5,
        [
            _base_row(
                name="Alice Example",
                sis="1001",
                period3="Excused Absence",
                period5="Sports-Athletics",
                note="Late parent note",
            )
        ],
    )

    ingest_attendance_file(conn, period3, period3.name, class_period=3)
    assert _record_code(conn, "Alice Example", "2025-09-02", 3) == "Illness"
    assert _record_code(conn, "Alice Example", "2025-09-02", 5) is None

    ingest_attendance_file(conn, period5, period5.name, class_period=5)
    # Period 3 history is left alone; only the tagged period 5 column is imported.
    assert _record_code(conn, "Alice Example", "2025-09-02", 3) == "Illness"
    assert _record_code(conn, "Alice Example", "2025-09-02", 5) == "Sports-Athletics"

    row = conn.execute(
        "SELECT sis_number, last_attendance_upload_id FROM students WHERE name = ?",
        ("Alice Example",),
    ).fetchone()
    assert row["sis_number"] == "1001"
    assert row["last_attendance_upload_id"] == 2


def test_student_with_no_codes_still_clears_old_records(tmp_path: Path) -> None:
    conn = _memory_db()
    student_id = upsert_student(conn, "Alice Example", "10", sis_number="1001")
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
        [_base_row(name="Alice Example", sis="1001")],
    )

    result = ingest_attendance_file(conn, empty_export, empty_export.name, class_period=3)
    assert result.records_cleared == 1
    assert _count_records(conn, "Alice Example") == 0


def test_find_header_row_skips_preamble_lines() -> None:
    text = (
        "School Attendance Report\n"
        "Period 3 - Room 204\n"
        "\n"
        "Generated: 2025-09-15\n"
        "Student Name\tSis Number\tGrade\tDate\tPeriod 0\tPeriod 1\tPeriod 2\tPeriod 3\t"
        "Period 4\tPeriod 5\tPeriod 6\tPeriod 7\tNote\n"
        "Alice Example\t1001\t10\t2025-09-02\t\t\t\tIllness\t\t\t\t\t\n"
    )
    assert find_header_row_index(text, "\t") == 4


def test_text_export_with_preamble_parses_correctly(tmp_path: Path) -> None:
    conn = _memory_db()
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

    result = ingest_attendance_file(conn, export, export.name, class_period=3)

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
        [_base_row(name="Alice Example", sis="1001", period3="Illness")],
    )

    df = load_attendance_dataframe(export)
    assert find_header_row_index(export.read_text(encoding="utf-8"), "\t") == 0
    assert "Student Name" in list(df.columns)
    assert "Sis Number" in list(df.columns)
    assert len(df) == 1


def test_quoted_column_headers_from_live_export(tmp_path: Path) -> None:
    conn = _memory_db()
    export = tmp_path / "live_format.txt"
    export.write_text(
        'School Name\tSchool Year\t"Student Name"\t"Legal Formatted Name"\t'
        '"Sis Number"\t"Grade"\tDate\tPeriod 0\tPeriod 1\tPeriod 2\tPeriod 3\t'
        'Period 4\tPeriod 5\tPeriod 6\tPeriod 7\t"Note"\n'
        "Coquille Jr/Sr High School\t2025-2026\tJane Doe\tDOE, JANE\t"
        "12345\t10\t2025-09-02\t\t\t\tIllness\t\t\t\t\t\n",
        encoding="utf-8",
    )

    result = ingest_attendance_file(conn, export, export.name, class_period=3)

    assert result.students_touched == 1
    assert result.records_upserted == 1
    assert _record_code(conn, "Jane Doe", "2025-09-02", 3) == "Illness"


def _period_headers() -> list[str]:
    return [f"Period {i}" for i in range(8)]


def test_excel_export_with_preamble_parses_correctly(tmp_path: Path) -> None:
    conn = _memory_db()
    export = tmp_path / "period3_with_preamble.xlsx"
    header = ["Student Name", "Sis Number", "Grade", "Date", *_period_headers(), "Note"]
    data_row = [
        "Alice Example",
        "1001",
        "10",
        "2025-09-02",
        "",
        "",
        "",
        "Illness",
        "",
        "",
        "",
        "",
        "",
    ]
    raw = pd.DataFrame(
        [
            ["Jefferson High School"] + [""] * (len(header) - 1),
            ["Attendance Detail - Year to Date"] + [""] * (len(header) - 1),
            [""] * len(header),
            header,
            data_row,
        ]
    )
    raw.to_excel(export, index=False, header=False)

    result = ingest_attendance_file(conn, export, export.name, class_period=3)

    assert result.students_touched == 1
    assert result.records_upserted == 1
    assert _record_code(conn, "Alice Example", "2025-09-02", 3) == "Illness"


def test_excel_plain_header_still_parses(tmp_path: Path) -> None:
    export = tmp_path / "plain.xlsx"
    _write_fixture(
        export.with_suffix(".txt"),
        [_base_row(name="Alice Example", sis="1001", period3="Illness")],
    )
    pd.DataFrame([_base_row(name="Alice Example", sis="1001", period3="Illness")]).to_excel(
        export, index=False
    )

    df = load_attendance_dataframe(export)
    assert "Student Name" in list(df.columns)
    assert "Sis Number" in list(df.columns)
    assert find_excel_header_row_index(
        pd.read_excel(export, header=None, dtype=str)
    ) == 0


def test_excel_missing_header_row_raises_clear_error(tmp_path: Path) -> None:
    export = tmp_path / "no_header.xlsx"
    pd.DataFrame(
        [
            ["School Attendance Report"],
            ["Generated: 2025-09-15"],
            ["Alice Example", "10", "2025-09-02", "Illness"],
        ]
    ).to_excel(export, index=False, header=False)

    try:
        load_attendance_dataframe(export)
    except ValueError as exc:
        assert HEADER_MISSING_MESSAGE in str(exc)
    else:
        raise AssertionError("Expected ValueError for missing header row")


def test_legacy_xls_is_rejected_with_clear_message(tmp_path: Path) -> None:
    export = tmp_path / "old.xls"
    export.write_bytes(b"not a real xls")
    try:
        load_attendance_dataframe(export)
    except ValueError as exc:
        assert LEGACY_XLS_MESSAGE in str(exc)
    else:
        raise AssertionError("Expected ValueError for .xls")


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


def test_same_display_name_different_sis_both_import(tmp_path: Path) -> None:
    conn = _memory_db()
    export = tmp_path / "twins.txt"
    _write_fixture(
        export,
        [
            _base_row(name="Jordan Lee", sis="1", period3="Illness"),
            _base_row(name="Jordan Lee", sis="2", period3="Excused Absence"),
        ],
    )

    result = ingest_attendance_file(conn, export, export.name, class_period=3)
    assert result.students_touched == 2
    assert result.students_rejected == 0
    assert result.records_upserted == 2

    rows = conn.execute(
        "SELECT sis_number, name FROM students ORDER BY sis_number"
    ).fetchall()
    assert [(r["sis_number"], r["name"]) for r in rows] == [
        ("1", "Jordan Lee"),
        ("2", "Jordan Lee"),
    ]
    assert _count_by_sis(conn, "1") == 1
    assert _count_by_sis(conn, "2") == 1


def test_import_outcome_success_partial_failed(tmp_path: Path) -> None:
    conn = _memory_db()
    good = tmp_path / "all_good.txt"
    _write_fixture(
        good,
        [_base_row(name="Carol Good", sis="3003", period3="Illness")],
    )
    success = ingest_attendance_file(conn, good, good.name, class_period=3)
    assert success.outcome == "success"

    mixed = tmp_path / "mixed.txt"
    mixed.write_text(
        "Student Name\tSis Number\tGrade\tDate\tPeriod 0\tPeriod 1\tPeriod 2\t"
        "Period 3\tPeriod 4\tPeriod 5\tPeriod 6\tPeriod 7\tNote\n"
        "Alex Rivera\t\t10\t2025-09-02\t\t\t\tIllness\t\t\t\t\t\n"
        "Carol Good\t3003\t10\t2025-09-02\t\t\t\tIllness\t\t\t\t\t\n",
        encoding="utf-8",
    )
    partial = ingest_attendance_file(conn, mixed, mixed.name, class_period=3)
    assert partial.outcome == "partial"

    bad = tmp_path / "all_bad.txt"
    bad.write_text(
        "Student Name\tSis Number\tGrade\tDate\tPeriod 0\tPeriod 1\tPeriod 2\t"
        "Period 3\tPeriod 4\tPeriod 5\tPeriod 6\tPeriod 7\tNote\n"
        "Alex Rivera\t\t10\t2025-09-02\t\t\t\tIllness\t\t\t\t\t\n",
        encoding="utf-8",
    )
    failed = ingest_attendance_file(conn, bad, bad.name, class_period=3)
    assert failed.outcome == "failed"
    assert failed.students_touched == 0


def test_missing_sis_rejects_but_others_import(tmp_path: Path) -> None:
    conn = _memory_db()
    export = tmp_path / "partial.txt"
    # Write as text so empty SIS does not force a float column on other IDs.
    export.write_text(
        "Student Name\tSis Number\tGrade\tDate\tPeriod 0\tPeriod 1\tPeriod 2\t"
        "Period 3\tPeriod 4\tPeriod 5\tPeriod 6\tPeriod 7\tNote\n"
        "Alex Rivera\t\t10\t2025-09-02\t\t\t\tIllness\t\t\t\t\t\n"
        "Carol Good\t3003\t10\t2025-09-02\t\t\t\tIllness\t\t\t\t\t\n",
        encoding="utf-8",
    )

    result = ingest_attendance_file(conn, export, export.name, class_period=3)
    assert result.students_touched == 1
    assert result.students_rejected == 1
    assert result.records_upserted == 1
    assert any(
        r.name == "Alex Rivera" and MISSING_SIS_MESSAGE in r.reason
        for r in result.rejections
    )
    assert _count_records(conn, "Carol Good") == 1
    assert (
        conn.execute(
            "SELECT COUNT(*) AS c FROM students WHERE name = ?",
            ("Alex Rivera",),
        ).fetchone()["c"]
        == 0
    )


def test_missing_name_with_sis_is_rejected_not_silent(tmp_path: Path) -> None:
    conn = _memory_db()
    export = tmp_path / "no_name.txt"
    export.write_text(
        "Student Name\tSis Number\tGrade\tDate\tPeriod 0\tPeriod 1\tPeriod 2\t"
        "Period 3\tPeriod 4\tPeriod 5\tPeriod 6\tPeriod 7\tNote\n"
        "\t5555\t10\t2025-09-02\t\t\t\tIllness\t\t\t\t\t\n"
        "Carol Good\t3003\t10\t2025-09-02\t\t\t\tIllness\t\t\t\t\t\n",
        encoding="utf-8",
    )

    result = ingest_attendance_file(conn, export, export.name, class_period=3)
    assert result.students_touched == 1
    assert result.students_rejected == 1
    assert _count_records(conn, "Carol Good") == 1
    assert any(
        r.sis_number == "5555" and MISSING_NAME_MESSAGE in r.reason
        for r in result.rejections
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) AS c FROM students WHERE sis_number = '5555'"
        ).fetchone()["c"]
        == 0
    )


def test_blank_name_row_uses_name_from_other_row_same_sis(tmp_path: Path) -> None:
    conn = _memory_db()
    export = tmp_path / "mixed_name.txt"
    export.write_text(
        "Student Name\tSis Number\tGrade\tDate\tPeriod 0\tPeriod 1\tPeriod 2\t"
        "Period 3\tPeriod 4\tPeriod 5\tPeriod 6\tPeriod 7\tNote\n"
        "\t1001\t10\t2025-09-02\t\t\t\tIllness\t\t\t\t\t\n"
        "Alice Example\t1001\t10\t2025-09-03\t\t\t\tExcused Absence\t\t\t\t\t\n",
        encoding="utf-8",
    )

    result = ingest_attendance_file(conn, export, export.name, class_period=3)
    assert result.students_touched == 1
    assert result.students_rejected == 0
    assert _count_records(conn, "Alice Example") == 2
    assert _record_code(conn, "Alice Example", "2025-09-02", 3) == "Illness"
    assert _record_code(conn, "Alice Example", "2025-09-03", 3) == "Excused Absence"


def test_duplicate_sis_in_file_last_write_wins(tmp_path: Path) -> None:
    conn = _memory_db()
    export = tmp_path / "dup_sis.txt"
    _write_fixture(
        export,
        [
            _base_row(name="Alice Old", sis="1001", period3="Illness"),
            _base_row(
                name="Alice New",
                sis="1001",
                period3="Excused Absence",
                note="Updated",
            ),
        ],
    )

    result = ingest_attendance_file(conn, export, export.name, class_period=3)
    assert result.students_touched == 1
    row = conn.execute(
        "SELECT name, sis_number FROM students WHERE sis_number = '1001'"
    ).fetchone()
    assert row["name"] == "Alice New"
    assert _record_code(conn, "Alice New", "2025-09-02", 3) == "Excused Absence"


def test_garbage_dates_do_not_wipe_existing_attendance(tmp_path: Path) -> None:
    conn = _memory_db()
    good = tmp_path / "good.txt"
    _write_fixture(
        good,
        [
            _base_row(name="Dave", sis="4004", period3="Illness"),
            _base_row(name="Carol", sis="3003", period3="Illness"),
        ],
    )
    ingest_attendance_file(conn, good, good.name, class_period=3)
    assert _count_by_sis(conn, "4004") == 1

    bad = tmp_path / "bad_dates.txt"
    _write_fixture(
        bad,
        [
            {
                **_base_row(name="Dave", sis="4004", period3="Illness"),
                "Date": "???",
            },
            _base_row(name="Carol", sis="3003", period3="Excused Absence"),
        ],
    )
    result = ingest_attendance_file(conn, bad, bad.name, class_period=3)

    assert _count_by_sis(conn, "4004") == 1  # preserved
    assert _record_code(conn, "Carol", "2025-09-02", 3) == "Excused Absence"
    assert any(r.sis_number == "4004" for r in result.rejections)
    assert result.students_touched == 1


def test_decimal_sis_is_rejected(tmp_path: Path) -> None:
    conn = _memory_db()
    export = tmp_path / "decimal.txt"
    # Non-integer decimal cannot be a real student ID (integer-looking floats
    # from pandas/Excel are accepted as whole numbers).
    export.write_text(
        "Student Name\tSis Number\tGrade\tDate\tPeriod 0\tPeriod 1\tPeriod 2\t"
        "Period 3\tPeriod 4\tPeriod 5\tPeriod 6\tPeriod 7\tNote\n"
        "Floaty\t12.34\t10\t2025-09-02\t\t\t\tIllness\t\t\t\t\t\n"
        "Ok\t9999\t10\t2025-09-02\t\t\t\tIllness\t\t\t\t\t\n",
        encoding="utf-8",
    )
    result = ingest_attendance_file(conn, export, export.name, class_period=3)
    assert result.students_touched == 1
    assert result.students_rejected == 1
    assert _count_by_sis(conn, "9999") == 1
    assert any("decimal" in r.reason.lower() for r in result.rejections)


def test_parse_sis_cell_whole_number_float_and_string_decimal() -> None:
    from app.services.attendance_parser import _parse_sis_cell

    assert _parse_sis_cell(12345.0) == ("12345", None)
    assert _parse_sis_cell(12345) == ("12345", None)
    assert _parse_sis_cell(" 10001 ") == ("10001", None)
    assert _parse_sis_cell("12345.0") == ("12345", None)
    assert _parse_sis_cell("001234") == ("001234", None)
    sis, err = _parse_sis_cell(12.5)
    assert sis is None and err is not None
    sis, err = _parse_sis_cell("12.34")
    assert sis is None and err is not None and "decimal" in err.lower()


def test_require_sis_number_column(tmp_path: Path) -> None:
    export = tmp_path / "no_sis_col.txt"
    df = pd.DataFrame(
        [
            {
                "Student Name": "Alice",
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
        ]
    )
    df.to_csv(export, sep="\t", index=False)
    conn = _memory_db()
    try:
        ingest_attendance_file(conn, export, export.name, class_period=3)
    except ValueError as exc:
        assert "Sis Number" in str(exc)
    else:
        raise AssertionError("Expected missing Sis Number column to fail")


def test_leading_zeros_preserved_when_sis_is_text(tmp_path: Path) -> None:
    conn = _memory_db()
    export = tmp_path / "padded.txt"
    export.write_text(
        "Student Name\tSis Number\tGrade\tDate\tPeriod 0\tPeriod 1\tPeriod 2\t"
        "Period 3\tPeriod 4\tPeriod 5\tPeriod 6\tPeriod 7\tNote\n"
        "Padded\t001234\t10\t2025-09-02\t\t\t\tIllness\t\t\t\t\t\n"
        "Neighbor\t001235\t10\t2025-09-02\t\t\t\tIllness\t\t\t\t\t\n",
        encoding="utf-8",
    )

    result = ingest_attendance_file(conn, export, export.name, class_period=3)
    assert result.students_touched == 2
    stored = [
        row["sis_number"]
        for row in conn.execute(
            "SELECT sis_number FROM students ORDER BY sis_number"
        ).fetchall()
    ]
    assert stored == ["001234", "001235"]


def test_blank_sis_does_not_strip_neighbors_leading_zeros(tmp_path: Path) -> None:
    conn = _memory_db()
    export = tmp_path / "mixed.txt"
    export.write_text(
        "Student Name\tSis Number\tGrade\tDate\tPeriod 0\tPeriod 1\tPeriod 2\t"
        "Period 3\tPeriod 4\tPeriod 5\tPeriod 6\tPeriod 7\tNote\n"
        "Missing\t\t10\t2025-09-02\t\t\t\tIllness\t\t\t\t\t\n"
        "Padded\t001234\t10\t2025-09-02\t\t\t\tIllness\t\t\t\t\t\n",
        encoding="utf-8",
    )

    result = ingest_attendance_file(conn, export, export.name, class_period=3)
    assert result.students_touched == 1
    assert result.students_rejected == 1
    row = conn.execute("SELECT sis_number FROM students").fetchone()
    assert row["sis_number"] == "001234"


def test_excel_float_sis_does_not_create_duplicate_student(tmp_path: Path) -> None:
    conn = _memory_db()
    upsert_student(conn, "Alice Example", "10", "001001")
    conn.commit()

    export = tmp_path / "numeric.xlsx"
    pd.DataFrame(
        [
            {
                "Student Name": "Alice Example",
                "Sis Number": 1001,
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
        ]
    ).to_excel(export, index=False)

    result = ingest_attendance_file(conn, export, export.name, class_period=3)
    assert result.students_touched == 1
    assert result.students_rejected == 0
    rows = conn.execute("SELECT id, sis_number, name FROM students").fetchall()
    assert len(rows) == 1
    assert rows[0]["sis_number"] == "001001"
    assert _count_by_sis(conn, "001001") == 1


def test_garbage_dates_respect_leading_zero_equivalent(tmp_path: Path) -> None:
    conn = _memory_db()
    first = tmp_path / "padded.txt"
    export_text = (
        "Student Name\tSis Number\tGrade\tDate\tPeriod 0\tPeriod 1\tPeriod 2\t"
        "Period 3\tPeriod 4\tPeriod 5\tPeriod 6\tPeriod 7\tNote\n"
        "Dave\t001001\t10\t2025-09-02\t\t\t\tIllness\t\t\t\t\t\n"
    )
    first.write_text(export_text, encoding="utf-8")
    ingest_attendance_file(conn, first, first.name, class_period=3)
    assert _count_by_sis(conn, "001001") == 1

    bad = tmp_path / "bad.xlsx"
    pd.DataFrame(
        [
            {
                "Student Name": "Dave",
                "Sis Number": 1001,
                "Grade": 10,
                "Date": "???",
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
        ]
    ).to_excel(bad, index=False)

    result = ingest_attendance_file(conn, bad, bad.name, class_period=3)
    assert _count_by_sis(conn, "001001") == 1
    assert any("No usable attendance" in r.reason for r in result.rejections)


def test_upsert_reuses_leading_zero_equivalent_instead_of_splitting() -> None:
    conn = _memory_db()
    first = upsert_student(conn, "Alice", "10", "001001")
    second = upsert_student(conn, "Alice Updated", "11", "1001")
    conn.commit()
    assert first == second
    row = conn.execute("SELECT name, sis_number, grade FROM students").fetchone()
    assert row["sis_number"] == "001001"
    assert row["name"] == "Alice Updated"
    assert row["grade"] == "11"


def test_upload_result_lists_absence_codes(tmp_path: Path) -> None:
    conn = _memory_db()
    export = tmp_path / "codes.txt"
    _write_fixture(
        export,
        [
            _base_row(name="Alice", sis="1001", period3="illness"),
            _base_row(
                name="Bob",
                sis="2002",
                date="2025-09-03",
                period3="Unexcused Absence",
            ),
        ],
    )
    result = ingest_attendance_file(conn, export, export.name, class_period=3)
    assert "illness" in result.absence_codes or "Illness" in result.absence_codes
    assert any("Unexcused" in code for code in result.absence_codes)


def test_tagged_period_import_ignores_other_period_columns(tmp_path: Path) -> None:
    conn = _memory_db()
    export = tmp_path / "period1.txt"
    row = _base_row(name="Pat", sis="114007", period3="Appointment")
    row["Period 1"] = "Excused Absence"
    _write_fixture(export, [row])

    result = ingest_attendance_file(conn, export, export.name, class_period=1)
    assert result.class_period == 1
    assert result.students_touched == 1
    assert result.records_upserted == 1
    assert _record_code(conn, "Pat", "2025-09-02", 1) == "Excused Absence"
    assert _record_code(conn, "Pat", "2025-09-02", 3) is None
    member = conn.execute(
        """
        SELECT period, active
        FROM student_class_periods scp
        JOIN students s ON s.id = scp.student_id
        WHERE s.sis_number = '114007'
        """
    ).fetchall()
    assert [(r["period"], r["active"]) for r in member] == [(1, 1)]


def test_other_period_membership_required_to_use_those_absences(
    tmp_path: Path,
) -> None:
    from app.services.attendance_parser import student_has_class_period
    from app.services.student_lookup import list_eligible_dates_by_sis

    conn = _memory_db()
    period1 = tmp_path / "p1.txt"
    _write_fixture(
        period1,
        [_base_row(name="Pat", sis="114007", period3="Appointment")],
    )
    ingest_attendance_file(conn, period1, period1.name, class_period=1)

    student_id = int(
        conn.execute(
            "SELECT id FROM students WHERE sis_number = '114007'"
        ).fetchone()["id"]
    )
    conn.execute(
        """
        INSERT INTO attendance_records (
            student_id, absence_date, period, absence_code
        ) VALUES (?, '2025-09-02', 3, 'Appointment')
        """,
        (student_id,),
    )
    conn.commit()

    assert student_has_class_period(conn, student_id, 1) is True
    assert student_has_class_period(conn, student_id, 3) is False
    student, dates = list_eligible_dates_by_sis(conn, 3, "114007")
    assert student is not None
    assert dates == []


def test_leaving_class_deactivates_roster_but_keeps_history(tmp_path: Path) -> None:
    conn = _memory_db()
    first = tmp_path / "with_pat.txt"
    _write_fixture(
        first,
        [
            _base_row(name="Pat", sis="114007", period3="Illness"),
            _base_row(name="Sam", sis="2002", period3="Illness"),
        ],
    )
    ingest_attendance_file(conn, first, first.name, class_period=3)

    second = tmp_path / "without_pat.txt"
    _write_fixture(
        second,
        [_base_row(name="Sam", sis="2002", period3="Illness")],
    )
    result = ingest_attendance_file(conn, second, second.name, class_period=3)
    assert result.roster_removed == 1
    assert _record_code(conn, "Pat", "2025-09-02", 3) == "Illness"
    row = conn.execute(
        """
        SELECT scp.active
        FROM student_class_periods scp
        JOIN students s ON s.id = scp.student_id
        WHERE s.sis_number = '114007' AND scp.period = 3
        """
    ).fetchone()
    assert row["active"] == 0
    from app.services.attendance_parser import student_has_class_period

    pat_id = conn.execute(
        "SELECT id FROM students WHERE sis_number = '114007'"
    ).fetchone()["id"]
    assert student_has_class_period(conn, pat_id, 3) is True


def test_names_may_collide_in_schema() -> None:
    conn = _memory_db()
    id1 = upsert_student(conn, "Jordan Lee", "10", "1")
    id2 = upsert_student(conn, "Jordan Lee", "11", "2")
    conn.commit()
    assert id1 != id2
    count = conn.execute("SELECT COUNT(*) AS c FROM students").fetchone()["c"]
    assert count == 2
