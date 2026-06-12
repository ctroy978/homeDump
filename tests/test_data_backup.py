"""Tests for classroom data backup and restore."""

from __future__ import annotations

import sqlite3
import tarfile
from pathlib import Path

import pytest

from app.database import init_schema
from app.services.data_backup import (
    BACKUP_PREFIX,
    BackupError,
    backup_archive_name,
    create_data_backup,
    data_dir_has_backup_content,
    restore_data_backup,
    write_data_backup,
)


def _seed_data_dir(data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = data_dir / "app.db"
    conn = sqlite3.connect(db_path)
    init_schema(conn)
    conn.execute("INSERT INTO students (name, grade) VALUES ('Backup Student', '10')")
    conn.commit()
    conn.close()

    assignment_dir = data_dir / "assignments" / "1"
    assignment_dir.mkdir(parents=True, exist_ok=True)
    (assignment_dir / "original.pdf").write_bytes(b"%PDF-1.4 backup-test")
    (data_dir / "uploads" / "attendance").mkdir(parents=True, exist_ok=True)
    (data_dir / "uploads" / "attendance" / "sample.txt").write_text(
        "attendance snapshot",
        encoding="utf-8",
    )


def test_create_and_restore_data_backup_round_trip(tmp_path: Path) -> None:
    source_data = tmp_path / "source-data"
    destination = tmp_path / "usb"
    restore_target = tmp_path / "restored-data"
    _seed_data_dir(source_data)

    summary = create_data_backup(source_data, destination)
    assert summary.archive_path.exists()
    assert summary.archive_path.name.startswith(f"{BACKUP_PREFIX}-")
    assert summary.file_count >= 3

    with tarfile.open(summary.archive_path, "r:gz") as tar:
        names = tar.getnames()
    assert "backup-manifest.json" in names
    assert any(name.startswith("data/") for name in names)

    restore_summary = restore_data_backup(summary.archive_path, restore_target)
    assert restore_summary.file_count == summary.file_count
    assert (restore_target / "app.db").exists()
    assert (restore_target / "assignments" / "1" / "original.pdf").exists()

    conn = sqlite3.connect(restore_target / "app.db")
    row = conn.execute("SELECT name FROM students").fetchone()
    conn.close()
    assert row[0] == "Backup Student"


def test_restore_moves_existing_data_aside(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    backup_dir = tmp_path / "backups"
    _seed_data_dir(data_dir)
    (data_dir / "marker.txt").write_text("old", encoding="utf-8")

    archive = create_data_backup(data_dir, backup_dir).archive_path
    (data_dir / "marker.txt").write_text("changed", encoding="utf-8")

    summary = restore_data_backup(archive, data_dir)
    assert summary.previous_data_path is not None
    assert summary.previous_data_path.exists()
    assert (summary.previous_data_path / "marker.txt").read_text() == "changed"
    assert (data_dir / "marker.txt").read_text() == "old"


def test_write_data_backup_rejects_empty_directory(tmp_path: Path) -> None:
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    archive = tmp_path / "empty.tar.gz"

    with pytest.raises(BackupError, match="empty"):
        write_data_backup(empty_dir, archive)


def test_data_dir_has_backup_content(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    assert not data_dir_has_backup_content(data_dir)
    _seed_data_dir(data_dir)
    assert data_dir_has_backup_content(data_dir)


def test_backup_archive_name_uses_standard_prefix() -> None:
    assert backup_archive_name().startswith(f"{BACKUP_PREFIX}-")


def test_restore_rejects_invalid_archive(tmp_path: Path) -> None:
    bad_archive = tmp_path / "bad.tar.gz"
    bad_archive.write_text("not a tarball", encoding="utf-8")
    target = tmp_path / "data"

    with pytest.raises(BackupError, match="not a valid"):
        restore_data_backup(bad_archive, target)