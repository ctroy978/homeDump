"""Shared FastAPI dependencies."""

from __future__ import annotations

import hashlib
import hmac

from fastapi import HTTPException, Request, status

from app.config import settings

ADMIN_COOKIE_NAME = "admin_token"
ADMIN_COOKIE_MAX_AGE = 60 * 60 * 24 * 7  # one week


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