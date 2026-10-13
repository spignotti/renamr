"""Renamer pipeline tests."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from litellm.exceptions import AuthenticationError

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
    original = existing / "first.txt"
    original.write_text("Synthetic invoice")
    config = AppConfig(
        inbox=[
            InboxConfig(path=str(existing)),
            InboxConfig(path=str(tmp_path / "missing")),
        ]
    )

    with pytest.raises(FileNotFoundError, match="Inbox path does not exist"):
        run(config, dry_run=False, compress=False, data_dir=tmp_path / "data")
    assert original.exists()
    assert list(existing.iterdir()) == [original]


def test_uses_ollama_model_detection() -> None:
    """Test that Ollama lifecycle detection covers both model and vision_model."""
    from renamr.renamer import _uses_ollama_model

    assert _uses_ollama_model("ollama/gemma4:e2b") is True
    assert _uses_ollama_model("openai/gpt-4o") is False
    assert _uses_ollama_model("gpt-4o-mini") is False


@pytest.mark.parametrize("dry_run", [False, True])
def test_auth_failure_preserves_previous_undo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dry_run: bool
) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "first.txt").write_text("Synthetic document")
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    undo_path = data_dir / "undo.json"
    undo_path.write_text("[]")
    calls: list[object] = []

    def failing_metadata(**kwargs: object) -> None:
        calls.append(kwargs)
        raise AuthenticationError(message="synthetic", llm_provider="openrouter", model="test")

    monkeypatch.setattr(renamer_module, "extract_metadata", failing_metadata)
    config = AppConfig(inbox=[InboxConfig(path=str(inbox))])
    summary = run(config, dry_run=dry_run, compress=False, data_dir=data_dir)

    assert summary.failed == 1
    assert summary.results[0].fatal
    assert undo_path.read_text() == "[]"
    assert len(calls) == 1


def test_auth_failure_keeps_completed_renames_undoable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    for name in ["a.txt", "b.txt", "c.txt"]:
        (inbox / name).write_text("Synthetic document")
    calls: list[object] = []

    def metadata_then_failure(**kwargs: object) -> FileMetadata:
        calls.append(kwargs)
        if len(calls) > 1:
            raise AuthenticationError(message="synthetic", llm_provider="openrouter", model="test")
        return _fake_metadata()

    monkeypatch.setattr(renamer_module, "extract_metadata", metadata_then_failure)
    config = AppConfig(inbox=[InboxConfig(path=str(inbox))])
    summary = run(config, dry_run=False, compress=False, data_dir=tmp_path / "data")

    assert summary.renamed == 1
    assert summary.failed == 1
    assert len(calls) == 2
    renamer_module.undo_last_run(tmp_path / "data")
    assert sorted(path.name for path in inbox.iterdir()) == ["a.txt", "b.txt", "c.txt"]
