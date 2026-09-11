"""Import a class roster CSV (Perm ID + Student Name) into one period."""

from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path

from app.services.attendance_parser import (
    StudentImportRejection,
    StudentRosterEntry,
    sync_class_period_roster,
    upsert_student,
    validate_class_period,
)
from app.services.sis import find_student_row_by_sis, normalize_sis_number

SUPPORTED_ROSTER_EXTENSIONS = {".csv", ".txt", ".tsv"}

PERM_ID_ALIASES = frozenset(
    {"perm id", "permid", "perm_id", "sis number", "sis_number"}
)
NAME_ALIASES = frozenset({"student name", "student_name", "name"})

MISSING_PERM_ID_MESSAGE = (
    "Missing student ID (Perm ID). Add their ID to the roster file "
    "and re-upload this class."
)
MISSING_NAME_MESSAGE = (
    "Missing student name. Add their name to the roster file "
    "and re-upload this class."
)
HEADER_MISSING_MESSAGE = (
    "Could not find roster header row. The file must include columns "
    "named 'Perm ID' and 'Student Name'."
)
EMPTY_ROSTER_MESSAGE = (
    "No students were imported from this file. Existing class membership "
    "was left unchanged."
)


@dataclass
class RosterImportResult:
    """Summary returned after ingesting a class-list CSV."""

    upload_id: int
    filename: str
    class_period: int
    rows_read: int = 0
    students_touched: int = 0
    students_rejected: int = 0
    roster_removed: int = 0
    rejections: list[StudentImportRejection] = field(default_factory=list)

    @property
    def outcome(self) -> str:
        if self.students_touched and not self.students_rejected:
            return "success"
        if self.students_touched:
            return "partial"
        return "failed"


def _normalize_header(value: object) -> str:
    text = str(value or "").strip().strip('"').strip("'")
    return " ".join(text.split()).lower()


def _decode_text(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _header_indexes(fields: list[str]) -> tuple[int, int] | None:
    perm_idx = None
    name_idx = None
    for index, field in enumerate(fields):
        label = _normalize_header(field)
        if perm_idx is None and label in PERM_ID_ALIASES:
            perm_idx = index
        if name_idx is None and label in NAME_ALIASES:
            name_idx = index
    if perm_idx is None or name_idx is None:
        return None
    return perm_idx, name_idx


def _detect_delimiter(text: str, suffix: str) -> str:
    if suffix == ".tsv":
        return "\t"
    if suffix == ".csv":
        return ","
    for delimiter in (",", "\t"):
        for line in text.splitlines():
            if not line.strip():
                continue
            row = next(csv.reader([line], delimiter=delimiter))
            if _header_indexes(row) is not None:
                return delimiter
    return ","


def parse_roster_rows(
    text: str,
    suffix: str = ".csv",
) -> tuple[dict[str, StudentRosterEntry], list[StudentImportRejection], int]:
    """
    Parse Perm ID / Student Name rows into a SIS-keyed roster.

    Duplicate Perm IDs keep the last name. Missing or invalid IDs become
    rejections and are not imported.
    """
    delimiter = _detect_delimiter(text, suffix)
    reader = csv.reader(StringIO(text), delimiter=delimiter)
    rows = [row for row in reader if any(cell.strip() for cell in row)]
    header_index = None
    indexes = None
    for index, row in enumerate(rows):
        found = _header_indexes(row)
        if found is not None:
            header_index = index
            indexes = found
            break
    if header_index is None or indexes is None:
        raise ValueError(HEADER_MISSING_MESSAGE)

    perm_idx, name_idx = indexes
    roster: dict[str, StudentRosterEntry] = {}
    rejections: list[StudentImportRejection] = []
    rejected_missing_perm: set[str] = set()
    rejected_invalid: set[str] = set()
    rejected_missing_name: set[str] = set()
    data_rows = 0

    for row in rows[header_index + 1 :]:
        data_rows += 1
        perm_cell = row[perm_idx] if perm_idx < len(row) else ""
        name_cell = row[name_idx] if name_idx < len(row) else ""
        name = " ".join(str(name_cell).split()) or None

        try:
            sis_number = normalize_sis_number(perm_cell)
            sis_error = None
        except ValueError as exc:
            sis_number = None
            sis_error = str(exc)

        if sis_error is not None:
            marker = f"{name or ''}|{sis_error}"
            if marker not in rejected_invalid:
                rejected_invalid.add(marker)
                rejections.append(
                    StudentImportRejection(
                        reason=sis_error,
                        name=name,
                        sis_number=str(perm_cell).strip() or None,
                    )
                )
            continue

        if not sis_number:
            if name and name not in rejected_missing_perm:
                rejected_missing_perm.add(name)
                rejections.append(
                    StudentImportRejection(
                        reason=MISSING_PERM_ID_MESSAGE,
                        name=name,
                    )
                )
            continue

        if not name:
            if sis_number not in rejected_missing_name:
                rejected_missing_name.add(sis_number)
                rejections.append(
                    StudentImportRejection(
                        reason=MISSING_NAME_MESSAGE,
                        sis_number=sis_number,
                    )
                )
            continue

        roster[sis_number] = StudentRosterEntry(
            key=sis_number,
            name=name,
            grade=None,
            sis_number=sis_number,
        )

    return roster, rejections, data_rows


def ingest_roster_file(
    conn: sqlite3.Connection,
    source_path: Path,
    original_filename: str,
    class_period: int,
) -> RosterImportResult:
    """
    Load a class list and replace active membership for ``class_period``.

    Does not write attendance rows. Students missing from this file are
    marked inactive for the period so named copies match the current class.
    """
    class_period = validate_class_period(class_period)
    suffix = source_path.suffix.lower()
    if suffix not in SUPPORTED_ROSTER_EXTENSIONS:
        supported = ", ".join(sorted(SUPPORTED_ROSTER_EXTENSIONS))
        raise ValueError(
            f"Unsupported file type '{suffix}'. Use one of: {supported}"
        )

    text = _decode_text(source_path.read_bytes())
    roster, rejections, data_rows = parse_roster_rows(text, suffix)
    if not roster and not rejections:
        raise ValueError(EMPTY_ROSTER_MESSAGE)

    cursor = conn.execute(
        """
        INSERT INTO roster_uploads (filename, row_count, class_period)
        VALUES (?, ?, ?)
        """,
        (original_filename, data_rows, class_period),
    )
    upload_id = int(cursor.lastrowid)
    conn.commit()

    result = RosterImportResult(
        upload_id=upload_id,
        filename=original_filename,
        class_period=class_period,
        rows_read=data_rows,
        rejections=list(rejections),
    )

    member_ids: set[int] = set()
    for entry in roster.values():
        try:
            student_id = upsert_student(
                conn,
                entry.name,
                entry.grade,
                entry.sis_number,
            )
            conn.commit()
            result.students_touched += 1
            member_ids.add(student_id)
        except Exception as exc:  # noqa: BLE001 — isolate per student
            conn.rollback()
            existing = find_student_row_by_sis(conn, entry.sis_number)
            if existing is not None:
                member_ids.add(int(existing["id"]))
            result.rejections.append(
                StudentImportRejection(
                    reason=str(exc),
                    name=entry.name,
                    sis_number=entry.sis_number,
                )
            )

    result.students_rejected = len(result.rejections)
    if not member_ids:
        return result

    try:
        result.roster_removed = sync_class_period_roster(
            conn,
            class_period,
            member_ids,
            upload_id=None,
            deactivate_missing=True,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return result
