"""Regression tests for quiet, private, sanitized diagnostics."""

import json
import logging
from contextlib import closing
from pathlib import Path

import pytest
import structlog

from renamr.logging import _PrivateRotatingFileHandler, setup_logging


def test_diagnostics_are_private_and_sanitized(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    log_path = tmp_path / "renamr.log"
    setup_logging("DEBUG", True, log_path)
    try:
        raise RuntimeError("SYNTHETIC_PROVIDER_BODY")
    except RuntimeError:
        structlog.get_logger("renamr.test").exception(
            "synthetic_failure", error="SYNTHETIC_PROVIDER_BODY"
        )
    logging.getLogger("third_party").warning("SYNTHETIC_PROVIDER_BODY")

    events = log_path.read_text()
    assert "synthetic_failure" in events
    assert "RuntimeError" in events
    assert "test_diagnostics_are_private" in events
    assert "SYNTHETIC_PROVIDER_BODY" not in events
    assert json.loads(events.splitlines()[0])["stack_frames"]
    assert log_path.stat().st_mode & 0o777 == 0o600
    assert capsys.readouterr().err == ""


def test_verbose_diagnostics_are_sanitized(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    setup_logging("DEBUG", False, tmp_path / "renamr.log", verbose=True)
    try:
        raise RuntimeError("SYNTHETIC_PROVIDER_BODY")
    except RuntimeError:
        structlog.get_logger("renamr.test").exception("synthetic_failure")

    output = capsys.readouterr().err
    assert "synthetic_failure" in output
    assert "RuntimeError" in output
    assert "SYNTHETIC_PROVIDER_BODY" not in output
    assert "Traceback (most recent call last)" not in output


def test_rotated_logs_keep_private_permissions(tmp_path: Path) -> None:
    log_path = tmp_path / "renamr.log"
    with closing(_PrivateRotatingFileHandler(log_path, maxBytes=30, backupCount=2)) as handler:
        for _ in range(4):
            handler.emit(logging.LogRecord("renamr.test", logging.ERROR, "", 0, "x" * 20, (), None))

    files = list(tmp_path.glob("renamr.log*"))
    assert len(files) == 3
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in files)


def test_existing_log_backups_become_private(tmp_path: Path) -> None:
    paths = [tmp_path / name for name in ["renamr.log", "renamr.log.1", "renamr.log.2"]]
    for path in paths:
        path.write_text("synthetic history")
        path.chmod(0o644)

    setup_logging("WARNING", False, paths[0])
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in paths)
