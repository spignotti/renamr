"""Registered root registry with boundary and symlink protections.

Only registered roots may be read, and only explicitly allowed targets may
receive routed files. Every condition (existence, state-overlap, nested-root
overlap, duplicate path, allowed-target existence) is validated before the row is
written, so a rejected registration never leaves a root behind.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from renamr.core.db import Database
from renamr.core.errors import PathBoundaryError, RootError
from renamr.core.ids import new_id
from renamr.core.paths import cloud_sync_hint, is_within, state_overlaps_root


class RootRole(StrEnum):
    """Role of a registered root in the workflow."""

    INBOX = "inbox"
    LIBRARY = "library"
    ARCHIVE = "archive"


class Root(BaseModel):
    """A registered document root."""

    model_config = ConfigDict(extra="forbid")

    root_id: str
    canonical_path: Path
    role: RootRole
    description: str = ""
    trusted_examples: bool = False
    automation_enabled: bool = False
    allowed_target_ids: list[str] = []


def register_root(
    db: Database,
    *,
    path: Path,
    role: RootRole,
    state_dir: Path,
    description: str = "",
    trusted_examples: bool = False,
    automation_enabled: bool = False,
    allowed_targets: list[str] | None = None,
) -> Root:
    """Register a document root after validating every precondition.

    Args:
        db: Open catalog database.
        path: Existing directory to register.
        role: Inbox, library, or archive role.
        state_dir: Private state directory used for the overlap check.
        description: Short human description used for onboarding and rules.
        trusted_examples: Whether the root holds trusted training examples.
        automation_enabled: Whether unattended routing is permitted here.
        allowed_targets: Root ids this root may route files into.

    Returns:
        The stored root.

    Raises:
        RootError: If the path is invalid, overlaps state, overlaps an existing
            root, is already registered, or names an unknown allowed target.
    """
    canonical = validate_root_path(path)
    targets = _validate_registration(db, canonical, Path(state_dir), allowed_targets)
    return _insert_root(
        db,
        canonical=canonical,
        role=role,
        description=description,
        trusted_examples=trusted_examples,
        automation_enabled=automation_enabled,
        targets=targets,
    )


def _validate_registration(
    db: Database, canonical: Path, state_dir: Path, allowed_targets: list[str] | None
) -> list[str]:
    """Validate every registration precondition, returning deduplicated targets."""
    check_root_allowed(canonical, state_dir=state_dir)
    targets = list(dict.fromkeys(allowed_targets or []))
    with db.read() as connection:
        _reject_existing_overlap(connection, canonical)
        _require_targets(connection, targets)
    return targets


def _insert_root(
    db: Database,
    *,
    canonical: Path,
    role: RootRole,
    description: str,
    trusted_examples: bool,
    automation_enabled: bool,
    targets: list[str],
) -> Root:
    """Write a validated root and return the stored record."""
    root_id = new_id("root")
    now = datetime.now(UTC).isoformat()
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO roots (root_id, canonical_path, role, description, trusted_examples, "
            "automation_enabled, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                root_id,
                str(canonical),
                str(role),
                description,
                int(trusted_examples),
                int(automation_enabled),
                now,
                now,
            ),
        )
        for target in targets:
            connection.execute(
                "INSERT INTO root_targets (source_root_id, target_root_id) VALUES (?, ?)",
                (root_id, target),
            )
    return Root(
        root_id=root_id,
        canonical_path=canonical,
        role=role,
        description=description,
        trusted_examples=trusted_examples,
        automation_enabled=automation_enabled,
        allowed_target_ids=targets,
    )


def validate_root_path(path: Path) -> Path:
    """Return the canonical path of a valid root directory.

    Raises:
        RootError: If the path is missing, not a directory, or cannot resolve.
    """
    candidate = Path(path).expanduser()
    if not candidate.exists():
        raise RootError("root path does not exist")
    if not candidate.is_dir():
        raise RootError("root path is not a directory")
    try:
        return candidate.resolve()
    except OSError:
        raise RootError("root path cannot be resolved") from None


def check_root_allowed(canonical: Path, *, state_dir: Path) -> str | None:
    """Return a cloud-sync warning for a root, or None.

    Raises:
        RootError: If the root overlaps the private state directory.
    """
    if state_overlaps_root(state_dir, canonical):
        raise RootError("root must not contain or be inside the private state directory")
    return cloud_sync_hint(canonical)


def _reject_existing_overlap(connection: sqlite3.Connection, canonical: Path) -> None:
    """Raise when a new root duplicates or nests with a registered root."""
    rows = connection.execute("SELECT root_id, canonical_path FROM roots").fetchall()
    for row in rows:
        existing = Path(str(row["canonical_path"]))
        if existing == canonical:
            raise RootError("root path is already registered")
        if is_within(canonical, existing) or is_within(existing, canonical):
            raise RootError("root must not overlap another registered root")


def _require_targets(connection: sqlite3.Connection, targets: list[str]) -> None:
    """Raise when an allowed target id is unknown."""
    for target in targets:
        row = connection.execute("SELECT 1 FROM roots WHERE root_id = ?", (target,)).fetchone()
        if row is None:
            raise RootError("allowed target references an unknown root id")


def list_roots(db: Database) -> list[Root]:
    """Return all registered roots ordered by path."""
    with db.read() as connection:
        rows = connection.execute("SELECT * FROM roots ORDER BY canonical_path").fetchall()
        return [_row_to_root(connection, row) for row in rows]


def get_root(db: Database, root_id: str) -> Root:
    """Return a root by id.

    Raises:
        RootError: If the id is unknown.
    """
    with db.read() as connection:
        row = connection.execute("SELECT * FROM roots WHERE root_id = ?", (root_id,)).fetchone()
        if row is None:
            raise RootError("unknown root id")
        return _row_to_root(connection, row)


def find_root_by_path(db: Database, path: Path) -> Root | None:
    """Return the registered root whose canonical path exactly matches ``path``."""
    canonical = str(Path(path).expanduser().resolve())
    with db.read() as connection:
        row = connection.execute(
            "SELECT * FROM roots WHERE canonical_path = ?", (canonical,)
        ).fetchone()
        return None if row is None else _row_to_root(connection, row)


def containing_root(db: Database, path: Path) -> Root | None:
    """Return the most specific registered root containing a path, or None."""
    candidate = Path(path).expanduser()
    with db.read() as connection:
        rows = connection.execute("SELECT * FROM roots").fetchall()
        matches = [
            _row_to_root(connection, row)
            for row in rows
            if is_within(candidate, Path(str(row["canonical_path"])))
        ]
    if not matches:
        return None
    return max(matches, key=lambda root: len(root.canonical_path.parts))


def set_allowed_targets(db: Database, source_root_id: str, target_root_ids: list[str]) -> None:
    """Replace the allowed target set for a source root."""
    now = datetime.now(UTC).isoformat()
    targets = list(dict.fromkeys(target_root_ids))
    with db.transaction() as connection:
        owner = connection.execute(
            "SELECT root_id FROM roots WHERE root_id = ?", (source_root_id,)
        ).fetchone()
        if owner is None:
            raise RootError("unknown root id")
        _require_targets(connection, targets)
        connection.execute("DELETE FROM root_targets WHERE source_root_id = ?", (source_root_id,))
        for target in targets:
            connection.execute(
                "INSERT INTO root_targets (source_root_id, target_root_id) VALUES (?, ?)",
                (source_root_id, target),
            )
        connection.execute(
            "UPDATE roots SET updated_at = ? WHERE root_id = ?", (now, source_root_id)
        )


def require_allowed_target(db: Database, source_root_id: str, target_root_id: str) -> None:
    """Raise unless ``target_root_id`` is an allowed target for the source root.

    Raises:
        PathBoundaryError: When the target is not explicitly allowed.
    """
    with db.read() as connection:
        row = connection.execute(
            "SELECT 1 FROM root_targets WHERE source_root_id = ? AND target_root_id = ?",
            (source_root_id, target_root_id),
        ).fetchone()
    if row is None:
        raise PathBoundaryError("target root is not allowed for this source root")


def resolve_subpath(root: Root, relative: str) -> Path:
    """Resolve a relative subpath inside a root, rejecting escapes and symlinks.

    Raises:
        PathBoundaryError: On absolute paths, traversal, backslashes, or a
            symlinked descendant component.
    """
    stripped = relative.strip()
    if stripped in ("", "."):
        return root.canonical_path
    if stripped.startswith(("/", "\\")) or _has_drive_prefix(stripped):
        raise PathBoundaryError("subpath must be relative")
    if "\\" in stripped:
        raise PathBoundaryError("subpath must not contain backslashes")
    current = root.canonical_path
    for part in stripped.split("/"):
        if part in ("", ".", ".."):
            raise PathBoundaryError("subpath must not contain traversal")
        current = current / part
        if current.is_symlink():
            raise PathBoundaryError("subpath must not cross a symlink")
    try:
        resolved = current.resolve()
    except OSError:
        raise PathBoundaryError("subpath cannot be resolved") from None
    if not is_within(resolved, root.canonical_path):
        raise PathBoundaryError("subpath escapes its root")
    return current


def containing_root_for_target(db: Database, path: Path) -> Root:
    """Return the registered root containing a destination path.

    Raises:
        PathBoundaryError: If the path is outside every registered root.
    """
    root = containing_root(db, path)
    if root is None:
        raise PathBoundaryError("target is outside every registered root")
    return root


def require_within_root(root: Root, path: Path) -> None:
    """Raise unless a path is inside a root without crossing a symlink.

    A root whose canonical path has itself become a symlink is refused, so a
    registered directory swapped for a link cannot redirect a mutation.

    Raises:
        PathBoundaryError: If the root is a symlink, the path is not under it, or
            a descendant component is a symlink.
    """
    if root.canonical_path.is_symlink():
        raise PathBoundaryError("root path crosses a symlink boundary")
    try:
        relative = Path(path).relative_to(root.canonical_path)
    except ValueError:
        raise PathBoundaryError("path is outside its registered root") from None
    if relative.as_posix() == ".":
        return
    resolve_subpath(root, relative.as_posix())


def require_within_registered_roots(db: Database, path: Path, *, what: str) -> None:
    """Raise unless a path is inside a live registered root, judged now.

    The registry is read at call time so a mutation is judged against the current
    roots, not a snapshot taken before the writer lock.

    Raises:
        PathBoundaryError: If no registered root contains the path, or the
            containing root crosses a symlink boundary.
    """
    candidate = Path(path)
    for root in list_roots(db):
        try:
            candidate.relative_to(root.canonical_path)
        except ValueError:
            continue
        try:
            require_within_root(root, candidate)
        except PathBoundaryError:
            raise PathBoundaryError(f"{what} crosses a registered-root boundary") from None
        return
    raise PathBoundaryError(f"{what} is outside every registered root")


def _has_drive_prefix(value: str) -> bool:
    """Return whether a value starts with a Windows drive prefix."""
    return len(value) >= 2 and value[1] == ":" and value[0].isalpha()


def _row_to_root(connection: sqlite3.Connection, row: sqlite3.Row) -> Root:
    """Build a Root from a row plus its allowed targets."""
    targets = connection.execute(
        "SELECT target_root_id FROM root_targets WHERE source_root_id = ? ORDER BY target_root_id",
        (row["root_id"],),
    ).fetchall()
    return Root(
        root_id=str(row["root_id"]),
        canonical_path=Path(str(row["canonical_path"])),
        role=RootRole(str(row["role"])),
        description=str(row["description"]),
        trusted_examples=bool(row["trusted_examples"]),
        automation_enabled=bool(row["automation_enabled"]),
        allowed_target_ids=[str(item["target_root_id"]) for item in targets],
    )
