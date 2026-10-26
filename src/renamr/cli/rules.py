"""Rule listing, deterministic drafting, testing, activation, and maintenance."""

from __future__ import annotations

import json
from typing import Annotated

import typer

from renamr.cli.common import CliError, ConfigOption, StateOption, open_workspace
from renamr.core import learning, rule_maintenance, rules, workflow
from renamr.core.sanitize import clean_text

rules_app = typer.Typer(help="List, suggest, test, activate, revise, and roll back rules.")


@rules_app.command("list")
def list_command(config: ConfigOption = None, state_dir: StateOption = None) -> None:
    """List current rule revisions."""
    workspace = open_workspace(config, state_dir)
    for rule in workflow.list_rules(workspace):
        typer.echo(
            f"{rule.rule_id} rev={rule.revision} status={rule.status} "
            f"scope={rule.scope_root_id}/{rule.scope_subtree or '.'} "
            f"target={rule.target_root_id}/{rule.target_subdir or '.'} "
            f"template={rule.filename_template} priority={rule.priority}"
        )


@rules_app.command("show")
def show_command(rule_id: str, config: ConfigOption = None, state_dir: StateOption = None) -> None:
    """Show one rule revision and its bounded predicates."""
    workspace = open_workspace(config, state_dir)
    rule = rules.get_rule(workspace.db, rule_id)
    typer.echo(json.dumps(rule.model_dump(mode="json"), indent=2))


@rules_app.command("suggest")
def suggest_command(
    create: Annotated[
        bool, typer.Option("--create", help="Create drafts from the shown candidates.")
    ] = False,
    config: ConfigOption = None,
    state_dir: StateOption = None,
) -> None:
    """Show deterministic candidates learned from confirmed destination examples."""
    workspace = open_workspace(config, state_dir)
    candidates = workflow.propose_rule_candidates(workspace)
    if not candidates:
        typer.echo("No confirmed destination examples are available for a conservative draft.")
        return
    for index, candidate in enumerate(candidates, start=1):
        typer.echo(
            f"candidate {index}: scope={candidate.scope_root_id} "
            f"target={candidate.target_root_id}/{candidate.target_subdir or '.'} "
            f"documents={candidate.impact.matching_documents} "
            f"positives={candidate.impact.positive_examples} "
            f"negatives={candidate.impact.negative_examples} "
            f"conflicts={candidate.impact.conflicts}"
        )
        for predicate in candidate.predicates:
            typer.echo(f"  {predicate.field} {predicate.operator} {', '.join(predicate.values)}")
        if create:
            rule = workflow.create_rule_from_candidate(workspace, candidate)
            typer.echo(f"  created draft {rule.rule_id} (not active)")


@rules_app.command("test")
def test_command(
    rule_id: str,
    mark_tested: Annotated[
        bool, typer.Option("--mark-tested/--no-mark-tested", help="Record the tested state.")
    ] = True,
    config: ConfigOption = None,
    state_dir: StateOption = None,
) -> None:
    """Evaluate a rule against examples and catalog counts before activation."""
    workspace = open_workspace(config, state_dir)
    rule = rules.get_rule(workspace.db, rule_id)
    impact = learning.evaluate(
        workspace.db,
        learning.RuleCandidate(
            scope_root_id=rule.scope_root_id,
            target_root_id=rule.target_root_id,
            target_subdir=rule.target_subdir,
            predicates=rule.predicates,
            filename_template=rule.filename_template,
            example_ids=[],
            impact=learning.RuleImpact(),
        ),
    )
    typer.echo(
        f"impact documents={impact.matching_documents} positives={impact.positive_examples} "
        f"negatives={impact.negative_examples} conflicts={impact.conflicts}"
    )
    if impact.negative_examples or impact.conflicts:
        typer.echo("warning: negative examples or conflicting active rules match this draft")
    if mark_tested and rule.status is rules.RuleStatus.DRAFT:
        updated = workflow.mark_tested(workspace, rule_id)
        typer.echo(f"marked tested at revision {updated.revision}; activation is separate")


@rules_app.command("activate")
def activate_command(
    rule_id: str,
    config: ConfigOption = None,
    state_dir: StateOption = None,
) -> None:
    """Activate a tested rule after the impact review."""
    workspace = open_workspace(config, state_dir)
    rule = workflow.activate_rule(workspace, rule_id)
    typer.echo(f"activated {rule.rule_id} revision={rule.revision}")


@rules_app.command("revise")
def revise_command(
    rule_id: str,
    template: Annotated[str | None, typer.Option("--template")] = None,
    priority: Annotated[int | None, typer.Option("--priority")] = None,
    target_subdir: Annotated[str | None, typer.Option("--target-subdir")] = None,
    config: ConfigOption = None,
    state_dir: StateOption = None,
) -> None:
    """Append a new rule revision; an active rule keeps its state."""
    workspace = open_workspace(config, state_dir)
    changes: dict[str, object] = {}
    if template is not None:
        changes["filename_template"] = template
    if priority is not None:
        changes["priority"] = priority
    if target_subdir is not None:
        changes["target_subdir"] = target_subdir
    if not changes:
        raise CliError("provide at least one revision field")
    rule = workflow.revise_rule(workspace, rule_id, **changes)
    typer.echo(f"revised {rule.rule_id} revision={rule.revision} status={rule.status}")


@rules_app.command("disable")
def disable_command(
    rule_id: str, config: ConfigOption = None, state_dir: StateOption = None
) -> None:
    """Disable a rule without deleting its history."""
    workspace = open_workspace(config, state_dir)
    rule = workflow.disable_rule(workspace, rule_id, reason="explicit_disable")
    typer.echo(f"disabled {rule.rule_id} revision={rule.revision}")


@rules_app.command("rollback")
def rollback_command(
    rule_id: str,
    revision: Annotated[int | None, typer.Option("--revision")] = None,
    config: ConfigOption = None,
    state_dir: StateOption = None,
) -> None:
    """Restore an earlier rule revision as a new active revision."""
    workspace = open_workspace(config, state_dir)
    rule = workflow.rollback_rule(workspace, rule_id, revision=revision)
    typer.echo(f"rolled back {rule.rule_id} to revision content in revision {rule.revision}")


@rules_app.command("history")
def history_command(
    rule_id: str, config: ConfigOption = None, state_dir: StateOption = None
) -> None:
    """Show every stored revision of one rule."""
    workspace = open_workspace(config, state_dir)
    for rule in workflow.rule_history(workspace, rule_id):
        typer.echo(
            f"revision={rule.revision} status={rule.status} template={rule.filename_template} "
            f"provenance={clean_text(rule.provenance, limit=80)}"
        )


@rules_app.command("maintain")
def maintain_command(config: ConfigOption = None, state_dir: StateOption = None) -> None:
    """Show rule/example maintenance counts without deleting history."""
    workspace = open_workspace(config, state_dir)
    report = rule_maintenance.maintenance_report(workspace.db)
    for key, value in sorted(report.items()):
        typer.echo(f"{key}: {value}")
