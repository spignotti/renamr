"""P3 tests: no-clobber publication, crash recovery, and safety barriers."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from renamr.core import catalog, operations, publication
from renamr.core.db import Database
from renamr.core.errors import LockError
from renamr.core.journal import ItemState
from renamr.core.lock import WriterLock


def _setup(tmp_path: Path) -> tuple[Database, WriterLock, Path, Path]:
    state = tmp_path / "state"
    db = Database(state / "catalog.sqlite3")
    db.initialize()
    lock = WriterLock.for_state(state)
    source = tmp_path / "inbox"
    target = tmp_path / "archive"
    source.mkdir()
    target.mkdir()
    return db, lock, source, target


def _plan(db: Database, source: Path, target: Path) -> str:
    run_id = operations.create_run(db, kind="rename")
    observed = catalog.fingerprint(source)
    operations.add_plan_item(
        db,
        run_id,
        source_path=source,
        target_path=target,
        content_hash=observed.content_hash,
        size_bytes=observed.size_bytes,
        source_dev=observed.dev,
        source_ino=observed.ino,
        source_mtime_ns=observed.mtime_ns,
    )
    return run_id


def _item(db: Database, run_id: str) -> operations.RunItem:
    return operations.run_items(db, run_id)[0]


def test_same_volume_rename_preserves_bytes(tmp_path: Path) -> None:
    db, lock, source_dir, target_dir = _setup(tmp_path)
    source = source_dir / "note.txt"
    source.write_text("synthetic content")
    original_hash = catalog.hash_file(source)
    target = target_dir / "renamed.txt"
    run_id = _plan(db, source, target)

    with lock:
        result = operations.apply_run(db, run_id, lock=lock)

    assert result.committed == 1
    assert not source.exists()
    assert target.exists()
    assert catalog.hash_file(target) == original_hash
    assert _item(db, run_id).state is ItemState.COMMITTED


def test_repeated_correct_path_is_noop(tmp_path: Path) -> None:
    db, lock, source_dir, _ = _setup(tmp_path)
    source = source_dir / "note.txt"
    source.write_text("synthetic content")
    run_id = _plan(db, source, source)

    with lock:
        result = operations.apply_run(db, run_id, lock=lock)

    assert result.noops == 1
    assert source.exists()
    assert _item(db, run_id).state is ItemState.NOOP


def test_destination_never_overwritten(tmp_path: Path) -> None:
    db, lock, source_dir, target_dir = _setup(tmp_path)
    source = source_dir / "note.txt"
    source.write_text("synthetic content")
    target = target_dir / "renamed.txt"
    target.write_text("someone else's file")
    run_id = _plan(db, source, target)

    with lock:
        result = operations.apply_run(db, run_id, lock=lock)

    assert result.blocked == 1
    assert source.read_text() == "synthetic content"
    assert target.read_text() == "someone else's file"
    assert _item(db, run_id).state is ItemState.BLOCKED


def test_destination_race_into_existence_is_blocked(tmp_path: Path) -> None:
    db, lock, source_dir, target_dir = _setup(tmp_path)
    source = source_dir / "note.txt"
    source.write_text("synthetic content")
    target = target_dir / "renamed.txt"
    run_id = _plan(db, source, target)

    # A foreign file appears after planning but before apply.
    target.write_text("raced in")
    with lock:
        result = operations.apply_run(db, run_id, lock=lock)

    assert result.blocked == 1
    assert source.exists()
    assert target.read_text() == "raced in"


def test_source_edit_invalidates_plan(tmp_path: Path) -> None:
    db, lock, source_dir, target_dir = _setup(tmp_path)
    source = source_dir / "note.txt"
    source.write_text("original")
    run_id = _plan(db, source, target_dir / "renamed.txt")

    source.write_text("edited after planning")
    with lock:
        result = operations.apply_run(db, run_id, lock=lock)

    assert result.blocked == 1
    assert source.read_text() == "edited after planning"
    assert not (target_dir / "renamed.txt").exists()


class _Crash(Exception):
    """Raised by an injected fault to simulate a process dying mid-transition."""


@pytest.mark.parametrize("point", ["after_stage", "after_publish", "after_retire"])
def test_fault_at_each_transition_is_recoverable(tmp_path: Path, point: str) -> None:
    db, lock, source_dir, target_dir = _setup(tmp_path)
    source = source_dir / "note.txt"
    source.write_text("synthetic content")
    original_hash = catalog.hash_file(source)
    target = target_dir / "renamed.txt"
    run_id = _plan(db, source, target)

    def fault(where: str) -> None:
        if where == point:
            raise _Crash(where)

    with lock:
        with pytest.raises(_Crash):
            operations.apply_run(db, run_id, lock=lock, fault=fault)

    assert source.exists() or target.exists()
    survivors = [path for path in (source, target) if path.exists()]
    assert any(catalog.hash_file(path) == original_hash for path in survivors)

    with lock:
        recovery = operations.recover_run(db, run_id, lock=lock)

    assert recovery.resumed == 1
    assert target.exists()
    assert not source.exists()
    assert catalog.hash_file(target) == original_hash
    assert _item(db, run_id).state is ItemState.COMMITTED


def test_cross_volume_copy_preserves_bytes_and_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db, lock, source_dir, target_dir = _setup(tmp_path)
    source = source_dir / "note.txt"
    source.write_text("synthetic content")
    os.chmod(source, 0o640)
    original_hash = catalog.hash_file(source)
    original_mtime = source.stat().st_mtime_ns
    target = target_dir / "renamed.txt"
    run_id = _plan(db, source, target)

    monkeypatch.setattr(publication, "_same_volume", lambda *_: False)
    with lock:
        result = operations.apply_run(db, run_id, lock=lock)

    assert result.committed == 1
    assert not source.exists()
    assert catalog.hash_file(target) == original_hash
    assert target.stat().st_mtime_ns == original_mtime
    assert target.stat().st_mode & 0o777 == 0o640
    assert not list(target_dir.glob(".renamr-stage-*"))


@pytest.mark.parametrize("point", ["after_stage", "after_publish", "after_retire"])
def test_cross_volume_fault_at_each_transition_preserves_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, point: str
) -> None:
    db, lock, source_dir, target_dir = _setup(tmp_path)
    source = source_dir / "note.txt"
    source.write_text("synthetic content")
    original_hash = catalog.hash_file(source)
    target = target_dir / "renamed.txt"
    run_id = _plan(db, source, target)
    monkeypatch.setattr(publication, "_same_volume", lambda *_: False)

    def fault(where: str) -> None:
        if where == point:
            raise _Crash(where)

    with lock:
        with pytest.raises(_Crash):
            operations.apply_run(db, run_id, lock=lock, fault=fault)
        operations.recover_run(db, run_id, lock=lock)

    survivors = [path for path in (source, target) if path.exists()]
    assert any(catalog.hash_file(path) == original_hash for path in survivors)
    assert not list(target_dir.glob(".renamr-stage-*"))


def test_cross_volume_failure_leaves_source_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db, lock, source_dir, target_dir = _setup(tmp_path)
    source = source_dir / "note.txt"
    source.write_text("synthetic content")
    original_hash = catalog.hash_file(source)
    target = target_dir / "renamed.txt"
    run_id = _plan(db, source, target)

    monkeypatch.setattr(publication, "_same_volume", lambda *_: False)

    def fault(where: str) -> None:
        if where == "after_stage":
            raise _Crash(where)

    with lock:
        with pytest.raises(_Crash):
            operations.apply_run(db, run_id, lock=lock, fault=fault)
        recovery = operations.recover_run(db, run_id, lock=lock)

    assert recovery.discarded_stages == 1
    assert source.exists()
    assert catalog.hash_file(source) == original_hash
    assert not target.exists()


def test_apply_requires_writer_lock(tmp_path: Path) -> None:
    db, _, source_dir, target_dir = _setup(tmp_path)
    source = source_dir / "note.txt"
    source.write_text("synthetic content")
    run_id = _plan(db, source, target_dir / "renamed.txt")

    with pytest.raises(LockError):
        operations.apply_run(db, run_id, lock=WriterLock(tmp_path / "free.lock"))


def test_colliding_targets_do_not_overwrite(tmp_path: Path) -> None:
    db, lock, source_dir, target_dir = _setup(tmp_path)
    first = source_dir / "one.txt"
    second = source_dir / "two.txt"
    first.write_text("first content")
    second.write_text("second content")
    target = target_dir / "same.txt"
    run_id = operations.create_run(db, kind="rename")
    for source in (first, second):
        operations.add_plan_item(
            db,
            run_id,
            source_path=source,
            target_path=target,
            content_hash=catalog.fingerprint(source).content_hash,
            size_bytes=source.stat().st_size,
        )

    with lock:
        result = operations.apply_run(db, run_id, lock=lock)

    assert result.committed == 1
    assert result.blocked == 1
    assert target.read_text() in {"first content", "second content"}
    assert not (first.exists() and second.exists())


def test_cross_volume_metadata_failure_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db, lock, source_dir, target_dir = _setup(tmp_path)
    source = source_dir / "note.txt"
    source.write_text("synthetic content")
    target = target_dir / "renamed.txt"
    run_id = _plan(db, source, target)

    monkeypatch.setattr(publication, "_same_volume", lambda *_: False)

    def unsupported(*_: object) -> None:
        from renamr.core.errors import MetadataUnsupportedError

        raise MetadataUnsupportedError("cannot preserve metadata")

    monkeypatch.setattr(publication, "_copy_metadata", unsupported)
    with lock:
        result = operations.apply_run(db, run_id, lock=lock)

    assert result.blocked == 1
    assert source.exists()
    assert not target.exists()
    assert not list(target_dir.glob(".renamr-stage-*"))


def test_source_replaced_after_publish_is_not_deleted(tmp_path: Path) -> None:
    db, lock, source_dir, target_dir = _setup(tmp_path)
    source = source_dir / "note.txt"
    source.write_text("original bytes")
    original_hash = catalog.hash_file(source)
    target = target_dir / "renamed.txt"
    run_id = _plan(db, source, target)

    def fault(where: str) -> None:
        if where == "after_publish":
            source.unlink()
            source.write_text("replacement bytes")

    with lock:
        result = operations.apply_run(db, run_id, lock=lock, fault=fault)

    assert result.blocked == 1
    assert source.read_text() == "replacement bytes"
    assert catalog.hash_file(target) == original_hash
    assert _item(db, run_id).state is ItemState.BLOCKED


def test_recovery_does_not_adopt_foreign_identical_target(tmp_path: Path) -> None:
    db, lock, source_dir, target_dir = _setup(tmp_path)
    source = source_dir / "note.txt"
    source.write_text("identical bytes")
    target = target_dir / "renamed.txt"
    run_id = _plan(db, source, target)
    # A separate, foreign file with identical bytes appears at the destination.
    target.write_text("identical bytes")

    with lock:
        recovery = operations.recover_run(db, run_id, lock=lock)

    assert recovery.blocked == 0
    assert recovery.untouched == 1
    assert source.exists()
    assert target.exists()
    assert _item(db, run_id).state is ItemState.PLANNED


def test_cross_volume_preserves_extended_attributes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from renamr.core import xattr

    if not xattr.supported():
        pytest.skip("extended attributes are unsupported on this runtime")
    db, lock, source_dir, target_dir = _setup(tmp_path)
    source = source_dir / "note.txt"
    source.write_text("synthetic content")
    xattr.write(source, "user.renamr.test", b"finder-tag")
    target = target_dir / "renamed.txt"
    run_id = _plan(db, source, target)

    monkeypatch.setattr(publication, "_same_volume", lambda *_: False)
    with lock:
        result = operations.apply_run(db, run_id, lock=lock)

    assert result.committed == 1
    assert xattr.read(target, "user.renamr.test") == b"finder-tag"
