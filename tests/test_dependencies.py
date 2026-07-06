"""Tests for shared FastAPI dependencies."""

from __future__ import annotations

from dataclasses import replace

import pytest

from app import config
import app.dependencies as dependencies
from app.dependencies import pin_matches


@pytest.fixture
def scan_pin_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    test_settings = replace(config.settings, scan_pin="1234")
    monkeypatch.setattr(config, "settings", test_settings)
    monkeypatch.setattr(dependencies, "settings", test_settings)


def test_pin_matches_accepts_correct_pin(scan_pin_settings: None) -> None:
    assert pin_matches("1234") is True


def test_pin_matches_rejects_wrong_pin(scan_pin_settings: None) -> None:
    assert pin_matches("0000") is False


def test_pin_matches_rejects_non_digit_pin(scan_pin_settings: None) -> None:
    assert pin_matches("12ab") is False