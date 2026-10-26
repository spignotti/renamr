"""Focused review resolution with interactive and scripted paths."""

from __future__ import annotations

import json
import sys
from typing import Annotated

import typer

from renamr.cli.common import CliError, ConfigOption, StateOption, open_workspace
from renamr.core import review, workflow
from renamr.core.errors import CoreError
from renamr.core.sanitize import clean_text
from renamr.core.workspace import Workspace

_LABEL_FIELDS = {"document_type", "issuer", "date", "subject", "language", "entity_id"}

JsonOpt = Annotated[bool, typer.Option("--json", help="List questions as JSON.")]
ObserveOpt = Annotated[
    bool, typer.Option("--observe", help="Re-check possible manual moves without confirming.")
]
ReviewIdOpt = Annotated[
    str | None, typer.Option("--review-id", help="Resolve one specific review item.")
]
LabelOpt = Annotated[
    list[str] | None,
    typer.Option("--label", help="FIELD=VALUE one-off confirmed label (repeatable)."),
]
TemplateOpt = Annotated[
    str | None, typer.Option("--template", help="Explicit template for a naming question.")
]
AcceptOpt = Annotated[
    bool | None, typer.Option("--accept/--reject", help="Confirm or reject a manual move.")
]
DismissOpt = Annotated[bool, typer.Option("--dismiss", help="Resolve without an example.")]
NegativeOpt = Annotated[
    bool, typer.Option("--negative", help="Record --label as a rejected (negative) example.")
]
NotesOpt = Annotated[str, typer.Option("--notes", help="Short local decision note.")]
YesOpt = Annotated[bool, typer.Option("--yes", "-y", help="Confirm interactive questions.")]


def review_command(
    config: ConfigOption = None,
    state_dir: StateOption = None,
    as_json: JsonOpt = False,
    observe: ObserveOpt = False,
    review_id: ReviewIdOpt = None,
    label: LabelOpt = None,
    template: TemplateOpt = None,
    accept: AcceptOpt = None,
    dismiss: DismissOpt = False,
    negative: NegativeOpt = False,
    notes: NotesOpt = "",
    yes: YesOpt = False,
) -> None:
    """List and resolve pending document, move, rule, and identity questions."""
    workspace = open_workspace(config, state_dir)
    if observe:
        _observe(workspace)
        return
    if review_id is not None:
        _resolve_one(
            workspace,
            review_id,
            label=label or [],
            template=template,
            accept=accept,
            dismiss=dismiss,
            negative=negative,
            notes=notes,
        )
        return
    questions = review.review_questions(workspace.db)
    if as_json or not sys.stdin.isatty():
        typer.echo(json.dumps([item.model_dump(mode="json") for item in questions], indent=2))
        if not as_json:
            typer.echo(
                "Noninteractive: use --review-id with --label/--accept/--template/--dismiss."
            )
        return
    if not questions:
        typer.echo("No open review items.")
        return
    for question in questions:
        _ask(workspace, question, yes=yes)


def _observe(workspace: Workspace) -> None:
    """Print conservative manual-move classifications without confirming any."""
    observations = workflow.observe_manual_moves(workspace)
    for item in observations:
        typer.echo(
            f"{item.observation_id} status={item.status} match={item.match_kind} "
            f"revision={item.revision_id}"
        )
    if not observations:
        typer.echo("No possible-move pair is currently classifiable.")


def _resolve_one(
    workspace: Workspace,
    review_id: str,
    *,
    label: list[str],
    template: str | None,
    accept: bool | None,
    dismiss: bool,
    negative: bool,
    notes: str,
) -> None:
    """Resolve one review item from explicit flags."""
    actions = [
        bool(label) and not negative,
        template is not None,
        accept is not None,
        dismiss,
        negative,
    ]
    if sum(actions) != 1:
        raise CliError(
            "choose exactly one of --label, --negative, --template, --accept/--reject, --dismiss"
        )
    if negative:
        fields = _fields(label)
        example = workflow.resolve_negative(workspace, review_id, fields=fields)
        typer.echo(f"Recorded negative example {example.example_id}; no rule was activated.")
    elif label:
        fields = _fields(label)
        example = workflow.resolve_label(workspace, review_id, fields=fields)
        typer.echo(f"Recorded one-off label example {example.example_id}; no rule was activated.")
    elif template is not None:
        rule = workflow.resolve_naming(workspace, review_id, template=template)
        typer.echo(
            f"Created draft revision {rule.rule_id}.{rule.revision}; test it before activation."
        )
    elif accept is not None:
        example = workflow.resolve_manual_move(workspace, review_id, accept=accept)
        if example is None:
            typer.echo("Rejected the move candidate; no example was created.")
        else:
            typer.echo(f"Confirmed manual-move example {example.example_id}.")
    else:
        workflow.dismiss_review(workspace, review_id, note=notes)
        typer.echo("Review item dismissed without creating an example.")


def _ask(workspace: Workspace, question: review.ReviewQuestion, *, yes: bool) -> None:
    """Ask one focused question and apply the explicit answer."""
    typer.echo(f"\n{question.review_id} [{question.kind}] {clean_text(question.question)}")
    if question.kind in ("possible_move", "path_missing"):
        answer = yes or typer.confirm("Confirm this manual move?", default=False)
        try:
            example = workflow.resolve_manual_move(workspace, question.review_id, accept=answer)
        except CoreError as exc:
            typer.echo(f"left open: {exc}")
            return
        typer.echo("confirmed" if example is not None else "rejected")
        return
    if question.kind in ("features_unresolved", "feature_blocked", "extraction_failed"):
        fields = _prompt_fields()
        if fields:
            workflow.resolve_label(workspace, question.review_id, fields=fields)
            typer.echo("recorded one-off labels")
        return
    if question.kind == "naming_field_missing":
        template = typer.prompt("Template without the missing field", default="")
        if template.strip():
            rule = workflow.resolve_naming(workspace, question.review_id, template=template)
            typer.echo(f"draft revision {rule.revision} created; activate explicitly")
        return
    if typer.confirm("Dismiss this question?", default=False):
        workflow.dismiss_review(workspace, question.review_id, note="interactive_dismiss")
        typer.echo("dismissed")


def _prompt_fields() -> dict[str, str]:
    """Prompt for confirmed fields, returning only non-empty answers."""
    prompts = {
        "document_type": "Document type",
        "issuer": "Issuer",
        "date": "Issue date (ISO or German)",
        "subject": "Subject",
        "language": "Language code",
    }
    fields: dict[str, str] = {}
    for name, prompt in prompts.items():
        value = typer.prompt(prompt, default="", show_default=False).strip()
        if value:
            fields[name] = value
    return fields


def _fields(values: list[str]) -> dict[str, str]:
    """Parse repeated FIELD=VALUE options."""
    parsed: dict[str, str] = {}
    for item in values:
        key, separator, value = item.partition("=")
        if not separator or key not in _LABEL_FIELDS:
            raise CliError(f"invalid --label value: {item}")
        parsed[key] = value.strip()
    if not parsed:
        raise CliError("at least one --label is required")
    return parsed
