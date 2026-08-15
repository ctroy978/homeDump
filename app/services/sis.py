"""Shared student-ID (SIS) normalization for import and lookup."""

from __future__ import annotations

import re
import sqlite3

INVALID_SIS_DECIMAL_MESSAGE = (
    "Student ID must not contain a decimal point. Check the SIS number in the export."
)

_WHOLE_NUMBER_DECIMAL_RE = re.compile(r"^-?\d+\.0+$")
_BLANK_TOKENS = frozenset({"", "nan", "<na>", "nat", "none", "null"})


def normalize_sis_number(value: object) -> str | None:
    """
    Normalize a SIS / student ID for storage and lookup.

    - Trims whitespace
    - Returns None when blank after trim
    - Whole-number floats and ``12345.0`` strings become integer text
      (Excel / pandas artifacts)
    - Non-integer decimals (``12.34``) raise ValueError
    - Leading zeros on digit strings are preserved
    - No fixed length
    """
    if value is None:
        return None
    if isinstance(value, bool):
        text = str(value).strip()
        return text or None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value:  # NaN
            return None
        if value.is_integer():
            return str(int(value))
        raise ValueError(INVALID_SIS_DECIMAL_MESSAGE)

    text = str(value).strip()
    if not text or text.lower() in _BLANK_TOKENS:
        return None
    if "." in text:
        if _WHOLE_NUMBER_DECIMAL_RE.fullmatch(text):
            return str(int(float(text)))
        raise ValueError(INVALID_SIS_DECIMAL_MESSAGE)
    return text


def sis_digit_key(sis_number: str) -> str | None:
    """Return a leading-zero-insensitive key when the ID is all digits."""
    if sis_number.isdigit():
        return str(int(sis_number))
    return None


def find_student_row_by_sis(
    conn: sqlite3.Connection,
    sis_number: str,
) -> sqlite3.Row | None:
    """
    Find a student by SIS.

    Exact match wins. If that misses and the ID is all digits, a unique
    leading-zero variant (``001234`` vs ``1234``) is accepted so Excel
    numeric cells do not split one person into two rows.
    """
    row = conn.execute(
        """
        SELECT id, name, sis_number
        FROM students
        WHERE sis_number = ?
        """,
        (sis_number,),
    ).fetchone()
    if row is not None:
        return row

    key = sis_digit_key(sis_number)
    if key is None:
        return None

    matches: list[sqlite3.Row] = []
    candidates = conn.execute(
        """
        SELECT id, name, sis_number
        FROM students
        WHERE sis_number IS NOT NULL
        """
    ).fetchall()
    for candidate in candidates:
        stored = str(candidate["sis_number"])
        if sis_digit_key(stored) == key:
            matches.append(candidate)
            if len(matches) > 1:
                return None
    return matches[0] if matches else None
