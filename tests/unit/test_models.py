"""Config model tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from renamr.models import AppConfig


def test_filename_template_accepts_supported_placeholders() -> None:
    config = AppConfig(filename_template="{date}_{subject}")

    assert config.filename_template == "{date}_{subject}"


def test_filename_template_rejects_unknown_placeholders() -> None:
    with pytest.raises(ValidationError, match="unknown placeholder: project"):
        AppConfig(filename_template="{date}_{project}")


def test_logging_defaults_to_warning_and_plain_text() -> None:
    config = AppConfig()

    assert config.logging.level == "WARNING"
    assert config.logging.json_logs is False
