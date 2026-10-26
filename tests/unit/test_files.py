"""Tests for filename component sanitization."""

from __future__ import annotations

from renamr.files import sanitize_component


def test_sanitize_component_handles_unsafe_and_empty_values() -> None:
    assert sanitize_component(" ACME:/Invoice ") == "ACMEInvoice"
    assert sanitize_component("   ") == "Unknown"
    assert sanitize_component("foo   bar") == "foo bar"
    assert sanitize_component("a*b?c") == "abc"


def test_sanitize_component_normalizes_underscores() -> None:
    assert sanitize_component("2026_03_14_Rechnung") == "2026 03 14 Rechnung"
