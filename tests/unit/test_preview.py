"""Tests for preview extraction."""

from __future__ import annotations

from pathlib import Path

import fitz
from PIL import Image

import renamr.preview as preview_module
from renamr.ocr import OCRResult
from renamr.preview import (
    extract_content,
    is_image_file,
    render_pdf_page,
)


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
    assert is_image_file(Path("image.tiff")) is True
    assert is_image_file(Path("image.txt")) is False


class TestExtractContentTxt:
    """Tests for text file extraction."""

    def test_returns_text_only(self, tmp_path: Path) -> None:
        text_path = tmp_path / "note.txt"
        text_path.write_text("Sample document content")

        text, image_base64 = extract_content(text_path)

        assert text == "Sample document content"
        assert image_base64 is None


class TestExtractContentPdf:
    """Tests for PDF extraction."""

    def test_text_pdf_returns_text_only(self, tmp_path: Path) -> None:
        pdf_path = tmp_path / "document.pdf"
        document = fitz.open()
        page = document.new_page()
        page.insert_text((50, 50), "This is a text PDF with content")
        document.save(pdf_path)
        document.close()

        text, image_base64 = extract_content(pdf_path)

        assert "This is a text PDF" in text
        assert image_base64 is None

    def test_scan_pdf_ocr_usable_returns_text(self, tmp_path: Path, monkeypatch) -> None:
        """Scan PDF with usable OCR returns text, no image."""
        pdf_path = tmp_path / "scan.pdf"
        document = fitz.open()
        document.new_page()
        document.save(pdf_path)
        document.close()

        # Patch ocr_image at the module where it's imported (preview.py)
        monkeypatch.setattr(
            preview_module,
            "ocr_image",
            lambda _: OCRResult(text="A" * 50, mean_confidence=0.9, line_count=3),
        )

        text, image_base64 = extract_content(pdf_path)

        assert "A" * 50 in text
        assert image_base64 is None

    def test_scan_pdf_ocr_unusable_returns_vision(self, tmp_path: Path, monkeypatch) -> None:
        """Scan PDF with unusable OCR returns first-page vision image."""
        pdf_path = tmp_path / "scan.pdf"
        document = fitz.open()
        document.new_page()
        document.save(pdf_path)
        document.close()

        # OCR returns unusable result (too short)
        monkeypatch.setattr(
            preview_module,
            "ocr_image",
            lambda _: OCRResult(text="Hi", mean_confidence=0.9, line_count=1),
        )

        text, image_base64 = extract_content(pdf_path)

        assert text == ""
        assert image_base64 is not None


class TestExtractContentImage:
    """Tests for image file extraction."""

    def test_image_ocr_usable_returns_text(self, tmp_path: Path, monkeypatch) -> None:
        """Image with usable OCR returns text, no base64."""
        image_path = tmp_path / "scan.png"
        img = Image.new("RGB", (100, 100), color="white")
        img.save(image_path)

        monkeypatch.setattr(
            preview_module,
            "ocr_image",
            lambda _: OCRResult(
                text="Invoice from ACME Corp for services rendered",
                mean_confidence=0.95,
                line_count=2,
            ),
        )

        text, image_base64 = extract_content(image_path)

        assert "Invoice from ACME Corp" in text
        assert image_base64 is None

    def test_image_ocr_unusable_returns_vision(self, tmp_path: Path, monkeypatch) -> None:
        """Image with unusable OCR returns base64 vision payload."""
        image_path = tmp_path / "photo.jpg"
        img = Image.new("RGB", (100, 100), color="white")
        img.save(image_path)

        # OCR returns nothing useful
        monkeypatch.setattr(preview_module, "ocr_image", lambda _: None)

        text, image_base64 = extract_content(image_path)

        assert text == ""
        assert image_base64 is not None


class TestExtractContentUnsupported:
    """Tests for unsupported file types."""

    def test_unsupported_extension_returns_empty(self, tmp_path: Path) -> None:
        doc_path = tmp_path / "document.doc"
        doc_path.write_text("Some content")

        text, image_base64 = extract_content(doc_path)

        assert text == ""
        assert image_base64 is None
