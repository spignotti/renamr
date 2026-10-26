"""Local search, evidence display, run history, and conflict-safe undo."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from renamr.cli.common import CliError, ConfigOption, StateOption, confirm, open_workspace
from renamr.core import catalog, history, mutation, workflow
from renamr.core.sanitize import clean_text
from renamr.core.workspace import Workspace

document_app = typer.Typer(help="Show page/fact evidence and export explicit text.")


def search_command(
    query: str,
    limit: Annotated[int, typer.Option("--limit", help="Maximum hits (bounded).")] = 20,
    config: ConfigOption = None,
    state_dir: StateOption = None,
) -> None:
    """Search stored page text locally."""
    workspace = open_workspace(config, state_dir)
    hits = workflow.search(workspace, query, limit=limit)
    if not hits:
        typer.echo("No matching page text.")
        return
    for hit in hits:
        location = _first_location(workspace, hit.revision_id)
        label = "-" if location is None else str(location.canonical_path)
        typer.echo(f"{hit.revision_id} page={hit.page_number} {label}")
        typer.echo(f"  {clean_text(hit.snippet, limit=300)}")


@document_app.command("show")
def document_show(
    revision_id: str, config: ConfigOption = None, state_dir: StateOption = None
) -> None:
    """Show revision metadata, facts, and page provenance."""
    workspace = open_workspace(config, state_dir)
    revision = catalog.get_revision(workspace.db, revision_id)
    typer.echo(json.dumps(revision.model_dump(mode="json"), indent=2))
    for fact in catalog.get_facts(workspace.db, revision_id):
        evidence = ""
        if fact.evidence_page is not None:
            evidence = f" page={fact.evidence_page} quote={clean_text(fact.evidence_quote or '')}"
        typer.echo(
            f"fact {fact.field}={clean_text(fact.value or '')} "
            f"state={fact.validation_state}{evidence}"
        )
    for page in catalog.get_pages(workspace.db, revision_id):
        typer.echo(
            f"page={page.page_number} method={page.method} quality={page.quality} "
            f"confidence={page.confidence} incomplete={page.incomplete} chars={len(page.text)}"
        )


@document_app.command("export")
def document_export(
    revision_id: str,
    to: Annotated[Path, typer.Option("--to", help="Explicit private UTF-8 text destination.")],
    config: ConfigOption = None,
    state_dir: StateOption = None,
) -> None:
    """Export complete stored page text; original bytes are never rewritten."""
    workspace = open_workspace(config, state_dir)
    destination = workflow.export_text(workspace, revision_id, to)
    typer.echo(f"Exported private text to {destination} (local handling applies).")


def history_command(
    run_id: Annotated[str | None, typer.Option("--run", help="Show one run's items.")] = None,
    limit: Annotated[int, typer.Option("--limit")] = 20,
    config: ConfigOption = None,
    state_dir: StateOption = None,
) -> None:
    """List operation runs and their journal states."""
    workspace = open_workspace(config, state_dir)
    if run_id is not None:
        for item in mutation.run_items(workspace, run_id):
            typer.echo(
                f"{item.item_id} state={item.state} source={clean_text(str(item.source_path))} "
                f"target={clean_text(str(item.target_path or '-'))} error={item.error or '-'}"
            )
        return
    for run in mutation.history_runs(workspace, limit=limit):
        typer.echo(
            f"{run['run_id']} kind={run['kind']} status={run['status']} "
            f"root={run['root_id']} created={run['created_at']}"
        )


def undo_command(
    run_id: Annotated[
        str | None, typer.Argument(help="Run id; defaults to latest rename run.")
    ] = None,
    preview: Annotated[bool, typer.Option("--preview", help="Show reversibility only.")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Confirm the reversal.")] = False,
    config: ConfigOption = None,
    state_dir: StateOption = None,
) -> None:
    """Preview and reverse committed renames without overwriting anything."""
    workspace = open_workspace(config, state_dir)
    selected = run_id or _latest_run(workspace)
    candidates = history.preview_undo(workspace.db, selected)
    if not candidates:
        typer.echo("No committed renames to reverse.")
        return
    for candidate in candidates:
        typer.echo(
            f"{clean_text(str(candidate.target_path))} -> "
            f"{clean_text(str(candidate.source_path))} status={candidate.status}"
        )
    if preview:
        return
    if not confirm(f"Reverse run {selected}?", yes=yes):
        typer.echo("Undo left unapplied.")
        return
    outcome = mutation.undo(workspace, selected)
    typer.echo(
        f"reversed={outcome.reversed} refused={outcome.refused} "
        f"reasons={','.join(outcome.reasons) or '-'}"
    )


def _latest_run(workspace: Workspace) -> str:
    """Return the newest rename/structure run id."""
    for run in mutation.history_runs(workspace, limit=50):
        if run["kind"] in ("rename", "structure"):
            return str(run["run_id"])
    raise CliError("no rename run is available to undo")


def _first_location(workspace: Workspace, revision_id: str) -> catalog.LocationRecord | None:
    """Return the first present location for a revision."""
    locations = catalog.locations_for_revision(workspace.db, revision_id)
    return locations[0] if locations else None
