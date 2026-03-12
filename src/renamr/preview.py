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

logger = structlog.get_logger(__name__)
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif", ".webp"}


def extract_text_preview(filepath: Path, max_chars: int = 1000) -> str:
    """Extract preview text from supported text-based files."""
    suffix = filepath.suffix.lower()
    if suffix == ".txt":
        try:
            return filepath.read_text(errors="ignore")[:max_chars]
        except OSError as exc:
            logger.warning("text_preview_failed", path=str(filepath), error=str(exc))
            return ""
    if suffix != ".pdf":
        return ""
    for attempt in range(3):
        try:
            reader = PdfReader(str(filepath))
            text = ""
            for page in reader.pages:
                text += page.extract_text() or ""
                if len(text) >= max_chars:
                    break
            return text[:max_chars]
        except OSError as exc:
            if exc.errno == errno.EDEADLK and attempt < 2:
                time.sleep(0.5 * (attempt + 1))
                continue
            logger.warning("pdf_preview_failed", path=str(filepath), error=str(exc))
            return ""
        except Exception as exc:
            logger.warning("pdf_preview_failed", path=str(filepath), error=str(exc))
            return ""
    return ""


def render_pdf_page(pdf_path: Path, dpi: int = 200) -> Path | None:
    """Render the first PDF page into a temporary PNG file."""
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
        logger.warning("pdf_render_failed", path=str(pdf_path), error=str(exc))
        return None


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
