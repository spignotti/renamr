"""P2 tests: page-addressable extraction, quality, limits, and the worker."""

from __future__ import annotations

from pathlib import Path

from renamr.core.catalog import PageMethod, QualityState
from renamr.core.extract import (
    ExtractorLimits,
    extract_document,
    preflight_ocr_assets,
)
from renamr.core.extract_runner import InProcessRunner, SubprocessRunner
from tests.unit.core_support import (
    FakeOCR,
    make_blank_pdf,
    make_encrypted_pdf,
    make_mixed_pdf,
    make_multiframe_tiff,
    make_png,
    make_text_pdf,
    ocr_text,
)


def test_text_pdf_extracts_every_page(tmp_path: Path) -> None:
    pdf = make_text_pdf(tmp_path / "multi.pdf", ["First page body", "Second page body", "Third"])

    result = extract_document(pdf, ocr=FakeOCR([]))

    assert result.page_count == 3
    assert [page.page_number for page in result.pages] == [1, 2, 3]
    assert all(page.method is PageMethod.PDF_TEXT for page in result.pages)
    assert "Second page body" in result.pages[1].text
    assert result.incomplete is False


def test_mixed_pdf_decides_per_page(tmp_path: Path) -> None:
    pdf = make_mixed_pdf(tmp_path / "mixed.pdf", embedded=[0], page_count=3)
    ocr = FakeOCR([ocr_text("Scanned page two", 0.91), ocr_text("Scanned page three", 0.88)])

    result = extract_document(pdf, ocr=ocr)

    assert result.pages[0].method is PageMethod.PDF_TEXT
    assert result.pages[1].method is PageMethod.OCR
    assert "Scanned page two" in result.pages[1].text
    assert result.pages[2].method is PageMethod.OCR


def test_rotation_is_preserved(tmp_path: Path) -> None:
    pdf = make_blank_pdf(tmp_path / "rotated.pdf", page_count=1, rotation=90)
    result = extract_document(pdf, ocr=FakeOCR([ocr_text("Rotated text", 0.8)]))
    assert result.pages[0].rotation == 90


def test_ocr_confidence_is_real_and_not_fabricated(tmp_path: Path) -> None:
    pdf = make_blank_pdf(tmp_path / "scan.pdf", page_count=1)
    result = extract_document(pdf, ocr=FakeOCR([ocr_text("Invoice text", 0.63)]))
    page = result.pages[0]
    assert page.confidence == 0.63
    assert page.confidence != 1.0
    assert page.quality is QualityState.OK


def test_low_confidence_marks_incomplete(tmp_path: Path) -> None:
    pdf = make_blank_pdf(tmp_path / "scan.pdf", page_count=1)
    result = extract_document(pdf, ocr=FakeOCR([ocr_text("Blurry", 0.2)]))
    assert result.pages[0].quality is QualityState.LOW
    assert result.incomplete is True


def test_ocr_failure_is_explicit(tmp_path: Path) -> None:
    pdf = make_blank_pdf(tmp_path / "scan.pdf", page_count=1)
    result = extract_document(pdf, ocr=FakeOCR([None]))
    assert result.pages[0].method is PageMethod.NONE
    assert result.pages[0].quality is QualityState.FAILED
    assert "ocr_failed" in result.errors
    assert result.incomplete is True


def test_multiframe_image_yields_one_page_per_frame(tmp_path: Path) -> None:
    tiff = make_multiframe_tiff(tmp_path / "frames.tiff", frames=3)
    ocr = FakeOCR(
        [ocr_text("Frame one", 0.8), ocr_text("Frame two", 0.8), ocr_text("Frame three", 0.8)]
    )

    result = extract_document(tiff, ocr=ocr)

    assert result.page_count == 3
    assert [page.page_number for page in result.pages] == [1, 2, 3]
    assert all(page.method is PageMethod.OCR for page in result.pages)


def test_single_image_ocr(tmp_path: Path) -> None:
    png = make_png(tmp_path / "photo.png")
    result = extract_document(png, ocr=FakeOCR([ocr_text("Photo text", 0.9)]))
    assert result.pages[0].method is PageMethod.OCR
    assert result.media_type == "image/png"


def test_encrypted_pdf_fails_closed(tmp_path: Path) -> None:
    pdf = make_encrypted_pdf(tmp_path / "locked.pdf")
    result = extract_document(pdf)
    assert result.incomplete is True
    assert "encrypted" in result.errors
    assert result.pages == []


def test_malformed_pdf_fails_closed(tmp_path: Path) -> None:
    pdf = tmp_path / "broken.pdf"
    pdf.write_bytes(b"%PDF-1.7 not really a pdf" * 5)
    result = extract_document(pdf)
    assert result.incomplete is True
    assert "unreadable" in result.errors


def test_page_limit_marks_incomplete(tmp_path: Path) -> None:
    pdf = make_text_pdf(tmp_path / "long.pdf", ["one", "two", "three"])
    result = extract_document(pdf, ocr=FakeOCR([]), limits=ExtractorLimits(max_pages=1))
    assert result.page_count == 3
    assert len(result.pages) == 1
    assert result.incomplete is True
    assert "page_limit" in result.errors


def test_file_size_limit(tmp_path: Path) -> None:
    pdf = make_text_pdf(tmp_path / "big.pdf", ["payload"])
    result = extract_document(pdf, limits=ExtractorLimits(max_file_bytes=1))
    assert result.errors == ["file_too_large"]
    assert result.pages == []


def test_text_truncation_marks_incomplete(tmp_path: Path) -> None:
    text = tmp_path / "note.txt"
    text.write_text("x" * 5000)
    result = extract_document(text, limits=ExtractorLimits(max_output_chars=100))
    assert len(result.pages[0].text) == 100
    assert result.incomplete is True
    assert "char_limit" in result.errors


def test_zero_byte_pdf_reports_incomplete(tmp_path: Path) -> None:
    pdf = tmp_path / "empty.pdf"
    pdf.write_bytes(b"")
    result = extract_document(pdf, ocr=FakeOCR([]))
    assert result.incomplete is True
    assert result.pages == []
    assert result.errors


def test_preflight_ocr_assets_with_injected_probe() -> None:
    assert preflight_ocr_assets(lambda: (True, "present")).available is True
    assert preflight_ocr_assets(lambda: (False, "missing")).available is False


def test_in_process_runner_uses_injected_provider(tmp_path: Path) -> None:
    pdf = make_blank_pdf(tmp_path / "scan.pdf", page_count=1)
    runner = InProcessRunner(FakeOCR([ocr_text("Runner text", 0.77)]))
    result = runner.run(pdf, ExtractorLimits())
    assert result.pages[0].confidence == 0.77


def test_subprocess_runner_round_trips(tmp_path: Path) -> None:
    pdf = make_text_pdf(tmp_path / "worker.pdf", ["Worker embedded text", "second"])
    runner = SubprocessRunner(use_ocr=False)
    result = runner.run(pdf, ExtractorLimits(timeout_s=60))
    assert [page.page_number for page in result.pages] == [1, 2]
    assert "Worker embedded text" in result.pages[0].text
