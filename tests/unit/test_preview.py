"""Tests for preview extraction."""

from __future__ import annotations

from pathlib import Path

import fitz
from PIL import Image

from renamr.preview import (
    extract_content,
    extract_text_preview,
    is_image_file,
    render_pdf_page,
)


def test_extract_text_preview_reads_text_file(tmp_path: Path) -> None:
    text_path = tmp_path / "note.txt"
    text_path.write_text("hello world")

    assert extract_text_preview(text_path) == "hello world"


def test_render_pdf_page_cleans_up_temp_file_on_save_failure(
    tmp_path: Path, monkeypatch,
) -> None:
    pdf_path = tmp_path / "sample.pdf"
    document = fitz.open()
    document.new_page()
    document.save(pdf_path)
    document.close()

    created_paths: list[Path] = []
    original_save = fitz.Pixmap.save

    def failing_save(self: fitz.Pixmap, filename: str, *args: object, **kwargs: object) -> None:
        created_paths.append(Path(filename))
        raise RuntimeError("save failed")

    monkeypatch.setattr(fitz.Pixmap, "save", failing_save)

    result = render_pdf_page(pdf_path)

    monkeypatch.setattr(fitz.Pixmap, "save", original_save)
    assert result is None
    assert created_paths
    assert not created_paths[0].exists()



def test_is_image_file_matches_supported_extensions() -> None:
    assert is_image_file(Path("image.jpg")) is True
    assert is_image_file(Path("image.png")) is True
    assert is_image_file(Path("image.txt")) is False


class TestExtractContent:
    """Tests for hybrid text-first, vision-fallback extraction."""

    def test_extract_content_from_txt_returns_text_only(self, tmp_path: Path) -> None:
        text_path = tmp_path / "note.txt"
        text_path.write_text("Sample document content")

        text, image_base64 = extract_content(text_path)

        assert text == "Sample document content"
        assert image_base64 is None

    def test_extract_content_from_image_returns_base64_only(self, tmp_path: Path) -> None:
        image_path = tmp_path / "scan.png"
        # Create a small test image
        img = Image.new("RGB", (100, 100), color="white")
        img.save(image_path)

        text, image_base64 = extract_content(image_path)

        assert text == ""
        assert image_base64 is not None
        assert isinstance(image_base64, str)

    def test_extract_content_from_text_pdf_returns_text_only(self, tmp_path: Path) -> None:
        """PDF with text content should return text, no image."""
        pdf_path = tmp_path / "document.pdf"
        document = fitz.open()
        page = document.new_page()
        # Insert text into the PDF
        page.insert_text((50, 50), "This is a text PDF with content")
        document.save(pdf_path)
        document.close()

        text, image_base64 = extract_content(pdf_path)

        assert "This is a text PDF" in text
        assert image_base64 is None

    def test_extract_content_from_scan_pdf_returns_image_only(self, tmp_path: Path) -> None:
        """PDF with no text (image-only) should return empty text and base64 image."""
        pdf_path = tmp_path / "scan.pdf"
        document = fitz.open()
        # Create a page with no text (blank/scan-like)
        document.new_page()
        document.save(pdf_path)
        document.close()

        text, image_base64 = extract_content(pdf_path)

        # Empty text (blank page has no extractable text)
        assert text == ""
        # Should have an image payload for vision processing
        assert image_base64 is not None

    def test_extract_content_unsupported_extension_returns_empty(self, tmp_path: Path) -> None:
        doc_path = tmp_path / "document.doc"
        doc_path.write_text("Some content")

        text, image_base64 = extract_content(doc_path)

        assert text == ""
        assert image_base64 is None
