#!/usr/bin/env python3
"""
Build a named attendance fixture from the anonymized cleanatt.xlsx sample.

Usage:
    uv run python scripts/build_test_fixture.py

Creates anonymized test fixtures with a synthetic Student Name column:
  - tests/fixtures/named_attendance.xlsx
  - tests/fixtures/named_attendance.txt  (tab-delimited, like real exports)
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SOURCE = PROJECT_ROOT / "cleanatt.xlsx"
OUTPUT_XLSX = PROJECT_ROOT / "tests" / "fixtures" / "named_attendance.xlsx"
OUTPUT_TXT = PROJECT_ROOT / "tests" / "fixtures" / "named_attendance.txt"
DEFAULT_STUDENT_NAME = "Test Student A"


def main() -> None:
    if not SOURCE.exists():
        raise SystemExit(
            f"Source file not found: {SOURCE}\n"
            "Place cleanatt.xlsx in the project root first."
        )

    df = pd.read_excel(SOURCE)
    if "Student Name" in df.columns:
        print(f"Student Name column already present in {SOURCE.name}; copying as-is.")
    else:
        df.insert(0, "Student Name", DEFAULT_STUDENT_NAME)

    OUTPUT_XLSX.parent.mkdir(parents=True, exist_ok=True)
    df.to_excel(OUTPUT_XLSX, index=False)
    df.to_csv(OUTPUT_TXT, sep="\t", index=False)

    print(f"Wrote {OUTPUT_XLSX}")
    print(f"Wrote {OUTPUT_TXT}")
    print(f"  rows: {len(df)}")
    print(f"  student: {DEFAULT_STUDENT_NAME}")
    print()
    print("Upload either file at http://localhost:8000/admin/attendance")


if __name__ == "__main__":
    main()