"""Tests for metadata parsing."""

from datetime import date, datetime
from types import SimpleNamespace

import renamr.metadata as metadata_module
from renamr.metadata import _parse_date_string, _parse_metadata, extract_metadata
from renamr.models import AppConfig


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
    assert _parse_date_string("20241332") is None
    assert _parse_date_string("none") is None


def test_extract_metadata_prepends_language_instruction(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_completion(**kwargs):
        captured["messages"] = kwargs["messages"]
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=(
                            '{"sender":"ACME","subject":"Invoice",'
                            '"date":"2024-01-31","filename_format":"date_subject"}'
                        )
                    )
                )
            ]
        )

    monkeypatch.setattr(metadata_module, "completion", fake_completion)

    extract_metadata(
        filename="note.txt",
        created_at=datetime(2024, 1, 31),
        preview_text="invoice",
        image_base64=None,
        config=AppConfig(language="de"),
    )

    messages = captured["messages"]
    assert isinstance(messages, list)
    assert messages[0]["role"] == "system"
    assert messages[0]["content"].startswith(
        "Language for all extracted metadata values: de\n\n"
    )
