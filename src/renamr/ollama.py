"""Owned local Ollama server startup and shutdown.

Only a process started here is ever stopped. Startup forces a literal loopback
bind, disables cloud access and debug output, removes inherited proxy settings,
and discards server output so no raw provider log or undrained pipe exists.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from typing import Any

import structlog

from renamr.local_transport import LocalClientError, LocalOllamaTransport, validate_endpoint

logger = structlog.get_logger(__name__)

NO_CLOUD_ENV = "OLLAMA_NO_CLOUD"
HOST_ENV = "OLLAMA_HOST"
DEBUG_ENV = "OLLAMA_DEBUG"
_POLL_SECONDS = 0.5
_PROXY_ENV_NAMES = frozenset(
    {"http_proxy", "https_proxy", "all_proxy", "no_proxy", "ftp_proxy", "socks_proxy"}
)


def is_running(host: str) -> bool:
    """Return whether a local server answers a bounded readiness request."""
    try:
        LocalOllamaTransport(host, timeout=2).request("GET", "/api/tags", None)
    except (LocalClientError, OSError):
        return False
    return True


def owned_environment(host: str) -> dict[str, str]:
    """Return a local-only, proxy-free environment for an owned server.

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


def start(host: str, startup_timeout: int) -> tuple[subprocess.Popen[Any] | None, bool]:
    """Start an owned local server and wait for readiness.

    Args:
        host: Loopback server URL.
        startup_timeout: Seconds to wait for readiness.

    Returns:
        ``(process, started_by_us)``; process is None when an external server is
        already answering.

    Raises:
        TimeoutError: If the server does not become ready.
        RuntimeError: If the owned process exits during startup.
        EndpointValidationError: If the host is not loopback.
    """
    if is_running(host):
        logger.debug("ollama_already_running", host=host)
        return None, False
    environment = owned_environment(host)
    binary = shutil.which("ollama")
    if binary is None:
        raise RuntimeError("Ollama executable is not installed")
    logger.info("ollama_starting", host=host)
    process = subprocess.Popen(
        [binary, "serve"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
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
    """Stop a locally owned server process."""
    logger.debug("ollama_stopping", pid=process.pid)
    _terminate(process)


def _terminate(process: subprocess.Popen[Any]) -> None:
    """Terminate a process, then kill it if it does not exit promptly."""
    process.terminate()
    try:
        process.wait(timeout=5)
        logger.debug("ollama_stopped_gracefully", pid=process.pid)
    except subprocess.TimeoutExpired:
        logger.warning("ollama_kill_forced", pid=process.pid)
        process.kill()
        process.wait()
