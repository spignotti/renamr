"""Tests for metadata parsing."""

from datetime import date, datetime
from types import SimpleNamespace

import renamr.metadata as metadata_module
from renamr.metadata import _parse_date_string, _parse_metadata, extract_metadata
from renamr.models import AppConfig, InboxConfig


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

    config = AppConfig(inbox=[InboxConfig(path="/test")])
    extract_metadata(
        filename="note.txt",
        created_at=datetime(2024, 1, 31),
        preview_text="invoice",
        image_base64=None,
        language="de",
        rename_prompt="Extract sender, subject, date from this document.",
        model=config.llm.model,
        api_base=config.llm.api_base,
        vision_model=config.llm.vision_model or config.llm.model,
        vision_api_base=config.llm.vision_api_base,
        temperature=config.llm.temperature,
        max_retries=config.llm.max_retries,
        timeout=config.llm.timeout,
    )

    messages = captured["messages"]
    assert isinstance(messages, list)
    assert messages[0]["role"] == "system"
    assert messages[0]["content"].startswith("Language for all extracted metadata values: de\n\n")


def test_extract_metadata_selects_text_model_for_text_only(monkeypatch) -> None:
    """Text-only input uses the text model, not the vision model."""
    captured: dict[str, object] = {}

    def fake_completion(**kwargs):
        captured["model"] = kwargs.get("model")
        captured["api_base"] = kwargs.get("api_base")
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
        preview_text="invoice content",
        image_base64=None,
        language="en",
        rename_prompt="Extract metadata.",
        model="openai/gpt-4o-mini",
        api_base="https://api.openai.com",
        vision_model="ollama/vision",
        vision_api_base="http://localhost:11434",
        temperature=0.2,
        max_retries=2,
        timeout=120,
    )

    assert captured["model"] == "openai/gpt-4o-mini"
    assert captured["api_base"] == "https://api.openai.com"


def test_extract_metadata_selects_vision_model_for_image(monkeypatch) -> None:
    """Image payload uses the vision model."""
    captured: dict[str, object] = {}

    def fake_completion(**kwargs):
        captured["model"] = kwargs.get("model")
        captured["api_base"] = kwargs.get("api_base")
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
        filename="scan.jpg",
        created_at=datetime(2024, 1, 31),
        preview_text="",
        image_base64="fakebase64data",
        language="en",
        rename_prompt="Extract metadata.",
        model="openai/gpt-4o-mini",
        api_base="https://api.openai.com",
        vision_model="ollama/vision",
        vision_api_base="http://localhost:11434",
        temperature=0.2,
        max_retries=2,
        timeout=120,
    )

    assert captured["model"] == "ollama/vision"
    assert captured["api_base"] == "http://localhost:11434"
