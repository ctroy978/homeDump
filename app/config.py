"""Application configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Project root is one level above app/
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Load .env from project root if present
load_dotenv(PROJECT_ROOT / ".env")

# Default absence codes that qualify a student for makeup homework.
# Override via ALLOWABLE_ABSENCE_CODES in .env (comma-separated).
DEFAULT_ALLOWABLE_CODES: tuple[str, ...] = (
    "Excused Absence",
    "Sports-Athletics",
    "Illness",
    "Appointment",
    "Family Emergency",
    "Field Trip/School A",
    "Tardy Excused",
    "In-School Absence",
    "Nurse's Office",
    "School Activity",
)


def _parse_allowable_codes(raw: str | None) -> tuple[str, ...]:
    if not raw or not raw.strip():
        return DEFAULT_ALLOWABLE_CODES
    return tuple(code.strip() for code in raw.split(",") if code.strip())


@dataclass(frozen=True)
class Settings:
    """Runtime settings for the classroom server."""

    project_root: Path = PROJECT_ROOT
    data_dir: Path = field(
        default_factory=lambda: PROJECT_ROOT / os.getenv("DATA_DIR", "data")
    )
    database_path: Path = field(default_factory=lambda: _default_database_path())
    admin_password: str = field(
        default_factory=lambda: os.getenv("ADMIN_PASSWORD", "changeme")
    )
    secret_key: str = field(
        default_factory=lambda: os.getenv(
            "SECRET_KEY", "dev-secret-change-before-classroom-use"
        )
    )
    host: str = field(default_factory=lambda: os.getenv("HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: int(os.getenv("PORT", "8000")))
    debug: bool = field(
        default_factory=lambda: os.getenv("DEBUG", "false").lower() == "true"
    )
    allowable_codes: tuple[str, ...] = field(
        default_factory=lambda: _parse_allowable_codes(
            os.getenv("ALLOWABLE_ABSENCE_CODES")
        )
    )

    @property
    def attendance_upload_dir(self) -> Path:
        return self.data_dir / "uploads" / "attendance"

    @property
    def assignments_dir(self) -> Path:
        return self.data_dir / "assignments"

    @property
    def qrcodes_dir(self) -> Path:
        return self.data_dir / "qrcodes"

    @property
    def claims_dir(self) -> Path:
        return self.data_dir / "claims"

    public_base_url: str | None = field(
        default_factory=lambda: os.getenv("PUBLIC_BASE_URL") or None
    )

    def ensure_directories(self) -> None:
        """Create data directories if they do not exist yet."""
        for path in (
            self.data_dir,
            self.attendance_upload_dir,
            self.assignments_dir,
            self.qrcodes_dir,
            self.claims_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)


def _default_database_path() -> Path:
    explicit = os.getenv("DATABASE_PATH")
    if explicit:
        return Path(explicit)
    return PROJECT_ROOT / os.getenv("DATA_DIR", "data") / "app.db"


# Single shared settings instance used across the app
settings = Settings()