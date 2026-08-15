"""Unit tests for shared SIS normalization."""

from __future__ import annotations

import math

import pytest

from app.services.sis import (
    INVALID_SIS_DECIMAL_MESSAGE,
    normalize_sis_number,
    sis_digit_key,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        ("", None),
        ("   ", None),
        ("nan", None),
        (float("nan"), None),
        (10001, "10001"),
        (10001.0, "10001"),
        (" 10001 ", "10001"),
        ("001234", "001234"),
        ("12345.0", "12345"),
        ("12345.000", "12345"),
    ],
)
def test_normalize_sis_number(raw: object, expected: str | None) -> None:
    if isinstance(raw, float) and math.isnan(raw):
        assert normalize_sis_number(raw) is None
        return
    assert normalize_sis_number(raw) == expected


@pytest.mark.parametrize("raw", [12.5, "12.34", "12345.10"])
def test_normalize_sis_number_rejects_true_decimals(raw: object) -> None:
    with pytest.raises(ValueError, match="decimal"):
        normalize_sis_number(raw)
    assert "decimal" in INVALID_SIS_DECIMAL_MESSAGE.lower()


def test_sis_digit_key() -> None:
    assert sis_digit_key("001234") == "1234"
    assert sis_digit_key("1234") == "1234"
    assert sis_digit_key("12A") is None
    assert sis_digit_key("0") == "0"
