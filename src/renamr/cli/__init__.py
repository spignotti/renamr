"""Small Typer registrar for the local-first renamr CLI.

Command bodies live in focused modules. Help and ``version`` never create a
config file, state directory, database, or model process.
"""

from __future__ import annotations

import typer

from renamr.cli.benchmark import benchmark_command
from renamr.cli.bootstrap import config_app, doctor_command, init_command, version_command
from renamr.cli.chat import chat_command
from renamr.cli.common import CliError, describe_error
from renamr.cli.contracts import contracts_app
from renamr.cli.library import document_app, history_command, search_command, undo_command
from renamr.cli.mcp import mcp_app, permissions_app
from renamr.cli.onboard import onboard_command
from renamr.cli.review import review_command
from renamr.cli.rules import rules_app
from renamr.cli.run import run_command
from renamr.cli.service import service_app
from renamr.cli.structure import structure_app
from renamr.core.errors import CoreError

app = typer.Typer(
    add_completion=False,
    help="Local-first document catalog, cautious routing, and read-only MCP sharing.",
    no_args_is_help=True,
)
app.command("version")(version_command)
app.command("init")(init_command)
app.command("doctor")(doctor_command)
app.command("onboard")(onboard_command)
app.command("run")(run_command)
app.command("review")(review_command)
app.command("chat")(chat_command)
app.command("search")(search_command)
app.command("benchmark")(benchmark_command)
app.command("history")(history_command)
app.command("undo")(undo_command)
app.add_typer(config_app, name="config")
app.add_typer(rules_app, name="rules")
app.add_typer(structure_app, name="structure")
app.add_typer(document_app, name="document")
app.add_typer(contracts_app, name="contracts")
app.add_typer(service_app, name="service")
app.add_typer(mcp_app, name="mcp")
app.add_typer(permissions_app, name="permissions")


def main() -> None:
    """Run the Typer application with sanitized error output."""
    try:
        app()
    except (CliError, CoreError) as exc:
        typer.secho(f"Error: {describe_error(exc)}", fg=typer.colors.RED, err=True)
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        typer.secho("Cancelled.", fg=typer.colors.YELLOW, err=True)
        raise SystemExit(130) from None
    except Exception as exc:
        typer.secho(f"Error: {describe_error(exc)}", fg=typer.colors.RED, err=True)
        raise SystemExit(1) from None
