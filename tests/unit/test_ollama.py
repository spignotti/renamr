"""Ollama lifecycle management tests."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from renamr.models import OllamaConfig
from renamr.ollama import (
    DEBUG_ENV,
    HOST_ENV,
    NO_CLOUD_ENV,
    is_running,
    ollama_lifecycle,
    owned_environment,
    start,
    stop,
)


class TestIsRunning:
    """Tests for is_running() function."""

    def test_returns_true_when_server_responds_with_200(self) -> None:
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=None)

        with patch("renamr.ollama.urlopen", return_value=mock_response):
            result = is_running("http://localhost:11434")

        assert result is True

    def test_returns_false_when_server_returns_non_200(self) -> None:
        mock_response = MagicMock()
        mock_response.status = 404
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=None)

        with patch("renamr.ollama.urlopen", return_value=mock_response):
            result = is_running("http://localhost:11434")

        assert result is False

    def test_returns_false_on_connection_error(self) -> None:
        from urllib.error import URLError

        with patch("renamr.ollama.urlopen", side_effect=URLError("Connection refused")):
            result = is_running("http://localhost:11434")

        assert result is False

    def test_returns_false_on_timeout(self) -> None:
        with patch("renamr.ollama.urlopen", side_effect=TimeoutError()):
            result = is_running("http://localhost:11434")

        assert result is False


class TestStart:
    """Tests for start() function."""

    def test_returns_none_when_already_running(self) -> None:
        with patch("renamr.ollama.is_running", return_value=True):
            process, did_start = start("http://localhost:11434", startup_timeout=15)

        assert process is None
        assert did_start is False

    def test_starts_process_when_not_running(self) -> None:
        mock_process = MagicMock(spec=subprocess.Popen)
        mock_process.pid = 12345
        mock_process.poll.return_value = None

        with (
            patch("renamr.ollama.is_running", side_effect=[False, True]),
            patch("renamr.ollama.subprocess.Popen", return_value=mock_process) as mock_popen,
            patch("renamr.ollama.time.monotonic", side_effect=[0, 0.1, 0.2]),
            patch("renamr.ollama.time.sleep"),
        ):
            process, did_start = start("http://localhost:11434", startup_timeout=15)

        mock_popen.assert_called_once()
        args, kwargs = mock_popen.call_args
        assert args == (["ollama", "serve"],)
        assert kwargs["stdout"] is subprocess.DEVNULL
        assert kwargs["stderr"] is subprocess.DEVNULL
        assert kwargs["env"][NO_CLOUD_ENV] == "1"
        assert kwargs["env"][HOST_ENV] == "127.0.0.1:11434"
        assert process is mock_process
        assert did_start is True

    def test_start_detects_early_exit(self) -> None:
        mock_process = MagicMock(spec=subprocess.Popen)
        mock_process.pid = 12345
        mock_process.poll.return_value = 1
        mock_process.returncode = 1

        with (
            patch("renamr.ollama.is_running", return_value=False),
            patch("renamr.ollama.subprocess.Popen", return_value=mock_process),
            patch("renamr.ollama.time.monotonic", side_effect=[0, 0.1]),
        ):
            with pytest.raises(RuntimeError, match="exited during startup"):
                start("http://localhost:11434", startup_timeout=15)

    def test_start_redirects_logs_to_bounded_file(self, tmp_path: Path) -> None:
        mock_process = MagicMock(spec=subprocess.Popen)
        mock_process.pid = 12345
        mock_process.poll.return_value = None
        log_path = tmp_path / "ollama.log"
        with log_path.open("wb") as handle:
            with (
                patch("renamr.ollama.is_running", side_effect=[False, True]),
                patch("renamr.ollama.subprocess.Popen", return_value=mock_process) as mock_popen,
                patch("renamr.ollama.time.monotonic", side_effect=[0, 0.1, 0.2]),
                patch("renamr.ollama.time.sleep"),
            ):
                start("http://localhost:11434", startup_timeout=15, log_handle=handle)
        _, kwargs = mock_popen.call_args
        assert kwargs["stdout"] is handle
        assert kwargs["stderr"] is subprocess.STDOUT

    def test_raises_timeout_when_server_fails_to_start(self) -> None:
        mock_process = MagicMock(spec=subprocess.Popen)
        mock_process.pid = 12345
        mock_process.poll.return_value = None

        with (
            patch("renamr.ollama.is_running", return_value=False),
            patch("renamr.ollama.subprocess.Popen", return_value=mock_process),
            patch("renamr.ollama.time.monotonic", side_effect=[0, 1, 2, 3]),
            patch("renamr.ollama.time.sleep"),
        ):
            with pytest.raises(TimeoutError, match="Ollama failed to start within 2s"):
                start("http://localhost:11434", startup_timeout=2)

        # Should have terminated the process on timeout
        mock_process.terminate.assert_called_once()
        mock_process.wait.assert_called_once()


class TestOwnedEnvironment:
    """Tests for the private local-only subprocess environment."""

    def test_owned_environment_forces_loopback_and_no_cloud(self) -> None:
        environment = owned_environment("http://localhost:11500")
        assert environment[NO_CLOUD_ENV] == "1"
        assert environment[HOST_ENV] == "127.0.0.1:11500"
        assert environment[DEBUG_ENV] == "0"

    def test_owned_environment_strips_inherited_proxies(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:8080")
        monkeypatch.setenv("https_proxy", "http://proxy.invalid:8080")
        monkeypatch.setenv("ALL_PROXY", "socks5://proxy.invalid:1080")

        environment = owned_environment("http://localhost:11434")

        assert "HTTP_PROXY" not in environment
        assert "https_proxy" not in environment
        assert "ALL_PROXY" not in environment

    def test_owned_environment_rejects_non_loopback(self) -> None:
        from renamr.local_transport import EndpointValidationError

        with pytest.raises(EndpointValidationError):
            owned_environment("http://example.com:11434")


class TestStop:
    """Tests for stop() function."""

    def test_terminates_process_gracefully(self) -> None:
        mock_process = MagicMock(spec=subprocess.Popen)
        mock_process.pid = 12345
        mock_process.wait.return_value = 0

        stop(mock_process)

        mock_process.terminate.assert_called_once()
        mock_process.wait.assert_called_once_with(timeout=5)

    def test_kills_process_when_terminate_times_out(self) -> None:
        mock_process = MagicMock(spec=subprocess.Popen)
        mock_process.pid = 12345
        # First call raises TimeoutExpired, second call (after kill) succeeds
        mock_process.wait.side_effect = [
            subprocess.TimeoutExpired(cmd="ollama", timeout=5),
            0,
        ]

        stop(mock_process)

        mock_process.terminate.assert_called_once()
        mock_process.kill.assert_called_once()
        assert mock_process.wait.call_count == 2


class TestOllamaLifecycle:
    """Tests for ollama_lifecycle context manager."""

    def test_no_action_when_auto_start_disabled(self) -> None:
        config = OllamaConfig(auto_start=False, stop_after_run=True)

        with patch("renamr.ollama.start") as mock_start:
            with ollama_lifecycle(config, needs_ollama=True):
                pass

        mock_start.assert_not_called()

    def test_no_action_when_not_needs_ollama(self) -> None:
        config = OllamaConfig(auto_start=True, stop_after_run=True)

        with patch("renamr.ollama.start") as mock_start:
            with ollama_lifecycle(config, needs_ollama=False):
                pass

        mock_start.assert_not_called()

    def test_starts_and_stops_when_enabled_and_needed(self) -> None:
        config = OllamaConfig(auto_start=True, stop_after_run=True)
        mock_process = MagicMock(spec=subprocess.Popen)

        with (
            patch("renamr.ollama.start", return_value=(mock_process, True)) as mock_start,
            patch("renamr.ollama.stop") as mock_stop,
        ):
            with ollama_lifecycle(config, needs_ollama=True):
                pass

        mock_start.assert_called_once_with(config.host, config.startup_timeout)
        mock_stop.assert_called_once_with(mock_process)

    def test_does_not_stop_when_stop_after_run_disabled(self) -> None:
        config = OllamaConfig(auto_start=True, stop_after_run=False)
        mock_process = MagicMock(spec=subprocess.Popen)

        with (
            patch("renamr.ollama.start", return_value=(mock_process, True)) as mock_start,
            patch("renamr.ollama.stop") as mock_stop,
        ):
            with ollama_lifecycle(config, needs_ollama=True):
                pass

        mock_start.assert_called_once()
        mock_stop.assert_not_called()

    def test_does_not_stop_when_we_did_not_start_it(self) -> None:
        config = OllamaConfig(auto_start=True, stop_after_run=True)

        with (
            patch("renamr.ollama.start", return_value=(None, False)) as mock_start,
            patch("renamr.ollama.stop") as mock_stop,
        ):
            with ollama_lifecycle(config, needs_ollama=True):
                pass

        mock_start.assert_called_once()
        mock_stop.assert_not_called()

    def test_stops_even_on_exception(self) -> None:
        config = OllamaConfig(auto_start=True, stop_after_run=True)
        mock_process = MagicMock(spec=subprocess.Popen)

        with (
            patch("renamr.ollama.start", return_value=(mock_process, True)),
            patch("renamr.ollama.stop") as mock_stop,
        ):
            with pytest.raises(ValueError, match="test error"):
                with ollama_lifecycle(config, needs_ollama=True):
                    raise ValueError("test error")

        mock_stop.assert_called_once_with(mock_process)
