"""Tests for file helpers."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from renamr.files import (
    build_filename,
    is_icloud_stub,
    resolve_conflict,
    sanitize_component,
    select_date_prefix,
)


def test_sanitize_component_handles_unsafe_and_empty_values() -> None:
    assert sanitize_component(" ACME:/Invoice ") == "ACMEInvoice"
    assert sanitize_component("   ") == "Unknown"
    assert sanitize_component("foo   bar") == "foo bar"


def test_build_filename_supports_both_formats_and_truncation() -> None:
    sender_name = build_filename(
        "240101",
        "ACME",
        "Invoice",
        ".pdf",
        "{date}_{sender}_{subject}",
        "date_sender_subject",
    )
    subject_only = build_filename(
        "240101",
        "Unknown",
        "Invoice",
        ".pdf",
        "{date}_{sender}_{subject}",
        "date_subject",
    )
    truncated = build_filename(
        "240101",
        "ACME",
        "X" * 250,
        ".pdf",
        "{date}_{sender}_{subject}",
        "date_sender_subject",
    )

    assert sender_name == "240101_ACME_Invoice.pdf"
    assert subject_only == "240101_Invoice.pdf"
    assert len(truncated) <= 200


def test_resolve_conflict_appends_incrementing_suffix(tmp_path: Path) -> None:
    first = tmp_path / "file.pdf"
    second = tmp_path / "file_2.pdf"
    first.write_text("a")
    second.write_text("b")

    resolved = resolve_conflict(first)

    assert resolved.name == "file_3.pdf"


def test_is_icloud_stub_matches_expected_pattern() -> None:
    assert is_icloud_stub(Path(".file.pdf.icloud")) is True
    assert is_icloud_stub(Path("file.pdf")) is False


def test_select_date_prefix_prefers_document_date() -> None:
    assert select_date_prefix(date(2024, 1, 31), date(2024, 2, 1)) == "240131"
    assert select_date_prefix(None, date(2024, 2, 1)) == "240201"
