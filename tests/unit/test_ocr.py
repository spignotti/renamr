"""Tests for the local OCR adapter."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import renamr.ocr as ocr_module
from renamr.ocr import OCRResult, is_usable_ocr, ocr_image


class TestIsUsableOcr:
    """Tests for the OCR usability heuristic."""

    def test_none_is_not_usable(self) -> None:
        assert is_usable_ocr(None) is False

    def test_empty_text_is_not_usable(self) -> None:
        result = OCRResult(text="", mean_confidence=0.9, line_count=0)
        assert is_usable_ocr(result) is False

    def test_short_text_is_not_usable(self) -> None:
        result = OCRResult(text="Hi", mean_confidence=0.9, line_count=1)
        assert is_usable_ocr(result) is False

    def test_low_confidence_is_not_usable(self) -> None:
        result = OCRResult(text="A" * 50, mean_confidence=0.3, line_count=3)
        assert is_usable_ocr(result) is False

    def test_good_text_and_confidence_is_usable(self) -> None:
        result = OCRResult(text="A" * 50, mean_confidence=0.8, line_count=3)
        assert is_usable_ocr(result) is True

    def test_usable_with_exact_thresholds(self) -> None:
        result = OCRResult(text="A" * 20, mean_confidence=0.5, line_count=1)
        assert is_usable_ocr(result) is True


class TestOcrImage:
    """Tests for the ocr_image wrapper."""

    def test_returns_ocr_result_on_success(self, monkeypatch, tmp_path: Path) -> None:
        img_path = tmp_path / "test.png"
        img_path.touch()

        fake_result = SimpleNamespace(
            txts=("Hello world", "Line 2"),
            scores=(0.95, 0.88),
        )
        # _get_engine must return a callable (the engine itself)
        monkeypatch.setattr(
            ocr_module,
            "_get_engine",
            lambda: (lambda _: fake_result),
        )

        result = ocr_image(img_path)

        assert result is not None
        assert "Hello world" in result.text
        assert result.line_count == 2
        assert result.mean_confidence > 0.9

    def test_returns_none_on_empty_output(self, monkeypatch, tmp_path: Path) -> None:
        img_path = tmp_path / "blank.png"
        img_path.touch()

        fake_result = SimpleNamespace(txts=(), scores=())
        monkeypatch.setattr(
            ocr_module,
            "_get_engine",
            lambda: (lambda _: fake_result),
        )

        result = ocr_image(img_path)
        assert result is None

    def test_returns_none_on_exception(self, monkeypatch, tmp_path: Path) -> None:
        img_path = tmp_path / "bad.png"
        img_path.touch()

        def failing_engine():
            raise RuntimeError("OCR crash")

        monkeypatch.setattr(ocr_module, "_get_engine", failing_engine)

        result = ocr_image(img_path)
        assert result is None
