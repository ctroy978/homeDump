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


def _parse_public_base_url(raw: str | None) -> str | None:
    """Normalize PUBLIC_BASE_URL from .env (trim whitespace and quotes)."""
    if not raw:
        return None
    value = raw.strip().strip('"').strip("'")
    return value or None


def _parse_github_token(raw: str | None) -> str | None:
    if not raw or not raw.strip():
        return None
    return raw.strip()


def _parse_scan_pin(raw: str | None) -> str | None:
    if not raw or not raw.strip():
        return None
    pin = raw.strip()
    if len(pin) == 4 and pin.isdigit():
        return pin
    raise ValueError("SCAN_PIN must be exactly 4 digits.")


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

    @property
    def attendance_upload_dir(self) -> Path:
        return self.data_dir / "uploads" / "attendance"

    @property
    def assignments_dir(self) -> Path:
        return self.data_dir / "assignments"

    @property
    def claims_dir(self) -> Path:
        return self.data_dir / "claims"

    public_base_url: str | None = field(
        default_factory=lambda: _parse_public_base_url(
            os.getenv("PUBLIC_BASE_URL")
        )
    )
    github_token: str | None = field(
        default_factory=lambda: _parse_github_token(os.getenv("GITHUB_TOKEN"))
    )
    github_owner: str = field(
        default_factory=lambda: os.getenv("GITHUB_OWNER", "krewten-978")
    )
    github_repo_filter: str = field(
        default_factory=lambda: os.getenv("GITHUB_REPO_FILTER", "scope")
    )
    scan_pin: str | None = field(
        default_factory=lambda: _parse_scan_pin(os.getenv("SCAN_PIN"))
    )

    @property
    def github_enabled(self) -> bool:
        return bool(self.github_token)

    @property
    def scan_enabled(self) -> bool:
        return self.github_enabled and bool(self.scan_pin)

    def ensure_directories(self) -> None:
        """Create data directories if they do not exist yet."""
        for path in (
            self.data_dir,
            self.attendance_upload_dir,
            self.assignments_dir,
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