"""Preview-first routing run with explicit apply."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from renamr.cli.common import (
    CliError,
    ConfigOption,
    StateOption,
    catalog_then_features,
    open_workspace,
)
from renamr.core import mutation, roots, workflow
from renamr.core.roots import Root, RootRole
from renamr.core.sanitize import clean_text
from renamr.core.workspace import Workspace
from renamr.logging import setup_logging

InboxPaths = Annotated[
    list[Path] | None,
    typer.Option("--inbox", help="Registered inbox root to process (repeatable)."),
]
RecursiveOpt = Annotated[
    bool,
    typer.Option(
        "--recursive/--no-recursive",
        help="v2 always scans recursively; --no-recursive is a documented compatibility no-op.",
    ),
]
ApplyOpt = Annotated[
    bool, typer.Option("--apply", help="Apply the displayed, journaled routing plan.")
]
DryRunOpt = Annotated[bool, typer.Option("--dry-run", help="Explicit preview alias (the default).")]
CompressOpt = Annotated[
    bool | None,
    typer.Option("--compress/--no-compress", help="Compression is rejected in v2."),
]
VerboseOpt = Annotated[bool, typer.Option("--verbose", help="Show sanitized debug diagnostics.")]


def run_command(
    config: ConfigOption = None,
    state_dir: StateOption = None,
    inbox: InboxPaths = None,
    recursive: RecursiveOpt = True,
    apply: ApplyOpt = False,
    dry_run: DryRunOpt = False,
    compress: CompressOpt = None,
    verbose: VerboseOpt = False,
) -> None:
    """Scan registered inboxes, preview routing, and apply only with --apply."""
    if compress is True:
        raise CliError(
            "PDF compression is not supported in v2 because it rewrites original bytes; "
            "remove --compress (use --no-compress as a documented no-op)"
        )
    if not recursive:
        typer.echo("note: v2 scans registered roots recursively; --no-recursive is a no-op")
    workspace = open_workspace(config, state_dir)
    setup_logging(
        "DEBUG" if verbose else "WARNING", False, workspace.state_dir / "logs" / "cli.log", verbose
    )
    settings = workspace.settings()
    selected = _selected_roots(workspace, inbox or [])
    if not selected:
        raise CliError("no registered inbox root is selected; run `renamr onboard` first")
    catalog_then_features(workspace, [item.root_id for item in selected], settings)
    if compress is False:
        typer.echo("Compression remains disabled (documented no-op).")
    run_ids: list[str] = []
    for root in selected:
        run_id = mutation.preview_routing(workspace, root.root_id)
        run_ids.append(run_id)
        _print_preview(workspace, run_id, root.root_id)
    if not apply:
        typer.echo(
            f"Preview only; {len(run_ids)} run(s) persisted. Rerun with --apply to move files."
        )
        return
    _apply_runs(workspace, run_ids)
    typer.echo("Applied through the journal; `renamr undo` can reverse the selected run.")


def _apply_runs(workspace: Workspace, run_ids: list[str]) -> None:
    """Apply each persisted routing run and print sanitized counts."""
    for run_id in run_ids:
        result = mutation.apply(workspace, run_id)
        typer.echo(
            f"apply {run_id}: committed={result.committed} noops={result.noops} "
            f"blocked={result.blocked} failed={result.failed}"
        )


def _selected_roots(workspace: Workspace, inbox: list[Path]) -> list[Root]:
    """Resolve explicit registered inbox roots without expanding read scope."""
    if not inbox:
        return [item for item in workflow.list_roots(workspace) if item.role is RootRole.INBOX]
    selected: list[Root] = []
    for path in inbox:
        root = roots.find_root_by_path(workspace.db, path)
        if root is None:
            raise CliError(f"--inbox is not a registered root: {path}; run `renamr onboard`")
        selected.append(root)
    return list({item.root_id: item for item in selected}.values())


def _print_preview(workspace: Workspace, run_id: str, root_id: str) -> None:
    """Print one run's planned items and review count."""
    items = mutation.run_items(workspace, run_id)
    typer.echo(f"preview {run_id} root={root_id}: planned={len(items)}")
    for item in items:
        target = "-" if item.target_path is None else clean_text(str(item.target_path))
        typer.echo(f"  {clean_text(str(item.source_path))} -> {target}")
    typer.echo(f"  open_reviews={len(workflow.review_items(workspace))}")
