"""Tests for metadata parsing."""

from datetime import date

from renamr.metadata import _parse_date_string, _parse_metadata


def test_parse_metadata_handles_valid_json_and_fallbacks() -> None:
    parsed = _parse_metadata(
        '{"sender":"ACME","subject":"Invoice","date":"2024-01-31",'
        '"filename_format":"date_sender_subject"}'
    )
    fallback = _parse_metadata('{"subject":"Only Subject","date":"none"}')

    assert parsed.sender == "ACME"
    assert parsed.subject == "Invoice"
    assert parsed.document_date == date(2024, 1, 31)
    assert parsed.filename_format == "date_sender_subject"
    assert fallback.sender == "Unknown"
    assert fallback.subject == "Only Subject"
    assert fallback.document_date is None
    assert fallback.filename_format == "date_subject"


def test_parse_date_string_supports_expected_formats() -> None:
    assert _parse_date_string("2024-01-31") == date(2024, 1, 31)
    assert _parse_date_string("31.01.2024") == date(2024, 1, 31)
    assert _parse_date_string("31. Maerz 2024") == date(2024, 3, 31)
    assert _parse_date_string("none") is None
