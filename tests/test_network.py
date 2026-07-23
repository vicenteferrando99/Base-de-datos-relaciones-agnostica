"""Tests de `app/network.py` (detección de la IP pública, sin red real)."""

import pytest

from app import network


class _FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *args) -> bool:
        return False

    def read(self) -> bytes:
        return self._payload


@pytest.fixture(autouse=True)
def _clean_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cada test parte sin IP cacheada (y se restaura al salir)."""
    monkeypatch.setattr(network, "_cached_ip", None)


def _patch_urlopen(monkeypatch, payload: bytes) -> list:
    calls: list = []

    def fake_urlopen(url, timeout):
        calls.append(url)
        return _FakeResponse(payload)

    monkeypatch.setattr(network.urllib.request, "urlopen", fake_urlopen)
    return calls


def test_parses_and_caches_valid_ip(monkeypatch) -> None:
    calls = _patch_urlopen(monkeypatch, b"203.0.113.7\n")
    assert network.get_public_ip() == "203.0.113.7"
    assert network.get_public_ip() == "203.0.113.7"
    assert len(calls) == 1  # la segunda llamada sale de la caché


def test_invalid_payload_returns_none_and_is_not_cached(monkeypatch) -> None:
    calls = _patch_urlopen(monkeypatch, b"<html>error</html>")
    assert network.get_public_ip() is None
    assert network.get_public_ip() is None
    assert len(calls) == 2  # los fallos NO se cachean: se reintenta


def test_network_error_returns_none(monkeypatch) -> None:
    def boom(url, timeout):
        raise OSError("sin internet")

    monkeypatch.setattr(network.urllib.request, "urlopen", boom)
    assert network.get_public_ip() is None
