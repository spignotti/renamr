"""Operation runs: planning, applying, and recovering journaled file operations.

A run holds planned items and their journal state. Applying and recovering hold
the writer lock and delegate per-item transitions to :mod:`renamr.core.publication`.
No database write transaction is held across an inference or user-input step.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from renamr.core import publication
from renamr.core.db import Database
from renamr.core.errors import JournalError, LockError
from renamr.core.ids import new_id
from renamr.core.journal import TERMINAL_STATES, FaultInjector, ItemState
from renamr.core.lock import WriterLock
from renamr.core.runitem import RunGuard, RunItem, row_to_item

__all__ = [
    "ApplyResult",
    "RecoveryResult",
    "RunGuard",
    "RunItem",
    "add_plan_item",
    "apply_run",
    "create_run",
    "get_run",
    "list_runs",
    "recover_run",
    "run_items",
]


class ApplyResult(BaseModel):
    """Summary of an apply run."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    committed: int = 0
    noops: int = 0
    blocked: int = 0
    failed: int = 0


class RecoveryResult(BaseModel):
    """Summary of a recovery pass."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    resumed: int = 0
    discarded_stages: int = 0
    blocked: int = 0
    untouched: int = 0


def create_run(
    db: Database,
    *,
    kind: str,
    root_id: str | None = None,
    rule_policy_version: str | None = None,
) -> str:
    """Create a persisted operation run and return its id."""
    run_id = new_id("run")
    now = datetime.now(UTC).isoformat()
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO runs (run_id, kind, status, root_id, rule_policy_version, created_at) "
            "VALUES (?, ?, 'planned', ?, ?, ?)",
            (run_id, kind, root_id, rule_policy_version, now),
        )
    return run_id


def add_plan_item(
    db: Database,
    run_id: str,
    *,
    source_path: Path,
    target_path: Path,
    content_hash: str,
    size_bytes: int,
    revision_id: str | None = None,
    location_id: str | None = None,
    rule_id: str | None = None,
    rule_revision: int | None = None,
    annotation: str = "",
    source_dev: int | None = None,
    source_ino: int | None = None,
    source_mtime_ns: int | None = None,
) -> str:
    """Persist one planned item in the ``planned`` state.

    Source identity (device, inode, size, mtime) is stored alongside the content
    hash so apply and recovery can detect an external edit or replacement.
    """
    item_id = new_id("item")
    now = datetime.now(UTC).isoformat()
    values = _plan_values(
        item_id,
        run_id,
        source_path,
        target_path,
        content_hash,
        size_bytes,
        revision_id,
        location_id,
        rule_id,
        rule_revision,
        annotation,
        source_dev,
        source_ino,
        source_mtime_ns,
        now,
    )
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO run_items (item_id, run_id, revision_id, location_id, annotation, "
            "source_path, target_path, state, content_hash, size_bytes, source_dev, source_ino, "
            "source_size, source_mtime_ns, rule_id, rule_revision, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'planned', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            values,
        )
    return item_id


def _plan_values(
    item_id: str,
    run_id: str,
    source_path: Path,
    target_path: Path,
    content_hash: str,
    size_bytes: int,
    revision_id: str | None,
    location_id: str | None,
    rule_id: str | None,
    rule_revision: int | None,
    annotation: str,
    source_dev: int | None,
    source_ino: int | None,
    source_mtime_ns: int | None,
    now: str,
) -> tuple[object, ...]:
    """Build the positional values for a planned run-item insert."""
    return (
        item_id,
        run_id,
        revision_id,
        location_id,
        annotation,
        str(source_path),
        str(target_path),
        content_hash,
        size_bytes,
        source_dev,
        source_ino,
        size_bytes,
        source_mtime_ns,
        rule_id,
        rule_revision,
        now,
        now,
    )


def get_run(db: Database, run_id: str) -> dict[str, object]:
    """Return a run row as a dictionary, or raise when unknown."""
    with db.read() as connection:
        row = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if row is None:
        raise JournalError("unknown run id")
    return dict(row)


def run_items(db: Database, run_id: str) -> list[RunItem]:
    """Return every item of a run in creation order."""
    with db.read() as connection:
        rows = connection.execute(
            "SELECT * FROM run_items WHERE run_id = ? ORDER BY created_at, item_id", (run_id,)
        ).fetchall()
    return [row_to_item(row) for row in rows]


def list_runs(db: Database, *, limit: int = 50) -> list[dict[str, object]]:
    """Return recent runs newest first."""
    with db.read() as connection:
        rows = connection.execute(
            "SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (max(1, limit),)
        ).fetchall()
    return [dict(row) for row in rows]


def apply_run(
    db: Database,
    run_id: str,
    *,
    lock: WriterLock,
    fault: FaultInjector | None = None,
    guard: RunGuard | None = None,
) -> ApplyResult:
    """Apply every non-terminal item, holding the writer lock.

    Args:
        db: Open catalog database.
        run_id: Run to apply.
        lock: A held writer lock; required so only one writer runs at a time.
        fault: Optional test hook called after each journal transition.
        guard: Optional per-item policy check run before any mutation; raising an
            :class:`OperationError` blocks the item.

    Returns:
        Counts of committed, no-op, blocked, and failed items.

    Raises:
        LockError: If the writer lock is not held.
    """
    if not lock.held:
        raise LockError("apply_run requires a held writer lock")
    result = ApplyResult(run_id=run_id)
    for item in run_items(db, run_id):
        if item.state in TERMINAL_STATES:
            continue
        publication.apply_item(db, item, fault, guard)
        final = _get_item(db, item.item_id)
        if final.state is ItemState.COMMITTED:
            result.committed += 1
        elif final.state is ItemState.NOOP:
            result.noops += 1
        elif final.state is ItemState.FAILED:
            result.failed += 1
        else:
            result.blocked += 1
    _set_run_status(db, run_id, "blocked" if result.blocked or result.failed else "completed")
    return result


def recover_run(
    db: Database,
    run_id: str,
    *,
    lock: WriterLock,
    guard: RunGuard | None = None,
) -> RecoveryResult:
    """Reconcile a run's journal before any new writer runs.

    Args:
        db: Open catalog database.
        run_id: Run to reconcile.
        lock: A held writer lock.
        guard: Optional per-item policy check; raising blocks the item.

    Returns:
        Counts of resumed, discarded, blocked, and untouched items.

    Raises:
        LockError: If the writer lock is not held.
    """
    if not lock.held:
        raise LockError("recover_run requires a held writer lock")
    result = RecoveryResult(run_id=run_id)
    for item in run_items(db, run_id):
        if item.state in TERMINAL_STATES:
            continue
        outcome = publication.recover_item(db, item, guard)
        if outcome == "resumed":
            result.resumed += 1
        elif outcome == "discarded":
            result.discarded_stages += 1
        elif outcome == "blocked":
            result.blocked += 1
        else:
            result.untouched += 1
    if result.resumed or result.blocked or result.discarded_stages:
        _set_run_status(db, run_id, "recovered")
    return result


def _set_run_status(db: Database, run_id: str, status: str) -> None:
    """Record the terminal status of a run."""
    now = datetime.now(UTC).isoformat()
    with db.transaction() as connection:
        connection.execute(
            "UPDATE runs SET status = ?, completed_at = ? WHERE run_id = ?", (status, now, run_id)
        )


def _get_item(db: Database, item_id: str) -> RunItem:
    """Return a single item row."""
    with db.read() as connection:
        row = connection.execute("SELECT * FROM run_items WHERE item_id = ?", (item_id,)).fetchone()
    if row is None:
        raise JournalError("unknown item id")
    return row_to_item(row)
