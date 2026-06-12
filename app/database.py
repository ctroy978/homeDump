"""SQLite database helpers and schema initialization."""

from __future__ import annotations

import sqlite3
from collections.abc import Generator
from pathlib import Path

from app.config import settings

# SQL for all tables. Created in Phase 1; populated in later phases.
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS students (
    id INTEGER PRIMARY KEY,
    sis_number TEXT,
    name TEXT NOT NULL UNIQUE,
    grade TEXT,
    last_attendance_upload_id INTEGER REFERENCES attendance_uploads(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS attendance_uploads (
    id INTEGER PRIMARY KEY,
    filename TEXT NOT NULL,
    uploaded_at TEXT NOT NULL DEFAULT (datetime('now')),
    row_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS attendance_records (
    id INTEGER PRIMARY KEY,
    student_id INTEGER NOT NULL REFERENCES students(id),
    absence_date TEXT NOT NULL,
    period INTEGER NOT NULL CHECK (period BETWEEN 0 AND 7),
    absence_code TEXT NOT NULL,
    note TEXT,
    upload_id INTEGER REFERENCES attendance_uploads(id),
    UNIQUE(student_id, absence_date, period)
);

CREATE TABLE IF NOT EXISTS assignments (
    id INTEGER PRIMARY KEY,
    assigned_date TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    pdf_filename TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS assignment_periods (
    assignment_id INTEGER NOT NULL REFERENCES assignments(id) ON DELETE CASCADE,
    period INTEGER NOT NULL CHECK (period BETWEEN 0 AND 7),
    PRIMARY KEY (assignment_id, period)
);

CREATE TABLE IF NOT EXISTS claim_tokens (
    id INTEGER PRIMARY KEY,
    token TEXT NOT NULL UNIQUE,
    student_id INTEGER NOT NULL REFERENCES students(id),
    assignment_id INTEGER NOT NULL REFERENCES assignments(id),
    period INTEGER NOT NULL CHECK (period BETWEEN 0 AND 7),
    absence_date TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS claim_logs (
    id INTEGER PRIMARY KEY,
    student_name TEXT NOT NULL,
    assignment_id INTEGER REFERENCES assignments(id),
    period INTEGER,
    absence_date TEXT,
    token TEXT,
    client_ip TEXT,
    user_agent TEXT,
    success INTEGER NOT NULL,
    message TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS print_queue (
    id INTEGER PRIMARY KEY,
    token TEXT NOT NULL UNIQUE REFERENCES claim_tokens(token),
    queued_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def get_connection(db_path: Path | None = None) -> sqlite3.Connection:
    """Open a SQLite connection with sensible defaults for a web app."""
    path = db_path or settings.database_path
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {str(row[1]) for row in rows}


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Add columns and tables introduced after the initial schema."""
    student_columns = _table_columns(conn, "students")
    if "sis_number" not in student_columns:
        conn.execute("ALTER TABLE students ADD COLUMN sis_number TEXT")
    if "last_attendance_upload_id" not in student_columns:
        conn.execute(
            """
            ALTER TABLE students
            ADD COLUMN last_attendance_upload_id INTEGER
            REFERENCES attendance_uploads(id)
            """
        )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_students_sis_number
        ON students(sis_number)
        WHERE sis_number IS NOT NULL
        """
    )

    claim_columns = _table_columns(conn, "claim_tokens")
    if "period" not in claim_columns:
        conn.execute("ALTER TABLE claim_tokens ADD COLUMN period INTEGER")
    if "printed_at" not in claim_columns:
        conn.execute("ALTER TABLE claim_tokens ADD COLUMN printed_at TEXT")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS assignment_periods (
            assignment_id INTEGER NOT NULL REFERENCES assignments(id) ON DELETE CASCADE,
            period INTEGER NOT NULL CHECK (period BETWEEN 0 AND 7),
            PRIMARY KEY (assignment_id, period)
        )
        """
    )
    assignment_columns = _table_columns(conn, "assignments")
    if "period" in assignment_columns:
        conn.execute(
            """
            INSERT OR IGNORE INTO assignment_periods (assignment_id, period)
            SELECT id, period FROM assignments WHERE period IS NOT NULL
            """
        )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS print_queue (
            id INTEGER PRIMARY KEY,
            token TEXT NOT NULL UNIQUE REFERENCES claim_tokens(token),
            queued_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )


def init_schema(conn: sqlite3.Connection | None = None) -> None:
    """
    Create all tables if they are missing.

    Safe to call on every startup — uses CREATE TABLE IF NOT EXISTS.
    """
    owns_connection = conn is None
    db = conn or get_connection()
    try:
        db.executescript(SCHEMA_SQL)
        _apply_migrations(db)
        db.commit()
    finally:
        if owns_connection:
            db.close()


def get_db() -> Generator[sqlite3.Connection, None, None]:
    """
    FastAPI dependency that yields a database connection per request.

    Usage (in later phases):
        def endpoint(db: sqlite3.Connection = Depends(get_db)):
            ...
    """
    conn = get_connection()
    try:
        yield conn
    finally:
        conn.close()


def list_tables(conn: sqlite3.Connection | None = None) -> list[str]:
    """Return table names currently in the database (useful for health checks)."""
    owns_connection = conn is None
    db = conn or get_connection()
    try:
        rows = db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        return [row[0] for row in rows]
    finally:
        if owns_connection:
            db.close()