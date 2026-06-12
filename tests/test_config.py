"""Tests for environment configuration parsing."""

from __future__ import annotations

from app.config import _parse_public_base_url


def test_parse_public_base_url_strips_quotes_and_whitespace() -> None:
    assert _parse_public_base_url('  "http://classroom-pc.local:8000"  ') == (
        "http://classroom-pc.local:8000"
    )
    assert _parse_public_base_url("") is None
    assert _parse_public_base_url(None) is None