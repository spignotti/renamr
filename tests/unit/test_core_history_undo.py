"""P3 tests: run history, conflict-safe undo, and the legacy undo adapter."""

from __future__ import annotations

import json
from pathlib import Path

from renamr.core import catalog, history, mutation, operations, workflow
from renamr.core.db import Database
from renamr.core.journal import ItemState
from renamr.core.lock import WriterLock
from renamr.core.roots import RootRole
from renamr.core.workspace import Workspace


def _setup(tmp_path: Path) -> tuple[Database, WriterLock, Path, Path]:
    state = tmp_path / "state"
    db = Database(state / "catalog.sqlite3")
    db.initialize()
    source_dir = tmp_path / "inbox"
    target_dir = tmp_path / "archive"
    source_dir.mkdir()
    target_dir.mkdir()
    return db, WriterLock.for_state(state), source_dir, target_dir


def _commit_rename(db: Database, lock: WriterLock, source: Path, target: Path) -> str:
    run_id = operations.create_run(db, kind="rename")
    operations.add_plan_item(
        db,
        run_id,
        source_path=source,
        target_path=target,
        content_hash=catalog.fingerprint(source).content_hash,
        size_bytes=source.stat().st_size,
    )
    with lock:
        operations.apply_run(db, run_id, lock=lock)
    return run_id


def test_undo_restores_original_path(tmp_path: Path) -> None:
    db, lock, source_dir, target_dir = _setup(tmp_path)
    source = source_dir / "note.txt"
    source.write_text("synthetic content")
    target = target_dir / "20260314_Rechnung.txt"
    run_id = _commit_rename(db, lock, source, target)
    assert target.exists() and not source.exists()

    with lock:
        outcome = history.undo_run(db, run_id, lock=lock)

    assert outcome.reversed == 1
    assert source.exists()
    assert not target.exists()
    assert catalog.hash_file(source) == catalog.hash_file(source)


def test_undo_refuses_occupied_original(tmp_path: Path) -> None:
    db, lock, source_dir, target_dir = _setup(tmp_path)
    source = source_dir / "note.txt"
    source.write_text("synthetic content")
    target = target_dir / "renamed.txt"
    run_id = _commit_rename(db, lock, source, target)

    source.write_text("a new file occupies the original name")
    with lock:
        outcome = history.undo_run(db, run_id, lock=lock)

    assert outcome.reversed == 0
    assert outcome.refused == 1
    assert "original_occupied" in outcome.reasons
    assert target.exists()
    assert source.read_text() == "a new file occupies the original name"


def test_undo_refuses_edited_target(tmp_path: Path) -> None:
    db, lock, source_dir, target_dir = _setup(tmp_path)
    source = source_dir / "note.txt"
    source.write_text("synthetic content")
    target = target_dir / "renamed.txt"
    run_id = _commit_rename(db, lock, source, target)

    target.write_text("edited after the rename")
    with lock:
        outcome = history.undo_run(db, run_id, lock=lock)

    assert outcome.refused == 1
    assert "target_changed" in outcome.reasons
    assert not source.exists()
    assert target.read_text() == "edited after the rename"


def test_preview_undo_reports_status(tmp_path: Path) -> None:
    db, lock, source_dir, target_dir = _setup(tmp_path)
    source = source_dir / "note.txt"
    source.write_text("synthetic content")
    target = target_dir / "renamed.txt"
    run_id = _commit_rename(db, lock, source, target)

    candidates = history.preview_undo(db, run_id)
    assert [candidate.status for candidate in candidates] == ["reversible"]
    assert operations.run_items(db, run_id)[0].state is ItemState.COMMITTED


def test_legacy_undo_preview_and_apply(tmp_path: Path) -> None:
    db, _, source_dir, _ = _setup(tmp_path)
    ws = Workspace(db=db, state_dir=tmp_path / "state")
    workflow.register_root(ws, source_dir, role=RootRole.LIBRARY)
    old_path = source_dir / "20260314_Rechnung.txt"
    new_path = source_dir / "note.txt"
    new_path.write_text("legacy content")
    undo_file = tmp_path / "undo.json"
    undo_file.write_text(json.dumps([{"old_path": str(old_path), "new_path": str(new_path)}]))

    preview = history.preview_legacy_undo(undo_file, roots=[source_dir])
    assert preview.entries[0].status == "reversible"

    outcome = mutation.legacy_undo(ws, preview, confirmed_indices={0})

    assert outcome.reversed == 1
    assert old_path.exists()
    assert not new_path.exists()
    assert old_path.read_text() == "legacy content"


def test_legacy_undo_preview_rejects_bad_records(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("x")
    occupied_old = inbox / "occupied.txt"
    occupied_old.write_text("already here")
    present_new = inbox / "present.txt"
    present_new.write_text("current bytes")
    undo_file = tmp_path / "undo.json"
    undo_file.write_text(
        json.dumps(
            [
                {"old_path": 123, "new_path": str(outside)},
                {"old_path": str(occupied_old), "new_path": str(present_new)},
                {"old_path": str(inbox / "gone.txt"), "new_path": str(outside)},
            ]
        )
    )
    preview = history.preview_legacy_undo(undo_file, roots=[inbox])
    statuses = [entry.status for entry in preview.entries]
    assert statuses[0] == "shape"
    assert statuses[1] == "original_occupied"
    assert statuses[2] == "outside_roots"


def test_legacy_undo_requires_confirmation(tmp_path: Path) -> None:
    db, _, source_dir, _ = _setup(tmp_path)
    ws = Workspace(db=db, state_dir=tmp_path / "state")
    workflow.register_root(ws, source_dir, role=RootRole.LIBRARY)
    new_path = source_dir / "note.txt"
    new_path.write_text("legacy content")
    old_path = source_dir / "renamed.txt"
    undo_file = tmp_path / "undo.json"
    undo_file.write_text(json.dumps([{"old_path": str(old_path), "new_path": str(new_path)}]))
    preview = history.preview_legacy_undo(undo_file, roots=[source_dir])

    outcome = mutation.legacy_undo(ws, preview, confirmed_indices=set())

    assert outcome.reversed == 0
    assert new_path.exists()
