"""Page-addressable extraction for PDFs, images, and text files.

Each page carries its own method (embedded text or OCR), real confidence, page
rotation, and quality. Mixed PDFs decide per page. Multi-frame images produce one
page per frame. Malformed, encrypted, or over-limit inputs produce explicit
incomplete states instead of a silent empty result. Original bytes are only
read; nothing here rewrites or compresses a source file.
"""

from __future__ import annotations

import math
import tempfile
from collections.abc import Callable
from pathlib import Path

import pymupdf
import structlog
from PIL import Image, ImageSequence
from pydantic import BaseModel, ConfigDict, Field

from renamr.core.catalog import PageMethod, PageRecord, QualityState

logger = structlog.get_logger(__name__)

IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif", ".webp"})
TEXT_SUFFIXES = frozenset({".txt", ".md", ".text", ".csv", ".log"})
PDF_SUFFIX = ".pdf"

_OCR_MIN_CONFIDENCE = 0.5
_MIN_RENDER_DPI = 72.0


class ExtractorLimits(BaseModel):
    """Bounds enforced during extraction."""

    model_config = ConfigDict(extra="forbid")

    max_pages: int = Field(default=50, ge=1)
    max_render_pixels: int = Field(default=40_000_000, ge=1)
    max_file_bytes: int = Field(default=200 * 1024 * 1024, ge=1)
    max_output_chars: int = Field(default=200_000, ge=1)
    timeout_s: float = Field(default=120.0, gt=0)
    dpi: int = Field(default=200, ge=_MIN_RENDER_DPI)

    def signature(self) -> str:
        """Return a stable signature of the limits that affect extracted text."""
        return f"{self.max_pages}:{self.max_render_pixels}:{self.max_file_bytes}:{self.dpi}"


class OCRText(BaseModel):
    """OCR output with a real, positive confidence value."""

    model_config = ConfigDict(extra="forbid")

    text: str
    confidence: float = Field(ge=0.0, le=1.0)
    line_count: int = Field(default=1, ge=0)


class ExtractionResult(BaseModel):
    """Extraction outcome with explicit completeness and sanitized errors."""

    model_config = ConfigDict(extra="forbid")

    media_type: str
    pages: list[PageRecord]
    page_count: int
    incomplete: bool
    errors: list[str] = []
    limits_report: dict[str, str] = {}

    @property
    def text(self) -> str:
        """Return the concatenated page text."""
        return "\n".join(page.text for page in self.pages if page.text)


class AssetStatus(BaseModel):
    """Offline preflight result for OCR assets."""

    model_config = ConfigDict(extra="forbid")

    available: bool
    detail: str


OcrProvider = Callable[[Path], OCRText | None]


def detect_media_type(path: Path) -> str:
    """Return a media type based on the file suffix."""
    suffix = path.suffix.lower()
    if suffix == PDF_SUFFIX:
        return "application/pdf"
    if suffix in TEXT_SUFFIXES:
        return "text/plain"
    if suffix in IMAGE_SUFFIXES:
        return f"image/{suffix.lstrip('.').replace('jpg', 'jpeg').replace('tif', 'tiff')}"
    return "application/octet-stream"


def estimate_page_count(path: Path) -> int:
    """Return a cheap page/frame count estimate without hashing or OCR."""
    suffix = path.suffix.lower()
    if suffix == PDF_SUFFIX:
        document: pymupdf.Document | None = None
        try:
            document = pymupdf.open(str(path))
            return max(0, document.page_count)
        except Exception:
            return 1
        finally:
            if document is not None:
                document.close()
    if suffix in IMAGE_SUFFIXES:
        try:
            with Image.open(path) as image:
                return max(1, int(getattr(image, "n_frames", 1)))
        except Exception:
            return 1
    return 1


def default_ocr(path: Path) -> OCRText | None:
    """Run the bundled OCR engine, returning None on failure.

    The engine is reused from the existing adapter; this module never downloads
    model weights.
    """
    from renamr.ocr import ocr_image

    try:
        result = ocr_image(str(path))
    except Exception:
        return None
    if result is None or not result.text.strip():
        return None
    confidence = float(result.mean_confidence)
    if confidence <= 0.0:
        confidence = 0.0
    return OCRText(text=result.text, confidence=min(confidence, 1.0), line_count=result.line_count)


def preflight_ocr_assets(probe: Callable[[], tuple[bool, str]] | None = None) -> AssetStatus:
    """Check OCR assets offline without triggering a download.

    Args:
        probe: Optional injectable probe returning ``(available, detail)``.

    Returns:
        The asset status; a missing engine or bundled models make it unavailable.
    """
    checker = probe or _default_ocr_probe
    available, detail = checker()
    return AssetStatus(available=available, detail=detail)


def extract_document(
    path: Path,
    *,
    limits: ExtractorLimits | None = None,
    ocr: OcrProvider | None = None,
) -> ExtractionResult:
    """Extract page-addressable text from a supported file.

    Args:
        path: Source file; only read.
        limits: Optional bounds override.
        ocr: Optional OCR provider injected for tests; defaults to the bundled
            engine.

    Returns:
        The extraction result with per-page provenance and an explicit
        incompleteness flag.
    """
    bounds = limits or ExtractorLimits()
    provider = ocr or default_ocr
    media_type = detect_media_type(path)
    try:
        size = path.stat().st_size
    except OSError:
        return _result(media_type, [], 0, incomplete=True, errors=["unreadable"])
    if size > bounds.max_file_bytes:
        return _result(media_type, [], 0, incomplete=True, errors=["file_too_large"])
    suffix = path.suffix.lower()
    if suffix == PDF_SUFFIX:
        return _extract_pdf(path, bounds, provider, media_type)
    if suffix in IMAGE_SUFFIXES:
        return _extract_image(path, bounds, provider, media_type)
    if suffix in TEXT_SUFFIXES:
        return _extract_text(path, bounds, media_type)
    return _result(media_type, [], 0, incomplete=True, errors=["unsupported_type"])


def _extract_pdf(
    path: Path, limits: ExtractorLimits, ocr: OcrProvider, media_type: str
) -> ExtractionResult:
    """Extract each PDF page, deciding per page between embedded text and OCR."""
    document: pymupdf.Document | None = None
    try:
        document = pymupdf.open(str(path))
        if document.needs_pass or document.is_encrypted:
            return _result(media_type, [], 0, incomplete=True, errors=["encrypted"])
        page_count = document.page_count
        if page_count == 0:
            return _result(media_type, [], 0, incomplete=True, errors=["empty_document"])
        selected = min(page_count, limits.max_pages)
        errors: list[str] = []
        if selected < page_count:
            errors.append("page_limit")
        pages = [_pdf_page(document, index, limits, ocr, errors) for index in range(selected)]
        incomplete = selected < page_count or any(page.incomplete for page in pages)
        return _result(media_type, pages, page_count, incomplete=incomplete, errors=errors)
    except Exception as exc:
        logger.warning("pdf_extraction_failed", error_type=type(exc).__name__)
        return _result(media_type, [], 0, incomplete=True, errors=["unreadable"])
    finally:
        if document is not None:
            document.close()


def _pdf_page(
    document: pymupdf.Document,
    index: int,
    limits: ExtractorLimits,
    ocr: OcrProvider,
    errors: list[str],
) -> PageRecord:
    """Extract one PDF page with embedded text or OCR fallback."""
    page = document.load_page(index)
    rotation = int(page.rotation) % 360
    extracted = page.get_text("text")
    text = extracted if isinstance(extracted, str) else ""
    if text.strip():
        return PageRecord(
            page_number=index + 1,
            text=text,
            method=PageMethod.PDF_TEXT,
            rotation=rotation,
            quality=QualityState.OK,
        )
    image_path = _render_page(page, limits)
    if image_path is None:
        _note(errors, "render_limit")
        return PageRecord(
            page_number=index + 1,
            text="",
            method=PageMethod.NONE,
            rotation=rotation,
            quality=QualityState.INCOMPLETE,
            incomplete=True,
        )
    try:
        return _page_from_image(index + 1, image_path, rotation, ocr, errors)
    finally:
        image_path.unlink(missing_ok=True)


def _extract_image(
    path: Path, limits: ExtractorLimits, ocr: OcrProvider, media_type: str
) -> ExtractionResult:
    """Extract every frame of an image, including multi-frame formats."""
    pages: list[PageRecord] = []
    errors: list[str] = []
    try:
        with Image.open(path) as image:
            frame_total = getattr(image, "n_frames", 1)
            selected = min(frame_total, limits.max_pages)
            if selected < frame_total:
                errors.append("page_limit")
            for index, frame in enumerate(ImageSequence.Iterator(image)):
                if index >= selected:
                    break
                frame_path = _save_frame(frame, limits)
                if frame_path is None:
                    _note(errors, "render_limit")
                    pages.append(_empty_page(index + 1, QualityState.INCOMPLETE))
                    continue
                try:
                    pages.append(_page_from_image(index + 1, frame_path, 0, ocr, errors))
                finally:
                    frame_path.unlink(missing_ok=True)
        incomplete = selected < frame_total or any(page.incomplete for page in pages)
        return _result(media_type, pages, frame_total, incomplete=incomplete, errors=errors)
    except Exception as exc:
        logger.warning("image_extraction_failed", error_type=type(exc).__name__)
        return _result(media_type, [], 0, incomplete=True, errors=["unreadable"])


def _extract_text(path: Path, limits: ExtractorLimits, media_type: str) -> ExtractionResult:
    """Read a text file as a single page, flagging output truncation."""
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return _result(media_type, [], 0, incomplete=True, errors=["unreadable"])
    truncated = len(content) > limits.max_output_chars
    page = PageRecord(
        page_number=1,
        text=content[: limits.max_output_chars],
        method=PageMethod.TEXT,
        quality=QualityState.OK,
        incomplete=truncated,
    )
    return _result(
        media_type,
        [page],
        1,
        incomplete=truncated,
        errors=["char_limit"] if truncated else [],
    )


def _page_from_image(
    page_number: int,
    image_path: Path,
    rotation: int,
    ocr: OcrProvider,
    errors: list[str],
) -> PageRecord:
    """Run OCR on a rendered image and build a page record with real confidence."""
    try:
        result = ocr(image_path)
    except Exception as exc:
        logger.warning("ocr_provider_failed", error_type=type(exc).__name__)
        result = None
    if result is None or not result.text.strip():
        _note(errors, "ocr_failed")
        return PageRecord(
            page_number=page_number,
            text="",
            method=PageMethod.NONE,
            rotation=rotation,
            quality=QualityState.FAILED,
            incomplete=True,
        )
    quality = QualityState.OK if result.confidence >= _OCR_MIN_CONFIDENCE else QualityState.LOW
    return PageRecord(
        page_number=page_number,
        text=result.text,
        method=PageMethod.OCR,
        confidence=result.confidence,
        rotation=rotation,
        quality=quality,
        incomplete=quality is not QualityState.OK,
    )


def _render_page(page: pymupdf.Page, limits: ExtractorLimits) -> Path | None:
    """Render a PDF page to a bounded temp PNG, or None when too large."""
    dpi = _bounded_dpi(page.rect.width, page.rect.height, limits)
    if dpi is None:
        return None
    try:
        pixmap = page.get_pixmap(dpi=dpi)
    except Exception:
        return None
    return _save_pixmap(pixmap)


def _save_pixmap(pixmap: pymupdf.Pixmap) -> Path | None:
    """Save a pixmap to a temp PNG file, cleaning up on failure."""
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
            temp_path = Path(handle.name)
        pixmap.save(str(temp_path))
        return temp_path
    except Exception:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        return None


def _save_frame(frame: Image.Image, limits: ExtractorLimits) -> Path | None:
    """Save one image frame to a bounded temp PNG."""
    width, height = frame.size
    if width * height > limits.max_render_pixels:
        return None
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
            temp_path = Path(handle.name)
        frame.convert("RGB").save(temp_path, format="PNG")
        return temp_path
    except Exception:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        return None


def _bounded_dpi(width: float, height: float, limits: ExtractorLimits) -> float | None:
    """Return a DPI whose rendered area fits the pixel bound, or None."""
    if width <= 0 or height <= 0:
        return None
    base = limits.dpi
    pixels = (width / 72.0 * base) * (height / 72.0 * base)
    if pixels <= limits.max_render_pixels:
        return base
    allowed = math.sqrt(limits.max_render_pixels / (width * height)) * 72.0
    if allowed < _MIN_RENDER_DPI:
        return None
    return allowed


def _empty_page(page_number: int, quality: QualityState) -> PageRecord:
    """Return an incomplete page with no text."""
    return PageRecord(
        page_number=page_number,
        text="",
        method=PageMethod.NONE,
        quality=quality,
        incomplete=True,
    )


def _note(errors: list[str], code: str) -> None:
    """Append a sanitized error code once."""
    if code not in errors:
        errors.append(code)


def _result(
    media_type: str,
    pages: list[PageRecord],
    page_count: int,
    *,
    incomplete: bool,
    errors: list[str],
) -> ExtractionResult:
    """Build an extraction result with de-duplicated error codes."""
    unique: list[str] = []
    for code in errors:
        if code not in unique:
            unique.append(code)
    return ExtractionResult(
        media_type=media_type,
        pages=pages,
        page_count=page_count,
        incomplete=incomplete,
        errors=unique,
    )


def _default_ocr_probe() -> tuple[bool, str]:
    """Check for a bundled OCR engine and model files without downloading."""
    try:
        import rapidocr
    except Exception:
        return False, "rapidocr_not_installed"
    module_path = getattr(rapidocr, "__file__", None)
    if not module_path:
        return False, "rapidocr_path_unknown"
    models = list(Path(module_path).parent.rglob("*.onnx"))
    if not models:
        return False, "ocr_models_missing"
    return True, "bundled_models_present"
