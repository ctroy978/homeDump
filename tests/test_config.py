"""Tests for environment configuration parsing."""

from __future__ import annotations

import pytest

from app.config import (
    Settings,
    _parse_github_token,
    _parse_public_base_url,
    _parse_scan_pin,
)


def test_parse_public_base_url_strips_quotes_and_whitespace() -> None:
    assert _parse_public_base_url('  "http://classroom-pc.local:8000"  ') == (
        "http://classroom-pc.local:8000"
    )
    assert _parse_public_base_url("") is None
    assert _parse_public_base_url(None) is None


def test_parse_github_token() -> None:
    assert _parse_github_token("  ghp_test  ") == "ghp_test"
    assert _parse_github_token("") is None
    assert _parse_github_token(None) is None


@pytest.mark.parametrize(
    "raw",
    ["1234", " 5678 "],
)
def test_parse_scan_pin_accepts_four_digits(raw: str) -> None:
    assert _parse_scan_pin(raw) == raw.strip()


@pytest.mark.parametrize(
    "raw",
    [None, "", "   "],
)
def test_parse_scan_pin_missing_is_none(raw: str | None) -> None:
    assert _parse_scan_pin(raw) is None


@pytest.mark.parametrize(
    "raw",
    ["123", "12345", "12ab", "abcd"],
)
def test_parse_scan_pin_rejects_malformed(raw: str) -> None:
    with pytest.raises(ValueError, match="exactly 4 digits"):
        _parse_scan_pin(raw)


def test_settings_github_and_scan_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("SCAN_PIN", raising=False)

    disabled = Settings()
    assert disabled.github_enabled is False
    assert disabled.scan_enabled is False

    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
    github_only = Settings()
    assert github_only.github_enabled is True
    assert github_only.scan_enabled is False

    monkeypatch.setenv("SCAN_PIN", "4321")
    enabled = Settings()
    assert enabled.github_enabled is True
    assert enabled.scan_enabled is True
    assert enabled.github_owner == "krewten-978"
    assert enabled.github_repo_filter == "scope"