"""Bounded extraction worker executed in a separate process.

The parent runs this module as ``python -m renamr.core.extract_worker`` and passes
a JSON job on stdin; the worker applies resource limits, performs extraction, and
writes a JSON :class:`ExtractionResult` to stdout. Running parsing and OCR out of
process keeps an uninterruptible call from taking down the caller.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from renamr.core.extract import ExtractorLimits, OCRText, default_ocr, extract_document

_BYTES_PER_MB = 1024 * 1024


def apply_limits(max_memory_mb: int | None, max_cpu_s: int | None) -> dict[str, str]:
    """Apply best-effort process resource limits.

    Args:
        max_memory_mb: Address-space/data limit in megabytes.
        max_cpu_s: CPU-time limit in seconds.

    Returns:
        A mapping of limit name to ``applied``/``unsupported`` for diagnostics.
    """
    import resource

    applied: dict[str, str] = {}
    if max_memory_mb:
        soft = max_memory_mb * _BYTES_PER_MB
        for name in ("RLIMIT_AS", "RLIMIT_DATA"):
            constant = getattr(resource, name, None)
            if constant is None:
                continue
            try:
                resource.setrlimit(constant, (soft, soft))
                applied[name] = "applied"
                break
            except (ValueError, OSError):
                applied[name] = "unsupported"
    if max_cpu_s:
        constant = getattr(resource, "RLIMIT_CPU", None)
        if constant is not None:
            try:
                resource.setrlimit(constant, (int(max_cpu_s), int(max_cpu_s) + 1))
                applied["RLIMIT_CPU"] = "applied"
            except (ValueError, OSError):
                applied["RLIMIT_CPU"] = "unsupported"
    return applied


def _no_ocr(_: Path) -> OCRText | None:
    """Return no OCR output when the job disables OCR."""
    return None


def main() -> int:
    """Read a JSON job, extract, and print the result."""
    try:
        job = json.loads(sys.stdin.buffer.read() or b"{}")
    except json.JSONDecodeError:
        sys.stderr.write("invalid job\n")
        return 2
    applied = apply_limits(job.get("max_memory_mb"), job.get("max_cpu_s"))
    limits = ExtractorLimits.model_validate(job.get("limits") or {})
    provider = default_ocr if job.get("use_ocr", True) else _no_ocr
    result = extract_document(Path(job["path"]), limits=limits, ocr=provider)
    result = result.model_copy(update={"limits_report": applied})
    sys.stdout.write(result.model_dump_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
