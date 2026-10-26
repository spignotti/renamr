"""Structure proposal, inspection, and explicitly confirmed application."""

from __future__ import annotations

import json
from typing import Annotated

import typer

from renamr.cli.common import CliError, ConfigOption, StateOption, confirm, open_workspace
from renamr.core import structure, workflow
from renamr.core.sanitize import clean_text

structure_app = typer.Typer(help="Propose, inspect, and apply bounded folder/rule changes.")


@structure_app.command("propose")
def propose_command(
    root_id: str,
    map_value: Annotated[
        list[str] | None, typer.Option("--map", help="SOURCE:TARGET root-relative folder mapping.")
    ] = None,
    template: Annotated[
        str | None, typer.Option("--template", help="Rename mapped files in place with a template.")
    ] = None,
    rule_id: Annotated[
        str | None, typer.Option("--rule-id", help="Active rule to update after files move.")
    ] = None,
    rule_revision: Annotated[
        int | None, typer.Option("--rule-revision", help="Expected active rule revision.")
    ] = None,
    config: ConfigOption = None,
    state_dir: StateOption = None,
) -> None:
    """Persist a draft plan without moving any file."""
    workspace = open_workspace(config, state_dir)
    mappings = [_mapping(item) for item in (map_value or [])]
    rule_update = None
    if rule_id is not None:
        if rule_revision is None or template is None:
            raise CliError("--rule-id needs --rule-revision and --template")
        rule_update = structure.RuleUpdate(
            rule_id=rule_id, rule_revision=rule_revision, filename_template=template
        )
    if not mappings and template is None:
        raise CliError("provide at least one --map or --template")
    plan = workflow.propose_structure(
        workspace,
        structure.StructureDraft(
            root_id=root_id,
            mappings=mappings,
            rename_template=template,
            rule_update=rule_update,
        ),
    )
    typer.echo(f"draft plan {plan.plan_id}: items={len(plan.items)} state={plan.state}")
    for item in plan.items:
        typer.echo(f"  {clean_text(str(item.source_path))} -> {clean_text(str(item.target_path))}")
    typer.echo("No file changed. Apply explicitly with `renamr structure apply <plan-id> --yes`.")


@structure_app.command("show")
def show_command(plan_id: str, config: ConfigOption = None, state_dir: StateOption = None) -> None:
    """Show one plan, its snapshot, and item states."""
    workspace = open_workspace(config, state_dir)
    plan = structure.get_plan(workspace.db, plan_id)
    typer.echo(
        json.dumps(
            plan.model_dump(
                mode="json",
                exclude={"items": {"__all__": {"content_hash"}}},
            ),
            indent=2,
        )
    )


@structure_app.command("list")
def list_command(config: ConfigOption = None, state_dir: StateOption = None) -> None:
    """List persisted structure plans."""
    workspace = open_workspace(config, state_dir)
    for plan in structure.list_plans(workspace.db):
        typer.echo(f"{plan.plan_id} root={plan.root_id} state={plan.state} items={len(plan.items)}")


@structure_app.command("apply")
def apply_command(
    plan_id: str,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Confirm the displayed plan.")] = False,
    config: ConfigOption = None,
    state_dir: StateOption = None,
) -> None:
    """Apply one plan under the writer lock, then update rules only after success."""
    workspace = open_workspace(config, state_dir)
    plan = structure.get_plan(workspace.db, plan_id)
    typer.echo(f"plan {plan.plan_id} items={len(plan.items)} state={plan.state}")
    for item in plan.items:
        typer.echo(f"  {clean_text(str(item.source_path))} -> {clean_text(str(item.target_path))}")
    if not confirm(f"Apply plan {plan.plan_id}?", yes=yes):
        typer.echo("Plan left unapplied.")
        return
    result = workflow.apply_structure(workspace, plan.plan_id)
    typer.echo(
        f"state={result.state} committed={result.committed} blocked={result.blocked} "
        f"failed={result.failed} rule_updated={result.rule_updated}"
    )


def _mapping(value: str) -> structure.FolderMapping:
    """Parse one SOURCE:TARGET mapping."""
    source, separator, target = value.partition(":")
    if not separator:
        raise CliError("expected SOURCE:TARGET for --map")
    return structure.FolderMapping(source=source.strip(), target=target.strip())
