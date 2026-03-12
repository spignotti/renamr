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
