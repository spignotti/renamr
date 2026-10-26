"""Trusted onboarding of explicitly selected sibling roots.

Registration is validated by the root registry (no nesting, no state overlap).
Cataloging reads only the selected scopes and never mutates a document. Trusted
example roots and unsorted material are recorded separately so later draft
generation can prefer confirmed examples.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from renamr.core import catalog, folders, roots
from renamr.core.db import Database
from renamr.core.errors import RootError
from renamr.core.roots import Root, RootRole


class OnboardSpec(BaseModel):
    """One explicitly selected root to register during onboarding."""

    model_config = ConfigDict(extra="forbid")

    path: Path
    role: RootRole
    description: str = ""
    alias: str = ""
    trusted_examples: bool = False
    automation_enabled: bool = False


class FolderSummary(BaseModel):
    """Catalog counts for one folder without exposing document text."""

    model_config = ConfigDict(extra="forbid")

    relative_path: str
    documents: int = 0
    unresolved: int = 0
    description: str = ""


def register_siblings(
    db: Database,
    specs: list[OnboardSpec],
    *,
    state_dir: Path,
) -> list[Root]:
    """Register selected roots, then wire allowed target roots.

    Raises:
        RootError: If the selection is empty, overlaps, or is invalid.
    """
    if not specs:
        raise RootError("select at least one root to onboard")
    selected = [Path(spec.path).expanduser().resolve() for spec in specs]
    for left in selected:
        for right in selected:
            if left != right and (left in right.parents or right in left.parents):
                raise RootError("onboarding roots must not be nested")
    registered: list[Root] = []
    for spec in specs:
        registered.append(
            roots.register_root(
                db,
                path=spec.path,
                role=spec.role,
                state_dir=state_dir,
                description=spec.description,
                alias=spec.alias,
                trusted_examples=spec.trusted_examples,
                automation_enabled=spec.automation_enabled,
            )
        )
    _wire_targets(db, registered)
    return [roots.get_root(db, item.root_id) for item in registered]


def folder_summaries(db: Database, root_id: str) -> list[FolderSummary]:
    """Return per-folder catalog counts and existing descriptions."""
    root = roots.get_root(db, root_id)
    counts: dict[str, list[str]] = {}
    for location in catalog.locations_for_root(db, root.root_id):
        relative = folders.folder_for_path(root, location.canonical_path)
        counts.setdefault(relative, []).append(location.revision_id)
    unresolved = _unresolved_by_revision(db)
    overrides = {item.relative_path: item for item in folders.list_folder_overrides(db, root_id)}
    summaries: list[FolderSummary] = []
    for relative in sorted(counts):
        revisions = counts[relative]
        override = overrides.get(relative)
        summaries.append(
            FolderSummary(
                relative_path=relative,
                documents=len(revisions),
                unresolved=sum(1 for item in revisions if item in unresolved),
                description="" if override is None else override.description,
            )
        )
    return summaries


def _wire_targets(db: Database, registered: list[Root]) -> None:
    """Allow inbox roots to target archive and library roots."""
    targets = [
        item.root_id for item in registered if item.role in (RootRole.ARCHIVE, RootRole.LIBRARY)
    ]
    for item in registered:
        if item.role is RootRole.INBOX and targets:
            roots.set_allowed_targets(db, item.root_id, targets)


def _unresolved_by_revision(db: Database) -> set[str]:
    """Return revision ids with an open review item."""
    return {
        str(item["revision_id"])
        for item in catalog.open_review_items(db)
        if item.get("revision_id") is not None
    }
