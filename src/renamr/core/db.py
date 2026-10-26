"""SQLite connection, schema migration, backup, integrity, and safe restore.

Connections enable foreign keys and a bounded busy timeout; the database uses
WAL. Write transactions are short and explicit and must never wrap OCR or
inference. Corruption and newer-than-supported schemas fail closed. The
:class:`Database` handle is intentionally small and delegates maintenance to
focused module functions.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import tempfile
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from renamr.core.errors import (
    DatabaseCorruptError,
    DatabaseNewerVersionError,
    DatabaseUnavailableError,
    LockError,
    MigrationError,
)
from renamr.core.lock import WriterLock
from renamr.core.paths import (
    BACKUPS_DIRNAME,
    ensure_private_dir,
    ensure_private_file,
    harden_sidecars,
)
from renamr.core.schema import DATABASE_APPLICATION_ID, MIGRATIONS, SCHEMA_VERSION

BUSY_TIMEOUT_MS = 10_000
DEFAULT_BACKUP_RETENTION = 3


class Database:
    """A small handle to a catalog database at a fixed path."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def connect(self, *, read_only: bool = False, timeout: float = 30.0) -> sqlite3.Connection:
        """Open a configured connection (see :func:`open_connection`)."""
        return open_connection(self.path, read_only=read_only, timeout=timeout)

    @contextlib.contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        """Yield a read-only connection that is always closed."""
        connection = open_connection(self.path, read_only=True)
        try:
            yield connection
        finally:
            connection.close()

    @contextlib.contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        """Run one short write transaction, rolling back on any exception."""
        connection = open_connection(self.path)
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            connection.commit()
        finally:
            connection.close()

    def schema_version(self) -> int:
        """Return the database ``user_version`` (0 for an empty file)."""
        with contextlib.closing(self.connect()) as connection:
            return int(connection.execute("PRAGMA user_version").fetchone()[0])

    def integrity_check(self, *, read_only: bool = False) -> bool:
        """Return whether the database passes integrity verification."""
        try:
            connection = open_connection(self.path, read_only=read_only)
        except DatabaseUnavailableError:
            return False
        with contextlib.closing(connection) as handle:
            try:
                check_integrity(handle)
            except DatabaseCorruptError:
                return False
        return True

    def initialize(self, *, retention: int = DEFAULT_BACKUP_RETENTION) -> None:
        """Verify and migrate the schema, failing closed on trouble."""
        initialize_database(self, retention=retention)

    def backup_to(
        self,
        destination: Path,
        *,
        retention: int = DEFAULT_BACKUP_RETENTION,
        overwrite: bool = False,
    ) -> Path:
        """Copy the database with SQLite's online backup API."""
        return backup_database(self, destination, retention=retention, overwrite=overwrite)

    def restore_from(self, backup_path: Path, *, lock: WriterLock) -> None:
        """Restore a verified backup (see :func:`restore_database`)."""
        restore_database(self, backup_path, lock=lock)


def open_connection(
    path: Path, *, read_only: bool = False, timeout: float = 30.0
) -> sqlite3.Connection:
    """Open a configured connection, refusing a symlinked database file.

    Raises:
        DatabaseUnavailableError: If the path is a symlink or cannot be opened.
    """
    target = Path(path)
    if target.is_symlink():
        raise DatabaseUnavailableError("database path must not be a symlink")
    try:
        if read_only:
            connection = sqlite3.connect(read_only_uri(target), uri=True, timeout=timeout)
        else:
            ensure_private_dir(target.parent)
            ensure_private_file(target)
            connection = sqlite3.connect(target, timeout=timeout)
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        if not read_only:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            harden_sidecars(target)
        connection.execute("PRAGMA foreign_keys = ON")
    except sqlite3.Error:
        raise DatabaseUnavailableError("database cannot be opened") from None
    return connection


def read_only_uri(path: Path) -> str:
    """Return a properly escaped read-only SQLite URI for a path."""
    return f"{Path(path).resolve().as_uri()}?mode=ro"


def initialize_database(db: Database, *, retention: int = DEFAULT_BACKUP_RETENTION) -> None:
    """Verify and migrate the schema, failing closed on corruption or drift."""
    _verify_existing_database(db.path)
    try:
        with contextlib.closing(db.connect()) as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise DatabaseNewerVersionError("database schema is newer than this build supports")
            check_integrity(connection)
            application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
            if has_user_tables(connection) and application_id != DATABASE_APPLICATION_ID:
                raise DatabaseCorruptError("database is not a recognized renamr catalog")
            if version == 0 and not has_user_tables(connection):
                apply_migrations(connection, from_version=0)
                return
            if version < SCHEMA_VERSION:
                if version > 0:
                    db.backup_to(timestamped_backup(db.path, version), retention=retention)
                apply_migrations(connection, from_version=version)
    except sqlite3.DatabaseError as exc:
        raise DatabaseCorruptError("database cannot be read") from exc


def _verify_existing_database(path: Path) -> None:
    """Check existing file identity read-only before any writable connection."""
    if path.is_symlink():
        raise DatabaseUnavailableError("database path must not be a symlink")
    if not path.exists() or path.stat().st_size == 0:
        return
    try:
        with contextlib.closing(sqlite3.connect(read_only_uri(path), uri=True)) as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise DatabaseNewerVersionError("database schema is newer than this build supports")
            identity = int(connection.execute("PRAGMA application_id").fetchone()[0])
            if has_user_tables(connection) and identity != DATABASE_APPLICATION_ID:
                raise DatabaseCorruptError("database is not a recognized renamr catalog")
    except sqlite3.Error:
        raise DatabaseCorruptError("database cannot be read") from None


def apply_migrations(connection: sqlite3.Connection, *, from_version: int) -> None:
    """Apply every pending migration then record the new version."""
    for migration in MIGRATIONS:
        if migration.version <= from_version:
            continue
        try:
            connection.execute("BEGIN IMMEDIATE")
            for statement in migration.statements:
                connection.execute(statement)
            connection.execute(f"PRAGMA user_version = {migration.version}")
            connection.commit()
        except sqlite3.Error as exc:
            connection.rollback()
            raise MigrationError(f"schema migration {migration.version} failed") from exc


def check_integrity(connection: sqlite3.Connection) -> None:
    """Raise when ``PRAGMA integrity_check`` does not report ``ok``."""
    if not has_user_tables(connection):
        return
    try:
        rows = connection.execute("PRAGMA integrity_check").fetchall()
    except sqlite3.DatabaseError:
        raise DatabaseCorruptError("database integrity check failed") from None
    if len(rows) != 1 or str(rows[0][0]).lower() != "ok":
        raise DatabaseCorruptError("database integrity check failed")


def has_user_tables(connection: sqlite3.Connection) -> bool:
    """Return whether the database has any application tables."""
    row = connection.execute(
        "SELECT count(*) FROM sqlite_master WHERE type IN ('table', 'view') "
        "AND name NOT LIKE 'sqlite_%'"
    ).fetchone()
    return bool(row[0])


def copy_database(origin: sqlite3.Connection, sink: sqlite3.Connection) -> None:
    """Copy a database with SQLite's online backup API."""
    origin.backup(sink)


def backup_database(
    db: Database, destination: Path, *, retention: int, overwrite: bool = False
) -> Path:
    """Copy the database with the backup API, never clobbering by default.

    The copy is built in a unique exclusive temp file and linked into place, so
    a default backup refuses an existing destination instead of destroying
    another SQLite file. An explicit, user-confirmed replacement passes
    ``overwrite``. Pruning runs only inside the state ``backups`` directory and
    only removes backups this app recognizes.

    Raises:
        DatabaseUnavailableError: If the source cannot be opened or copied, or
            the destination already exists without ``overwrite``.
    """
    target = Path(destination)
    directory = ensure_private_dir(target.parent)
    if target.is_symlink():
        raise DatabaseUnavailableError("backup destination must not be a symlink")
    if target.exists() and not overwrite:
        raise DatabaseUnavailableError("backup destination already exists")
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=directory)
    os.close(descriptor)
    temp_path = Path(temp_name)
    try:
        with contextlib.closing(sqlite3.connect(temp_path)) as sink:
            with contextlib.closing(db.connect()) as source:
                copy_database(source, sink)
        _sync_file(temp_path)
        _place_backup(temp_path, target, overwrite=overwrite)
    except sqlite3.Error:
        temp_path.unlink(missing_ok=True)
        raise DatabaseUnavailableError("database backup failed") from None
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise
    if target.parent.resolve() == backups_dir(db.path.parent).resolve():
        prune_backups(target.parent, retention)
    return target


def _place_backup(temp_path: Path, target: Path, *, overwrite: bool) -> None:
    """Install a finished backup, linking no-clobber unless overwriting."""
    if overwrite:
        os.replace(temp_path, target)
        return
    try:
        os.link(temp_path, target)
    except FileExistsError:
        raise DatabaseUnavailableError("backup destination already exists") from None
    temp_path.unlink(missing_ok=True)


def _sync_file(path: Path) -> None:
    """Flush a finished file to stable storage."""
    with open(path, "rb") as handle:
        os.fsync(handle.fileno())


def restore_database(db: Database, backup_path: Path, *, lock: WriterLock) -> None:
    """Restore a verified backup through SQLite under a held writer lock.

    The verified backup is copied into the existing destination with SQLite's
    online backup API and the write-ahead log is checkpointed and truncated, so
    the main file, its log, and the catalog stay mutually consistent and no file
    is swapped under live readers. The writer lock is required, restoring from
    the live path itself is rejected before any write, and a log that cannot be
    quiesced is an error rather than a silent stale read.

    Raises:
        LockError: If the writer lock is not held.
        DatabaseCorruptError: If the backup equals the live path or fails a
            read-only integrity check.
        DatabaseUnavailableError: If the copy or the log checkpoint fails.
    """
    if not lock.held:
        raise LockError("restore_database requires a held writer lock")
    source = Path(backup_path)
    if source.resolve() == db.path.resolve():
        raise DatabaseCorruptError("cannot restore a database from itself")
    if not _is_recognized_backup(source):
        raise DatabaseCorruptError("backup is not a recognized renamr catalog")
    if not Database(source).integrity_check(read_only=True):
        raise DatabaseCorruptError("backup failed integrity verification")
    try:
        with contextlib.closing(Database(source).connect(read_only=True)) as origin:
            with contextlib.closing(db.connect()) as destination:
                copy_database(origin, destination)
                checkpoint_truncate(destination)
    except sqlite3.Error:
        raise DatabaseUnavailableError("database restore failed") from None


def checkpoint_truncate(connection: sqlite3.Connection) -> None:
    """Checkpoint and truncate the write-ahead log, failing when it is busy.

    Raises:
        DatabaseUnavailableError: If the log cannot be checkpointed or a reader
            still pins frames after the busy timeout.
    """
    try:
        row = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    except sqlite3.Error:
        raise DatabaseUnavailableError("database restore could not checkpoint") from None
    if row is not None and int(row[0]) != 0:
        raise DatabaseUnavailableError("database is busy; restore could not quiesce the log")


def backups_dir(state_dir: Path) -> Path:
    """Return the private backups directory for a state directory."""
    return Path(state_dir) / BACKUPS_DIRNAME


def timestamped_backup(db_path: Path, version: int) -> Path:
    """Return a timestamped backup path for a pre-migration snapshot."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    return backups_dir(db_path.parent) / f"{db_path.stem}-v{version}-{stamp}.sqlite3"


def prune_backups(directory: Path, retention: int) -> None:
    """Keep only the most recent ``retention`` recognized backups in a directory.

    Only files that are recognizable renamr catalog backups are considered, so an
    unrelated SQLite file that happens to sit in the managed directory survives.
    """
    if retention < 1:
        return
    backups = [path for path in directory.glob("*.sqlite3") if _is_recognized_backup(path)]
    backups.sort(key=lambda path: path.stat().st_mtime)
    for stale in backups[:-retention]:
        with contextlib.suppress(OSError):
            stale.unlink()


def _is_recognized_backup(path: Path) -> bool:
    """Return whether a file is a readable renamr catalog backup."""
    if path.is_symlink() or not path.is_file():
        return False
    try:
        with contextlib.closing(sqlite3.connect(read_only_uri(path), uri=True)) as connection:
            row = connection.execute(
                "SELECT count(*) FROM sqlite_master WHERE type = 'table' AND name = 'roots'"
            ).fetchone()
            application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
    except sqlite3.Error:
        return False
    return application_id == DATABASE_APPLICATION_ID and bool(row and row[0])
