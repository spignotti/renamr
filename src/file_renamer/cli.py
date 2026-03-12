"""CLI entrypoint for file-renamer."""

from typing import Annotated

import typer

from file_renamer import __version__

app = typer.Typer(
    add_completion=False,
    help="Rename local files from AI-extracted metadata.",
    no_args_is_help=True,
)


@app.command()
def version() -> None:
    """Print the installed version."""
    typer.echo(__version__)


@app.command()
def scaffold(
    config: Annotated[str, typer.Option(help="Path to the scaffold config file.")] = "config.toml",
) -> None:
    """Confirm the scaffold is installed and ready for implementation."""
    typer.echo(f"file-renamer scaffold ready. Config path: {config}")


def main() -> None:
    """Run the Typer application."""
    app()
