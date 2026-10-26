"""Persisted operation item record and its row mapping.

Kept apart from the operation engine so publication and recovery share one
definition without an import cycle.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from renamr.core.journal import ItemState


class RunItem(BaseModel):
    """One persisted operation item and its journal state."""

    model_config = ConfigDict(extra="forbid")

    item_id: str
    run_id: str
    revision_id: str | None = None
    location_id: str | None = None
    annotation: str = ""
    source_path: Path
    target_path: Path | None = None
    state: ItemState
    content_hash: str | None = None
    size_bytes: int | None = None
    source_dev: int | None = None
    source_ino: int | None = None
    source_size: int | None = None
    source_mtime_ns: int | None = None
    target_dev: int | None = None
    target_ino: int | None = None
    rule_id: str | None = None
    rule_revision: int | None = None
    error: str | None = None


RunGuard = Callable[[RunItem], None]


def row_to_item(row: sqlite3.Row) -> RunItem:
    """Build a RunItem from a database row."""
    target = row["target_path"]
    return RunItem(
        item_id=str(row["item_id"]),
        run_id=str(row["run_id"]),
        revision_id=_optional(row["revision_id"]),
        location_id=_optional(row["location_id"]),
        annotation=str(row["annotation"]),
        source_path=Path(str(row["source_path"])),
        target_path=None if target is None else Path(str(target)),
        state=ItemState(str(row["state"])),
        content_hash=_optional(row["content_hash"]),
        size_bytes=row["size_bytes"],
        source_dev=row["source_dev"],
        source_ino=row["source_ino"],
        source_size=row["source_size"],
        source_mtime_ns=row["source_mtime_ns"],
        target_dev=row["target_dev"],
        target_ino=row["target_ino"],
        rule_id=_optional(row["rule_id"]),
        rule_revision=row["rule_revision"],
        error=_optional(row["error"]),
    )


def _optional(value: object) -> str | None:
    """Return a non-empty string or None."""
    return None if value is None else str(value)
