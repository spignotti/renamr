"""Config model tests."""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest
from pydantic import ValidationError

from renamr.models import AppConfig, InboxConfig, LLMConfig, load_config


def test_filename_template_accepts_supported_placeholders() -> None:
    config = AppConfig(
        filename_template="{date}_{subject}",
        inbox=[InboxConfig(path="/test")],
    )

    assert config.filename_template == "{date}_{subject}"


def test_filename_template_rejects_unknown_placeholders() -> None:
    with pytest.raises(ValidationError, match="unknown placeholder: project"):
        AppConfig(
            filename_template="{date}_{project}",
            inbox=[InboxConfig(path="/test")],
        )


def test_logging_defaults_to_warning_and_plain_text() -> None:
    config = AppConfig(inbox=[InboxConfig(path="/test")])

    assert config.logging.level == "WARNING"
    assert config.logging.json_logs is False


class TestInboxConfigMerge:
    """Tests for effective config merge logic."""

    def test_inbox_uses_global_defaults_when_no_override(self) -> None:
        config = AppConfig(
            language="en",
            filename_template="{date}_{subject}",
            inbox=[InboxConfig(path="/test/path")],
        )
        effective = config.get_effective_config(config.inboxes[0])

        assert effective.language == "en"
        assert effective.filename_template == "{date}_{subject}"
        assert effective.path == Path("/test/path")

    def test_inbox_overrides_global_language(self) -> None:
        config = AppConfig(
            language="en",
            inbox=[InboxConfig(path="/test", language="de")],
        )
        effective = config.get_effective_config(config.inboxes[0])

        assert effective.language == "de"

    def test_inbox_overrides_global_filename_template(self) -> None:
        config = AppConfig(
            filename_template="{date}_{subject}",
            inbox=[InboxConfig(path="/test", filename_template="{date}_{sender}")],
        )
        effective = config.get_effective_config(config.inboxes[0])

        assert effective.filename_template == "{date}_{sender}"

    def test_inbox_overrides_global_rename_prompt(self) -> None:
        config = AppConfig(
            rename_prompt="Global prompt",
            inbox=[InboxConfig(path="/test", rename_prompt="Custom prompt")],
        )
        effective = config.get_effective_config(config.inboxes[0])

        assert effective.rename_prompt == "Custom prompt"

    def test_inbox_partial_override_uses_global_for_missing(self) -> None:
        config = AppConfig(
            language="en",
            filename_template="{date}_{subject}",
            rename_prompt="Global prompt",
            inbox=[InboxConfig(path="/test", language="de")],
        )
        effective = config.get_effective_config(config.inboxes[0])

        assert effective.language == "de"  # overridden
        assert effective.filename_template == "{date}_{subject}"  # global
        assert effective.rename_prompt == "Global prompt"  # global

    def test_path_is_expanded_and_resolved(self, tmp_path: Path) -> None:
        config = AppConfig(inbox=[InboxConfig(path=str(tmp_path))])
        effective = config.get_effective_config(config.inboxes[0])

        assert effective.path == tmp_path.resolve()

    def test_inbox_uses_global_model_when_no_override(self) -> None:
        config = AppConfig(
            inbox=[InboxConfig(path="/test")],
            llm=LLMConfig(model="gpt-4o", api_base="https://api.openai.com"),
        )
        effective = config.get_effective_config(config.inboxes[0])

        assert effective.model == "gpt-4o"
        assert effective.api_base == "https://api.openai.com"

    def test_inbox_overrides_global_model(self) -> None:
        config = AppConfig(
            inbox=[InboxConfig(path="/test", model="ollama/llama3")],
            llm=LLMConfig(model="gpt-4o", api_base="https://api.openai.com"),
        )
        effective = config.get_effective_config(config.inboxes[0])

        assert effective.model == "ollama/llama3"

    def test_inbox_overrides_global_api_base(self) -> None:
        config = AppConfig(
            inbox=[InboxConfig(path="/test", api_base="http://localhost:11434")],
            llm=LLMConfig(model="gpt-4o", api_base="https://api.openai.com"),
        )
        effective = config.get_effective_config(config.inboxes[0])

        assert effective.api_base == "http://localhost:11434"
        assert effective.model == "gpt-4o"  # global fallback

    def test_inbox_model_partial_override_uses_global(self) -> None:
        config = AppConfig(
            inbox=[InboxConfig(path="/test", model="ollama/llama3")],
            llm=LLMConfig(model="gpt-4o", api_base="https://api.openai.com"),
        )
        effective = config.get_effective_config(config.inboxes[0])

        assert effective.model == "ollama/llama3"  # overridden
        assert effective.api_base == "https://api.openai.com"  # global fallback

    def test_inbox_api_base_none_uses_global(self) -> None:
        """Test that explicit None in api_base falls back to global."""
        config = AppConfig(
            inbox=[InboxConfig(path="/test", api_base=None)],
            llm=LLMConfig(model="gpt-4o", api_base="https://api.openai.com"),
        )
        effective = config.get_effective_config(config.inboxes[0])

        assert effective.api_base == "https://api.openai.com"

    def test_vision_model_inherits_from_text_model(self) -> None:
        """When no vision_model set, vision_model == model."""
        config = AppConfig(
            inbox=[InboxConfig(path="/test")],
            llm=LLMConfig(model="gpt-4o", api_base="https://api.openai.com"),
        )
        effective = config.get_effective_config(config.inboxes[0])

        assert effective.vision_model == "gpt-4o"
        assert effective.vision_api_base == "https://api.openai.com"

    def test_global_vision_model_overrides_inheritance(self) -> None:
        """Global vision_model is used when no inbox override."""
        config = AppConfig(
            inbox=[InboxConfig(path="/test")],
            llm=LLMConfig(
                model="openai/gpt-4o-mini",
                vision_model="ollama/vision",
                vision_api_base="http://localhost:11434",
            ),
        )
        effective = config.get_effective_config(config.inboxes[0])

        assert effective.vision_model == "ollama/vision"
        assert effective.vision_api_base == "http://localhost:11434"
        assert effective.model == "openai/gpt-4o-mini"

    def test_inbox_vision_model_overrides_global(self) -> None:
        """Inbox-level vision_model takes priority over global."""
        config = AppConfig(
            inbox=[InboxConfig(path="/test", vision_model="openai/gpt-4o")],
            llm=LLMConfig(
                model="openai/gpt-4o-mini",
                vision_model="ollama/vision",
                vision_api_base="http://localhost:11434",
            ),
        )
        effective = config.get_effective_config(config.inboxes[0])

        assert effective.vision_model == "openai/gpt-4o"
        assert effective.vision_api_base == "http://localhost:11434"

    def test_vision_api_base_inherits_from_text_api_base(self) -> None:
        """When no vision_api_base set, inherits from text api_base."""
        config = AppConfig(
            inbox=[InboxConfig(path="/test")],
            llm=LLMConfig(
                model="openai/gpt-4o-mini",
                api_base="https://api.openai.com",
            ),
        )
        effective = config.get_effective_config(config.inboxes[0])

        assert effective.vision_api_base == "https://api.openai.com"

    def test_vision_api_base_independent_of_text(self) -> None:
        """Separate vision endpoint for cloud text + local vision."""
        config = AppConfig(
            inbox=[InboxConfig(path="/test")],
            llm=LLMConfig(
                model="openai/gpt-4o-mini",
                api_base="https://api.openai.com",
                vision_model="ollama/gemma4:e2b",
                vision_api_base="http://localhost:11434",
            ),
        )
        effective = config.get_effective_config(config.inboxes[0])

        assert effective.model == "openai/gpt-4o-mini"
        assert effective.api_base == "https://api.openai.com"
        assert effective.vision_model == "ollama/gemma4:e2b"
        assert effective.vision_api_base == "http://localhost:11434"

    def test_file_extensions_include_image_formats(self) -> None:
        """Default file extensions include all supported image formats."""
        config = AppConfig(inbox=[InboxConfig(path="/test")])
        assert ".tif" in config.file_extensions
        assert ".tiff" in config.file_extensions
        assert ".bmp" in config.file_extensions
        assert ".gif" in config.file_extensions
        assert ".webp" in config.file_extensions


class TestBackwardsCompatibility:
    """Tests for legacy inbox_paths support."""

    def test_legacy_inbox_paths_converts_to_inboxes(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.toml"
        config_file.write_text('inbox_paths = ["/path/one", "/path/two"]\nlanguage = "en"\n')

        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            config = load_config(config_file)

        assert len(config.inboxes) == 2
        assert config.inboxes[0].path == "/path/one"
        assert config.inboxes[1].path == "/path/two"

    def test_legacy_inbox_paths_emits_deprecation_warning(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.toml"
        config_file.write_text('inbox_paths = ["/test"]\n')

        with warnings.catch_warnings(record=True) as warning_list:
            warnings.simplefilter("always")
            load_config(config_file)

        assert len(warning_list) == 1
        assert issubclass(warning_list[0].category, DeprecationWarning)
        assert "inbox_paths is deprecated" in str(warning_list[0].message)

    def test_new_inbox_format_takes_precedence_over_legacy(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.toml"
        config_file.write_text('inbox_paths = ["/legacy"]\n[[inbox]]\npath = "/new"\n')

        with warnings.catch_warnings(record=True) as warning_list:
            warnings.simplefilter("always")
            config = load_config(config_file)

        # No deprecation warning from our code when inboxes is present
        inbox_path_warnings = [
            w for w in warning_list if "inbox_paths is deprecated" in str(w.message)
        ]
        assert len(inbox_path_warnings) == 0
        assert len(config.inboxes) == 1
        assert config.inboxes[0].path == "/new"
        assert len(config.inboxes) == 1
        assert config.inboxes[0].path == "/new"


class TestInboxConfigValidation:
    """Tests for InboxConfig validation."""

    def test_inbox_validates_filename_template(self) -> None:
        with pytest.raises(ValidationError, match="unknown placeholder"):
            InboxConfig(path="/test", filename_template="{invalid}")

    def test_inbox_accepts_valid_filename_template(self) -> None:
        inbox = InboxConfig(path="/test", filename_template="{date}_{sender}_{subject}")
        assert inbox.filename_template == "{date}_{sender}_{subject}"
