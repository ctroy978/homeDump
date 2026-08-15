"""SQLite connection defaults for a busy classroom."""

from __future__ import annotations

from pathlib import Path

from app.database import get_connection, init_schema


def test_file_database_uses_wal_and_busy_timeout(tmp_path: Path) -> None:
    path = tmp_path / "classroom.db"
    conn = get_connection(path)
    try:
        init_schema(conn)
        mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        assert mode == "wal"
        timeout = int(conn.execute("PRAGMA busy_timeout").fetchone()[0])
        assert timeout == 30_000
    finally:
        conn.close()
