"""Tests for preview extraction."""

from __future__ import annotations

from pathlib import Path

import fitz

from renamr.preview import extract_text_preview, is_image_file, render_pdf_page


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
