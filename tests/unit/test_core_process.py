"""Owned-server supervision tests: no raw provider logs, hardened environment."""

from __future__ import annotations

import inspect

import pytest

from renamr.core import process
from renamr.core.process import OwnedServer


def test_owned_server_has_no_raw_log_surface() -> None:
    parameters = inspect.signature(OwnedServer.__init__).parameters
    assert "log_path" not in parameters
    assert "max_log_bytes" not in parameters
    assert not hasattr(OwnedServer, "_open_log")


def test_owned_server_discards_server_output(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, int, dict[str, object]]] = []

    def fake_start(endpoint: str, startup_timeout: int, **kwargs: object) -> tuple[None, bool]:
        calls.append((endpoint, startup_timeout, kwargs))
        return None, False

    monkeypatch.setattr(process, "start", fake_start)
    server = OwnedServer(endpoint="http://127.0.0.1:11434", startup_timeout=5)

    assert server.start_if_needed() is False
    assert server.started_by_us is False
    assert calls == [("http://127.0.0.1:11434", 5, {})]


def test_owned_server_stops_only_owned_process(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Process:
        returncode = 0

    owned = _Process()
    monkeypatch.setattr(process, "start", lambda *_args, **_kwargs: (owned, True))
    stopped: list[object] = []
    monkeypatch.setattr(process, "stop", lambda target: stopped.append(target))

    server = OwnedServer()
    assert server.start_if_needed() is True
    assert server.started_by_us is True

    server.stop()

    assert stopped == [owned]
    assert server.started_by_us is False


def test_owned_server_does_not_stop_external_process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(process, "start", lambda *_args, **_kwargs: (None, False))
    stopped: list[object] = []
    monkeypatch.setattr(process, "stop", lambda target: stopped.append(target))

    server = OwnedServer()
    assert server.start_if_needed() is False

    server.stop()

    assert stopped == []
