"""Focused review resolution with interactive and scripted paths."""

from __future__ import annotations

import json
import sys
from typing import Annotated

import typer

from renamr.cli.common import CliError, ConfigOption, StateOption, confirm, open_workspace
from renamr.core import review, workflow
from renamr.core.errors import CoreError
from renamr.core.placement import PlacementPreview
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
DestinationRootOpt = Annotated[
    str | None,
    typer.Option("--destination-root", "--target-root", help="Explicit allowed target root id."),
]
DestinationFolderOpt = Annotated[
    str | None,
    typer.Option(
        "--destination-folder", "--target-folder", help="Root-relative destination folder."
    ),
]
DestinationNameOpt = Annotated[
    str | None,
    typer.Option("--destination-name", help="Optional destination filename; defaults to source."),
]


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
    destination_root: DestinationRootOpt = None,
    destination_folder: DestinationFolderOpt = None,
    destination_name: DestinationNameOpt = None,
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
            destination_root=destination_root,
            destination_folder=destination_folder or "",
            destination_name=destination_name,
            yes=yes,
        )
        return
    _finish_questions(workspace, as_json=as_json, yes=yes)


def _finish_questions(workspace: Workspace, *, as_json: bool, yes: bool) -> None:
    """Print pending questions noninteractively or ask them in sequence."""
    questions = review.review_questions(workspace.db)
    if as_json or not sys.stdin.isatty():
        typer.echo(json.dumps([item.model_dump(mode="json") for item in questions], indent=2))
        if not as_json:
            typer.echo(
                "Noninteractive: use --review-id with --label/--accept/--template/"
                "--destination-root/--dismiss."
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
    destination_root: str | None,
    destination_folder: str,
    destination_name: str | None,
    yes: bool,
) -> None:
    """Resolve one review item from explicit flags."""
    actions = [
        bool(label) and not negative,
        template is not None,
        accept is not None,
        dismiss,
        negative,
        destination_root is not None,
    ]
    if sum(actions) != 1:
        raise CliError(
            "choose exactly one of --label, --negative, --template, --accept/--reject, "
            "--destination-root, --dismiss"
        )
    if destination_root is not None:
        _resolve_destination(
            workspace,
            review_id,
            target_root_id=destination_root,
            target_folder=destination_folder,
            name=destination_name,
            yes=yes,
        )
        return
    _resolve_non_destination(
        workspace,
        review_id,
        label=label,
        template=template,
        accept=accept,
        dismiss=dismiss,
        negative=negative,
        notes=notes,
    )


def _resolve_non_destination(
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
    """Resolve a non-destination review action from explicit flags."""
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


def _resolve_destination(
    workspace: Workspace,
    review_id: str,
    *,
    target_root_id: str,
    target_folder: str,
    name: str | None,
    yes: bool,
) -> None:
    """Preview and apply one explicit destination move after confirmation."""
    preview = workflow.preview_placement(
        workspace,
        review_id,
        target_root_id=target_root_id,
        target_subdir=target_folder,
        filename=name,
    )
    _print_placement(preview)
    if not confirm(
        "Move this document to the displayed destination?",
        yes=yes,
        input_text="yes" if yes else None,
    ):
        typer.echo("Destination left unapplied.")
        return
    result = workflow.apply_placement(
        workspace,
        review_id,
        target_root_id=target_root_id,
        target_subdir=target_folder,
        filename=name,
    )
    typer.echo(
        f"moved through run {result.run_id}; recorded destination example "
        f"{result.example_id}; no rule was activated"
    )
    if result.draft_rule_ids:
        typer.echo(
            f"created {len(result.draft_rule_ids)} testable draft rule(s) from repeated "
            "placements; test and activation remain separate"
        )


def _print_placement(preview: PlacementPreview) -> None:
    """Render the validated source, destination, and name before confirmation."""
    typer.echo(f"source: {clean_text(str(preview.source_path))}")
    typer.echo(f"destination folder: {clean_text(str(preview.target_path.parent))}")
    typer.echo(f"filename: {clean_text(preview.filename)}")
    typer.echo(f"destination: {clean_text(str(preview.target_path))}")


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
    if question.kind == "unmatched_placement":
        _ask_destination(workspace, question, yes=yes)
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


def _ask_destination(workspace: Workspace, question: review.ReviewQuestion, *, yes: bool) -> None:
    """Interactively choose and confirm an explicit target root and folder."""
    try:
        targets = workflow.placement_targets(workspace, question.review_id)
    except CoreError as exc:
        typer.echo(f"left open: {exc}")
        return
    if not targets:
        typer.echo("No allowed target root is configured for this source root.")
        return
    for target in targets:
        typer.echo(
            f"  target {target.root_id} alias={target.alias or '-'} "
            f"role={target.role} path={clean_text(str(target.canonical_path))}"
        )
    choice = typer.prompt("Target root id", default=targets[0].root_id).strip()
    if all(target.root_id != choice for target in targets):
        typer.echo("Unknown target root id; left open.")
        return
    folder = typer.prompt("Target folder relative to the root", default="").strip()
    name = typer.prompt("Filename (empty keeps the source name)", default="").strip()
    try:
        _resolve_destination(
            workspace,
            question.review_id,
            target_root_id=choice,
            target_folder=folder,
            name=name or None,
            yes=yes,
        )
    except CoreError as exc:
        typer.echo(f"left open: {exc}")


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
