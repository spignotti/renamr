"""Shared offline helpers for the core (P1-P3) unit tests.

This module is intentionally not named ``test_*`` so pytest does not collect it.
Every helper is synthetic and touches only temporary directories.
"""

from __future__ import annotations

from pathlib import Path

import pymupdf
from PIL import Image

from renamr.benchmark_schema import DocumentFeatures, FeatureValidation, GroundingEvidence
from renamr.core.extract import OCRText
from renamr.core.features import FeatureExtraction, FeatureRequest


def make_text_pdf(path: Path, pages: list[str]) -> Path:
    """Create a PDF with real embedded text on each page."""
    document = pymupdf.open()
    for text in pages:
        page = document.new_page()
        page.insert_text((50, 50), text)
    document.save(path)
    document.close()
    return path


def make_blank_pdf(path: Path, page_count: int, *, rotation: int = 0) -> Path:
    """Create a PDF with blank (image-only) pages for the OCR path."""
    document = pymupdf.open()
    for _ in range(page_count):
        page = document.new_page()
        if rotation:
            page.set_rotation(rotation)
    document.save(path)
    document.close()
    return path


def make_mixed_pdf(path: Path, embedded: list[int], page_count: int) -> Path:
    """Create a PDF where only some pages carry embedded text."""
    document = pymupdf.open()
    for index in range(page_count):
        page = document.new_page()
        if index in embedded:
            page.insert_text((50, 50), f"Embedded page {index + 1}")
    document.save(path)
    document.close()
    return path


def make_encrypted_pdf(path: Path, password: str = "synthetic") -> Path:
    """Create a password-protected PDF."""
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((50, 50), "Confidential synthetic page")
    encryption = getattr(pymupdf, "PDF_ENCRYPT_AES_256", 2624)
    document.save(path, encryption=encryption, user_pw=password, owner_pw=password)
    document.close()
    return path


def make_multiframe_tiff(path: Path, frames: int) -> Path:
    """Create a multi-frame TIFF image."""
    images = [Image.new("RGB", (120, 80), color="white") for _ in range(frames)]
    images[0].save(path, save_all=True, append_images=images[1:], format="TIFF")
    return path


def make_png(path: Path, size: tuple[int, int] = (200, 120)) -> Path:
    """Create a simple PNG image."""
    Image.new("RGB", size, color="white").save(path, format="PNG")
    return path


class FakeOCR:
    """Callable OCR provider returning canned results in call order."""

    def __init__(self, results: list[OCRText | None]) -> None:
        self._results = list(results)
        self.calls = 0

    def __call__(self, path: Path) -> OCRText | None:
        index = self.calls
        self.calls += 1
        if index < len(self._results):
            return self._results[index]
        return None


def ocr_text(text: str, confidence: float = 0.87) -> OCRText:
    """Build a realistic OCR result with a positive confidence."""
    return OCRText(text=text, confidence=confidence, line_count=len(text.splitlines()) or 1)


class FakeExtractor:
    """Feature extractor that returns grounded features and counts calls."""

    def __init__(
        self,
        *,
        document_type: str = "invoice",
        issuer: str | None = None,
        date: str | None = None,
        needs_review: bool = False,
        evidence: bool = True,
    ) -> None:
        self._document_type = document_type
        self._issuer = issuer
        self._date = date
        self._needs_review = needs_review
        self._evidence = evidence
        self.calls = 0

    def extract(self, request: FeatureRequest) -> FeatureExtraction:
        """Return grounded features for the first page, counting invocations."""
        self.calls += 1
        page_texts = request.page_texts
        first = page_texts[0] if page_texts else ""
        first_line = first.strip().splitlines()[0].strip() if first.strip() else ""
        if not any(text.strip() for text in page_texts):
            return FeatureExtraction(
                validation=FeatureValidation(ok=False, reasons=["no_extractable_text"]),
                blocked_reason="no_extractable_text",
                incomplete=True,
            )
        grounding = (
            [GroundingEvidence(page=1, source_text=first_line)]
            if self._evidence and first_line
            else []
        )
        features = DocumentFeatures(
            document_type=self._document_type,  # type: ignore[arg-type]
            issuer=self._issuer,
            date=self._date,
            subject="Rechnung",
            language="de",
            needs_review=self._needs_review,
            evidence=grounding,
        )
        return FeatureExtraction(
            features=features,
            validation=FeatureValidation(ok=True),
        )
