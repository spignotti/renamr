"""Extraction runners: in-process (tests) and bounded subprocess (production).

The subprocess runner enforces limits inside the extraction code and a wall-clock
timeout on the child process. Every return path reports which limits were applied
so an unsupported hard cap is never claimed as enforced.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Protocol

from renamr.core.extract import (
    ExtractionResult,
    ExtractorLimits,
    OcrProvider,
    detect_media_type,
    extract_document,
)

_WORKER_MODULE = "renamr.core.extract_worker"
IN_PROCESS_REPORT: dict[str, str] = {
    "mode": "in_process",
    "wall_clock": "not_enforced",
    "memory": "not_enforced",
    "cpu": "not_enforced",
    "page_and_pixel": "enforced",
}


class ExtractionRunner(Protocol):
    """Runs extraction for one file."""

    def run(self, path: Path, limits: ExtractorLimits) -> ExtractionResult:
        """Extract one file within its limits."""
        ...


class InProcessRunner:
    """Runs extraction in the current process, with an optional OCR provider."""

    def __init__(self, ocr: OcrProvider | None = None) -> None:
        self._ocr = ocr

    def run(self, path: Path, limits: ExtractorLimits) -> ExtractionResult:
        """Extract using the injected OCR provider."""
        result = extract_document(path, limits=limits, ocr=self._ocr)
        return result.model_copy(update={"limits_report": dict(IN_PROCESS_REPORT)})


class SubprocessRunner:
    """Runs extraction in a bounded child process."""

    def __init__(
        self,
        *,
        python: str | None = None,
        use_ocr: bool = True,
        max_memory_mb: int | None = 2048,
        max_cpu_s: int | None = 120,
        timeout_s: float | None = None,
    ) -> None:
        self._python = python or sys.executable
        self._use_ocr = use_ocr
        self._max_memory_mb = max_memory_mb
        self._max_cpu_s = max_cpu_s
        self._timeout_s = timeout_s

    def run(self, path: Path, limits: ExtractorLimits) -> ExtractionResult:
        """Run the worker with a wall-clock timeout and resource limits."""
        timeout = self._timeout_s or limits.timeout_s
        request = {
            "path": str(path),
            "limits": limits.model_dump(),
            "use_ocr": self._use_ocr,
            "max_memory_mb": self._max_memory_mb,
            "max_cpu_s": self._max_cpu_s,
        }
        try:
            completed = subprocess.run(
                [self._python, "-m", _WORKER_MODULE],
                input=json.dumps(request).encode("utf-8"),
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return _failure(path, "timeout", wall_clock="enforced")
        if completed.returncode != 0:
            return _failure(path, "worker_failed", wall_clock="enforced")
        try:
            return ExtractionResult.model_validate_json(completed.stdout)
        except ValueError:
            return _failure(path, "worker_invalid_output", wall_clock="enforced")


def build_runner(
    *, ocr: OcrProvider | None = None, subprocess_mode: bool = True
) -> ExtractionRunner:
    """Return a bounded subprocess runner, or an in-process runner for tests."""
    if subprocess_mode:
        return SubprocessRunner()
    return InProcessRunner(ocr)


def _failure(path: Path, code: str, *, wall_clock: str) -> ExtractionResult:
    """Return an explicit incomplete result for a runner failure with its report."""
    return ExtractionResult(
        media_type=detect_media_type(path),
        pages=[],
        page_count=0,
        incomplete=True,
        errors=[code],
        limits_report={
            "mode": "subprocess",
            "wall_clock": wall_clock,
            "outcome": code,
            "memory": "reported_by_worker",
            "cpu": "reported_by_worker",
        },
    )
