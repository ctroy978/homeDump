"""Command-line entry point for starting the classroom server."""

from __future__ import annotations

import uvicorn

from app.config import settings


def main() -> None:
    """Start the FastAPI app with host/port from .env."""
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
    )


if __name__ == "__main__":
    main()