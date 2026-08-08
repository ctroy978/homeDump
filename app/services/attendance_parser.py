"""Parse teacher attendance exports (Excel or tab-delimited text) into the database."""

from __future__ import annotations

import csv
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


MISSING_SIS_MESSAGE = (
    "Missing student ID (SIS number). Add their SIS in the attendance export "
    "and re-upload this class."
)
INVALID_SIS_DECIMAL_MESSAGE = (
    "Student ID must not contain a decimal point. Check the SIS number in the export."
)
NO_USABLE_ROWS_MESSAGE = (
    "No usable attendance rows for this student (check dates in the export). "
    "Existing records were left unchanged."
)


@dataclass(frozen=True)
class StudentRosterEntry:
    """One student found in an attendance export (keyed by SIS)."""

    key: str
    name: str
    grade: str | None
    sis_number: str


@dataclass(frozen=True)
class StudentImportRejection:
    """One student (or name-only row) that could not be imported."""

    reason: str
    name: str | None = None
    sis_number: str | None = None

    def display(self) -> str:
        who = self.name or self.sis_number or "Unknown student"
        if self.name and self.sis_number:
            who = f"{self.name} (SIS {self.sis_number})"
        elif self.sis_number and not self.name:
            who = f"SIS {self.sis_number}"
        return f"{who} — {self.reason}"


@dataclass
class AttendanceParseResult:
    """Summary returned after ingesting an attendance workbook."""

    upload_id: int
    filename: str
    rows_read: int = 0
    records_upserted: int = 0
    records_cleared: int = 0
    students_touched: int = 0
    students_rejected: int = 0
    rows_skipped: int = 0
    warnings: list[str] = field(default_factory=list)
    rejections: list[StudentImportRejection] = field(default_factory=list)


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


def _parse_sis_cell(value: object) -> tuple[str | None, str | None]:
    """
    Normalize a SIS cell from an export.

    Returns ``(sis_number, error_message)``. When the cell is blank,
    both are None. When invalid, sis is None and error_message is set.

    Whole-number floats (common when pandas infers a numeric column) become
    integer strings without a decimal. String values that already contain '.'
    are rejected.
    """
    if value is None:
        return None, None
    try:
        if pd.isna(value):
            return None, None
    except (TypeError, ValueError):
        pass

    if isinstance(value, bool):
        return str(value).strip() or None, None
    if isinstance(value, int):
        return str(value), None
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value)), None
        return None, INVALID_SIS_DECIMAL_MESSAGE

    text = str(value).strip()
    if not text:
        return None, None
    if "." in text:
        return None, INVALID_SIS_DECIMAL_MESSAGE
    return text, None


def _normalize_column_label(value: object) -> str:
    """Strip whitespace and surrounding quotes from export column labels."""
    text = str(value).strip()
    if len(text) >= 2 and text[0] == text[-1] == '"':
        text = text[1:-1].strip()
    return text


def _find_period_columns(columns: list[str]) -> dict[str, int]:
    """Map column header -> period number (0-7)."""
    period_columns: dict[str, int] = {}
    for column in columns:
        label = _normalize_column_label(column)
        match = PERIOD_HEADER_RE.match(label)
        if match:
            period_columns[column] = int(match.group(1))
    return period_columns


def _require_column(columns: list[str], name: str) -> str:
    """Return the actual column label matching name (case-insensitive)."""
    lowered = {_normalize_column_label(col).lower(): col for col in columns}
    key = name.strip().lower()
    if key not in lowered:
        raise ValueError(f"Missing required column: {name}")
    return str(lowered[key])


def _optional_column(columns: list[str], name: str) -> str | None:
    target = name.lower()
    for column in columns:
        if _normalize_column_label(column).lower() == target:
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


def _split_header_fields(line: str, delimiter: str) -> list[str]:
    """Split one export line into normalized field names."""
    row = next(csv.reader([line], delimiter=delimiter))
    return [_normalize_column_label(field) for field in row]


def _is_attendance_header(fields: list[str]) -> bool:
    """
    Return True when a line looks like the attendance table header.

    Requires Student Name, Date, and at least one Period 0-7 column so preamble
    rows (school name, report title, etc.) are ignored.
    """
    if len(fields) < 3:
        return False

    lowered = {field.lower() for field in fields if field}
    if "student name" not in lowered or "date" not in lowered:
        return False

    return any(PERIOD_HEADER_RE.match(field) for field in fields if field)


def find_header_row_index(text: str, delimiter: str) -> int | None:
    """Return the zero-based line index of the attendance header row, if found."""
    for index, line in enumerate(text.splitlines()):
        if not line.strip():
            continue
        fields = _split_header_fields(line, delimiter)
        if _is_attendance_header(fields):
            return index
    return None


def _detect_delimiter(text: str, suffix: str) -> str:
    """
    Guess the field delimiter for a text attendance export.

    Real-world exports from the sample system's .txt reports are tab-delimited.
    When the file has preamble lines, scan for the row that contains the
    attendance headers instead of assuming the first line is the header.
    """
    if suffix == ".csv":
        return ","
    if suffix == ".tsv":
        return "\t"

    for delimiter in ("\t", ","):
        if find_header_row_index(text, delimiter) is not None:
            return delimiter

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
    header_row = find_header_row_index(text, delimiter)
    if header_row is None:
        raise ValueError(
            "Could not find attendance header row. The file must include "
            "columns named 'Student Name', 'Date', and at least one 'Period 0'–'Period 7'."
        )

    # Keep blank preamble lines so the detected header index matches pandas.
    df = pd.read_csv(
        StringIO(text),
        sep=delimiter,
        header=header_row,
        skip_blank_lines=False,
    )
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

    df.columns = [_normalize_column_label(col) for col in df.columns]
    if not df.empty:
        df = df.dropna(how="all")
    return df


def parse_attendance_rows(
    df: pd.DataFrame,
) -> tuple[
    list[dict[str, object]],
    int,
    dict[str, int],
    dict[str, StudentRosterEntry],
    list[StudentImportRejection],
]:
    """
    Turn a workbook dataframe into normalized row dicts and a SIS roster.

    Each output row represents one student/date/period absence code.
    Students are keyed only by SIS number. Missing/invalid SIS rows become
    rejections. ``parseable_dates_by_sis`` counts rows with a valid date per SIS
    (even when all period codes are empty).
    """
    columns = list(df.columns)
    period_columns = _find_period_columns(columns)
    if not period_columns:
        raise ValueError("No Period 0–7 columns found in the attendance file.")

    date_col = _require_column(columns, "Date")
    note_col = _optional_column(columns, "Note")
    grade_col = _optional_column(columns, "Grade")
    sis_col = _require_column(columns, "Sis Number")

    if "student name" not in [c.lower() for c in columns]:
        raise ValueError(
            "Missing 'Student Name' column. The export must include student names, "
            "or use the anonymized test fixture from scripts/build_test_fixture.py."
        )
    name_col = _require_column(columns, "Student Name")

    parsed_rows: list[dict[str, object]] = []
    rows_skipped = 0
    parseable_dates_by_sis: dict[str, int] = {}
    roster: dict[str, StudentRosterEntry] = {}
    rejections: list[StudentImportRejection] = []
    rejected_missing_names: set[str] = set()
    rejected_invalid_sis: set[str] = set()

    for _, row in df.iterrows():
        student_name = _normalize_code(row.get(name_col))
        absence_date = parse_excel_date(row.get(date_col))
        sis_number, sis_error = _parse_sis_cell(row.get(sis_col))
        grade = _normalize_grade(row.get(grade_col)) if grade_col else None
        note = _normalize_code(row.get(note_col)) if note_col else None

        if sis_error is not None:
            marker = f"{student_name or ''}|{sis_error}"
            if marker not in rejected_invalid_sis:
                rejected_invalid_sis.add(marker)
                rejections.append(
                    StudentImportRejection(
                        reason=sis_error,
                        name=student_name,
                        sis_number=_normalize_code(row.get(sis_col)),
                    )
                )
            rows_skipped += 1
            continue

        if not sis_number:
            if student_name and student_name not in rejected_missing_names:
                rejected_missing_names.add(student_name)
                rejections.append(
                    StudentImportRejection(
                        reason=MISSING_SIS_MESSAGE,
                        name=student_name,
                    )
                )
            rows_skipped += 1
            continue

        if not student_name:
            rows_skipped += 1
            continue

        # Last write wins for name; keep prior grade when this row omits it.
        # Include the student even when the date is unparseable so we can refuse
        # to wipe existing attendance with an empty refresh.
        existing = roster.get(sis_number)
        roster[sis_number] = StudentRosterEntry(
            key=sis_number,
            name=student_name,
            grade=grade if grade is not None else (existing.grade if existing else None),
            sis_number=sis_number,
        )

        if not absence_date:
            rows_skipped += 1
            continue

        parseable_dates_by_sis[sis_number] = parseable_dates_by_sis.get(sis_number, 0) + 1

        row_had_code = False
        for column, period in period_columns.items():
            code = _normalize_code(row.get(column))
            if not code:
                continue
            row_had_code = True
            parsed_rows.append(
                {
                    "student_key": sis_number,
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

    return parsed_rows, rows_skipped, parseable_dates_by_sis, roster, rejections


def student_identity_key(name: str, sis_number: str | None) -> str:
    """Stable per-student key; SIS number is required for import identity."""
    if not sis_number:
        raise ValueError("SIS number is required for student identity.")
    return sis_number


def extract_students_from_dataframe(
    df: pd.DataFrame,
) -> tuple[dict[str, StudentRosterEntry], list[StudentImportRejection]]:
    """
    Return every SIS-identified student in the export, plus rejections.

    Identity is SIS only. Display names may collide. Last row wins for name/grade
    when the same SIS appears more than once.
    """
    _, _, _, roster, rejections = parse_attendance_rows(df)
    return roster, rejections


def upsert_student(
    conn: sqlite3.Connection,
    name: str,
    grade: str | None,
    sis_number: str,
) -> int:
    """Insert or update a student by SIS number and return its id."""
    if not sis_number or not str(sis_number).strip():
        raise ValueError("sis_number is required")
    sis = str(sis_number).strip()
    if "." in sis:
        raise ValueError(INVALID_SIS_DECIMAL_MESSAGE)

    by_sis = conn.execute(
        "SELECT id FROM students WHERE sis_number = ?",
        (sis,),
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

    conn.execute(
        "INSERT INTO students (sis_number, name, grade) VALUES (?, ?, ?)",
        (sis, name, grade),
    )
    row = conn.execute(
        "SELECT id FROM students WHERE sis_number = ?",
        (sis,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Failed to upsert student SIS {sis}: {name}")
    return int(row["id"])


def _clear_attendance_for_student(conn: sqlite3.Connection, student_id: int) -> int:
    """Remove all attendance rows for one student before reloading their snapshot."""
    cursor = conn.execute(
        "DELETE FROM attendance_records WHERE student_id = ?",
        (student_id,),
    )
    return int(cursor.rowcount)


def _existing_attendance_count(conn: sqlite3.Connection, student_id: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS total FROM attendance_records WHERE student_id = ?",
        (student_id,),
    ).fetchone()
    return int(row["total"])


def _records_for_student(
    parsed_rows: list[dict[str, object]],
    student_key: str,
    student_id: int,
    upload_id: int,
) -> dict[tuple[int, str, int], tuple[object, ...]]:
    """Collect deduplicated attendance rows for one student; last row wins."""
    records_by_key: dict[tuple[int, str, int], tuple[object, ...]] = {}
    for row in parsed_rows:
        if str(row["student_key"]) != student_key:
            continue
        key = (student_id, str(row["absence_date"]), int(row["period"]))
        records_by_key[key] = (
            student_id,
            row["absence_date"],
            row["period"],
            row["absence_code"],
            row["note"],
            upload_id,
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
    records_by_key = _records_for_student(
        parsed_rows, student_key, student_id, upload_id
    )
    cleared = _clear_attendance_for_student(conn, student_id)

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


def _import_one_student(
    conn: sqlite3.Connection,
    entry: StudentRosterEntry,
    parsed_rows: list[dict[str, object]],
    parseable_dates: int,
    upload_id: int,
) -> tuple[int, int]:
    """
    Upsert one student and replace their attendance.

    Raises ValueError when the refresh would wipe existing data with no usable
    rows (unparseable dates only).
    """
    existing = conn.execute(
        "SELECT id FROM students WHERE sis_number = ?",
        (entry.sis_number,),
    ).fetchone()
    prior_count = (
        _existing_attendance_count(conn, int(existing["id"])) if existing else 0
    )

    if prior_count > 0 and parseable_dates == 0:
        raise ValueError(NO_USABLE_ROWS_MESSAGE)

    student_id = upsert_student(
        conn,
        entry.name,
        entry.grade,
        entry.sis_number,
    )
    return replace_attendance_for_student(
        conn,
        student_id,
        parsed_rows,
        entry.key,
        upload_id,
    )


def ingest_attendance_file(
    conn: sqlite3.Connection,
    source_path: Path,
    original_filename: str,
) -> AttendanceParseResult:
    """
    Parse an attendance workbook and write normalized rows to SQLite.

    Identity is SIS number only (names may collide). Each student is committed
    independently so one failure does not block the rest of the class file.

    When a student appears in an upload, their attendance is cleared and
    reloaded from that file's year-to-date rows. Other students are untouched.
    Students without a SIS are rejected with a teacher-facing message.
    """
    df = load_attendance_dataframe(source_path)
    (
        parsed_rows,
        rows_skipped,
        parseable_dates_by_sis,
        roster,
        rejections,
    ) = parse_attendance_rows(df)

    cursor = conn.execute(
        "INSERT INTO attendance_uploads (filename, row_count) VALUES (?, ?)",
        (original_filename, len(df)),
    )
    upload_id = int(cursor.lastrowid)
    # Persist the upload metadata even if every student is later rejected.
    conn.commit()

    result = AttendanceParseResult(
        upload_id=upload_id,
        filename=original_filename,
        rows_read=len(df),
        rows_skipped=rows_skipped,
        rejections=list(rejections),
    )

    for entry in roster.values():
        try:
            cleared, inserted = _import_one_student(
                conn,
                entry,
                parsed_rows,
                parseable_dates_by_sis.get(entry.sis_number, 0),
                upload_id,
            )
            conn.commit()
            result.records_cleared += cleared
            result.records_upserted += inserted
            result.students_touched += 1
        except Exception as exc:  # noqa: BLE001 — isolate per student
            conn.rollback()
            result.rejections.append(
                StudentImportRejection(
                    reason=str(exc),
                    name=entry.name,
                    sis_number=entry.sis_number,
                )
            )

    result.students_rejected = len(result.rejections)
    return result
