"""Logging setup helpers."""

import logging

import structlog

NOISY_DEPENDENCY_LOGGERS = ("httpx", "httpcore", "litellm", "openai")


def setup_logging(level: str, json_logs: bool) -> None:
    """Configure stdlib logging and structlog."""
    log_level = getattr(logging, level.upper(), logging.WARNING)
    processors = [
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.stdlib.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    renderer = (
        structlog.processors.JSONRenderer()
        if json_logs
        else structlog.dev.ConsoleRenderer(colors=False)
    )
    logging.basicConfig(
        level=logging.WARNING,
        format="%(message)s",
        force=True,
    )
    logging.getLogger("renamr").setLevel(log_level)
    _configure_dependency_loggers()
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            *processors,
            renderer,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


def _configure_dependency_loggers() -> None:
    """Keep noisy dependency loggers quiet and routed through root only."""
    for logger_name in NOISY_DEPENDENCY_LOGGERS:
        logger = logging.getLogger(logger_name)
        logger.handlers.clear()
        logger.setLevel(logging.WARNING)
        logger.propagate = True
