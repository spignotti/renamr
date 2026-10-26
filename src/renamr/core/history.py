"""Run history, crash-safe undo, and a conservative legacy undo adapter.

Undo re-verifies both current bytes and path state before reversing a committed
rename, and refuses occupied or edited paths. The legacy adapter applies under
the same mutation-time boundary guard as every other writer, so a symlink planted
after preview cannot redirect the file outside its registered root.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from renamr.core import catalog, hashing
from renamr.core.db import Database
from renamr.core.errors import UndoError
from renamr.core.journal import FaultInjector, ItemState
from renamr.core.lock import WriterLock
from renamr.core.operations import RunGuard, add_plan_item, apply_run, create_run, run_items
from renamr.core.paths import is_within


class UndoOutcome(BaseModel):
    """Result of an undo request."""

    model_config = ConfigDict(extra="forbid")

    reversed: int = 0
    refused: int = 0
    reasons: list[str] = []


class UndoCandidate(BaseModel):
    """One candidate path reversal with its inspection verdict."""

    model_config = ConfigDict(extra="forbid")

    source_path: Path
    target_path: Path
    status: str


class LegacyUndoEntry(BaseModel):
    """One legacy undo record parsed from ``undo.json``."""

    model_config = ConfigDict(extra="forbid")

    index: int
    old_path: Path
    new_path: Path
    status: str


class LegacyUndoPreview(BaseModel):
    """Preview of a legacy undo file without executing anything."""

    model_config = ConfigDict(extra="forbid")

    undo_path: Path
    entries: list[LegacyUndoEntry] = []
    issues: list[str] = []


def preview_undo(db: Database, run_id: str) -> list[UndoCandidate]:
    """Return the reversibility of each committed rename in a run."""
    candidates: list[UndoCandidate] = []
    for item in run_items(db, run_id):
        if item.state is not ItemState.COMMITTED or item.target_path is None:
            continue
        if str(item.source_path) == str(item.target_path):
            continue
        status = _reversibility(item.source_path, item.target_path, item.content_hash)
        candidates.append(
            UndoCandidate(source_path=item.source_path, target_path=item.target_path, status=status)
        )
    return candidates


def undo_run(
    db: Database,
    run_id: str,
    *,
    lock: WriterLock,
    fault: FaultInjector | None = None,
    guard: RunGuard | None = None,
) -> UndoOutcome:
    """Reverse the committed renames of a run through the same journal."""
    if not lock.held:
        raise UndoError("undo_run requires a held writer lock")
    outcome = UndoOutcome()
    undo_id = create_run(db, kind="undo")
    planned = 0
    for item in run_items(db, run_id):
        if item.state is not ItemState.COMMITTED or item.target_path is None:
            continue
        if str(item.source_path) == str(item.target_path):
            continue
        status = _reversibility(item.source_path, item.target_path, item.content_hash)
        if status != "reversible":
            outcome.refused += 1
            _push(outcome.reasons, status)
            catalog.record_review(
                db,
                kind="undo_refused",
                detail=status,
                revision_id=item.revision_id,
                location_id=item.location_id,
            )
            continue
        _plan_undo_item(db, undo_id, item)
        planned += 1
    if planned:
        result = apply_run(db, undo_id, lock=lock, fault=fault, guard=guard)
        outcome.reversed = result.committed
        outcome.refused += result.blocked + result.failed
        if result.blocked:
            _push(outcome.reasons, "commit_blocked")
        if result.failed:
            _push(outcome.reasons, "commit_failed")
    return outcome


def _plan_undo_item(db: Database, undo_id: str, item: object) -> None:
    """Plan the reverse of one committed rename with its observed identity."""
    from renamr.core.operations import RunItem

    assert isinstance(item, RunItem)
    target = item.target_path
    assert target is not None
    observed = hashing.fingerprint(target)
    add_plan_item(
        db,
        undo_id,
        source_path=target,
        target_path=item.source_path,
        content_hash=observed.content_hash,
        size_bytes=observed.size_bytes,
        revision_id=item.revision_id,
        location_id=item.location_id,
        annotation="undo",
        source_dev=observed.dev,
        source_ino=observed.ino,
        source_mtime_ns=observed.mtime_ns,
    )


def _reversibility(source: Path, target: Path, content_hash: str | None) -> str:
    """Return whether a reversal is safe: reversible, or a refusal reason."""
    if not target.is_file() or target.is_symlink():
        return "target_missing"
    if content_hash and hashing.hash_file(target) != content_hash:
        return "target_changed"
    if source.exists():
        return "original_occupied"
    return "reversible"


def preview_legacy_undo(undo_path: Path, *, roots: list[Path]) -> LegacyUndoPreview:
    """Validate a legacy ``undo.json`` file without executing anything."""
    undo_path = Path(undo_path)
    if not undo_path.is_file():
        return LegacyUndoPreview(undo_path=undo_path, issues=["undo_file_missing"])
    try:
        raw = json.loads(undo_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return LegacyUndoPreview(undo_path=undo_path, issues=["undo_file_unreadable"])
    if not isinstance(raw, list):
        return LegacyUndoPreview(undo_path=undo_path, issues=["undo_shape_invalid"])
    entries = [_preview_legacy_entry(index, record, roots) for index, record in enumerate(raw)]
    return LegacyUndoPreview(undo_path=undo_path, entries=entries)


def apply_legacy_undo(
    db: Database,
    preview: LegacyUndoPreview,
    *,
    confirmed_indices: set[int],
    lock: WriterLock,
    guard: RunGuard,
    fault: FaultInjector | None = None,
) -> UndoOutcome:
    """Execute confirmed legacy reversals through the journal under a guard.

    ``guard`` is required: the legacy adapter never runs without the same
    mutation-time boundary guard as every other writer, so it cannot silently
    exercise a no-guard mutation path.
    """
    if not lock.held:
        raise UndoError("apply_legacy_undo requires a held writer lock")
    run_id = create_run(db, kind="legacy_undo")
    outcome = UndoOutcome()
    planned = 0
    for entry in preview.entries:
        if entry.index not in confirmed_indices:
            continue
        if entry.status != "reversible" or not _still_reversible(entry):
            outcome.refused += 1
            _push(outcome.reasons, entry.status if entry.status != "reversible" else "changed")
            continue
        observed = hashing.fingerprint(entry.new_path)
        add_plan_item(
            db,
            run_id,
            source_path=entry.new_path,
            target_path=entry.old_path,
            content_hash=observed.content_hash,
            size_bytes=observed.size_bytes,
            annotation="legacy_undo",
            source_dev=observed.dev,
            source_ino=observed.ino,
            source_mtime_ns=observed.mtime_ns,
        )
        planned += 1
    if planned:
        result = apply_run(db, run_id, lock=lock, fault=fault, guard=guard)
        outcome.reversed = result.committed
        outcome.refused += result.blocked + result.failed
    return outcome


def _still_reversible(entry: LegacyUndoEntry) -> bool:
    """Re-verify vacancy and regular-file state immediately before planning."""
    return (
        entry.new_path.is_file() and not entry.new_path.is_symlink() and not entry.old_path.exists()
    )


def _preview_legacy_entry(index: int, record: object, roots: list[Path]) -> LegacyUndoEntry:
    """Classify one legacy undo record."""
    if not isinstance(record, dict):
        return LegacyUndoEntry(index=index, old_path=Path(), new_path=Path(), status="shape")
    old_raw = record.get("old_path")
    new_raw = record.get("new_path")
    if not isinstance(old_raw, str) or not isinstance(new_raw, str):
        return LegacyUndoEntry(index=index, old_path=Path(), new_path=Path(), status="shape")
    old_path = Path(old_raw)
    new_path = Path(new_raw)
    status = _legacy_status(old_path, new_path, roots)
    return LegacyUndoEntry(index=index, old_path=old_path, new_path=new_path, status=status)


def _legacy_status(old_path: Path, new_path: Path, roots: list[Path]) -> str:
    """Return the eligibility status of a legacy reversal."""
    if not any(is_within(old_path, root) and is_within(new_path, root) for root in roots):
        return "outside_roots"
    if not new_path.is_file() or new_path.is_symlink():
        return "target_missing"
    if old_path.exists():
        return "original_occupied"
    return "reversible"


def _push(reasons: list[str], reason: str) -> None:
    """Append a reason once."""
    if reason not in reasons:
        reasons.append(reason)


def completed_items(db: Database, run_id: str) -> int:
    """Return the number of committed items in a run."""
    return sum(1 for item in run_items(db, run_id) if item.state is ItemState.COMMITTED)
