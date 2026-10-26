"""Date parsing and supported deterministic arithmetic tests."""

from __future__ import annotations

from datetime import date

from renamr.core.dates import add_months, days_before, find_document_date, parse_document_date


def test_parse_iso_and_german_numeric_dates() -> None:
    assert parse_document_date("Rechnungsdatum: 2026-03-14") == date(2026, 3, 14)
    assert parse_document_date("Datum: 14.03.2026") == date(2026, 3, 14)
    assert parse_document_date("14. März 2026") == date(2026, 3, 14)
    assert parse_document_date("14 March 2026") == date(2026, 3, 14)


def test_parse_rejects_invalid_and_ambiguous_text() -> None:
    assert parse_document_date("31.02.2026") is None
    assert parse_document_date("no date here") is None
    assert find_document_date("Header\nInvoice date: 2026-01-09\nTotal") == (
        date(2026, 1, 9),
        "Invoice date: 2026-01-09",
    )


def test_calendar_arithmetic_clamps_month_end() -> None:
    assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert add_months(date(2026, 1, 1), 12) == date(2027, 1, 1)
    assert days_before(date(2026, 1, 1), 1) == date(2025, 12, 31)


def test_arithmetic_rejects_negative_values() -> None:
    try:
        add_months(date(2026, 1, 1), -1)
    except ValueError:
        pass
    else:
        raise AssertionError("negative months must be rejected")
    try:
        days_before(date(2026, 1, 1), -1)
    except ValueError:
        pass
    else:
        raise AssertionError("negative days must be rejected")
