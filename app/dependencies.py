"""Shared FastAPI dependencies."""

from __future__ import annotations

import hashlib
import hmac

from fastapi import HTTPException, Request, status
from fastapi.responses import RedirectResponse

from app.config import settings

ADMIN_COOKIE_NAME = "admin_token"
ADMIN_COOKIE_MAX_AGE = 60 * 60 * 24 * 7  # one week
SCAN_COOKIE_NAME = "scan_token"
SCAN_COOKIE_MAX_AGE = 60 * 60 * 4  # covers a distribution day


def _expected_admin_token() -> str:
    """Deterministic signed token derived from the server secret key."""
    return hmac.new(
        settings.secret_key.encode("utf-8"),
        b"homework-makeup-admin",
        hashlib.sha256,
    ).hexdigest()


def is_admin(request: Request) -> bool:
    token = request.cookies.get(ADMIN_COOKIE_NAME)
    if not token:
        return False
    return hmac.compare_digest(token, _expected_admin_token())


def require_admin(request: Request) -> None:
    """Block admin pages when the teacher is not logged in."""
    if is_admin(request):
        return
    next_path = request.url.path
    raise HTTPException(
        status_code=status.HTTP_303_SEE_OTHER,
        headers={"Location": f"/admin/login?next={next_path}"},
    )


def _expected_scan_token() -> str:
    """Signed cookie value issued after a successful distribution PIN entry."""
    return hmac.new(
        settings.secret_key.encode("utf-8"),
        b"homework-makeup-scan-pin",
        hashlib.sha256,
    ).hexdigest()


def pin_matches(submitted: str) -> bool:
    """Compare a submitted PIN to SCAN_PIN without leaking length via timing."""
    expected = settings.scan_pin
    if not expected:
        return False
    candidate = submitted.strip()
    padded_expected = expected.zfill(4)
    padded_candidate = candidate.zfill(4)
    if len(padded_candidate) != 4 or not padded_candidate.isdigit():
        return False
    return hmac.compare_digest(padded_candidate, padded_expected)


def is_scan_authenticated(request: Request) -> bool:
    token = request.cookies.get(SCAN_COOKIE_NAME)
    if not token:
        return False
    return hmac.compare_digest(token, _expected_scan_token())


def set_scan_cookie(response: RedirectResponse) -> None:
    response.set_cookie(
        SCAN_COOKIE_NAME,
        _expected_scan_token(),
        httponly=True,
        max_age=SCAN_COOKIE_MAX_AGE,
        samesite="lax",
    )


def require_scan_pin(request: Request) -> None:
    """Block distribution registration when the scan PIN session is missing."""
    if not settings.scan_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Distribution scan is not configured.",
        )
    if not is_scan_authenticated(request):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Distribution PIN required.",
        )