"""Trusted onboarding of explicitly selected sibling roots.

Registration is validated by the root registry (no nesting, no state overlap).
Cataloging reads only the selected scopes and never mutates a document. Trusted
example roots and unsorted material are recorded separately so later draft
generation can prefer confirmed examples.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from renamr.core import catalog, examples, features, folders, roots
from renamr.core.db import Database
from renamr.core.errors import RootError
from renamr.core.roots import Root, RootRole


class OnboardSpec(BaseModel):
    """One explicitly selected root to register or update during onboarding."""

    model_config = ConfigDict(extra="forbid")

    path: Path
    role: RootRole
    description: str = ""
    alias: str = ""
    trusted_examples: bool = False
    update_trusted: bool | None = None
    automation_enabled: bool | None = None


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
    """Register the selected roots and extend only those inboxes' targets.

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
                automation_enabled=bool(spec.automation_enabled),
            )
        )
    wire_selected_inboxes(db, [item.root_id for item in registered])
    return [roots.get_root(db, item.root_id) for item in registered]


def wire_selected_inboxes(db: Database, selected_root_ids: list[str]) -> None:
    """Add selected library/archive roots to selected inboxes' targets only."""
    selected = {
        root_id: roots.get_root(db, root_id) for root_id in dict.fromkeys(selected_root_ids)
    }
    addresses = [
        item for item in selected.values() if item.role in (RootRole.LIBRARY, RootRole.ARCHIVE)
    ]
    for inbox in (item for item in selected.values() if item.role is RootRole.INBOX):
        targets = list(inbox.allowed_target_ids)
        targets.extend(item.root_id for item in addresses if item.root_id != inbox.root_id)
        roots.set_allowed_targets(db, inbox.root_id, list(dict.fromkeys(targets)))


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


class TrustedSeedResult(BaseModel):
    """Counts from seeding auditable placement examples."""

    model_config = ConfigDict(extra="forbid")

    examples: int = 0
    roots: int = 0
    skipped_roots: int = 0


def seed_trusted_examples(db: Database, *, root_ids: list[str] | None = None) -> TrustedSeedResult:
    """Seed conservative placement examples from trusted existing folders.

    Only trusted roots with complete, non-review classification seed examples.
    Each example is projected onto inboxes that explicitly allow the trusted root
    as a target. Examples are historical membership, never lifecycle proof and
    never an active rule.
    """
    registered = roots.list_roots(db)
    selected = set(root_ids or [])
    inboxes = [item for item in registered if item.role is RootRole.INBOX]
    existing = {
        (item.source, item.revision_id, json.dumps(item.payload, sort_keys=True))
        for item in examples.list_examples(db)
    }
    result = TrustedSeedResult()
    for root in registered:
        if selected and root.root_id not in selected:
            continue
        _seed_root(db, root, inboxes, existing, result)
    return result


def _seed_root(
    db: Database,
    root: Root,
    inboxes: list[Root],
    existing: set[tuple[str, str | None, str]],
    result: TrustedSeedResult,
) -> None:
    """Seed one trusted root's placement examples for allowed inboxes."""
    if not root.trusted_examples:
        return
    scopes = [item for item in inboxes if root.root_id in item.allowed_target_ids]
    if not scopes:
        result.skipped_roots += 1
        return
    grouped = _trusted_groups(db, root)
    if not grouped:
        result.skipped_roots += 1
        return
    result.roots += 1
    for key, location in grouped.items():
        document_type, issuer, language, entity_id, _folder = key
        for inbox in scopes:
            if _add_placement_example(
                db,
                root,
                inbox,
                location,
                document_type,
                issuer,
                language,
                entity_id,
                existing,
            ):
                result.examples += 1


def _add_placement_example(
    db: Database,
    root: Root,
    inbox: Root,
    location: catalog.LocationRecord,
    document_type: str,
    issuer: str | None,
    language: str | None,
    entity_id: str | None,
    existing: set[tuple[str, str | None, str]],
) -> bool:
    """Add one deduplicated placement example; return whether it was new."""
    payload = {
        "document_type": document_type,
        "issuer": issuer,
        "language": language,
        "entity_id": entity_id,
        "old_path": str(location.canonical_path),
        "new_path": str(location.canonical_path),
        "scope_root_id": inbox.root_id,
        "target_root_id": root.root_id,
        "trusted_root_id": root.root_id,
        "historical": root.role is RootRole.ARCHIVE,
    }
    payload = {name: value for name, value in payload.items() if value}
    identity = (
        str(examples.ExampleSource.TRUSTED_PLACEMENT),
        location.revision_id,
        json.dumps(payload, sort_keys=True),
    )
    if identity in existing:
        return False
    examples.add_example(
        db,
        source=examples.ExampleSource.TRUSTED_PLACEMENT,
        revision_id=location.revision_id,
        location_id=location.location_id,
        label="trusted_placement",
        payload=payload,
    )
    existing.add(identity)
    return True


def _trusted_groups(
    db: Database, root: Root
) -> dict[tuple[str, str | None, str | None, str | None, str], catalog.LocationRecord]:
    """Return one representative validated location per placement group."""
    grouped: dict[tuple[str, str | None, str | None, str | None, str], catalog.LocationRecord] = {}
    for location in catalog.locations_for_root(db, root.root_id):
        if location.presence is not catalog.Presence.PRESENT:
            continue
        revision = catalog.get_revision(db, location.revision_id)
        if revision.quality_state not in (catalog.QualityState.OK, catalog.QualityState.LOW):
            continue
        facts = catalog.get_facts(db, revision.revision_id)
        values = features.facts_to_features(facts)
        if values is None or values.needs_review:
            continue
        folder = folders.folder_for_path(root, location.canonical_path)
        entity_id = next((item.value for item in facts if item.field == "entity_id"), None)
        key = (values.document_type, values.issuer, values.language, entity_id, folder)
        grouped.setdefault(key, location)
    return grouped


def _unresolved_by_revision(db: Database) -> set[str]:
    """Return revision ids with an open review item."""
    return {
        str(item["revision_id"])
        for item in catalog.open_review_items(db)
        if item.get("revision_id") is not None
    }
