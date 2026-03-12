"""CLI tests."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

import renamr.metadata as metadata_module
from renamr.cli import app

runner = CliRunner()


def test_version_command_outputs_version() -> None:
    result = runner.invoke(app, ["version"])

    assert result.exit_code == 0
    assert "0.1.0" in result.stdout


def test_init_creates_config_from_package_data(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["init"])

    assert result.exit_code == 0
    assert (tmp_path / "config.toml").exists()
    assert (tmp_path / "data").is_dir()


def test_run_dry_run_does_not_rename_files(tmp_path: Path, monkeypatch) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    original_file = inbox / "note.txt"
    original_file.write_text("Invoice Date 2024-01-31")
    (tmp_path / "config.toml").write_text(
        f'inbox_path = "{inbox}"\nfile_extensions = [".txt"]\n'
    )

    monkeypatch.setattr(
        metadata_module,
        "completion",
        lambda **_: SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=(
                            '{"sender":"ACME","subject":"Invoice",'
                            '"date":"2024-01-31","filename_format":"date_sender_subject"}'
                        )
                    )
                )
            ]
        ),
    )

    result = runner.invoke(app, ["run", "--config", str(tmp_path / "config.toml"), "--dry-run"])

    assert result.exit_code == 0
    assert original_file.exists()
    assert "note.txt -> 240131_ACME_Invoice.txt" in result.stdout


def test_run_help_shows_expected_options() -> None:
    result = runner.invoke(app, ["run", "--help"])

    assert result.exit_code == 0
    assert "--config" in result.stdout
    assert "--dry-run" in result.stdout
    assert "--compress" in result.stdout
    assert "--inbox" in result.stdout
    assert "--recursive" in result.stdout
    assert "--verbose" in result.stdout
