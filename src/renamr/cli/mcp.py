"""Explicit stdio MCP serving plus local grant/revoke management."""

from __future__ import annotations

import asyncio
from typing import Annotated

import typer

from renamr.cli.common import (
    CliError,
    ConfigOption,
    StateOption,
    confirm,
    open_workspace,
    private_state_dir,
)
from renamr.core import mcp_gateway
from renamr.mcp_server import build_server

mcp_app = typer.Typer(help="Serve the read-only stdio MCP surface.")
permissions_app = typer.Typer(help="Grant or revoke enumerated MCP export fields.")

_DISCLOSURE = (
    "Permitted responses can reach any MCP client, including a cloud-hosted model. "
    "This interface cannot protect against a client that independently has shell or "
    "filesystem access."
)


@mcp_app.command("serve")
def serve_command(config: ConfigOption = None, state_dir: StateOption = None) -> None:
    """Serve only over stdio; there is no HTTP listener."""
    state = private_state_dir(config, state_dir)
    server = build_server(state)
    typer.echo(_DISCLOSURE, err=True)
    asyncio.run(server.run_stdio_async())


@permissions_app.command("grant")
def grant_command(
    contract_id: Annotated[str, typer.Option("--contract-id")],
    field: Annotated[list[str] | None, typer.Option("--field", help="Enumerated export field.")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Confirm the export grant.")] = False,
    config: ConfigOption = None,
    state_dir: StateOption = None,
) -> None:
    """Grant one or more allowlisted fields for a safe-aliased contract."""
    workspace = open_workspace(config, state_dir)
    if not field:
        raise CliError("provide at least one --field")
    typer.echo(_DISCLOSURE)
    if not confirm(f"Grant {', '.join(field or [])} for {contract_id}?", yes=yes):
        typer.echo("Grant cancelled.")
        return
    with workspace.lock():
        for name in field:
            grant = mcp_gateway.grant_field(workspace.db, contract_id, name)
            typer.echo(f"granted {grant.alias}.{grant.field}")


@permissions_app.command("revoke")
def revoke_command(
    contract_id: Annotated[str, typer.Option("--contract-id")],
    field: Annotated[list[str] | None, typer.Option("--field", help="Enumerated export field.")],
    config: ConfigOption = None,
    state_dir: StateOption = None,
) -> None:
    """Revoke fields; the next MCP request sees the change without restart."""
    workspace = open_workspace(config, state_dir)
    if not field:
        raise CliError("provide at least one --field")
    with workspace.lock():
        for name in field:
            grant = mcp_gateway.revoke_field(workspace.db, contract_id, name)
            typer.echo(f"revoked {grant.alias}.{grant.field}")


@permissions_app.command("list")
def list_command(config: ConfigOption = None, state_dir: StateOption = None) -> None:
    """List local grants with contract ids and aliases."""
    workspace = open_workspace(config, state_dir)
    for grant in mcp_gateway.list_grants(workspace.db):
        typer.echo(
            f"{grant.contract_id} alias={grant.alias} field={grant.field} granted={grant.granted}"
        )
    typer.echo(_DISCLOSURE)
