"""Preview extraction helpers for supported file types."""

from __future__ import annotations

import base64
import errno
import io
import tempfile
import time
from pathlib import Path

import fitz
import structlog
from PIL import Image
from pypdf import PdfReader

from renamr.ocr import OCRResult, is_usable_ocr, ocr_image

logger = structlog.get_logger(__name__)
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif", ".webp"}
_MAX_PREVIEW_CHARS = 1000

# Type for the OCR engine — could be RapidOCR or None before first use
_OCREngine = object


def render_pdf_page(pdf_path: Path, dpi: int = 200) -> Path | None:
    """Render the first PDF page into a temporary PNG file."""
    document: fitz.Document | None = None
    temp_path: Path | None = None
    try:
        document = fitz.open(str(pdf_path))
        if document.page_count == 0:
            return None
        page = document.load_page(0)
        pixmap = page.get_pixmap(dpi=dpi)
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as file_handle:
            temp_path = Path(file_handle.name)
        pixmap.save(str(temp_path))
        return temp_path
    except Exception as exc:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        logger.warning("pdf_render_failed", path=str(pdf_path), error=str(exc))
        return None
    finally:
        if document is not None:
            document.close()


def _render_pdf_pages(pdf_path: Path, max_pages: int, dpi: int = 200) -> list[Path]:
    """Render up to max_pages PDF pages into temporary PNG files.

    Returns a list of paths to temp images. Caller is responsible for cleanup.
    """
    rendered: list[Path] = []
    document: fitz.Document | None = None
    try:
        document = fitz.open(str(pdf_path))
        page_count = min(document.page_count, max_pages)
        for page_idx in range(page_count):
            page = document.load_page(page_idx)
            pixmap = page.get_pixmap(dpi=dpi)
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as file_handle:
                temp_path = Path(file_handle.name)
            pixmap.save(str(temp_path))
            rendered.append(temp_path)
    except Exception as exc:
        # Clean up any already-rendered pages
        for path in rendered:
            path.unlink(missing_ok=True)
        logger.warning("pdf_render_failed", path=str(pdf_path), error=str(exc))
        return []
    finally:
        if document is not None:
            document.close()
    return rendered


def encode_image_base64(image_path: Path) -> str | None:
    """Encode an image file as a base64 JPEG payload."""
    try:
        with Image.open(image_path) as image:
            rgb_image = _to_rgb(image)
            buffer = io.BytesIO()
            rgb_image.save(buffer, format="JPEG", quality=95)
        return base64.b64encode(buffer.getvalue()).decode("utf-8")
    except Exception as exc:
        logger.warning("image_encode_failed", path=str(image_path), error=str(exc))
        return None


def is_image_file(filepath: Path) -> bool:
    """Return True when the file has a supported image extension."""
    return filepath.suffix.lower() in IMAGE_EXTENSIONS


def compress_pdf(src: Path, dest: Path, dpi: int = 150, jpeg_quality: int = 80) -> bool:
    """Re-render a PDF to JPEG-backed pages and keep it only if smaller."""
    document: fitz.Document | None = None
    output: fitz.Document | None = None
    try:
        document = fitz.open(str(src))
        output = fitz.open()
        for page in document:
            pixmap = page.get_pixmap(dpi=dpi)
            image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=jpeg_quality)
            rect = fitz.Rect(0, 0, page.rect.width, page.rect.height)
            new_page = output.new_page(width=rect.width, height=rect.height)
            new_page.insert_image(rect, stream=buffer.getvalue())
        output.save(str(dest), deflate=True)
    except Exception as exc:
        logger.warning("pdf_compress_failed", path=str(src), error=str(exc))
        return False
    finally:
        if output is not None:
            output.close()
        if document is not None:
            document.close()
    if dest.stat().st_size >= src.stat().st_size:
        dest.unlink(missing_ok=True)
        return False
    return True


def extract_content(filepath: Path) -> tuple[str, str | None]:
    """Extract content from a file using OCR-first, vision-fallback strategy.

    Returns a tuple of (extracted_text, image_base64 | None).
    - For text files: returns (text, None)
    - For PDFs with text: returns (text, None)
    - For images / scan-PDFs: OCR first, vision fallback only if OCR fails.
    """
    suffix = filepath.suffix.lower()

    # Text files: read directly
    if suffix == ".txt":
        try:
            text = filepath.read_text(errors="ignore")
            return (text, None)
        except OSError as exc:
            logger.warning("text_read_failed", path=str(filepath), error=str(exc))
            return ("", None)

    # Image files: OCR first, vision fallback
    if is_image_file(filepath):
        return _ocr_or_vision_image(filepath)

    # PDF files: try text extraction first, OCR/vision for scans
    if suffix == ".pdf":
        text = _extract_pdf_text(filepath)
        if text.strip():
            return (text, None)
        return _ocr_or_vision_scan_pdf(filepath)

    # Unsupported file type
    return ("", None)


def _ocr_or_vision_image(image_path: Path) -> tuple[str, str | None]:
    """OCR an image file; fall back to vision if OCR is insufficient."""
    ocr_result = ocr_image(image_path)
    if ocr_result is not None and is_usable_ocr(ocr_result):
        return (ocr_result.text, None)
    image_base64 = encode_image_base64(image_path)
    return ("", image_base64)


def _ocr_or_vision_scan_pdf(pdf_path: Path) -> tuple[str, str | None]:
    """OCR all pages of a scan-PDF; fall back to first-page vision if insufficient.

    Renders each page as a temp PNG, runs OCR, and accumulates text up to
    the preview character limit. If aggregate OCR is unusable, returns the
    first page as a vision image.
    """
    document: fitz.Document | None = None
    try:
        document = fitz.open(str(pdf_path))
        page_count = document.page_count
    except Exception as exc:
        logger.warning("pdf_open_failed", path=str(pdf_path), error=str(exc))
        return ("", None)
    finally:
        if document is not None:
            document.close()

    if page_count == 0:
        return ("", None)

    # Render all pages, run OCR, accumulate text
    rendered = _render_pdf_pages(pdf_path, max_pages=page_count)
    try:
        parts: list[str] = []
        for page_path in rendered:
            ocr_result = ocr_image(page_path)
            if ocr_result and ocr_result.text.strip():
                parts.append(ocr_result.text)
            if len("".join(parts)) >= _MAX_PREVIEW_CHARS:
                break

        aggregate_text = "".join(parts)[:_MAX_PREVIEW_CHARS]
        if is_usable_ocr(OCRResult(aggregate_text, 1.0, len(parts))):
            return (aggregate_text, None)

        # OCR insufficient — fall back to first page vision
        if rendered:
            image_base64 = encode_image_base64(rendered[0])
            return ("", image_base64)
        return ("", None)
    finally:
        for path in rendered:
            path.unlink(missing_ok=True)


def _extract_pdf_text(filepath: Path, max_chars: int = _MAX_PREVIEW_CHARS) -> str:
    """Extract text from a PDF using pypdf."""
    for attempt in range(3):
        try:
            reader = PdfReader(str(filepath))
            text_parts = []
            for page in reader.pages:
                page_text = page.extract_text() or ""
                text_parts.append(page_text)
                if len("".join(text_parts)) >= max_chars:
                    break
            return "".join(text_parts)[:max_chars]
        except OSError as exc:
            if exc.errno == errno.EDEADLK and attempt < 2:
                time.sleep(0.5 * (attempt + 1))
                continue
            logger.warning("pdf_text_extraction_failed", path=str(filepath), error=str(exc))
            return ""
        except Exception as exc:
            logger.warning("pdf_text_extraction_failed", path=str(filepath), error=str(exc))
            return ""
    return ""


def _to_rgb(image: Image.Image) -> Image.Image:
    """Convert a Pillow image into RGB with alpha flattened to white."""
    if image.mode in {"RGBA", "LA"}:
        background = Image.new("RGB", image.size, (255, 255, 255))
        background.paste(image, mask=image.split()[-1])
        return background
    if image.mode == "P":
        return _to_rgb(image.convert("RGBA"))
    if image.mode != "RGB":
        return image.convert("RGB")
    return image.copy()
