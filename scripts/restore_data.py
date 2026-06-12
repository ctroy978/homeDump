#!/usr/bin/env python3
"""Restore classroom data from a backup archive."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.config import settings
from app.services.data_backup import BackupError, restore_data_backup


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Restore Homework Makeup classroom data from a backup archive.",
    )
    parser.add_argument(
        "archive",
        type=Path,
        help="Path to a homedump-data-*.tar.gz backup archive",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Restore without an interactive confirmation prompt",
    )
    args = parser.parse_args()

    if not args.yes:
        print("This will replace the current data/ folder with the backup.")
        print("Stop the classroom server before continuing.")
        answer = input("Type restore to continue: ").strip().lower()
        if answer != "restore":
            print("Restore cancelled.")
            return 1

    try:
        summary = restore_data_backup(args.archive, settings.data_dir)
    except BackupError as exc:
        print(f"Restore failed: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"Restore failed: {exc}", file=sys.stderr)
        return 1

    print("Restore completed successfully.")
    print(f"  Restored to: {summary.restored_to}")
    print(f"  Files:       {summary.file_count}")
    if summary.previous_data_path is not None:
        print(f"  Previous data moved to: {summary.previous_data_path}")
    print()
    print("Restart the server when ready:")
    print("  uv run main")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())