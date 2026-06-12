"""Resolve the classroom-facing base URL for links and QR codes."""

from __future__ import annotations

from fastapi import Request

from app.config import settings

_INVALID_HOSTS = frozenset({"0.0.0.0", "127.0.0.1", "localhost", "::1", "[::1]"})


class PublicUrlError(Exception):
    """Raised when the server cannot determine a student-reachable base URL."""


def resolve_public_base_url(request: Request) -> str:
    """
    Return the base URL encoded into claim QR codes.

    Uses ``PUBLIC_BASE_URL`` when set. Otherwise builds from the request's
    ``Host`` header, which reflects the address the browser actually used.
    Addresses like ``0.0.0.0`` and ``localhost`` are rejected because phones
    on the classroom network cannot reach them.
    """
    if settings.public_base_url:
        return settings.public_base_url.rstrip("/")

    host = request.headers.get("host", "").strip()
    if not host:
        raise PublicUrlError(
            "Set PUBLIC_BASE_URL in .env to the address students use in Chrome "
            "(for example http://192.168.1.50:8000)."
        )

    hostname = host.split(":", 1)[0].lower().strip("[]")
    if hostname in _INVALID_HOSTS:
        raise PublicUrlError(
            "Set PUBLIC_BASE_URL in .env to the address students use in Chrome "
            f"(for example http://192.168.1.50:8000). QR codes cannot use {hostname}."
        )

    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    return f"{scheme}://{host}".rstrip("/")


def suggest_public_base_url(request: Request) -> str | None:
    """Return a student-friendly URL hint for the admin dashboard, if known."""
    if settings.public_base_url:
        return settings.public_base_url.rstrip("/")

    try:
        return resolve_public_base_url(request)
    except PublicUrlError:
        return None