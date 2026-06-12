"""Resolve the classroom-facing base URL for links and QR codes."""

from __future__ import annotations

import socket
from urllib.parse import urlparse

from fastapi import Request

from app.config import settings

_INVALID_HOSTS = frozenset({"0.0.0.0", "127.0.0.1", "localhost", "::1", "[::1]"})


class PublicUrlError(Exception):
    """Raised when the server cannot determine a student-reachable base URL."""


def _hostname_from_base_url(base_url: str) -> str:
    parsed = urlparse(base_url)
    if not parsed.scheme or not parsed.netloc:
        raise PublicUrlError(
            "PUBLIC_BASE_URL must include a scheme and host, for example "
            "http://classroom-pc.local:8000."
        )
    hostname = (parsed.hostname or "").lower().strip("[]")
    if not hostname:
        raise PublicUrlError(
            "PUBLIC_BASE_URL must include a scheme and host, for example "
            "http://classroom-pc.local:8000."
        )
    return hostname


def _reject_unreachable_host(hostname: str) -> None:
    if hostname in _INVALID_HOSTS:
        raise PublicUrlError(
            "Set PUBLIC_BASE_URL in .env to the address students use in Chrome "
            f"(for example http://192.168.1.50:8000 or http://classroom-pc.local:8000). "
            f"QR codes cannot use {hostname}."
        )


def resolve_public_base_url(request: Request) -> str:
    """
    Return the base URL encoded into claim QR codes.

    Uses ``PUBLIC_BASE_URL`` when set. Otherwise builds from the request's
    ``Host`` header, which reflects the address the browser actually used.
    Addresses like ``0.0.0.0`` and ``localhost`` are rejected because phones
    on the classroom network cannot reach them.
    """
    if settings.public_base_url:
        configured = settings.public_base_url.rstrip("/")
        _reject_unreachable_host(_hostname_from_base_url(configured))
        return configured

    host = request.headers.get("host", "").strip()
    if not host:
        raise PublicUrlError(
            "Set PUBLIC_BASE_URL in .env to the address students use in Chrome "
            "(for example http://192.168.1.50:8000 or http://classroom-pc.local:8000)."
        )

    hostname = host.split(":", 1)[0].lower().strip("[]")
    _reject_unreachable_host(hostname)

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


def hostname_url_hints(port: int | None = None) -> list[str]:
    """
    Suggest hostname-based URLs teachers can try on the classroom network.

    Useful when the LAN IP changes but the computer name stays the same.
    """
    listen_port = port if port is not None else settings.port
    name = socket.gethostname().strip().lower()
    if not name or name in _INVALID_HOSTS:
        return []

    hints: list[str] = []
    for host in (name, f"{name}.local"):
        hints.append(f"http://{host}:{listen_port}")
    return hints