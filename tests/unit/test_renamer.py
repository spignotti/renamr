"""Renamer pipeline tests."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

import renamr.renamer as renamer_module
from renamr.metadata import FileMetadata
from renamr.models import AppConfig, InboxConfig
from renamr.renamer import run


def _fake_metadata(**kwargs):
    """Return fixed metadata regardless of input."""
    return FileMetadata(
        sender="ACME",
        subject="Invoice",
        document_date=date(2024, 1, 31),
        filename_format="date_sender_subject",
    )


def test_run_renames_files_from_multiple_inboxes(tmp_path: Path, monkeypatch) -> None:
    inbox_a = tmp_path / "inbox-a"
    inbox_b = tmp_path / "inbox-b"
    inbox_a.mkdir()
    inbox_b.mkdir()
    first_file = inbox_a / "first.txt"
    second_file = inbox_b / "second.txt"
    first_file.write_text("first")
    second_file.write_text("second")

    monkeypatch.setattr(renamer_module, "extract_metadata", _fake_metadata)

    config = AppConfig(
        inbox=[
            InboxConfig(path=str(inbox_a)),
            InboxConfig(path=str(inbox_b)),
        ],
        file_extensions=[".txt"],
    )
    summary = run(config, dry_run=False, compress=False, data_dir=tmp_path / "data")

    assert summary.renamed == 2
    assert (inbox_a / "240131_ACME_Invoice.txt").exists()
    assert (inbox_b / "240131_ACME_Invoice.txt").exists()


def test_run_raises_for_missing_inbox(tmp_path: Path) -> None:
    config = AppConfig(inbox=[InboxConfig(path=str(tmp_path / "missing"))])

    with pytest.raises(FileNotFoundError, match="Inbox path does not exist"):
        run(config, dry_run=True, compress=False, data_dir=tmp_path / "data")


def test_run_raises_when_any_inbox_is_missing(tmp_path: Path) -> None:
    existing = tmp_path / "existing"
    existing.mkdir()
    config = AppConfig(
        inbox=[
            InboxConfig(path=str(existing)),
            InboxConfig(path=str(tmp_path / "missing")),
        ]
    )

    with pytest.raises(FileNotFoundError, match="Inbox path does not exist"):
        run(config, dry_run=True, compress=False, data_dir=tmp_path / "data")


def test_uses_ollama_model_detection() -> None:
    """Test that Ollama lifecycle detection covers both model and vision_model."""
    from renamr.renamer import _uses_ollama_model

    assert _uses_ollama_model("ollama/gemma4:e2b") is True
    assert _uses_ollama_model("openai/gpt-4o") is False
    assert _uses_ollama_model("gpt-4o-mini") is False
