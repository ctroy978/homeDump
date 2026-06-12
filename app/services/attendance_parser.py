"""Parse teacher attendance exports (Excel or tab-delimited text) into the database."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from io import StringIO
from pathlib import Path

import pandas as pd

EXCEL_EXTENSIONS = {".xlsx", ".xlsm", ".xls"}
TEXT_EXTENSIONS = {".txt", ".tsv", ".csv"}
SUPPORTED_EXTENSIONS = EXCEL_EXTENSIONS | TEXT_EXTENSIONS

# Matches "Period 0" through "Period 7" in column headers.
PERIOD_HEADER_RE = re.compile(r"^Period\s*(\d)$", re.IGNORECASE)

# Excel's epoch for serial date numbers (Windows 1900 date system).
EXCEL_EPOCH = datetime(1899, 12, 30)


@dataclass(frozen=True)
class StudentRosterEntry:
    """One student found in an attendance export."""

    key: str
    name: str
    grade: str | None
    sis_number: str | None = None


@dataclass
class AttendanceParseResult:
    """Summary returned after ingesting an attendance workbook."""

    upload_id: int
    filename: str
    rows_read: int = 0
    records_upserted: int = 0
    records_cleared: int = 0
    students_touched: int = 0
    rows_skipped: int = 0
    warnings: list[str] = field(default_factory=list)


def parse_excel_date(value: object) -> str | None:
    """Convert an Excel/pandas date value to ISO YYYY-MM-DD."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if pd.isna(value):
        return None

    if isinstance(value, pd.Timestamp):
        return value.date().isoformat()
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (int, float)):
        return (EXCEL_EPOCH + timedelta(days=int(value))).date().isoformat()
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        parsed = pd.to_datetime(text, errors="coerce")
        if pd.isna(parsed):
            return None
        return parsed.date().isoformat()

    return None


def _normalize_grade(value: object) -> str | None:
    if value is None or pd.isna(value):
        return None
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip() or None


def _normalize_code(value: object) -> str | None:
    if value is None or pd.isna(value):
        return None
    text = str(value).strip()
    return text or None


def _find_period_columns(columns: list[str]) -> dict[str, int]:
    """Map column header -> period number (0-7)."""
    period_columns: dict[str, int] = {}
    for column in columns:
        match = PERIOD_HEADER_RE.match(str(column).strip())
        if match:
            period_columns[column] = int(match.group(1))
    return period_columns


def _require_column(columns: list[str], name: str) -> str:
    """Return the actual column label matching name (case-insensitive)."""
    lowered = {str(col).strip().lower(): col for col in columns}
    key = name.strip().lower()
    if key not in lowered:
        raise ValueError(f"Missing required column: {name}")
    return str(lowered[key])


def _optional_column(columns: list[str], name: str) -> str | None:
    for column in columns:
        if str(column).strip().lower() == name.lower():
            return str(column)
    return None


def _decode_text(raw: bytes) -> str:
    """Decode a text export, handling common school-system encodings."""
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _detect_delimiter(text: str, suffix: str) -> str:
    """
    Guess the field delimiter for a text attendance export.

    Real-world exports from the sample system's .txt reports are tab-delimited.
    """
    if suffix == ".csv":
        return ","
    if suffix == ".tsv":
        return "\t"

    header = next((line for line in text.splitlines() if line.strip()), "")
    if not header:
        return "\t"

    tab_count = header.count("\t")
    comma_count = header.count(",")
    return "\t" if tab_count >= comma_count else ","


def _load_text_export(path: Path) -> pd.DataFrame:
    """Read a tab- or comma-delimited attendance text export."""
    text = _decode_text(path.read_bytes())
    delimiter = _detect_delimiter(text, path.suffix.lower())
    df = pd.read_csv(StringIO(text), sep=delimiter)
    return df


def load_attendance_dataframe(path: Path) -> pd.DataFrame:
    """Read an attendance export from Excel or plain text."""
    suffix = path.suffix.lower()
    if suffix in EXCEL_EXTENSIONS:
        df = pd.read_excel(path, sheet_name=0)
    elif suffix in TEXT_EXTENSIONS:
        df = _load_text_export(path)
    else:
        supported = ", ".join(sorted(SUPPORTED_EXTENSIONS))
        raise ValueError(f"Unsupported file type '{suffix}'. Use one of: {supported}")

    df.columns = [str(col).strip() for col in df.columns]
    if not df.empty:
        df = df.dropna(how="all")
    return df


def parse_attendance_rows(df: pd.DataFrame) -> tuple[list[dict[str, object]], int]:
    """
    Turn a workbook dataframe into normalized row dicts.

    Each output row represents one student/date/period absence code.
    """
    columns = list(df.columns)
    period_columns = _find_period_columns(columns)
    if not period_columns:
        raise ValueError("No Period 0–7 columns found in the attendance file.")

    date_col = _require_column(columns, "Date")
    note_col = _optional_column(columns, "Note")
    grade_col = _optional_column(columns, "Grade")
    sis_col = _optional_column(columns, "Sis Number")

    name_col = None
    if "student name" in [c.lower() for c in columns]:
        name_col = _require_column(columns, "Student Name")
    else:
        raise ValueError(
            "Missing 'Student Name' column. The export must include student names, "
            "or use the anonymized test fixture from scripts/build_test_fixture.py."
        )

    parsed_rows: list[dict[str, object]] = []
    rows_skipped = 0

    for _, row in df.iterrows():
        student_name = _normalize_code(row.get(name_col))
        absence_date = parse_excel_date(row.get(date_col))
        if not student_name or not absence_date:
            rows_skipped += 1
            continue

        grade = _normalize_grade(row.get(grade_col)) if grade_col else None
        note = _normalize_code(row.get(note_col)) if note_col else None
        sis_number = _normalize_code(row.get(sis_col)) if sis_col else None
        student_key = sis_number or student_name

        row_had_code = False
        for column, period in period_columns.items():
            code = _normalize_code(row.get(column))
            if not code:
                continue
            row_had_code = True
            parsed_rows.append(
                {
                    "student_key": student_key,
                    "student_name": student_name,
                    "sis_number": sis_number,
                    "grade": grade,
                    "absence_date": absence_date,
                    "period": period,
                    "absence_code": code,
                    "note": note,
                }
            )

        if not row_had_code:
            rows_skipped += 1

    return parsed_rows, rows_skipped


def student_identity_key(name: str, sis_number: str | None) -> str:
    """Stable per-student key; SIS number wins when the export includes it."""
    return sis_number or name


def extract_students_from_dataframe(df: pd.DataFrame) -> dict[str, StudentRosterEntry]:
    """
    Return every student appearing in the export.

    Each student is keyed by SIS number when present, otherwise by name. This
    keeps schedule changes tied to the person, not the class roster upload.
    """
    columns = list(df.columns)
    name_col = _require_column(columns, "Student Name")
    grade_col = _optional_column(columns, "Grade")
    sis_col = _optional_column(columns, "Sis Number")

    students: dict[str, StudentRosterEntry] = {}
    for _, row in df.iterrows():
        name = _normalize_code(row.get(name_col))
        if not name:
            continue
        sis_number = _normalize_code(row.get(sis_col)) if sis_col else None
        grade = _normalize_grade(row.get(grade_col)) if grade_col else None
        key = student_identity_key(name, sis_number)
        existing = students.get(key)
        if existing is None:
            students[key] = StudentRosterEntry(
                key=key,
                name=name,
                grade=grade,
                sis_number=sis_number,
            )
        elif grade is not None:
            students[key] = StudentRosterEntry(
                key=key,
                name=name,
                grade=grade,
                sis_number=sis_number or existing.sis_number,
            )
    return students


def upsert_student(
    conn: sqlite3.Connection,
    name: str,
    grade: str | None,
    sis_number: str | None = None,
) -> int:
    """Insert or update a student row and return its id."""
    if sis_number:
        by_sis = conn.execute(
            "SELECT id FROM students WHERE sis_number = ?",
            (sis_number,),
        ).fetchone()
        if by_sis is not None:
            conn.execute(
                """
                UPDATE students
                SET name = ?, grade = COALESCE(?, grade)
                WHERE id = ?
                """,
                (name, grade, by_sis["id"]),
            )
            return int(by_sis["id"])

        by_name = conn.execute(
            "SELECT id, sis_number FROM students WHERE name = ?",
            (name,),
        ).fetchone()
        if by_name is not None and by_name["sis_number"] is None:
            conn.execute(
                """
                UPDATE students
                SET sis_number = ?, grade = COALESCE(?, grade)
                WHERE id = ?
                """,
                (sis_number, grade, by_name["id"]),
            )
            return int(by_name["id"])

        conn.execute(
            "INSERT INTO students (sis_number, name, grade) VALUES (?, ?, ?)",
            (sis_number, name, grade),
        )
    else:
        conn.execute(
            """
            INSERT INTO students (name, grade)
            VALUES (?, ?)
            ON CONFLICT(name) DO UPDATE SET
                grade = COALESCE(excluded.grade, students.grade)
            """,
            (name, grade),
        )

    if sis_number:
        row = conn.execute(
            "SELECT id FROM students WHERE sis_number = ?",
            (sis_number,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT id FROM students WHERE name = ?",
            (name,),
        ).fetchone()
    if row is None:
        raise RuntimeError(f"Failed to upsert student: {name}")
    return int(row["id"])


def _clear_attendance_for_student(conn: sqlite3.Connection, student_id: int) -> int:
    """Remove all attendance rows for one student before reloading their snapshot."""
    cursor = conn.execute(
        "DELETE FROM attendance_records WHERE student_id = ?",
        (student_id,),
    )
    return int(cursor.rowcount)


def _records_for_student(
    parsed_rows: list[dict[str, object]],
    student_key: str,
) -> dict[tuple[int, str, int], tuple[object, ...]]:
    """Collect deduplicated attendance rows for one student; last row wins."""
    records_by_key: dict[tuple[int, str, int], tuple[object, ...]] = {}
    for row in parsed_rows:
        if str(row["student_key"]) != student_key:
            continue
        key = (
            int(row["student_id"]),  # type: ignore[call-overload]
            str(row["absence_date"]),
            int(row["period"]),
        )
        records_by_key[key] = (
            row["student_id"],
            row["absence_date"],
            row["period"],
            row["absence_code"],
            row["note"],
            row["upload_id"],
        )
    return records_by_key


def replace_attendance_for_student(
    conn: sqlite3.Connection,
    student_id: int,
    parsed_rows: list[dict[str, object]],
    student_key: str,
    upload_id: int,
) -> tuple[int, int]:
    """
    Replace one student's attendance with the rows from the current export.

    Returns ``(records_cleared, records_inserted)``.
    """
    for row in parsed_rows:
        if str(row["student_key"]) == student_key:
            row["student_id"] = student_id
            row["upload_id"] = upload_id

    cleared = _clear_attendance_for_student(conn, student_id)
    records_by_key = _records_for_student(parsed_rows, student_key)

    for values in records_by_key.values():
        conn.execute(
            """
            INSERT INTO attendance_records (
                student_id, absence_date, period, absence_code, note, upload_id
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            values,
        )

    conn.execute(
        """
        UPDATE students
        SET last_attendance_upload_id = ?
        WHERE id = ?
        """,
        (upload_id, student_id),
    )
    return cleared, len(records_by_key)


def ingest_attendance_file(
    conn: sqlite3.Connection,
    source_path: Path,
    original_filename: str,
) -> AttendanceParseResult:
    """
    Parse an attendance workbook and write normalized rows to SQLite.

    Student replace strategy: the file may contain one class roster, but each
    student is handled independently. When a student appears in an upload,
    all of their attendance is cleared and reloaded from that file's
    year-to-date rows. Other students are untouched.

    This supports importing one class at a time and schedule changes: a student
    who moves from Period 3 to Period 5 is refreshed the next time they appear
    in the Period 5 export (matched by SIS number when available).
    """
    df = load_attendance_dataframe(source_path)
    parsed_rows, rows_skipped = parse_attendance_rows(df)

    cursor = conn.execute(
        "INSERT INTO attendance_uploads (filename, row_count) VALUES (?, ?)",
        (original_filename, len(df)),
    )
    upload_id = int(cursor.lastrowid)

    result = AttendanceParseResult(
        upload_id=upload_id,
        filename=original_filename,
        rows_read=len(df),
    )

    roster = extract_students_from_dataframe(df)

    for entry in roster.values():
        student_id = upsert_student(
            conn,
            entry.name,
            entry.grade,
            entry.sis_number,
        )
        cleared, inserted = replace_attendance_for_student(
            conn,
            student_id,
            parsed_rows,
            entry.key,
            upload_id,
        )
        result.records_cleared += cleared
        result.records_upserted += inserted

    result.students_touched = len(roster)
    result.rows_skipped = rows_skipped

    conn.commit()
    return result