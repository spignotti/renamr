"""Owned local server startup, environment, and shutdown tests."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from renamr.local_transport import EndpointValidationError
from renamr.ollama import (
    DEBUG_ENV,
    HOST_ENV,
    NO_CLOUD_ENV,
    is_running,
    owned_environment,
    start,
    stop,
)


def test_is_running_true_on_200() -> None:
    with patch("renamr.ollama.LocalOllamaTransport") as transport:
        transport.return_value.request.return_value = {"models": []}
        assert is_running("http://127.0.0.1:11434") is True


def test_is_running_false_on_error() -> None:
    from renamr.local_transport import LocalClientError

    with patch("renamr.ollama.LocalOllamaTransport") as transport:
        transport.return_value.request.side_effect = LocalClientError("refused")
        assert is_running("http://127.0.0.1:11434") is False


def test_start_reuses_running_server() -> None:
    with patch("renamr.ollama.is_running", return_value=True):
        process, did_start = start("http://127.0.0.1:11434", startup_timeout=5)
    assert process is None
    assert did_start is False


def test_start_forces_local_environment_and_discards_output() -> None:
    process = MagicMock(spec=subprocess.Popen)
    process.pid = 12345
    process.poll.return_value = None
    with (
        patch("renamr.ollama.is_running", side_effect=[False, True]),
        patch("renamr.ollama.subprocess.Popen", return_value=process) as popen,
        patch("renamr.ollama.shutil.which", return_value="/usr/bin/ollama"),
        patch("renamr.ollama.time.monotonic", side_effect=[0, 0.1, 0.2]),
        patch("renamr.ollama.time.sleep"),
    ):
        started, did_start = start("http://127.0.0.1:11434", startup_timeout=5)
    args, kwargs = popen.call_args
    assert args == (["/usr/bin/ollama", "serve"],)
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL
    assert kwargs["env"][NO_CLOUD_ENV] == "1"
    assert kwargs["env"][HOST_ENV] == "127.0.0.1:11434"
    assert started is process
    assert did_start is True


def test_start_detects_early_exit() -> None:
    process = MagicMock(spec=subprocess.Popen)
    process.pid = 1
    process.poll.return_value = 1
    process.returncode = 1
    with (
        patch("renamr.ollama.is_running", return_value=False),
        patch("renamr.ollama.subprocess.Popen", return_value=process),
        patch("renamr.ollama.shutil.which", return_value="/usr/bin/ollama"),
        patch("renamr.ollama.time.monotonic", side_effect=[0, 0.1]),
    ):
        with pytest.raises(RuntimeError, match="exited during startup"):
            start("http://127.0.0.1:11434", startup_timeout=5)


def test_start_timeout_terminates_process() -> None:
    process = MagicMock(spec=subprocess.Popen)
    process.pid = 1
    process.poll.return_value = None
    with (
        patch("renamr.ollama.is_running", return_value=False),
        patch("renamr.ollama.subprocess.Popen", return_value=process),
        patch("renamr.ollama.shutil.which", return_value="/usr/bin/ollama"),
        patch("renamr.ollama.time.monotonic", side_effect=[0, 1, 2, 3]),
        patch("renamr.ollama.time.sleep"),
    ):
        with pytest.raises(TimeoutError):
            start("http://127.0.0.1:11434", startup_timeout=2)
    process.terminate.assert_called_once()


def test_owned_environment_forces_loopback_and_strips_proxies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:8080")
    monkeypatch.setenv("ALL_PROXY", "socks5://proxy.invalid:1080")
    environment = owned_environment("http://localhost:11500")

    assert environment[NO_CLOUD_ENV] == "1"
    assert environment[HOST_ENV] == "127.0.0.1:11500"
    assert environment[DEBUG_ENV] == "0"
    assert "HTTP_PROXY" not in environment
    assert "ALL_PROXY" not in environment


def test_owned_environment_rejects_remote() -> None:
    with pytest.raises(EndpointValidationError):
        owned_environment("http://example.com:11434")


def test_stop_terminates_and_kills_after_timeout() -> None:
    process = MagicMock(spec=subprocess.Popen)
    process.pid = 1
    process.wait.side_effect = [subprocess.TimeoutExpired(cmd="ollama", timeout=5), 0]
    stop(process)
    process.terminate.assert_called_once()
    process.kill.assert_called_once()
