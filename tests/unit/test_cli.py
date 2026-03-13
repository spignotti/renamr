"""CLI tests."""

from __future__ import annotations

from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import click
from typer.main import get_command
from typer.testing import CliRunner

import renamr.cli as cli_module
import renamr.metadata as metadata_module
from renamr.cli import app

runner = CliRunner()


def test_version_command_outputs_version() -> None:
    result = runner.invoke(app, ["version"])

    assert result.exit_code == 0
    assert version("renamr") in result.stdout


def test_init_creates_config_from_package_data(tmp_path: Path, monkeypatch) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    config_dir = tmp_path / "config-home"
    monkeypatch.setattr(cli_module, "_config_dir", lambda: config_dir)

    result = runner.invoke(app, ["init"], input=f"{inbox}\nen\ngpt-4o-mini\n")

    assert result.exit_code == 0
    config_path = config_dir / "config.toml"
    assert config_path.exists()
    content = config_path.read_text()
    assert f'inbox_paths = ["{inbox.resolve()}"]' in content
    assert 'language = "en"' in content
    assert result.stdout.count(str(config_path)) == 1


def test_init_does_not_call_mkdir_when_config_exists(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "existing-config" / "config.toml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text('language = "en"\n')
    mkdir_calls: list[Path] = []
    original_mkdir = Path.mkdir

    def tracking_mkdir(
        self: Path,
        mode: int = 0o777,
        parents: bool = False,
        exist_ok: bool = False,
    ) -> None:
        mkdir_calls.append(self)
        original_mkdir(self, mode=mode, parents=parents, exist_ok=exist_ok)

    monkeypatch.setattr(Path, "mkdir", tracking_mkdir)

    result = runner.invoke(app, ["init", "--config", str(config_path)])

    assert result.exit_code == 0
    assert f"{config_path} already exists. Delete it to reinitialize." in result.stdout
    assert mkdir_calls == []


def test_run_dry_run_does_not_rename_files(tmp_path: Path, monkeypatch) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    original_file = inbox / "note.txt"
    original_file.write_text("Invoice Date 2024-01-31")
    (tmp_path / "config.toml").write_text(
        f'inbox_paths = ["{inbox}"]\nfile_extensions = [".txt"]\n'
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
    command = cast(click.Group, get_command(app))
    run_command = cast(click.Command, command.commands["run"])
    option_names = {
        option
        for param in run_command.params
        for option in [*param.opts, *param.secondary_opts]
    }

    assert "--config" in option_names
    assert "--dry-run" in option_names
    assert "--compress" in option_names
    assert "--inbox" in option_names
    assert "--recursive" in option_names
    assert "--verbose" in option_names
