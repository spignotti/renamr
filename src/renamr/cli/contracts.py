"""Contract grouping, refresh, evidence display, and explicit overrides."""

from __future__ import annotations

import json
from typing import Annotated

import typer

from renamr.cli.common import ConfigOption, StateOption, confirm, open_workspace
from renamr.core import contracts, workflow
from renamr.core.sanitize import clean_text

contracts_app = typer.Typer(help="Group contracts by folder and inspect source-backed facts.")


@contracts_app.command("preview")
def preview_command(
    root_id: str,
    base_path: Annotated[
        str | None,
        typer.Option("--base-path", help="Root-relative folder whose children are contracts."),
    ] = None,
    config: ConfigOption = None,
    state_dir: StateOption = None,
) -> None:
    """Show the each-child-folder convention before confirming it."""
    workspace = open_workspace(config, state_dir)
    entries = contracts.preview_groups(workspace.db, root_id, base_path=base_path)
    typer.echo("convention: each child folder below the base path is one contract")
    for entry in entries:
        typer.echo(
            f"folder={clean_text(entry.relative_path, limit=80)} "
            f"contract={entry.contract_id or 'new'} alias={entry.alias or '-'}"
        )
    if not entries:
        typer.echo("No immediate child folders were found.")


@contracts_app.command("configure")
def configure_command(
    root_id: str,
    base_path: Annotated[
        str | None,
        typer.Option("--base-path", help="Root-relative folder whose children are contracts."),
    ] = None,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Confirm the displayed convention.")
    ] = False,
    config: ConfigOption = None,
    state_dir: StateOption = None,
) -> None:
    """Confirm group membership once and assign stable contract ids."""
    workspace = open_workspace(config, state_dir)
    entries = contracts.preview_groups(workspace.db, root_id, base_path=base_path)
    for entry in entries:
        typer.echo(f"{clean_text(entry.relative_path, limit=80)} -> {entry.contract_id or 'new'}")
    if not confirm("Confirm each child folder as one contract?", yes=yes):
        typer.echo("Contract grouping left unconfigured.")
        return
    created = workflow.configure_contracts(workspace, root_id, confirmed=True, base_path=base_path)
    typer.echo(f"configured {len(created)} contract group(s) in root {root_id}")


@contracts_app.command("create")
def create_command(
    root_id: Annotated[str, typer.Option("--root")],
    path: Annotated[str, typer.Option("--path", help="Root-relative folder or file.")],
    alias: Annotated[str, typer.Option("--alias")] = "",
    description: Annotated[str, typer.Option("--description")] = "",
    config: ConfigOption = None,
    state_dir: StateOption = None,
) -> None:
    """Create one explicit contract group as a manual exception."""
    workspace = open_workspace(config, state_dir)
    contract = workflow.create_contract(
        workspace,
        root_id=root_id,
        relative_path=path,
        alias=alias,
        description=description,
    )
    typer.echo(
        f"{contract.contract_id} alias={contract.alias or '-'} "
        f"members={','.join(item.relative_path for item in contract.members)}"
    )


@contracts_app.command("list")
def list_command(config: ConfigOption = None, state_dir: StateOption = None) -> None:
    """List stable contract ids, aliases, states, and members."""
    workspace = open_workspace(config, state_dir)
    for contract in contracts.list_contracts(workspace.db):
        members = ",".join(f"{item.root_id}/{item.relative_path}" for item in contract.members)
        typer.echo(
            f"{contract.contract_id} alias={contract.alias or '-'} state={contract.state} "
            f"members={members or '-'}"
        )


@contracts_app.command("show")
def show_command(
    contract_id: str, config: ConfigOption = None, state_dir: StateOption = None
) -> None:
    """Show local evidence for every contract fact."""
    workspace = open_workspace(config, state_dir)
    contract = contracts.get_contract(workspace.db, contract_id)
    typer.echo(f"{contract.contract_id} alias={contract.alias} state={contract.state}")
    for fact in contracts.get_contract_facts(workspace.db, contract_id):
        quote = clean_text(fact.evidence_quote or "", limit=200)
        typer.echo(
            f"{fact.field}={fact.value or '-'} origin={fact.origin} "
            f"page={fact.source_page or '-'} relationship={fact.relationship or '-'} "
            f"effective={fact.effective_date or '-'} quote={quote or '-'}"
        )


@contracts_app.command("refresh")
def refresh_command(
    root_id: Annotated[str | None, typer.Option("--root")] = None,
    contract_id: Annotated[list[str] | None, typer.Option("--contract")] = None,
    config: ConfigOption = None,
    state_dir: StateOption = None,
) -> None:
    """Read complete stored page text and refresh deterministic contract facts."""
    workspace = open_workspace(config, state_dir)
    result = workflow.refresh_contracts(
        workspace, root_id=root_id, contract_ids=contract_id or None
    )
    typer.echo(
        f"contracts={result.contracts} facts={result.facts} "
        f"incomplete_documents={result.incomplete_documents} "
        f"conflicts={result.conflicts} skipped={result.skipped}"
    )


@contracts_app.command("alias")
def alias_command(
    contract_id: str,
    alias: str,
    config: ConfigOption = None,
    state_dir: StateOption = None,
) -> None:
    """Set a safe unique local alias for MCP discovery."""
    workspace = open_workspace(config, state_dir)
    contract = workflow.set_contract_alias(workspace, contract_id, alias)
    typer.echo(f"{contract.contract_id} alias={contract.alias}")


@contracts_app.command("assign")
def assign_command(
    contract_id: str,
    root_id: Annotated[str, typer.Option("--root")],
    relative_path: Annotated[str, typer.Option("--path")],
    config: ConfigOption = None,
    state_dir: StateOption = None,
) -> None:
    """Add one explicit manual membership exception."""
    workspace = open_workspace(config, state_dir)
    member = workflow.assign_contract_member(
        workspace, contract_id, root_id=root_id, relative_path=relative_path
    )
    typer.echo(f"assigned {member.root_id}/{member.relative_path} to {contract_id}")


@contracts_app.command("fact")
def fact_command(
    contract_id: str,
    field: Annotated[str, typer.Option("--field")],
    value: Annotated[str, typer.Option("--value")],
    config: ConfigOption = None,
    state_dir: StateOption = None,
) -> None:
    """Record one locally confirmed typed fact that survives refreshes."""
    workspace = open_workspace(config, state_dir)
    fact = workflow.set_contract_fact(workspace, contract_id, field, value)
    typer.echo(json.dumps(fact.model_dump(mode="json"), indent=2))
