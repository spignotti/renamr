"""Private diagnostic logging with optional console output."""

import logging
import os
import sys
import traceback
from io import TextIOWrapper
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import structlog
from structlog.types import EventDict

NOISY_DEPENDENCY_LOGGERS = ("httpx", "httpcore", "openai", "RapidOCR")


def _private_opener(path: str, flags: int) -> int:
    """Create each rotated log with owner-only permissions."""
    return os.open(path, flags, 0o600)


class _PrivateRotatingFileHandler(RotatingFileHandler):
    """Retain private permissions across log rotation."""

    def _open(self) -> TextIOWrapper:
        return open(
            self.baseFilename,
            "a",
            encoding=self.encoding,
            errors=self.errors,
            opener=_private_opener,
        )


class _SafeDependencyFilter(logging.Filter):
    """Exclude unstructured third-party response bodies from diagnostics."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        if not record.name.startswith("renamr."):
            record.msg = "Dependency warning from %s"
            record.args = (record.name,)
        return True


def setup_logging(
    level: str, json_logs: bool, log_path: Path | None = None, verbose: bool = False
) -> None:
    """Configure private rotating diagnostics and opt-in console logging."""
    handlers: list[logging.Handler] = []
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(log_path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        os.close(descriptor)
        for path in [log_path, Path(f"{log_path}.1"), Path(f"{log_path}.2")]:
            if path.exists():
                path.chmod(0o600)
        handlers.append(_PrivateRotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=2))
    if verbose:
        handlers.append(logging.StreamHandler())
    for handler in handlers:
        handler.addFilter(_SafeDependencyFilter())
    logging.basicConfig(
        level=logging.WARNING,
        format="%(message)s",
        handlers=handlers or [logging.NullHandler()],
        force=True,
    )
    logging.getLogger("renamr").setLevel(getattr(logging, level.upper(), logging.WARNING))
    configure_dependency_loggers()
    renderer = (
        structlog.processors.JSONRenderer()
        if json_logs
        else structlog.dev.ConsoleRenderer(colors=False)
    )
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.stdlib.add_log_level,
            _safe_diagnostics,
            renderer,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


def configure_dependency_loggers() -> None:
    """Silence dependency loggers that can expose raw provider response bodies."""
    for logger_name in NOISY_DEPENDENCY_LOGGERS:
        logger = logging.getLogger(logger_name)
        logger.handlers.clear()
        logger.setLevel(logging.WARNING)
        logger.propagate = False
        logger.addHandler(logging.NullHandler())


def _safe_diagnostics(logger: Any, method_name: str, event: EventDict) -> EventDict:
    """Keep exception types and stack locations while excluding response bodies."""
    event.pop("error", None)
    exc_info = event.pop("exc_info", None)
    if exc_info:
        _, exception, tb = sys.exc_info() if exc_info is True else exc_info
        if exception is not None:
            event["error_type"] = type(exception).__name__
        if tb is not None:
            event["stack_frames"] = [
                f"{frame.filename}:{frame.lineno} in {frame.name}"
                for frame in traceback.extract_tb(tb)
            ]
    return event
