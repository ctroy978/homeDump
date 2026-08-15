"""SQLite database helpers and schema initialization."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Generator
from pathlib import Path

from app.config import settings

logger = logging.getLogger(__name__)

# SQL for all tables. Created in Phase 1; populated in later phases.
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS students (
    id INTEGER PRIMARY KEY,
    sis_number TEXT,
    name TEXT NOT NULL,
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

    conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError as exc:
        if "readonly" not in str(exc).lower():
            raise
        logger.warning("Could not enable WAL; database is read-only: %s", exc)
    return conn


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {str(row[1]) for row in rows}


def _students_has_unique_name_constraint(conn: sqlite3.Connection) -> bool:
    """True when an older schema enforced UNIQUE on students.name."""
    for index in conn.execute("PRAGMA index_list(students)").fetchall():
        # index: seq, name, unique, origin, partial
        if not index[2]:
            continue
        index_name = str(index[1])
        cols = [
            str(row[2])
            for row in conn.execute(f"PRAGMA index_info('{index_name}')").fetchall()
        ]
        if cols == ["name"]:
            return True
    create_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'students'"
    ).fetchone()
    if create_sql and create_sql[0]:
        normalized = " ".join(str(create_sql[0]).upper().split())
        if "NAME TEXT NOT NULL UNIQUE" in normalized:
            return True
    return False


def _rebuild_students_without_unique_name(conn: sqlite3.Connection) -> None:
    """Recreate students so display names may collide; SIS remains the unique key."""
    # Foreign keys block DROP TABLE students while attendance_records reference it.
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("DROP TABLE IF EXISTS students_new")
        conn.execute(
            """
            CREATE TABLE students_new (
                id INTEGER PRIMARY KEY,
                sis_number TEXT,
                name TEXT NOT NULL,
                grade TEXT,
                last_attendance_upload_id INTEGER REFERENCES attendance_uploads(id),
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        columns = _table_columns(conn, "students")
        select_bits = [
            "id",
            "sis_number" if "sis_number" in columns else "NULL AS sis_number",
            "name",
            "grade" if "grade" in columns else "NULL AS grade",
            (
                "last_attendance_upload_id"
                if "last_attendance_upload_id" in columns
                else "NULL AS last_attendance_upload_id"
            ),
            (
                "created_at"
                if "created_at" in columns
                else "datetime('now') AS created_at"
            ),
        ]
        conn.execute(
            f"""
            INSERT INTO students_new (
                id, sis_number, name, grade, last_attendance_upload_id, created_at
            )
            SELECT {", ".join(select_bits)} FROM students
            """
        )
        conn.execute("DROP TABLE students")
        conn.execute("ALTER TABLE students_new RENAME TO students")
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


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
    if _students_has_unique_name_constraint(conn):
        _rebuild_students_without_unique_name(conn)

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

    _dedupe_claim_tokens(conn)
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_claim_tokens_identity
        ON claim_tokens (student_id, assignment_id, absence_date, period)
        """
    )

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

    assignment_columns = _table_columns(conn, "assignments")
    if "source" not in assignment_columns:
        conn.execute(
            "ALTER TABLE assignments ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'"
        )
    if "github_repo" not in assignment_columns:
        conn.execute("ALTER TABLE assignments ADD COLUMN github_repo TEXT")
    if "github_path" not in assignment_columns:
        conn.execute("ALTER TABLE assignments ADD COLUMN github_path TEXT")

    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_assignments_github_identity
        ON assignments (github_repo, github_path, assigned_date)
        WHERE source = 'github' AND github_repo IS NOT NULL
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS distribution_events (
            id INTEGER PRIMARY KEY,
            scanned_at TEXT NOT NULL DEFAULT (datetime('now')),
            assigned_date TEXT NOT NULL,
            github_repo TEXT NOT NULL,
            github_path TEXT NOT NULL,
            display_title TEXT NOT NULL,
            periods_requested TEXT NOT NULL,
            periods_added TEXT NOT NULL,
            periods_skipped TEXT NOT NULL,
            assignment_id INTEGER REFERENCES assignments(id),
            outcome TEXT NOT NULL CHECK (outcome IN (
                'success', 'partial', 'all_duplicate', 'failure'
            )),
            error_message TEXT,
            client_ip TEXT
        )
        """
    )


def _dedupe_claim_tokens(conn: sqlite3.Connection) -> None:
    """Keep the oldest row per student/assignment/date/period before uniquing."""
    extras = conn.execute(
        """
        SELECT token
        FROM claim_tokens
        WHERE id NOT IN (
            SELECT MIN(id)
            FROM claim_tokens
            GROUP BY student_id, assignment_id, absence_date, period
        )
        """
    ).fetchall()
    for row in extras:
        token = str(row["token"])
        conn.execute("DELETE FROM print_queue WHERE token = ?", (token,))
        conn.execute("DELETE FROM claim_tokens WHERE token = ?", (token,))


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
    except sqlite3.OperationalError as exc:
        if "readonly" not in str(exc).lower():
            raise
        # TestClient lifespan opens the classroom file as the developer user,
        # who may not own data/app.db. The request-scoped test DB is writable.
        logger.warning("Skipping schema changes; database is read-only: %s", exc)
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