"""Supervision for a locally owned Ollama process with sanitized diagnostics.

Reuses an already-running eligible server without claiming ownership and only
stops a process it started. Server output is discarded so no raw provider log
file is created and no pipe is left undrained; only sanitized lifecycle events
and the process exit code are kept.
"""

from __future__ import annotations

import contextlib
import subprocess
from collections.abc import Generator
from typing import Any

import structlog

from renamr.ollama import start, stop

logger = structlog.get_logger(__name__)


class OwnedServer:
    """Lifecycle owner for one locally started Ollama process."""

    def __init__(
        self,
        *,
        endpoint: str = "http://127.0.0.1:11434",
        startup_timeout: int = 15,
    ) -> None:
        self._endpoint = endpoint
        self._startup_timeout = startup_timeout
        self._process: subprocess.Popen[Any] | None = None

    @property
    def started_by_us(self) -> bool:
        """Return whether this supervisor started the running process."""
        return self._process is not None

    def start_if_needed(self) -> bool:
        """Start an owned server when none is running.

        Server output always goes to the null device, so no provider log file is
        ever created or followed.

        Returns:
            True when this supervisor started the process, False when an
            external server is reused.
        """
        process, did_start = start(self._endpoint, self._startup_timeout)
        if not did_start:
            return False
        self._process = process
        return True

    def stop(self) -> None:
        """Stop only a process this supervisor started."""
        if self._process is None:
            return
        process, self._process = self._process, None
        stop(process)
        logger.debug("owned_server_stopped", exit_code=process.returncode)

    @contextlib.contextmanager
    def lifecycle(self) -> Generator[bool, None, None]:
        """Yield whether an owned process was started, then clean up."""
        did_start = self.start_if_needed()
        try:
            yield did_start
        finally:
            if did_start:
                self.stop()
