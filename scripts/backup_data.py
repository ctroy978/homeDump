#!/usr/bin/env python3
"""Back up classroom data to a folder such as a USB drive."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.config import settings
from app.services.data_backup import (
    BackupError,
    create_data_backup,
    data_dir_has_backup_content,
)


def _format_size(byte_count: int) -> str:
    if byte_count < 1024:
        return f"{byte_count} B"
    if byte_count < 1024 * 1024:
        return f"{byte_count / 1024:.1f} KB"
    return f"{byte_count / (1024 * 1024):.1f} MB"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Back up Homework Makeup classroom data to a .tar.gz archive.",
    )
    parser.add_argument(
        "destination",
        type=Path,
        help="Folder to write the backup archive (for example a mounted USB drive)",
    )
    args = parser.parse_args()

    data_dir = settings.data_dir
    if not data_dir_has_backup_content(data_dir):
        print(
            "Nothing to back up yet — the data directory is empty.",
            file=sys.stderr,
        )
        return 1

    try:
        summary = create_data_backup(data_dir, args.destination)
    except BackupError as exc:
        print(f"Backup failed: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"Backup failed: {exc}", file=sys.stderr)
        return 1

    print("Backup created successfully.")
    print(f"  Archive: {summary.archive_path}")
    print(f"  Files:   {summary.file_count}")
    print(f"  Size:    {_format_size(summary.byte_count)}")
    print()
    print("To restore later:")
    print(
        f"  uv run python scripts/restore_data.py {summary.archive_path} --yes"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())