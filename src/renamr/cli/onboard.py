"""Onboarding command for explicitly selected existing document roots."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from renamr.cli.common import (
    CliError,
    ConfigOption,
    StateOption,
    feature_extractor,
    inference_session,
    needs_feature_work,
    open_workspace,
    verify_and_key,
)
from renamr.core import folders, onboarding, roots, workflow
from renamr.core.reconcile import ScanOutcome
from renamr.core.roots import Root, RootRole
from renamr.core.settings import ROUTINE_ROLE
from renamr.core.workspace import Workspace

InboxPaths = Annotated[
    list[Path] | None, typer.Option("--inbox", help="Existing inbox root (repeatable).")
]
LibraryPaths = Annotated[
    list[Path] | None, typer.Option("--library", help="Existing library root (repeatable).")
]
ArchivePaths = Annotated[
    list[Path] | None, typer.Option("--archive", help="Existing archive root (repeatable).")
]
PathList = Annotated[list[Path] | None, typer.Option("--unsorted", help="Unsorted root.")]
TrustedPaths = Annotated[
    list[Path] | None, typer.Option("--trusted", help="Mark a root as trusted examples.")
]
AutomationPaths = Annotated[
    list[Path] | None, typer.Option("--automation", help="Enable unattended routing.")
]
DescriptionPairs = Annotated[
    list[str] | None, typer.Option("--describe", help="PATH=TEXT short description.")
]
AliasPairs = Annotated[list[str] | None, typer.Option("--alias", help="PATH=ALIAS.")]


def onboard_command(
    inbox: InboxPaths = None,
    library: LibraryPaths = None,
    archive: ArchivePaths = None,
    unsorted: PathList = None,
    trusted: TrustedPaths = None,
    automation: AutomationPaths = None,
    describe: DescriptionPairs = None,
    alias: AliasPairs = None,
    no_catalog: Annotated[
        bool, typer.Option("--no-catalog", help="Register only; do not catalog page text.")
    ] = False,
    features: Annotated[
        bool,
        typer.Option(
            "--features/--no-features",
            help="Run the verified local small-role classification pass on selected scopes.",
        ),
    ] = True,
    no_questions: Annotated[
        bool, typer.Option("--no-questions", help="Skip interactive folder descriptions.")
    ] = False,
    config: ConfigOption = None,
    state_dir: StateOption = None,
) -> None:
    """Register sibling roots, describe them, classify, and seed cautious examples."""
    workspace = open_workspace(config, state_dir)
    specs = _specs(
        inbox or [],
        library or [],
        archive or [],
        unsorted or [],
        trusted or [],
        automation or [],
        describe or [],
        alias or [],
    )
    registered = _register(workspace, specs)
    for root in registered:
        typer.echo(f"registered {root.root_id} role={root.role} alias={root.alias or '-'}")
    if no_catalog:
        typer.echo("Registered only; no documents were catalogued.")
        return
    _catalog(workspace, registered, ask=not no_questions, features=features)
    seeded = workflow.seed_trusted_examples(workspace, [root.root_id for root in registered])
    if seeded.examples:
        typer.echo(
            f"seeded {seeded.examples} conservative placement example(s) from "
            f"{seeded.roots} trusted scope(s); no rule was activated"
        )
    typer.echo("Onboarding catalogued selected scopes; no document was modified.")


def _register(workspace: Workspace, specs: list[onboarding.OnboardSpec]) -> list[Root]:
    """Register selected roots and update only explicitly supplied fields."""
    existing = {root.canonical_path: root for root in workflow.list_roots(workspace)}
    new_specs: list[onboarding.OnboardSpec] = []
    for spec in specs:
        resolved = Path(spec.path).expanduser().resolve()
        if resolved not in existing:
            new_specs.append(spec)
    with workspace.lock():
        if new_specs:
            onboarding.register_siblings(workspace.db, new_specs, state_dir=workspace.state_dir)
        for spec in specs:
            resolved = Path(spec.path).expanduser().resolve()
            current = existing.get(resolved)
            if current is None:
                continue
            roots.update_root(
                workspace.db,
                current.root_id,
                description=spec.description or None,
                alias=spec.alias or None,
                trusted_examples=spec.update_trusted,
                automation_enabled=spec.automation_enabled,
            )
        onboarding.wire_selected_inboxes(
            workspace.db,
            [
                item.root_id
                for item in workflow.list_roots(workspace)
                if item.canonical_path in {Path(spec.path).expanduser().resolve() for spec in specs}
            ],
        )
    selected: list[Root] = []
    for spec in specs:
        resolved = Path(spec.path).expanduser().resolve()
        root = next(
            (item for item in workflow.list_roots(workspace) if item.canonical_path == resolved),
            None,
        )
        if root is not None and all(item.root_id != root.root_id for item in selected):
            selected.append(root)
    return selected


def _catalog(workspace: Workspace, registered: list[Root], *, ask: bool, features: bool) -> None:
    """Catalog selected roots and optionally run the verified small-role pass."""
    settings = workspace.settings()
    for root in registered:
        current = workflow.get_root(workspace, root.root_id)
        outcome = workflow.scan(workspace, current.root_id)
        _print_outcome(current, outcome)
        if ask and current.canonical_path.is_dir():
            _ask_descriptions(workspace, current.root_id)
    root_ids = [root.root_id for root in registered]
    if features and needs_feature_work(workspace, root_ids, settings):
        with inference_session(settings, role=ROUTINE_ROLE) as inference:
            verify_and_key(workspace, settings, inference, role=ROUTINE_ROLE)
            workspace.extractor = feature_extractor(settings, inference, role=ROUTINE_ROLE)
            for root_id in root_ids:
                current = workflow.get_root(workspace, root_id)
                outcome = workflow.scan(workspace, root_id)
                _print_outcome(current, outcome)
    for root_id in root_ids:
        current = workflow.get_root(workspace, root_id)
        for summary in onboarding.folder_summaries(workspace.db, current.root_id):
            typer.echo(
                f"  folder={summary.relative_path or '.'} documents={summary.documents} "
                f"unresolved={summary.unresolved} description={summary.description or '-'}"
            )


def _print_outcome(root: Root, outcome: ScanOutcome) -> None:
    """Print sanitized catalog counts for one root."""
    typer.echo(
        f"catalog {root.root_id}: discovered={outcome.discovered} "
        f"new={outcome.new_revisions} unchanged={outcome.unchanged} "
        f"features={outcome.features_run} reviews={outcome.reviews} "
        f"status={outcome.status}"
    )


def _specs(
    inbox: list[Path],
    library: list[Path],
    archive: list[Path],
    unsorted: list[Path],
    trusted: list[Path],
    automation: list[Path],
    describe: list[str],
    alias: list[str],
) -> list[onboarding.OnboardSpec]:
    """Build validated onboarding specs from command flags."""
    descriptions = _pairs(describe)
    aliases = _pairs(alias)
    unsorted_set = {str(Path(item).expanduser().resolve()) for item in unsorted}
    trusted_set = {str(Path(item).expanduser().resolve()) for item in trusted}
    automation_set = {str(Path(item).expanduser().resolve()) for item in automation}
    specs: list[onboarding.OnboardSpec] = []
    for paths, role in (
        (inbox, RootRole.INBOX),
        (library, RootRole.LIBRARY),
        (archive, RootRole.ARCHIVE),
    ):
        for path in paths:
            resolved = str(Path(path).expanduser().resolve())
            is_example = role is not RootRole.INBOX or resolved in trusted_set
            if resolved in unsorted_set:
                is_example = False
            update_trusted: bool | None = None
            if resolved in trusted_set:
                update_trusted = True
            elif resolved in unsorted_set:
                update_trusted = False
            specs.append(
                onboarding.OnboardSpec(
                    path=Path(path),
                    role=role,
                    description=descriptions.get(resolved, ""),
                    alias=aliases.get(resolved, ""),
                    trusted_examples=is_example,
                    update_trusted=update_trusted,
                    automation_enabled=True if resolved in automation_set else None,
                )
            )
    if not specs:
        raise CliError("select at least one --inbox/--library/--archive root")
    return specs


def _pairs(values: list[str]) -> dict[str, str]:
    """Parse repeated PATH=VALUE options, rejecting malformed entries."""
    parsed: dict[str, str] = {}
    for item in values:
        key, separator, value = item.partition("=")
        if not separator or not key.strip():
            raise CliError("expected PATH=VALUE for --describe/--alias")
        parsed[str(Path(key.strip()).expanduser().resolve())] = value.strip()
    return parsed


def _ask_descriptions(workspace: Workspace, root_id: str) -> None:
    """Ask for short descriptions of immediate child folders interactively."""
    root = workflow.get_root(workspace, root_id)
    for folder in folders.discover_folders(root):
        if not typer.confirm(f"Describe folder '{folder}'?", default=False):
            continue
        text = typer.prompt("Short description", default="")
        workflow.set_folder_override(workspace, root_id, relative_path=folder, description=text)
