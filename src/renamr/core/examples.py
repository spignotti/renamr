"""Confirmed one-off labels and conservative manual-move observations.

Labels and corrections are kept separate from routing rules. A manual move is
only a candidate example when exactly one present location has the same content
and the pair was not produced by this application's own operation journal.
Ambiguous, copied, offline, or own-operation pairs never become training data.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from renamr.core import catalog
from renamr.core.db import Database
from renamr.core.errors import CatalogError
from renamr.core.ids import new_id

_REVIEW_KIND = "possible_move"


class ExampleSource(StrEnum):
    """Where a confirmed example came from."""

    USER_LABEL = "user_label"
    USER_CORRECTION = "user_correction"
    MANUAL_MOVE = "manual_move"
    NEGATIVE = "negative"


class Example(BaseModel):
    """One confirmed label, correction, negative example, or manual move."""

    model_config = ConfigDict(extra="forbid")

    example_id: str
    revision_id: str | None = None
    location_id: str | None = None
    label: str = ""
    payload: dict[str, object] = {}
    source: str
    created_at: str = ""


class ManualMoveObservation(BaseModel):
    """One observed disappearance/appearance pair and its verdict."""

    model_config = ConfigDict(extra="forbid")

    observation_id: str
    revision_id: str
    old_location_id: str
    new_location_id: str
    old_path: Path
    new_path: Path
    match_kind: str
    status: str
    example_id: str | None = None


def add_example(
    db: Database,
    *,
    source: ExampleSource,
    revision_id: str | None,
    location_id: str | None,
    label: str = "",
    payload: Mapping[str, object] | None = None,
) -> Example:
    """Persist one confirmed example and return it."""
    example = Example(
        example_id=new_id("ex"),
        revision_id=revision_id,
        location_id=location_id,
        label=label,
        payload=dict(payload or {}),
        source=str(source),
        created_at=datetime.now(UTC).isoformat(),
    )
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO review_examples (example_id, revision_id, location_id, label, payload, "
            "source, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                example.example_id,
                example.revision_id,
                example.location_id,
                example.label,
                json.dumps(example.payload, sort_keys=True),
                example.source,
                example.created_at,
            ),
        )
    return example


def list_examples(db: Database) -> list[Example]:
    """Return every confirmed example oldest first."""
    with db.read() as connection:
        rows = connection.execute(
            "SELECT * FROM review_examples ORDER BY created_at, example_id"
        ).fetchall()
    examples: list[Example] = []
    for row in rows:
        payload = json.loads(str(row["payload"]))
        examples.append(
            Example(
                example_id=str(row["example_id"]),
                revision_id=None if row["revision_id"] is None else str(row["revision_id"]),
                location_id=None if row["location_id"] is None else str(row["location_id"]),
                label=str(row["label"] or ""),
                payload=payload if isinstance(payload, dict) else {},
                source=str(row["source"]),
                created_at=str(row["created_at"]),
            )
        )
    return examples


def observe_manual_moves(db: Database) -> list[ManualMoveObservation]:
    """Record verdicts for every open possible-move review without confirming any."""
    observations: list[ManualMoveObservation] = []
    for review in catalog.open_review_items(db):
        if str(review.get("kind")) != _REVIEW_KIND:
            continue
        location_id = review.get("location_id")
        revision_id = review.get("revision_id")
        if not isinstance(location_id, str) or not isinstance(revision_id, str):
            continue
        observation = _classify_move(db, revision_id, location_id)
        if observation is not None:
            observations.append(observation)
    return observations


def list_observations(db: Database, *, status: str | None = None) -> list[ManualMoveObservation]:
    """Return persisted observations, optionally filtered by status."""
    query = "SELECT * FROM manual_move_observations"
    params: tuple[object, ...] = ()
    if status is not None:
        query += " WHERE status = ?"
        params = (status,)
    query += " ORDER BY created_at, observation_id"
    with db.read() as connection:
        rows = connection.execute(query, params).fetchall()
    return [_row_to_observation(row) for row in rows]


def confirm_manual_move(db: Database, observation_id: str) -> Example:
    """Confirm one unique observed move as a manual-move example.

    Raises:
        CatalogError: If the observation is unknown, not unique, or already used.
    """
    observation = _get_observation(db, observation_id)
    if observation.status != "candidate":
        raise CatalogError("only a unique unconfirmed move can become an example")
    payload = {
        "old_path": str(observation.old_path),
        "new_path": str(observation.new_path),
        "match_kind": observation.match_kind,
    }
    example = add_example(
        db,
        source=ExampleSource.MANUAL_MOVE,
        revision_id=observation.revision_id,
        location_id=observation.new_location_id,
        label="move",
        payload=payload,
    )
    with db.transaction() as connection:
        connection.execute(
            "UPDATE manual_move_observations SET status = 'confirmed', example_id = ? "
            "WHERE observation_id = ?",
            (example.example_id, observation_id),
        )
        connection.execute(
            "UPDATE review_items SET status = 'resolved' WHERE kind = ? AND revision_id = ? "
            "AND location_id = ? AND status = 'open'",
            (_REVIEW_KIND, observation.revision_id, observation.old_location_id),
        )
        connection.execute(
            "UPDATE review_items SET status = 'resolved' WHERE kind = 'path_missing' "
            "AND revision_id = ? AND location_id = ? AND status = 'open'",
            (observation.revision_id, observation.old_location_id),
        )
    return example


def _classify_move(
    db: Database, revision_id: str, old_location_id: str
) -> ManualMoveObservation | None:
    """Classify one missing location, persisting a conservative verdict."""
    old = catalog.get_location(db, old_location_id)
    candidates = [
        item
        for item in catalog.locations_for_revision(db, revision_id)
        if item.location_id != old.location_id and item.presence is catalog.Presence.PRESENT
    ]
    status, match_kind, candidate = _verdict(db, old, candidates)
    if candidate is None:
        return None
    observation = ManualMoveObservation(
        observation_id=new_id("move"),
        revision_id=revision_id,
        old_location_id=old.location_id,
        new_location_id=candidate.location_id,
        old_path=old.canonical_path,
        new_path=candidate.canonical_path,
        match_kind=match_kind,
        status=status,
    )
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO manual_move_observations (observation_id, revision_id, old_location_id, "
            "new_location_id, old_path, new_path, match_kind, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                observation.observation_id,
                observation.revision_id,
                observation.old_location_id,
                observation.new_location_id,
                str(observation.old_path),
                str(observation.new_path),
                observation.match_kind,
                observation.status,
                datetime.now(UTC).isoformat(),
            ),
        )
    return observation


def _verdict(
    db: Database,
    old: catalog.LocationRecord,
    candidates: list[catalog.LocationRecord],
) -> tuple[str, str, catalog.LocationRecord | None]:
    """Return (status, match kind, candidate) for one disappearance."""
    if _own_operation(db, old.canonical_path, candidates):
        return "own_move", "none", None
    inode_matches = [
        item
        for item in candidates
        if item.dev is not None
        and item.ino is not None
        and item.dev == old.dev
        and item.ino == old.ino
    ]
    if len(inode_matches) == 1:
        return "candidate", "inode", inode_matches[0]
    if len(candidates) == 1:
        return "candidate", "content_hash", candidates[0]
    if len(candidates) > 1:
        return "ambiguous", "multiple", candidates[0]
    return "unresolved", "none", None


def _own_operation(db: Database, old_path: Path, candidates: list[catalog.LocationRecord]) -> bool:
    """Return whether this application's journal explains the pair."""
    paths = {str(old_path)} | {str(item.canonical_path) for item in candidates}
    with db.read() as connection:
        for path in paths:
            row = connection.execute(
                "SELECT 1 FROM run_items WHERE source_path = ? OR target_path = ? LIMIT 1",
                (path, path),
            ).fetchone()
            if row is not None:
                return True
    return False


def _get_observation(db: Database, observation_id: str) -> ManualMoveObservation:
    """Return one observation or raise."""
    with db.read() as connection:
        row = connection.execute(
            "SELECT * FROM manual_move_observations WHERE observation_id = ?", (observation_id,)
        ).fetchone()
    if row is None:
        raise CatalogError("unknown manual move observation")
    return _row_to_observation(row)


def _row_to_observation(row: sqlite3.Row) -> ManualMoveObservation:
    """Build an observation from a database row."""
    return ManualMoveObservation(
        observation_id=str(row["observation_id"]),
        revision_id=str(row["revision_id"]),
        old_location_id=str(row["old_location_id"]),
        new_location_id=str(row["new_location_id"]),
        old_path=Path(str(row["old_path"])),
        new_path=Path(str(row["new_path"])),
        match_kind=str(row["match_kind"]),
        status=str(row["status"]),
        example_id=None if row["example_id"] is None else str(row["example_id"]),
    )
