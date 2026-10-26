"""Persisted folder descriptions, aliases, and per-folder naming overrides.

Folder scopes stay inside one registered root: the scope is the root plus a
relative subtree, never a nested registered root. Descriptions and aliases are
local user context; routing overrides are proposals until an active rule or an
explicitly applied structure plan uses them.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from renamr.core.db import Database
from renamr.core.errors import RootError
from renamr.core.roots import Root, get_root
from renamr.core.settings import validate_filename_template


class FolderOverride(BaseModel):
    """One folder-scoped description or naming/routing override."""

    model_config = ConfigDict(extra="forbid")

    root_id: str
    relative_path: str
    description: str = ""
    alias: str = ""
    filename_template: str | None = None
    target_root_id: str | None = None
    target_subdir: str | None = None


def discover_folders(root: Root, *, limit: int = 200) -> list[str]:
    """Return immediate non-hidden child directories as relative POSIX paths."""
    found: list[str] = []
    try:
        entries = sorted(os.scandir(root.canonical_path), key=lambda item: item.name)
    except OSError:
        return found
    for entry in entries:
        if len(found) >= limit:
            break
        if entry.name.startswith(".") or not entry.is_dir(follow_symlinks=False):
            continue
        found.append(entry.name)
    return found


def set_folder_override(
    db: Database,
    root_id: str,
    *,
    relative_path: str,
    description: str | None = None,
    alias: str | None = None,
    filename_template: str | None = None,
    target_root_id: str | None = None,
    target_subdir: str | None = None,
) -> FolderOverride:
    """Create or replace one folder override after validating every field."""
    get_root(db, root_id)
    relative = validate_relative_path(relative_path)
    if filename_template is not None:
        _validate_template(filename_template)
    if target_root_id is not None:
        get_root(db, target_root_id)
    subdir = validate_relative_path(target_subdir) if target_subdir else ""
    now = datetime.now(UTC).isoformat()
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO folder_overrides (root_id, relative_path, description, alias, "
            "filename_template, target_root_id, target_subdir, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (root_id, relative_path) "
            "DO UPDATE SET description = excluded.description, alias = excluded.alias, "
            "filename_template = excluded.filename_template, "
            "target_root_id = excluded.target_root_id, target_subdir = excluded.target_subdir, "
            "updated_at = excluded.updated_at",
            (
                root_id,
                relative,
                description or "",
                alias or "",
                filename_template,
                target_root_id,
                subdir or None,
                now,
                now,
            ),
        )
    return FolderOverride(
        root_id=root_id,
        relative_path=relative,
        description=description or "",
        alias=alias or "",
        filename_template=filename_template,
        target_root_id=target_root_id,
        target_subdir=subdir or None,
    )


def list_folder_overrides(db: Database, root_id: str) -> list[FolderOverride]:
    """Return overrides for one root ordered by relative path."""
    with db.read() as connection:
        rows = connection.execute(
            "SELECT * FROM folder_overrides WHERE root_id = ? ORDER BY relative_path",
            (root_id,),
        ).fetchall()
    return [
        FolderOverride(
            root_id=str(row["root_id"]),
            relative_path=str(row["relative_path"]),
            description=str(row["description"]),
            alias=str(row["alias"]),
            filename_template=None
            if row["filename_template"] is None
            else str(row["filename_template"]),
            target_root_id=None if row["target_root_id"] is None else str(row["target_root_id"]),
            target_subdir=None if row["target_subdir"] is None else str(row["target_subdir"]),
        )
        for row in rows
    ]


def get_folder_override(db: Database, root_id: str, relative_path: str) -> FolderOverride | None:
    """Return one override or None."""
    relative = validate_relative_path(relative_path)
    for item in list_folder_overrides(db, root_id):
        if item.relative_path == relative:
            return item
    return None


def validate_relative_path(value: str) -> str:
    """Validate a folder-relative path and return it normalized.

    Raises:
        RootError: On absolute paths, drive prefixes, backslashes, or traversal.
    """
    stripped = value.strip().strip("/")
    if stripped in ("", "."):
        return ""
    if "\\" in stripped or stripped.startswith("/"):
        raise RootError("folder path must be relative without a root or backslash")
    if len(stripped) >= 2 and stripped[1] == ":" and stripped[0].isalpha():
        raise RootError("folder path must not use a drive prefix")
    for part in stripped.split("/"):
        if part in ("", ".", ".."):
            raise RootError("folder path must not contain traversal")
    return stripped


def folder_for_path(root: Root, path: Path) -> str:
    """Return the folder-relative path of a file, or an empty string."""
    try:
        relative = path.relative_to(root.canonical_path)
    except ValueError:
        return ""
    parent = relative.parent.as_posix()
    return "" if parent == "." else parent


def _validate_template(template: str) -> None:
    """Reject unsupported filename templates."""
    try:
        validate_filename_template(template)
    except ValueError as exc:
        raise RootError(str(exc)) from None
