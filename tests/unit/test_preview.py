"""Tests for preview extraction."""

from pathlib import Path

from renamr.preview import extract_text_preview, is_image_file


def test_extract_text_preview_reads_text_file(tmp_path: Path) -> None:
    text_path = tmp_path / "note.txt"
    text_path.write_text("hello world")

    assert extract_text_preview(text_path) == "hello world"


def test_is_image_file_matches_supported_extensions() -> None:
    assert is_image_file(Path("image.jpg")) is True
    assert is_image_file(Path("image.png")) is True
    assert is_image_file(Path("image.txt")) is False
