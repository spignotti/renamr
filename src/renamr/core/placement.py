"""First-class, journaled destination choice for one reviewed document.

The user explicitly chooses a registered target root and a root-relative folder.
The move uses the ordinary operation journal and safety guards, keeps the
existing filename unless a validated name is supplied, and records a separate
destination example. It never activates a rule.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from renamr import files
from renamr.benchmark_schema import DocumentFeatures
from renamr.core import catalog, examples, folders, learning, mutation, operations, roots
from renamr.core.catalog import Presence, QualityState
from renamr.core.db import Database
from renamr.core.errors import CatalogError, CoreError
from renamr.core.features import facts_to_features
from renamr.core.journal import FaultInjector
from renamr.core.workspace import Workspace

_MAX_NAME_BYTES = 255


class PlacementPreview(BaseModel):
    """Exact source, destination, and name shown before confirmation."""

    model_config = ConfigDict(extra="forbid")

    review_id: str
    location_id: str
    revision_id: str
    source_root_id: str
    source_path: Path
    target_root_id: str
    target_subdir: str
    filename: str
    target_path: Path
    document_type: str


class PlacementResult(BaseModel):
    """Outcome of one confirmed one-off destination move."""

    model_config = ConfigDict(extra="forbid")

    preview: PlacementPreview
    run_id: str
    committed: int
    example_id: str
    draft_rule_ids: list[str] = []


def placement_targets(db: Database, review_id: str) -> list[roots.Root]:
    """Return the allowed target roots for a reviewed document's source root."""
    review = _review(db, review_id)
    location_id = _optional(review["location_id"])
    if location_id is None:
        raise CatalogError("review has no document location")
    location = catalog.get_location(db, location_id)
    source_root = roots.get_root(db, location.root_id)
    return [roots.get_root(db, root_id) for root_id in source_root.allowed_target_ids]


def preview_placement(
    db: Database,
    review_id: str,
    *,
    target_root_id: str,
    target_subdir: str = "",
    filename: str | None = None,
) -> PlacementPreview:
    """Validate one explicit destination without changing anything.

    Raises:
        CatalogError: If the review has no present, readable document location.
        CoreError: If the target is not allowed or the destination is occupied.
    """
    location, revision, values = _review_document(db, review_id)
    source_root = roots.get_root(db, location.root_id)
    roots.require_allowed_target(db, source_root.root_id, target_root_id)
    target_root = roots.get_root(db, target_root_id)
    relative_folder = folders.validate_relative_path(target_subdir)
    target_dir = roots.resolve_subpath(target_root, relative_folder)
    name = _target_name(location.canonical_path, filename)
    target_path = target_dir / name
    if target_path == location.canonical_path:
        raise CoreError("destination is the current location")
    if target_path.exists():
        raise CoreError("destination already exists; choose another folder or name")
    return PlacementPreview(
        review_id=review_id,
        location_id=location.location_id,
        revision_id=revision.revision_id,
        source_root_id=source_root.root_id,
        source_path=location.canonical_path,
        target_root_id=target_root.root_id,
        target_subdir=relative_folder,
        filename=name,
        target_path=target_path,
        document_type=values.document_type,
    )


def _review_document(
    db: Database, review_id: str
) -> tuple[catalog.LocationRecord, catalog.RevisionRecord, DocumentFeatures]:
    """Return the present location and readable features for a review."""
    review = _review(db, review_id)
    revision_id = _optional(review["revision_id"])
    location_id = _optional(review["location_id"])
    if revision_id is None or location_id is None:
        raise CatalogError("review has no location to move")
    location = catalog.get_location(db, location_id)
    if location.presence is not Presence.PRESENT:
        raise CatalogError("document location is not present")
    revision = catalog.get_revision(db, revision_id)
    if revision.quality_state not in (QualityState.OK, QualityState.LOW):
        raise CatalogError("document extraction is incomplete; rescan or label it first")
    values = facts_to_features(catalog.get_facts(db, revision_id))
    if values is None:
        raise CatalogError("document metadata is not readable; rescan or label it first")
    return location, revision, values


def apply_placement(
    ws: Workspace,
    review_id: str,
    *,
    target_root_id: str,
    target_subdir: str = "",
    filename: str | None = None,
    fault: FaultInjector | None = None,
) -> PlacementResult:
    """Apply one confirmed destination move through the journal.

    The example is recorded only after the move commits. The caller has already
    displayed and confirmed the preview.

    Raises:
        CoreError: If the move is blocked or fails.
    """
    with ws.lock() as lock:
        preview = preview_placement(
            ws.db,
            review_id,
            target_root_id=target_root_id,
            target_subdir=target_subdir,
            filename=filename,
        )
        run_id = _plan_move(ws.db, preview)
        run = operations.get_run(ws.db, run_id)
        guard = mutation.routing_guard(ws, run)
        operations.recover_run(ws.db, run_id, lock=lock, guard=guard)
        result = operations.apply_run(ws.db, run_id, lock=lock, guard=guard, fault=fault)
        if result.committed != 1:
            raise CoreError("destination move was blocked; the document remains in place")
        example = _record_example(ws.db, preview)
        _resolve(ws.db, review_id)
        _remap_contract_member(ws.db, preview)
        drafts = learning.ensure_drafts_from_examples(ws.db)
        return PlacementResult(
            preview=preview,
            run_id=run_id,
            committed=result.committed,
            example_id=example.example_id,
            draft_rule_ids=[item.rule_id for item in drafts],
        )


def _plan_move(db: Database, preview: PlacementPreview) -> str:
    """Create and persist the one-off journaled move for a preview."""
    observed = catalog.fingerprint(preview.source_path)
    run_id = operations.create_run(db, kind="rename", root_id=preview.source_root_id)
    operations.add_plan_item(
        db,
        run_id,
        source_path=preview.source_path,
        target_path=preview.target_path,
        content_hash=observed.content_hash,
        size_bytes=observed.size_bytes,
        revision_id=preview.revision_id,
        location_id=preview.location_id,
        annotation="explicit_destination",
        source_dev=observed.dev,
        source_ino=observed.ino,
        source_mtime_ns=observed.mtime_ns,
    )
    return run_id


def _record_example(db: Database, preview: PlacementPreview) -> examples.Example:
    """Record the confirmed destination as a separate one-off example."""
    return examples.add_example(
        db,
        source=examples.ExampleSource.USER_DESTINATION,
        revision_id=preview.revision_id,
        location_id=preview.location_id,
        label="user_destination",
        payload=_example_payload(db, preview),
    )


def _target_name(source: Path, filename: str | None) -> str:
    """Return a validated destination filename or the unchanged source name."""
    if filename is None or not filename.strip():
        return source.name
    name = filename.strip()
    if (
        not name
        or name in (".", "..")
        or "/" in name
        or "\\" in name
        or any(ord(char) < 32 for char in name)
    ):
        raise CoreError("destination filename is not safe")
    if Path(name).suffix == "":
        name = f"{name}{source.suffix.lower()}"
    cleaned = files.sanitize_component(Path(name).stem)
    suffix = Path(name).suffix
    result = f"{cleaned}{suffix}"
    if len(result.encode("utf-8")) > _MAX_NAME_BYTES:
        raise CoreError("destination filename is too long")
    return result


def _example_payload(db: Database, preview: PlacementPreview) -> dict[str, object]:
    """Build an auditable destination example from confirmed facts."""
    facts = {fact.field: fact.value for fact in catalog.get_facts(db, preview.revision_id)}
    payload: dict[str, object] = {
        "old_path": str(preview.source_path),
        "new_path": str(preview.target_path),
        "scope_root_id": preview.source_root_id,
        "target_root_id": preview.target_root_id,
    }
    for name in ("document_type", "issuer", "date", "subject", "language", "entity_id"):
        value = facts.get(name)
        if value:
            payload[name] = value
    return payload


def _remap_contract_member(db: Database, preview: PlacementPreview) -> None:
    """Keep a stable contract membership when a member file moves."""
    from renamr.core import contracts

    contracts.remap_moved_path(
        db,
        old_path=preview.source_path,
        new_path=preview.target_path,
        target_root_id=preview.target_root_id,
    )


def _review(db: Database, review_id: str) -> dict[str, object]:
    """Return one open review row or raise."""
    for item in catalog.open_review_items(db):
        if str(item.get("review_id")) == review_id:
            return item
    raise CatalogError("unknown or already resolved review id")


def _resolve(db: Database, review_id: str) -> None:
    """Mark one review resolved."""
    with db.transaction() as connection:
        connection.execute(
            "UPDATE review_items SET status = 'resolved' WHERE review_id = ? AND status = 'open'",
            (review_id,),
        )


def _optional(value: object) -> str | None:
    """Return a non-empty string or None."""
    return None if value is None else str(value)
