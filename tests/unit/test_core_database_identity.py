"""Protect unrelated SQLite files from catalog initialization and retention."""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from renamr.core.db import Database
from renamr.core.errors import DatabaseCorruptError
from renamr.core.schema import DATABASE_APPLICATION_ID


def _foreign_database(path: Path) -> None:
    """Create an unrelated private database with a coincidentally named table."""
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE roots (value TEXT)")
        connection.execute("INSERT INTO roots VALUES ('synthetic user record')")
        connection.commit()
    path.chmod(0o600)


def test_new_catalog_has_explicit_application_identity(tmp_path: Path) -> None:
    database = Database(tmp_path / "state" / "catalog.sqlite3")
    database.initialize()

    with database.read() as connection:
        identity = connection.execute("PRAGMA application_id").fetchone()[0]

    assert identity == DATABASE_APPLICATION_ID


def test_retention_preserves_foreign_database_with_roots_table(tmp_path: Path) -> None:
    database = Database(tmp_path / "state" / "catalog.sqlite3")
    database.initialize()
    backups = database.path.parent / "backups"
    backups.mkdir(mode=0o700)
    foreign = backups / "unrelated.sqlite3"
    _foreign_database(foreign)
    original = hashlib.sha256(foreign.read_bytes()).hexdigest()

    database.backup_to(backups / "first.sqlite3", retention=1)
    database.backup_to(backups / "second.sqlite3", retention=1)

    assert foreign.exists()
    assert hashlib.sha256(foreign.read_bytes()).hexdigest() == original
    assert len(list(backups.glob("*.sqlite3"))) == 2


def test_initialization_rejects_foreign_database_without_modifying_it(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    foreign = state / "catalog.sqlite3"
    _foreign_database(foreign)
    original = foreign.read_bytes()

    with pytest.raises(DatabaseCorruptError, match="recognized renamr catalog"):
        Database(foreign).initialize()

    assert foreign.read_bytes() == original
