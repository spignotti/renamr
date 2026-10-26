"""P1 storage tests: private state, bootstrap precedence, locks, and database."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from renamr.core import paths
from renamr.core.db import Database, backups_dir, checkpoint_truncate
from renamr.core.errors import (
    ConfigError,
    DatabaseCorruptError,
    DatabaseNewerVersionError,
    DatabaseUnavailableError,
    LockError,
    StateError,
)
from renamr.core.lock import WriterLock
from renamr.core.schema import SCHEMA_VERSION


def test_private_state_layout_and_permissions(tmp_path: Path) -> None:
    state = paths.ensure_private_dir(tmp_path / "state")
    target = paths.write_private_file(state / "note.txt", b"private")

    assert state.stat().st_mode & 0o777 == 0o700
    assert target.stat().st_mode & 0o777 == 0o600
    assert target.read_bytes() == b"private"


def test_config_precedence_explicit_then_xdg_then_home(tmp_path: Path) -> None:
    explicit = tmp_path / "explicit.toml"
    env = {"XDG_CONFIG_HOME": str(tmp_path / "xdg")}

    assert paths.config_candidates(explicit, env)[0] == explicit
    xdg_candidate = paths.config_candidates(None, env)[0]
    assert xdg_candidate == tmp_path / "xdg" / "renamr" / "config.toml"
    home_candidate = paths.config_candidates(None, {})[0]
    assert home_candidate.name == "config.toml"


def test_default_state_dir_per_platform(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(paths.sys, "platform", "darwin")
    assert "Application Support" in str(paths.default_state_dir({}))
    monkeypatch.setattr(paths.sys, "platform", "linux")
    assert paths.default_state_dir({"XDG_STATE_HOME": str(tmp_path)}) == tmp_path / "renamr"


def test_state_overlap_and_cloud_hint(tmp_path: Path) -> None:
    root = tmp_path / "library"
    root.mkdir()
    assert paths.state_overlaps_root(root / "nested", root) is True
    assert paths.state_overlaps_root(tmp_path / "other", root) is False
    with pytest.raises(StateError):
        paths.require_outside_roots(root / "nested", [root])
    assert paths.cloud_sync_hint(Path("/Users/x/Library/Mobile Documents/a")) == "iCloud Drive"


def test_bootstrap_roundtrip_and_rejects_bad_version(tmp_path: Path) -> None:
    bootstrap = tmp_path / "config.toml"
    paths.write_bootstrap(bootstrap, tmp_path / "state")
    loaded = paths.load_bootstrap(bootstrap)
    assert loaded.state_dir == tmp_path / "state"

    bootstrap.write_text("format_version = 99\nstate_dir = '/x'\n")
    with pytest.raises(ConfigError):
        paths.load_bootstrap(bootstrap)


def test_writer_lock_excludes_second_instance(tmp_path: Path) -> None:
    lock_path = tmp_path / "writer.lock"
    first = WriterLock(lock_path)
    second = WriterLock(lock_path)
    assert first.acquire() is True
    assert second.acquire(blocking=False) is False
    first.release()
    assert second.acquire(blocking=False) is True
    second.release()


def test_writer_lock_alive_inode_is_never_removed(tmp_path: Path) -> None:
    lock_path = tmp_path / "writer.lock"
    lock = WriterLock(lock_path)
    lock.acquire()
    inode = lock_path.stat().st_ino
    lock.release()
    assert lock_path.exists()
    assert lock_path.stat().st_ino == inode


def test_writer_lock_released_when_holder_process_dies(tmp_path: Path) -> None:
    lock_path = tmp_path / "writer.lock"
    script = (
        "import sys, time\n"
        "from pathlib import Path\n"
        "from renamr.core.lock import WriterLock\n"
        "lock = WriterLock(Path(sys.argv[1]))\n"
        "lock.acquire()\n"
        "print('locked', flush=True)\n"
        "time.sleep(30)\n"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", script, str(lock_path)],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "locked"
        contender = WriterLock(lock_path)
        assert contender.acquire(blocking=False) is False
    finally:
        child.kill()
        child.wait()
    assert WriterLock(lock_path).acquire(blocking=True, timeout=5) is True


def test_database_initialize_sets_schema_version(tmp_path: Path) -> None:
    db = Database(tmp_path / "catalog.sqlite3")
    db.initialize()

    assert db.schema_version() == SCHEMA_VERSION
    assert db.integrity_check() is True
    with db.connect(read_only=True) as connection:
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            )
        }
    assert {"roots", "revisions", "locations", "pages", "facts", "rules", "runs"} <= names


def test_database_fails_closed_on_newer_schema(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite3"
    Database(path).initialize()
    with sqlite3.connect(path) as connection:
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 5}")
    with pytest.raises(DatabaseNewerVersionError):
        Database(path).initialize()


def test_database_fails_closed_on_corruption(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite3"
    path.write_bytes(b"not a sqlite database at all" * 10)
    path.chmod(0o600)
    with pytest.raises((DatabaseCorruptError, DatabaseUnavailableError)):
        Database(path).initialize()


def test_read_only_connection_cannot_write(tmp_path: Path) -> None:
    db = Database(tmp_path / "catalog.sqlite3")
    db.initialize()
    with db.connect(read_only=True) as connection:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("INSERT INTO settings (key, value, updated_at) VALUES ('x','y','z')")


def test_backup_is_consistent_and_restorable(tmp_path: Path) -> None:
    db = Database(tmp_path / "catalog.sqlite3")
    db.initialize()
    backup = db.backup_to(backups_dir(tmp_path) / "catalog-backup.sqlite3")

    assert backup.exists()
    assert backup.stat().st_mode & 0o777 == 0o600
    assert Database(backup).integrity_check() is True
    assert Database(backup).schema_version() == SCHEMA_VERSION

    restored = Database(tmp_path / "restored.sqlite3")
    lock = WriterLock.for_state(tmp_path)
    with lock:
        restored.restore_from(backup, lock=lock)
    assert restored.integrity_check() is True


def test_restore_replaces_data_that_lived_in_wal(tmp_path: Path) -> None:
    from renamr.core.settings import get_setting, set_setting

    db = Database(tmp_path / "catalog.sqlite3")
    db.initialize()
    set_setting(db, "marker", "before")
    backup = db.backup_to(backups_dir(tmp_path) / "catalog-backup.sqlite3")

    set_setting(db, "marker", "after-wal-write")  # now only in the WAL
    lock = WriterLock.for_state(tmp_path)
    with lock:
        db.restore_from(backup, lock=lock)

    reopened = Database(tmp_path / "catalog.sqlite3")
    assert reopened.integrity_check() is True
    assert get_setting(reopened, "marker") == "before"
    assert not Path(f"{db.path}-wal").exists() or Path(f"{db.path}-wal").stat().st_size == 0


def test_read_only_uri_handles_special_characters(tmp_path: Path) -> None:
    state = tmp_path / "we ird#dir"
    db = Database(state / "catalog.sqlite3")
    db.initialize()
    with db.read() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_ensure_private_dir_does_not_rewrite_existing(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o755)
    before = shared.stat().st_mode & 0o777

    with pytest.raises(StateError):
        paths.ensure_private_dir(shared)

    assert shared.stat().st_mode & 0o777 == before == 0o755


def test_ensure_private_dir_rejects_symlink(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)

    with pytest.raises(StateError):
        paths.ensure_private_dir(link)


def test_ensure_private_file_does_not_rewrite_existing(tmp_path: Path) -> None:
    existing = tmp_path / "file.txt"
    existing.write_text("data")
    existing.chmod(0o644)

    with pytest.raises(StateError):
        paths.ensure_private_file(existing)

    assert existing.stat().st_mode & 0o777 == 0o644


def test_write_private_file_refuses_symlinked_destination(tmp_path: Path) -> None:
    directory = paths.ensure_private_dir(tmp_path / "state")
    victim = tmp_path / "victim.txt"
    victim.write_text("original")
    destination = directory / "export.txt"
    destination.symlink_to(victim)

    with pytest.raises(StateError):
        paths.write_private_file(destination, b"private")

    assert victim.read_text() == "original"
    assert destination.is_symlink()


def test_database_rejects_symlinked_path(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    real = state / "real.sqlite3"
    real.write_bytes(b"")
    real.chmod(0o600)
    link = state / "link.sqlite3"
    link.symlink_to(real)

    with pytest.raises(DatabaseUnavailableError):
        Database(link).initialize()


def test_database_rejects_non_private_parent(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o755)

    with pytest.raises(StateError):
        Database(shared / "catalog.sqlite3").initialize()
    assert shared.stat().st_mode & 0o777 == 0o755


def test_write_bootstrap_escapes_path(tmp_path: Path) -> None:
    bootstrap = tmp_path / "config.toml"
    odd = tmp_path / 'we"ird\\dir with spaces'
    odd.mkdir()

    paths.write_bootstrap(bootstrap, odd)
    loaded = paths.load_bootstrap(bootstrap)

    assert loaded.state_dir == odd


def test_restore_rejects_self_source(tmp_path: Path) -> None:
    db = Database(tmp_path / "catalog.sqlite3")
    db.initialize()

    lock = WriterLock.for_state(tmp_path)
    with lock:
        with pytest.raises(DatabaseCorruptError, match="itself"):
            db.restore_from(db.path, lock=lock)


def test_restore_requires_held_writer_lock(tmp_path: Path) -> None:
    from renamr.core.settings import get_setting, set_setting

    db = Database(tmp_path / "catalog.sqlite3")
    db.initialize()
    set_setting(db, "marker", "live")
    backup = db.backup_to(tmp_path / "backups" / "snap.sqlite3")

    with pytest.raises(LockError):
        db.restore_from(backup, lock=WriterLock.for_state(tmp_path))

    assert get_setting(Database(tmp_path / "catalog.sqlite3"), "marker") == "live"


def test_restore_preserves_original_on_injected_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from renamr.core.settings import get_setting, set_setting

    db = Database(tmp_path / "catalog.sqlite3")
    db.initialize()
    set_setting(db, "marker", "live")
    backup = db.backup_to(tmp_path / "backups" / "snap.sqlite3")

    def fail_backup(origin: sqlite3.Connection, sink: sqlite3.Connection) -> None:
        raise sqlite3.OperationalError("synthetic failure")

    from renamr.core import db as db_module

    monkeypatch.setattr(db_module, "copy_database", fail_backup)
    lock = WriterLock.for_state(tmp_path)
    with lock:
        with pytest.raises(DatabaseUnavailableError):
            db.restore_from(backup, lock=lock)

    reopened = Database(tmp_path / "catalog.sqlite3")
    assert reopened.integrity_check() is True
    assert get_setting(reopened, "marker") == "live"


def test_restore_keeps_live_inode_and_no_sidecar_swap(tmp_path: Path) -> None:
    from renamr.core.settings import get_setting, set_setting

    db = Database(tmp_path / "catalog.sqlite3")
    db.initialize()
    set_setting(db, "marker", "before")
    backup = db.backup_to(backups_dir(tmp_path) / "snap.sqlite3")
    set_setting(db, "marker", "after")

    main_ino = db.path.stat().st_ino
    lock = WriterLock.for_state(tmp_path)
    with lock:
        db.restore_from(backup, lock=lock)

    assert db.path.stat().st_ino == main_ino
    assert get_setting(Database(tmp_path / "catalog.sqlite3"), "marker") == "before"


def test_backup_prunes_only_managed_directory(tmp_path: Path) -> None:
    db = Database(tmp_path / "state" / "catalog.sqlite3")
    db.initialize()
    other_dir = tmp_path / "other"
    other_dir.mkdir(mode=0o700)
    unrelated = other_dir / "unrelated.sqlite3"
    unrelated.write_text("keep me")

    db.backup_to(other_dir / "snapshot.sqlite3")

    assert unrelated.exists()
    managed = tmp_path / "state" / "backups"
    for index in range(5):
        db.backup_to(managed / f"managed-{index}.sqlite3")
    assert len(sorted(managed.glob("*.sqlite3"))) <= 4


def test_backup_refuses_existing_destination(tmp_path: Path) -> None:
    db = Database(tmp_path / "catalog.sqlite3")
    db.initialize()
    precious = tmp_path / "precious.sqlite3"
    with sqlite3.connect(precious) as connection:
        connection.execute("CREATE TABLE u (value TEXT)")
        connection.execute("INSERT INTO u VALUES ('precious')")

    with pytest.raises(DatabaseUnavailableError, match="already exists"):
        db.backup_to(precious)

    with sqlite3.connect(precious) as connection:
        names = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert names == {"u"}


def test_backup_overwrite_is_explicit(tmp_path: Path) -> None:
    db = Database(tmp_path / "catalog.sqlite3")
    db.initialize()
    destination = tmp_path / "backups" / "snap.sqlite3"
    db.backup_to(destination)

    with pytest.raises(DatabaseUnavailableError):
        db.backup_to(destination)
    replaced = db.backup_to(destination, overwrite=True)

    assert replaced.exists()
    assert Database(replaced).integrity_check() is True
    assert not list((tmp_path / "backups").glob(".snap.sqlite3.*"))


def test_prune_spares_unrelated_database_in_managed_dir(tmp_path: Path) -> None:
    db = Database(tmp_path / "state" / "catalog.sqlite3")
    db.initialize()
    managed = tmp_path / "state" / "backups"
    managed.mkdir(mode=0o700)
    unrelated = managed / "unrelated.sqlite3"
    with sqlite3.connect(unrelated) as connection:
        connection.execute("CREATE TABLE u (value TEXT)")
    stranger = managed / "notes.sqlite3"
    stranger.write_text("not a database")

    for index in range(5):
        db.backup_to(managed / f"managed-{index}.sqlite3")

    assert unrelated.exists()
    with sqlite3.connect(unrelated) as connection:
        assert connection.execute("SELECT name FROM sqlite_master").fetchall()
    assert stranger.read_text() == "not a database"


def test_restore_via_mutation_wrapper_is_typed(tmp_path: Path) -> None:
    from renamr.core import mutation
    from renamr.core.settings import get_setting, set_setting
    from renamr.core.workspace import Workspace

    state = tmp_path / "state"
    db = Database(state / "catalog.sqlite3")
    db.initialize()
    set_setting(db, "marker", "before")
    backup = db.backup_to(backups_dir(state) / "snap.sqlite3")
    set_setting(db, "marker", "after")
    workspace = Workspace(db=db, state_dir=state)

    result = mutation.restore(workspace, backup)

    assert result is None
    assert get_setting(Database(state / "catalog.sqlite3"), "marker") == "before"


def test_checkpoint_truncate_reports_busy(tmp_path: Path) -> None:
    db = Database(tmp_path / "catalog.sqlite3")
    db.initialize()

    writer = sqlite3.connect(db.path, isolation_level=None)
    writer.execute("PRAGMA journal_mode = WAL")
    writer.execute("PRAGMA wal_autocheckpoint = 0")
    reader = sqlite3.connect(db.path, isolation_level=None)
    reader.execute("BEGIN")
    reader.execute("SELECT value FROM settings").fetchall()
    writer.execute("INSERT INTO settings (key, value, updated_at) VALUES ('k', 'v', 'now')")

    target = sqlite3.connect(db.path, isolation_level=None)
    target.execute("PRAGMA busy_timeout = 50")
    try:
        with pytest.raises(DatabaseUnavailableError, match="busy"):
            checkpoint_truncate(target)
    finally:
        reader.execute("ROLLBACK")
        reader.close()
        writer.close()
        target.close()


def test_export_and_bootstrap_allow_non_private_parent(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o755)

    exported = paths.write_private_file(shared / "out.txt", b"x", require_private_parent=False)
    assert exported.read_bytes() == b"x"
    assert exported.stat().st_mode & 0o777 == 0o600
    assert shared.stat().st_mode & 0o777 == 0o755

    bootstrap = paths.write_bootstrap(shared / "config.toml", tmp_path / "state")
    assert bootstrap.stat().st_mode & 0o777 == 0o600
    assert paths.load_bootstrap(bootstrap).state_dir == tmp_path / "state"
