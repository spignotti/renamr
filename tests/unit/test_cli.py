"""v2 CLI bootstrap, preview/apply, compatibility, and rejection tests."""

from __future__ import annotations

from importlib.metadata import version
from pathlib import Path
from typing import cast

import click
from typer.main import get_command
from typer.testing import CliRunner

import renamr.cli.run as run_module
from renamr.benchmark_schema import RulePredicate
from renamr.cli import app
from renamr.cli.common import CliError
from renamr.core import workflow
from renamr.core.extract_runner import InProcessRunner
from renamr.core.roots import RootRole
from renamr.core.settings import load_settings
from renamr.core.workspace import Workspace
from tests.unit.core_support import FakeExtractor

runner = CliRunner()


def _prepared(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Create a private workspace, two roots, an active rule, and one document."""
    state = tmp_path / "state"
    config = tmp_path / "config.toml"
    workspace = Workspace.bootstrap(state, config_path=config)
    inbox, archive = tmp_path / "inbox", tmp_path / "archive"
    inbox.mkdir()
    archive.mkdir()
    inbox_root = workflow.register_root(workspace, inbox, role=RootRole.INBOX)
    archive_root = workflow.register_root(workspace, archive, role=RootRole.ARCHIVE)
    workflow.set_allowed_targets(workspace, inbox_root.root_id, [archive_root.root_id])
    draft = workflow.create_rule_draft(
        workspace,
        scope_root_id=inbox_root.root_id,
        target_root_id=archive_root.root_id,
        predicates=[RulePredicate(field="document_type", operator="equals", values=["invoice"])],
        filename_template="{date}_{subject}",
        target_subdir="invoices",
    )
    workflow.mark_tested(workspace, draft.rule_id)
    workflow.activate_rule(workspace, draft.rule_id)
    (inbox / "scan.txt").write_text("Rechnung von Beispiel\nRechnungsdatum: 2026-03-14")
    workspace.extractor = FakeExtractor(document_type="invoice")
    workspace.runner = InProcessRunner()
    workflow.scan(workspace, inbox_root.root_id)
    return config, inbox, archive


def test_version_does_not_create_state(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "statehome"))
    result = runner.invoke(app, ["version"])

    assert result.exit_code == 0
    assert version("renamr") in result.stdout
    assert not (tmp_path / "statehome").exists()


def test_help_does_not_create_state(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "statehome"))
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "Local-first document catalog" in result.stdout
    assert not (tmp_path / "statehome").exists()


def test_init_creates_bootstrap_and_catalog(tmp_path: Path) -> None:
    config = tmp_path / "new" / "config.toml"
    state = tmp_path / "state"

    result = runner.invoke(app, ["init", "--config", str(config), "--state-dir", str(state)])

    assert result.exit_code == 0
    assert config.exists()
    assert (state / "catalog.sqlite3").is_file()


def test_init_does_not_overwrite_existing_bootstrap(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    state = tmp_path / "state"
    Workspace.bootstrap(state, config_path=config)
    original = config.read_text()

    result = runner.invoke(app, ["init", "--config", str(config), "--state-dir", str(state)])

    assert result.exit_code == 0
    assert "already exists" in result.stdout
    assert config.read_text() == original


def test_init_legacy_preview_and_apply_leaves_source(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    legacy = tmp_path / "legacy.toml"
    original = (
        f'language = "de"\n[[inbox]]\npath = "{inbox}"\n'
        '[llm]\nmodel = "ollama/gemma4:e4b"\napi_base = "http://127.0.0.1:11434"\n'
    )
    legacy.write_text(original)
    config = tmp_path / "bootstrap.toml"
    state = tmp_path / "state"

    preview = runner.invoke(
        app,
        [
            "init",
            "--legacy-config",
            str(legacy),
            "--config",
            str(config),
            "--state-dir",
            str(state),
        ],
    )
    assert preview.exit_code == 0
    assert "Preview only" in preview.stdout
    assert not (state / "catalog.sqlite3").exists()

    applied = runner.invoke(
        app,
        [
            "init",
            "--legacy-config",
            str(legacy),
            "--config",
            str(config),
            "--state-dir",
            str(state),
            "--apply",
        ],
    )
    assert applied.exit_code == 0
    assert legacy.read_text() == original
    assert load_settings(Workspace.open(config_path=config).db).languages == ["de"]


def test_run_preview_does_not_move_and_apply_does(tmp_path: Path, monkeypatch) -> None:
    config, inbox, archive = _prepared(tmp_path)
    monkeypatch.setattr(run_module, "catalog_then_features", lambda *_args, **_kwargs: None)

    preview = runner.invoke(app, ["run", "--config", str(config)])

    assert preview.exit_code == 0
    assert "Preview only" in preview.stdout
    assert (inbox / "scan.txt").exists()

    applied = runner.invoke(app, ["run", "--config", str(config), "--apply"])

    assert applied.exit_code == 0
    moved = archive / "invoices" / "2026-03-14_Rechnung.txt"
    assert moved.exists()
    assert not (inbox / "scan.txt").exists()


def test_run_rejects_compress_before_processing(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    result = runner.invoke(app, ["run", "--config", str(config), "--compress"])

    assert result.exit_code == 1
    assert isinstance(result.exception, CliError)
    assert "compression is not supported" in str(result.exception).lower()
    assert not config.exists()


def test_run_rejects_unregistered_inbox(tmp_path: Path, monkeypatch) -> None:
    config, _, _ = _prepared(tmp_path)
    monkeypatch.setattr(run_module, "catalog_then_features", lambda *_args, **_kwargs: None)
    outside = tmp_path / "outside"
    outside.mkdir()

    result = runner.invoke(app, ["run", "--config", str(config), "--inbox", str(outside)])

    assert result.exit_code == 1
    assert isinstance(result.exception, CliError)
    assert "not a registered root" in str(result.exception)


def test_run_help_keeps_compatible_options() -> None:
    command = cast(click.Group, get_command(app))
    run_command = cast(click.Command, command.commands["run"])
    option_names = {
        option for param in run_command.params for option in [*param.opts, *param.secondary_opts]
    }

    for expected in (
        "--config",
        "--inbox",
        "--recursive",
        "--no-recursive",
        "--dry-run",
        "--apply",
        "--compress",
        "--no-compress",
        "--verbose",
    ):
        assert expected in option_names


def test_config_show_and_set(tmp_path: Path) -> None:
    config, _, _ = _prepared(tmp_path)

    shown = runner.invoke(app, ["config", "show", "--config", str(config)])
    assert shown.exit_code == 0
    assert "model_roles" in shown.stdout

    changed = runner.invoke(
        app,
        ["config", "set", "default_filename_template", "{date}_{subject}", "--config", str(config)],
    )
    assert changed.exit_code == 0
    assert load_settings(Workspace.open(config_path=config).db).default_filename_template == (
        "{date}_{subject}"
    )


def test_search_document_history_and_undo_journey(tmp_path: Path, monkeypatch) -> None:
    config, inbox, archive = _prepared(tmp_path)
    monkeypatch.setattr(run_module, "catalog_then_features", lambda *_args, **_kwargs: None)
    applied = runner.invoke(app, ["run", "--config", str(config), "--apply"])
    assert applied.exit_code == 0
    workspace = Workspace.open(config_path=config)
    revision_id = workflow.search(workspace, "Rechnung")[0].revision_id

    searched = runner.invoke(app, ["search", "Rechnung", "--config", str(config)])
    assert searched.exit_code == 0
    assert revision_id in searched.stdout

    document = runner.invoke(app, ["document", "show", revision_id, "--config", str(config)])
    assert document.exit_code == 0
    assert "quality" in document.stdout

    history = runner.invoke(app, ["history", "--config", str(config)])
    assert history.exit_code == 0
    run_id = str(history.stdout.split()[0])

    undone = runner.invoke(app, ["undo", "--yes", "--config", str(config)])
    assert undone.exit_code == 0
    assert (inbox / "scan.txt").exists()
    assert not (archive / "invoices" / "2026-03-14_Rechnung.txt").exists()
    assert run_id


def test_onboard_registers_sibling_roots_without_inference(tmp_path: Path) -> None:
    state = tmp_path / "state"
    config = tmp_path / "config.toml"
    Workspace.bootstrap(state, config_path=config)
    for name in ("Inbox", "Areas", "Archive"):
        folder = tmp_path / name
        folder.mkdir()
        (folder / "note.txt").write_text("synthetic text")

    result = runner.invoke(
        app,
        [
            "onboard",
            "--inbox",
            str(tmp_path / "Inbox"),
            "--library",
            str(tmp_path / "Areas"),
            "--archive",
            str(tmp_path / "Archive"),
            "--no-questions",
            "--no-features",
            "--config",
            str(config),
        ],
    )

    assert result.exit_code == 0
    assert len(workflow.list_roots(Workspace.open(config_path=config))) == 3
    assert "no document was modified" in result.stdout.lower()


def test_review_json_is_noninteractive(tmp_path: Path) -> None:
    config, _, _ = _prepared(tmp_path)
    result = runner.invoke(app, ["review", "--json", "--config", str(config)])

    assert result.exit_code == 0
    assert result.stdout.strip().startswith("[")
