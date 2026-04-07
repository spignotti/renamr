"""Ollama lifecycle management for local model support."""

import contextlib
import subprocess
import time
from collections.abc import Generator
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen

import structlog

from renamr.models import OllamaConfig

logger = structlog.get_logger(__name__)


def is_running(host: str) -> bool:
    """Check if Ollama server is responding at the given host.

    Args:
        host: Ollama server URL (e.g., "http://localhost:11434")

    Returns:
        True if server responds with HTTP 200, False otherwise
    """
    try:
        with urlopen(f"{host}/api/tags", timeout=2) as response:
            return response.status == 200
    except (URLError, TimeoutError):
        return False


def start(host: str, startup_timeout: int) -> tuple[subprocess.Popen[Any] | None, bool]:
    """Start Ollama server and wait for it to become ready.

    Args:
        host: Ollama server URL for health check
        startup_timeout: Seconds to wait for server to become ready

    Returns:
        Tuple of (process, did_we_start_it). Process is None if already running.

    Raises:
        TimeoutError: If server fails to start within startup_timeout seconds
    """
    if is_running(host):
        logger.debug("ollama_already_running", host=host)
        return None, False

    logger.info("ollama_starting", host=host)
    process = subprocess.Popen(
        ["ollama", "serve"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    deadline = time.monotonic() + startup_timeout
    while time.monotonic() < deadline:
        if is_running(host):
            logger.info("ollama_ready", host=host, pid=process.pid)
            return process, True
        time.sleep(0.5)

    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
    raise TimeoutError(f"Ollama failed to start within {startup_timeout}s")


def stop(process: subprocess.Popen[Any]) -> None:
    """Stop an Ollama server process.

    Args:
        process: The subprocess.Popen instance to terminate
    """
    logger.debug("ollama_stopping", pid=process.pid)
    process.terminate()
    try:
        process.wait(timeout=5)
        logger.debug("ollama_stopped_gracefully", pid=process.pid)
    except subprocess.TimeoutExpired:
        logger.warning("ollama_kill_forced", pid=process.pid)
        process.kill()
        process.wait()


@contextlib.contextmanager
def ollama_lifecycle(config: OllamaConfig, needs_ollama: bool) -> Generator[None, None, None]:
    """Context manager that starts/stops Ollama when needed.

    Only acts if needs_ollama is True and auto_start is enabled.
    Stops the server after the run if stop_after_run is enabled.

    Args:
        config: Ollama lifecycle configuration
        needs_ollama: Whether any inbox uses an Ollama model

    Yields:
        None
    """
    if not needs_ollama or not config.auto_start:
        yield
        return

    process, did_start = start(config.host, config.startup_timeout)
    try:
        yield
    finally:
        if did_start and config.stop_after_run and process is not None:
            stop(process)
