"""Ollama lifecycle management for a locally owned server process.

Only a process started here is ever stopped. Startup sets ``OLLAMA_NO_CLOUD=1``
and an explicit loopback bind so the server cannot reach a cloud backend or bind
a non-loopback interface. Output is discarded or redirected to a bounded file;
no pipe is left undrained. A process that exits during startup is detected.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import time
from collections.abc import Generator
from typing import Any, BinaryIO
from urllib.error import URLError
from urllib.request import urlopen

import structlog

from renamr.local_transport import validate_endpoint
from renamr.models import OllamaConfig

logger = structlog.get_logger(__name__)

NO_CLOUD_ENV = "OLLAMA_NO_CLOUD"
HOST_ENV = "OLLAMA_HOST"
DEBUG_ENV = "OLLAMA_DEBUG"
_POLL_SECONDS = 0.5
_PROXY_ENV_NAMES = frozenset(
    {"http_proxy", "https_proxy", "all_proxy", "no_proxy", "ftp_proxy", "socks_proxy"}
)


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


def owned_environment(host: str) -> dict[str, str]:
    """Return the environment for a locally owned server.

    The bind address is forced to a literal loopback address, cloud access and
    debug logging are disabled, and any inherited proxy variables are removed so
    the owned server cannot consult a proxy or emit raw payload logs, regardless
    of the caller's environment.

    Args:
        host: Requested server URL.

    Returns:
        A copy of the process environment with local-only overrides.

    Raises:
        EndpointValidationError: If the requested host is not loopback.
    """
    address, port = validate_endpoint(host)
    bind = f"[{address}]:{port}" if ":" in address else f"{address}:{port}"
    environment = {
        key: value for key, value in os.environ.items() if key.lower() not in _PROXY_ENV_NAMES
    }
    environment[HOST_ENV] = bind
    environment[NO_CLOUD_ENV] = "1"
    environment[DEBUG_ENV] = "0"
    return environment


def start(
    host: str,
    startup_timeout: int,
    *,
    log_handle: BinaryIO | None = None,
) -> tuple[subprocess.Popen[Any] | None, bool]:
    """Start a locally owned Ollama server and wait for readiness.

    Args:
        host: Ollama server URL for the readiness check; must be loopback.
        startup_timeout: Seconds to wait for the server to become ready.
        log_handle: Optional binary file for bounded server output. When omitted,
            output is discarded so no pipe is ever left undrained.

    Returns:
        Tuple of (process, did_we_start_it). Process is None if already running.

    Raises:
        TimeoutError: If the server fails to start within ``startup_timeout``.
        RuntimeError: If the owned process exits during startup.
        EndpointValidationError: If ``host`` is not loopback.
    """
    if is_running(host):
        logger.debug("ollama_already_running", host=host)
        return None, False

    environment = owned_environment(host)
    stdout: Any = log_handle if log_handle is not None else subprocess.DEVNULL
    stderr: Any = subprocess.STDOUT if log_handle is not None else subprocess.DEVNULL
    logger.info("ollama_starting", host=host)
    process = subprocess.Popen(
        ["ollama", "serve"],
        stdout=stdout,
        stderr=stderr,
        env=environment,
    )

    deadline = time.monotonic() + startup_timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Ollama server exited during startup (code {process.returncode})")
        if is_running(host):
            logger.info("ollama_ready", host=host, pid=process.pid)
            return process, True
        time.sleep(_POLL_SECONDS)

    _terminate(process)
    raise TimeoutError(f"Ollama failed to start within {startup_timeout}s")


def stop(process: subprocess.Popen[Any]) -> None:
    """Stop a locally owned Ollama server process.

    Args:
        process: The subprocess.Popen instance to terminate.
    """
    if process is None:
        return
    logger.debug("ollama_stopping", pid=process.pid)
    _terminate(process)


def _terminate(process: subprocess.Popen[Any]) -> None:
    """Terminate a process then kill it if it does not exit promptly."""
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
    """Context manager that starts/stops an owned Ollama server when needed.

    Only acts if ``needs_ollama`` is True and ``auto_start`` is enabled. A server
    started by this context is stopped afterward when ``stop_after_run`` is set;
    a server owned by another application is never stopped.

    Args:
        config: Ollama lifecycle configuration.
        needs_ollama: Whether any inbox uses an Ollama model.

    Yields:
        None.
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
