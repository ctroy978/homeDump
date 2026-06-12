"""Tests for classroom public URL resolution."""

from __future__ import annotations

import types

import pytest
from starlette.requests import Request

from app import config
from app.public_url import (
    PublicUrlError,
    hostname_url_hints,
    resolve_public_base_url,
    suggest_public_base_url,
)


def _request(
    host: str,
    *,
    scheme: str = "http",
    public_base_url: str | None = None,
) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [(b"host", host.encode())],
        "scheme": scheme,
        "server": ("testserver", 8000),
        "client": ("127.0.0.1", 12345),
    }
    return Request(scope)


def test_resolve_uses_public_base_url_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    test_settings = types.SimpleNamespace(public_base_url="http://192.168.1.50:8000")
    monkeypatch.setattr(config, "settings", test_settings)
    monkeypatch.setattr("app.public_url.settings", test_settings)
    url = resolve_public_base_url(_request("0.0.0.0:8000"))
    assert url == "http://192.168.1.50:8000"


def test_resolve_uses_request_host_for_classroom_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    test_settings = types.SimpleNamespace(public_base_url=None)
    monkeypatch.setattr(config, "settings", test_settings)
    monkeypatch.setattr("app.public_url.settings", test_settings)
    url = resolve_public_base_url(_request("192.168.1.50:8000"))
    assert url == "http://192.168.1.50:8000"


def test_resolve_rejects_zero_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    test_settings = types.SimpleNamespace(public_base_url=None)
    monkeypatch.setattr(config, "settings", test_settings)
    monkeypatch.setattr("app.public_url.settings", test_settings)
    with pytest.raises(PublicUrlError, match="PUBLIC_BASE_URL"):
        resolve_public_base_url(_request("0.0.0.0:8000"))


def test_suggest_returns_none_for_invalid_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    test_settings = types.SimpleNamespace(public_base_url=None)
    monkeypatch.setattr(config, "settings", test_settings)
    monkeypatch.setattr("app.public_url.settings", test_settings)
    assert suggest_public_base_url(_request("0.0.0.0:8000")) is None


def test_resolve_accepts_hostname(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    test_settings = types.SimpleNamespace(public_base_url=None)
    monkeypatch.setattr(config, "settings", test_settings)
    monkeypatch.setattr("app.public_url.settings", test_settings)
    url = resolve_public_base_url(_request("classroom-pc.local:8000"))
    assert url == "http://classroom-pc.local:8000"


def test_resolve_rejects_invalid_public_base_url_in_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    test_settings = types.SimpleNamespace(
        public_base_url="http://0.0.0.0:8000",
    )
    monkeypatch.setattr(config, "settings", test_settings)
    monkeypatch.setattr("app.public_url.settings", test_settings)
    with pytest.raises(PublicUrlError, match="0.0.0.0"):
        resolve_public_base_url(_request("classroom-pc.local:8000"))


def test_resolve_rejects_malformed_public_base_url_in_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    test_settings = types.SimpleNamespace(public_base_url="classroom-pc.local:8000")
    monkeypatch.setattr(config, "settings", test_settings)
    monkeypatch.setattr("app.public_url.settings", test_settings)
    with pytest.raises(PublicUrlError, match="scheme and host"):
        resolve_public_base_url(_request("classroom-pc.local:8000"))


def test_hostname_url_hints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    test_settings = types.SimpleNamespace(port=8000)
    monkeypatch.setattr("app.public_url.settings", test_settings)
    monkeypatch.setattr("app.public_url.socket.gethostname", lambda: "classroom-pc")
    assert hostname_url_hints() == [
        "http://classroom-pc:8000",
        "http://classroom-pc.local:8000",
    ]