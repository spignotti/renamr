"""Local OCR adapter for image-based files."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

# Thresholds for usability heuristic
_MIN_TOTAL_CHARS = 20
_MIN_MEAN_CONFIDENCE = 0.5

# Cached engine instance — created once, reused for all files in a run.
# Type: RapidOCR instance (callable with signature str -> RapidOCROutput)
_EngineCallable = Callable[[str], Any]
_engine: _EngineCallable | None = None


class OCRResult:
    """Structured output from the OCR engine."""

    __slots__ = ("line_count", "mean_confidence", "text")

    def __init__(self, text: str, mean_confidence: float, line_count: int) -> None:
        self.text = text
        self.mean_confidence = mean_confidence
        self.line_count = line_count


def _get_engine() -> _EngineCallable:
    """Return the cached RapidOCR engine, creating it on first call."""
    global _engine
    if _engine is None:
        from rapidocr import RapidOCR

        _engine = RapidOCR()
    return _engine


def ocr_image(image_path: str | Any) -> OCRResult | None:
    """Run OCR on an image file path and return structured results.

    Args:
        image_path: Path-like object pointing to an image file.

    Returns:
        OCRResult if OCR produced output, None on failure.
    """
    try:
        engine = _get_engine()
        result = engine(str(image_path))
        texts = result.txts or ()
        scores = result.scores or ()

        if not texts:
            return None

        full_text = "\n".join(str(t) for t in texts if t)
        mean_confidence = float(sum(scores)) / len(scores) if scores else 0.0
        return OCRResult(
            text=full_text,
            mean_confidence=mean_confidence,
            line_count=len(texts),
        )
    except Exception as exc:
        logger.warning("ocr_failed", path=str(image_path), error=str(exc))
        return None


def is_usable_ocr(result: OCRResult | None) -> bool:
    """Decide whether OCR output is sufficient for text-based metadata extraction.

    Returns True when the OCR text is long enough and confidence is reasonable.
    """
    if result is None:
        return False
    if len(result.text.strip()) < _MIN_TOTAL_CHARS:
        return False
    if result.mean_confidence < _MIN_MEAN_CONFIDENCE:
        return False
    return True
