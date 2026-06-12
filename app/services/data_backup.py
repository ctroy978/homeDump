"""Create and restore compressed backups of classroom data."""

from __future__ import annotations

import json
import shutil
import tarfile
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

BACKUP_PREFIX = "homedump-data"
MANIFEST_NAME = "backup-manifest.json"
ARCHIVE_DATA_PREFIX = "data"


class BackupError(Exception):
    """Raised when a backup or restore operation cannot complete."""


@dataclass(frozen=True)
class BackupSummary:
    """Metadata about a created backup archive."""

    archive_path: Path
    file_count: int
    byte_count: int


@dataclass(frozen=True)
class RestoreSummary:
    """Metadata about a completed restore."""

    restored_to: Path
    previous_data_path: Path | None
    file_count: int


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _iter_data_files(data_dir: Path) -> list[Path]:
    if not data_dir.exists():
        return []
    return sorted(path for path in data_dir.rglob("*") if path.is_file())


def _validate_archive(archive_path: Path) -> None:
    if not archive_path.exists():
        raise BackupError(f"Backup archive not found: {archive_path}")
    if not archive_path.is_file():
        raise BackupError(f"Backup path is not a file: {archive_path}")
    if not tarfile.is_tarfile(archive_path):
        raise BackupError("Backup file is not a valid .tar.gz archive.")


def _archive_has_data_members(tar: tarfile.TarFile) -> bool:
    for member in tar.getmembers():
        if member.isfile() and member.name.startswith(f"{ARCHIVE_DATA_PREFIX}/"):
            return True
    return False


def backup_archive_name() -> str:
    """Return the standard timestamped filename for a backup download."""
    return f"{BACKUP_PREFIX}-{_timestamp()}.tar.gz"


def data_dir_has_backup_content(data_dir: Path) -> bool:
    """Return whether the data directory contains files worth archiving."""
    return bool(_iter_data_files(data_dir))


def write_data_backup(data_dir: Path, archive_path: Path) -> BackupSummary:
    """
    Write a compressed backup archive to ``archive_path``.

    Only classroom data is included — not application code or ``.env`` secrets.
    """
    files = _iter_data_files(data_dir)
    if not files:
        raise BackupError("Nothing to back up — the data directory is empty.")

    archive_path.parent.mkdir(parents=True, exist_ok=True)
    byte_count = sum(path.stat().st_size for path in files)
    manifest = {
        "format_version": 1,
        "app": "homework-makeup",
        "created_at": _utc_now(),
        "file_count": len(files),
        "byte_count": byte_count,
    }

    with tarfile.open(archive_path, "w:gz") as tar:
        manifest_bytes = json.dumps(manifest, indent=2).encode("utf-8")
        manifest_info = tarfile.TarInfo(name=MANIFEST_NAME)
        manifest_info.size = len(manifest_bytes)
        tar.addfile(manifest_info, BytesIO(manifest_bytes))

        for path in files:
            arcname = Path(ARCHIVE_DATA_PREFIX) / path.relative_to(data_dir)
            tar.add(path, arcname=str(arcname))

    return BackupSummary(
        archive_path=archive_path,
        file_count=len(files),
        byte_count=byte_count,
    )


def create_data_backup(data_dir: Path, destination_dir: Path) -> BackupSummary:
    """
    Compress the ``data/`` directory into a timestamped .tar.gz archive.

    The archive is suitable for copying to a USB drive.
    """
    destination_dir.mkdir(parents=True, exist_ok=True)
    archive_path = destination_dir / backup_archive_name()
    return write_data_backup(data_dir, archive_path)


def restore_data_backup(
    archive_path: Path,
    data_dir: Path,
    *,
    preserve_previous: bool = True,
) -> RestoreSummary:
    """
    Restore classroom data from a backup archive.

    When ``preserve_previous`` is true, the existing ``data/`` folder is moved
    aside to ``data.before-restore-<timestamp>`` before extraction.
    """
    _validate_archive(archive_path)
    data_dir = data_dir.resolve()
    parent = data_dir.parent

    previous_data_path: Path | None = None
    if data_dir.exists() and any(data_dir.iterdir()) and preserve_previous:
        previous_data_path = parent / f"data.before-restore-{_timestamp()}"
        shutil.move(str(data_dir), str(previous_data_path))

    data_dir.mkdir(parents=True, exist_ok=True)
    restored_files = 0

    with tarfile.open(archive_path, "r:gz") as tar:
        if not _archive_has_data_members(tar):
            raise BackupError(
                "Backup archive does not contain any files under data/."
            )

        for member in tar.getmembers():
            if not member.isfile():
                continue
            if not member.name.startswith(f"{ARCHIVE_DATA_PREFIX}/"):
                continue

            relative = Path(member.name).relative_to(ARCHIVE_DATA_PREFIX)
            destination = data_dir / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            extracted = tar.extractfile(member)
            if extracted is None:
                raise BackupError(f"Could not extract {member.name} from archive.")
            destination.write_bytes(extracted.read())
            restored_files += 1

    return RestoreSummary(
        restored_to=data_dir,
        previous_data_path=previous_data_path,
        file_count=restored_files,
    )