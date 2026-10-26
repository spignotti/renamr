"""Local natural-language dialog with schema-validated, confirmed proposals."""

from __future__ import annotations

import json
from typing import Annotated

import typer

from renamr.cli.common import (
    CliError,
    ConfigOption,
    StateOption,
    confirm,
    inference_session,
    open_workspace,
)
from renamr.core import dialog, workflow
from renamr.core.errors import CoreError
from renamr.core.inference import LocalInference, verify_model
from renamr.core.sanitize import clean_text
from renamr.core.workspace import Workspace


def chat_command(
    message: Annotated[
        str | None, typer.Argument(help="One request; omit for a local loop.")
    ] = None,
    root: Annotated[
        list[str] | None,
        typer.Option("--root", help="Existing root id included in bounded context."),
    ] = None,
    apply: Annotated[
        bool, typer.Option("--apply", help="Ask for confirmation and apply each proposal.")
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Confirm displayed proposals.")] = False,
    list_proposals: Annotated[
        bool, typer.Option("--list", help="List persisted proposals instead of chatting.")
    ] = False,
    show: Annotated[str | None, typer.Option("--show", help="Show one persisted proposal.")] = None,
    apply_proposal: Annotated[
        str | None,
        typer.Option("--apply-proposal", help="Apply one explicitly confirmed proposal."),
    ] = None,
    config: ConfigOption = None,
    state_dir: StateOption = None,
) -> None:
    """Discuss rules/folders locally; the model only proposes, the terminal confirms."""
    workspace = open_workspace(config, state_dir)
    if list_proposals or show is not None:
        _list_or_show(workspace, show)
        return
    if apply_proposal is not None:
        confirmed = confirm(
            f"Apply proposal {apply_proposal}?", yes=yes, input_text="yes" if yes else None
        )
        outcome = workflow.apply_dialog(workspace, apply_proposal, confirm=confirmed)
        typer.echo(json.dumps(outcome, indent=2, default=str))
        return
    settings = workspace.settings()
    root_ids = root or [item.root_id for item in workflow.list_roots(workspace)][:5]
    if not root_ids:
        raise CliError("register at least one root before using local chat")
    with inference_session(settings, role="small") as inference:
        try:
            verify_model(inference, settings.model_roles.small)
        except CoreError as exc:
            raise CliError(f"chat model is not available locally ({exc.category})") from exc
        if message is not None:
            _turn(workspace, message, inference, root_ids, apply=apply, yes=yes)
            return
        _loop(workspace, inference, root_ids, apply=apply, yes=yes)


def _loop(
    workspace: Workspace,
    inference: LocalInference,
    root_ids: list[str],
    *,
    apply: bool,
    yes: bool,
) -> None:
    """Run a local request loop until the user enters an empty line or exit."""
    while True:
        text = typer.prompt("You", default="", show_default=False).strip()
        if not text or text.lower() in {"exit", "quit"}:
            return
        _turn(workspace, text, inference, root_ids, apply=apply, yes=yes)


def _turn(
    workspace: Workspace,
    message: str,
    inference: LocalInference,
    root_ids: list[str],
    *,
    apply: bool,
    yes: bool,
) -> None:
    """Run one dialog turn and optionally apply its proposal after confirmation."""
    settings = workspace.settings()
    try:
        proposal = workflow.propose_dialog(
            workspace,
            message=message,
            completer=inference,
            model=settings.model_roles.small,
            root_ids=root_ids,
            output_mode=settings.output_mode,
            keep_alive=settings.model_keep_alive.small,
        )
    except CoreError as exc:
        typer.echo(f"proposal rejected: {exc.category}")
        return
    response = proposal.payload.get("response")
    explanation = ""
    if isinstance(response, dict):
        explanation = clean_text(str(response.get("explanation", "")))
    typer.echo(f"proposal {proposal.proposal_id} intent={proposal.intent}")
    if explanation:
        typer.echo(explanation)
    if not apply:
        typer.echo("Not applied. Confirm later with `renamr chat --apply-proposal <id> --yes`.")
        return
    confirmed = confirm(
        f"Apply proposal {proposal.proposal_id}?", yes=yes, input_text="yes" if yes else None
    )
    if not confirmed:
        typer.echo("left as proposed")
        return
    outcome = workflow.apply_dialog(workspace, proposal.proposal_id, confirm=True)
    typer.echo(json.dumps(outcome, indent=2, default=str))


def _list_or_show(workspace: Workspace, show: str | None) -> None:
    """List all proposals or print one."""
    if show is not None:
        for proposal in dialog.list_proposals(workspace.db):
            if proposal.proposal_id == show:
                typer.echo(json.dumps(proposal.model_dump(mode="json"), indent=2))
                return
        raise CliError("unknown proposal")
    for proposal in dialog.list_proposals(workspace.db):
        typer.echo(f"{proposal.proposal_id} intent={proposal.intent} status={proposal.status}")
