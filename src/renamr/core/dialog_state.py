"""Stale-check fingerprint for persisted dialog proposals."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable, Sequence

from renamr.core import settings
from renamr.core.db import Database

_Add = Callable[[str, object], None]


def state_fingerprint(db: Database, root_ids: Sequence[str] = ()) -> str:
    """Fingerprint settings, policy, structure, locations, revisions, and facts."""
    hasher = hashlib.sha256()

    def add(label: str, value: object) -> None:
        hasher.update(f"{label}\x1f{value}\x1e".encode())

    add("settings", settings.load_settings(db).model_dump_json())
    with db.read() as connection:
        _add_static(connection, add)
        for root_id in dict.fromkeys(root_ids):
            _add_root(connection, root_id, add)
    return hasher.hexdigest()


def _add_static(connection: sqlite3.Connection, add: _Add) -> None:
    """Add rules, roots, targets, and folder policy rows."""
    for row in connection.execute(
        "SELECT rule_id, revision, scope_root_id, scope_subtree, priority, predicates, "
        "target_root_id, target_subdir, filename_template, status FROM rules ORDER BY rule_id"
    ):
        add("rule", tuple(row))
    for row in connection.execute(
        "SELECT root_id, canonical_path, role, trusted_examples, automation_enabled "
        "FROM roots ORDER BY root_id"
    ):
        add("root", tuple(row))
    for row in connection.execute(
        "SELECT source_root_id, target_root_id FROM root_targets "
        "ORDER BY source_root_id, target_root_id"
    ):
        add("target", tuple(row))
    for row in connection.execute(
        "SELECT root_id, relative_path, description, alias, filename_template, "
        "target_root_id, target_subdir FROM folder_overrides ORDER BY root_id, relative_path"
    ):
        add("folder", tuple(row))


def _add_root(connection: sqlite3.Connection, root_id: str, add: _Add) -> None:
    """Add one root's locations, revisions, facts, and structure plans."""
    for row in connection.execute(
        "SELECT location_id, revision_id, canonical_path, dev, ino, size_bytes, "
        "mtime_ns, presence FROM locations WHERE root_id = ? ORDER BY location_id",
        (root_id,),
    ):
        add("location", tuple(row))
    for row in connection.execute(
        "SELECT r.revision_id, r.content_hash, r.quality_state, r.feature_key, "
        "r.failed_stage FROM revisions r JOIN locations l "
        "ON l.revision_id = r.revision_id WHERE l.root_id = ? ORDER BY r.revision_id",
        (root_id,),
    ):
        add("revision", tuple(row))
    for row in connection.execute(
        "SELECT f.revision_id, f.field, f.value, f.validation_state FROM facts f "
        "JOIN locations l ON l.revision_id = f.revision_id WHERE l.root_id = ? "
        "ORDER BY f.revision_id, f.field",
        (root_id,),
    ):
        add("fact", tuple(row))
    for row in connection.execute(
        "SELECT plan_id, revision, state, rule_fingerprint FROM structure_plans "
        "WHERE root_id = ? ORDER BY plan_id",
        (root_id,),
    ):
        add("structure", tuple(row))
