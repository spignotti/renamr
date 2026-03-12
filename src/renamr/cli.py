"""CLI entrypoint for renamr."""

from __future__ import annotations

import importlib.resources
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from renamr import __version__
from renamr.logging import setup_logging
from renamr.models import load_config
from renamr.renamer import RunSummary, undo_last_run
from renamr.renamer import run as run_pipeline

app = typer.Typer(
    add_completion=False,
    help="Rename local files from AI-extracted metadata.",
    no_args_is_help=True,
)
console = Console()


@app.command()
def version() -> None:
    """Print the installed version."""
    typer.echo(__version__)


@app.command()
def init() -> None:
    """Create a local config file and data directory."""
    setup_logging("INFO", False)
    config_path = Path("config.toml")
    if config_path.exists():
        typer.echo("config.toml already exists")
    else:
        example = importlib.resources.files("renamr").joinpath("config.toml.example").read_text()
        config_path.write_text(example)
        typer.echo("Created config.toml")
    Path("data").mkdir(parents=True, exist_ok=True)
    typer.echo("Ensured data/ exists")


@app.command()
def run(
    config: Annotated[Path, typer.Option("--config")] = Path("config.toml"),
    dry_run: Annotated[bool, typer.Option("--dry-run/--no-dry-run")] = False,
    compress: Annotated[bool | None, typer.Option("--compress/--no-compress")] = None,
    inbox: Annotated[Path | None, typer.Option("--inbox")] = None,
    recursive: Annotated[bool | None, typer.Option("--recursive/--no-recursive")] = None,
    verbose: Annotated[bool, typer.Option("--verbose/--no-verbose")] = False,
) -> None:
    """Scan files, extract metadata, and rename them."""
    if not config.exists():
        typer.secho("Missing config.toml. Run `renamr init` first.", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    app_config = load_config(config)
    if inbox is not None:
        app_config = app_config.model_copy(update={"inbox_path": str(inbox)})
    if recursive is not None:
        app_config = app_config.model_copy(update={"recursive": recursive})
    if compress is None:
        compress = app_config.compress.enabled
    log_level = "DEBUG" if verbose else app_config.logging.level
    setup_logging(log_level, app_config.logging.json_logs)
    data_dir = config.parent / "data"
    summary = run_pipeline(app_config, dry_run=dry_run, compress=compress, data_dir=data_dir)
    _print_summary(summary)


@app.command()
def undo(
    config: Annotated[Path, typer.Option("--config")] = Path("config.toml"),
) -> None:
    """Undo the last successful rename run."""
    setup_logging("INFO", False)
    data_dir = config.parent / "data"
    reversed_pairs = undo_last_run(data_dir)
    if not reversed_pairs:
        typer.secho("Nothing to undo.", fg=typer.colors.YELLOW)
        return
    for new_path, old_path in reversed_pairs:
        typer.echo(f"{new_path.name} -> {old_path.name}")
    typer.secho(f"Undid {len(reversed_pairs)} rename(s).", fg=typer.colors.GREEN)


def _print_summary(summary: RunSummary) -> None:
    """Render a small summary table for a run."""
    table = Table(title="Renamr Summary")
    table.add_column("Status")
    table.add_column("Count", justify="right")
    table.add_row("renamed", str(summary.renamed), style="green")
    table.add_row("skipped", str(summary.skipped), style="yellow")
    table.add_row("failed", str(summary.failed), style="red")
    console.print(table)
    for result in summary.results:
        if result.new_name is None:
            message = result.old_path.name
        else:
            message = f"{result.old_path.name} -> {result.new_name}"
        color = _status_color(result.status)
        if result.error:
            message = f"{message} ({result.error})"
        typer.secho(message, fg=color)


def _status_color(status: str) -> str:
    """Return a Typer color name for one result status."""
    if status == "renamed":
        return typer.colors.GREEN
    if status == "failed":
        return typer.colors.RED
    return typer.colors.YELLOW


def main() -> None:
    """Run the Typer application."""
    app()
