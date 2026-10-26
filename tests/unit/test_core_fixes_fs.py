"""Regression tests for review blockers: roots, ownership, ACL, recovery, undo."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from renamr.benchmark_schema import RulePredicate
from renamr.core import catalog, fsmeta, history, mutation, operations, publication, workflow
from renamr.core.db import Database
from renamr.core.errors import RootError
from renamr.core.extract_runner import InProcessRunner
from renamr.core.journal import ItemState
from renamr.core.lock import WriterLock
from renamr.core.roots import Root, RootRole
from renamr.core.workspace import Workspace
from tests.unit.core_support import FakeExtractor, FakeOCR, make_blank_pdf, ocr_text


class _Crash(Exception):
    """Raised by an injected fault to simulate a process dying mid-transition."""


def _workspace(tmp_path: Path) -> Workspace:
    state = tmp_path / "state"
    db = Database(state / "catalog.sqlite3")
    db.initialize()
    return Workspace(
        db=db,
        state_dir=state,
        extractor=FakeExtractor(document_type="invoice"),
        runner=InProcessRunner(
            FakeOCR([ocr_text("Rechnung von Beispiel", 0.92), ocr_text("Rechnung", 0.92)])
        ),
    )


def _two_roots(ws: Workspace, tmp_path: Path) -> tuple[Root, Root]:
    inbox = tmp_path / "inbox"
    archive = tmp_path / "archive"
    inbox.mkdir()
    archive.mkdir()
    inbox_root = workflow.register_root(ws, inbox, role=RootRole.INBOX)
    archive_root = workflow.register_root(ws, archive, role=RootRole.ARCHIVE)
    workflow.set_allowed_targets(ws, inbox_root.root_id, [archive_root.root_id])
    return inbox_root, archive_root


def test_rejected_root_leaves_no_row(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    inner = ws.state_dir / "inner"
    inner.mkdir(mode=0o700)

    with pytest.raises(RootError):
        workflow.register_root(ws, inner, role=RootRole.LIBRARY)

    assert workflow.list_roots(ws) == []


def test_nested_root_rejected_before_insert(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    library = tmp_path / "library"
    child = library / "child"
    child.mkdir(parents=True)
    workflow.register_root(ws, library, role=RootRole.LIBRARY)

    with pytest.raises(RootError):
        workflow.register_root(ws, child, role=RootRole.LIBRARY)

    assert len(workflow.list_roots(ws)) == 1


def test_committed_move_transfers_owning_root(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    inbox_root, archive_root = _two_roots(ws, tmp_path)
    document = inbox_root.canonical_path / "scan.pdf"
    make_blank_pdf(document, page_count=1)
    workflow.scan(ws, inbox_root.root_id)
    draft = workflow.create_rule_draft(
        ws,
        scope_root_id=inbox_root.root_id,
        target_root_id=archive_root.root_id,
        predicates=[RulePredicate(field="document_type", operator="equals", values=["invoice"])],
        filename_template="{subject}",
        target_subdir="invoices",
    )
    workflow.mark_tested(ws, draft.rule_id)
    workflow.activate_rule(ws, draft.rule_id)
    run_id = mutation.preview_routing(ws, inbox_root.root_id)
    assert len(mutation.run_items(ws, run_id)) == 1
    mutation.apply(ws, run_id)

    inbox_locations = catalog.locations_for_root(ws.db, inbox_root.root_id)
    archive_locations = catalog.locations_for_root(ws.db, archive_root.root_id)
    assert inbox_locations == []
    assert len(archive_locations) == 1
    assert archive_locations[0].canonical_path.parent.name == "invoices"

    assert mutation.run_items(ws, mutation.preview_routing(ws, inbox_root.root_id)) == []
    rescanned = workflow.scan(ws, archive_root.root_id)
    assert rescanned.discovered == 1
    assert rescanned.unchanged == 1


def _plan_item(db: Database, source: Path, target: Path) -> str:
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


def test_staged_foreign_identical_target_is_not_adopted(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    lock = WriterLock.for_state(ws.state_dir)
    source_dir = tmp_path / "src"
    target_dir = tmp_path / "dst"
    source_dir.mkdir()
    target_dir.mkdir()
    source = source_dir / "note.txt"
    source.write_text("identical bytes")
    target = target_dir / "renamed.txt"
    run_id = _plan_item(ws.db, source, target)
    with ws.db.transaction() as connection:
        connection.execute("UPDATE run_items SET state = 'staged' WHERE run_id = ?", (run_id,))
    target.write_text("identical bytes")

    with lock:
        recovery = operations.recover_run(ws.db, run_id, lock=lock)

    assert recovery.blocked == 1
    assert source.exists()
    assert target.exists()
    assert operations.run_items(ws.db, run_id)[0].state is ItemState.BLOCKED


def test_apply_recovers_before_new_work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = _workspace(tmp_path)
    source_dir = tmp_path / "src"
    target_dir = tmp_path / "dst"
    source_dir.mkdir()
    target_dir.mkdir()
    workflow.register_root(ws, source_dir, role=RootRole.LIBRARY)
    workflow.register_root(ws, target_dir, role=RootRole.LIBRARY)
    source = source_dir / "note.txt"
    source.write_text("synthetic content")
    target = target_dir / "renamed.txt"
    run_id = _plan_item(ws.db, source, target)
    monkeypatch.setattr(publication, "_same_volume", lambda *_: False)

    def fault(where: str) -> None:
        if where == "after_stage":
            raise _Crash(where)

    with pytest.raises(_Crash):
        mutation.apply(ws, run_id, fault=fault)

    result = mutation.apply(ws, run_id)
    assert result.committed == 1
    assert target.read_text() == "synthetic content"
    assert not source.exists()


def test_cross_volume_acl_blocks_with_intact_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not fsmeta.detection_available():
        pytest.skip("access control lists are not available on this runtime")
    ws = _workspace(tmp_path)
    source_dir = tmp_path / "src"
    target_dir = tmp_path / "dst"
    source_dir.mkdir()
    target_dir.mkdir()
    workflow.register_root(ws, source_dir, role=RootRole.LIBRARY)
    workflow.register_root(ws, target_dir, role=RootRole.LIBRARY)
    source = source_dir / "note.txt"
    source.write_text("synthetic content")
    applied = subprocess.run(
        ["/bin/chmod", "+a", "everyone deny delete", str(source)], capture_output=True, text=True
    )
    if applied.returncode != 0:
        pytest.skip("could not set an access control list")
    assert fsmeta.acl_status(source) is True
    target = target_dir / "renamed.txt"
    run_id = _plan_item(ws.db, source, target)
    monkeypatch.setattr(publication, "_same_volume", lambda *_: False)

    result = mutation.apply(ws, run_id)
    subprocess.run(["/bin/chmod", "-N", str(source)], capture_output=True)

    assert result.blocked == 1
    assert source.exists()
    assert not target.exists()
    assert not list(target_dir.glob(".renamr-stage-*"))


def test_legacy_undo_symlink_escape_is_refused(tmp_path: Path) -> None:
    import json

    ws = _workspace(tmp_path)
    root = tmp_path / "root"
    sub = root / "sub"
    sub.mkdir(parents=True)
    workflow.register_root(ws, root, role=RootRole.LIBRARY)
    new_path = sub / "note.txt"
    new_path.write_text("legacy content")
    old_path = sub / "renamed.txt"
    undo_file = tmp_path / "undo.json"
    undo_file.write_text(json.dumps([{"old_path": str(old_path), "new_path": str(new_path)}]))
    preview = history.preview_legacy_undo(undo_file, roots=[root.resolve()])
    assert preview.entries[0].status == "reversible"

    outside = tmp_path / "outside"
    outside.mkdir()
    for child in sub.iterdir():
        child.rename(root / child.name)
    sub.rmdir()
    sub.symlink_to(outside)

    with ws.lock() as lock:
        outcome = history.apply_legacy_undo(
            ws.db,
            preview,
            confirmed_indices={0},
            lock=lock,
            guard=mutation._boundary_guard(ws),
        )

    assert outcome.reversed == 0
    assert outcome.refused == 1
    assert (root / "note.txt").exists()
    assert list(outside.iterdir()) == []


def test_legacy_undo_wrapper_refuses_symlink_escape(tmp_path: Path) -> None:
    import json

    ws = _workspace(tmp_path)
    root = tmp_path / "root"
    sub = root / "sub"
    sub.mkdir(parents=True)
    workflow.register_root(ws, root, role=RootRole.LIBRARY)
    new_path = sub / "note.txt"
    new_path.write_text("legacy content")
    old_path = sub / "renamed.txt"
    undo_file = tmp_path / "undo.json"
    undo_file.write_text(json.dumps([{"old_path": str(old_path), "new_path": str(new_path)}]))
    preview = history.preview_legacy_undo(undo_file, roots=[root.resolve()])
    assert preview.entries[0].status == "reversible"

    outside = tmp_path / "outside"
    outside.mkdir()
    for child in sub.iterdir():
        child.rename(root / child.name)
    sub.rmdir()
    sub.symlink_to(outside)

    outcome = mutation.legacy_undo(ws, preview, confirmed_indices={0})

    assert outcome.reversed == 0
    assert outcome.refused == 1
    assert (root / "note.txt").exists()
    assert list(outside.iterdir()) == []


def test_source_symlinked_parent_blocks_outside_delete(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    inbox = tmp_path / "inbox"
    archive = tmp_path / "archive"
    inbox.mkdir()
    archive.mkdir()
    inbox_root = workflow.register_root(ws, inbox, role=RootRole.INBOX)
    archive_root = workflow.register_root(ws, archive, role=RootRole.ARCHIVE)
    workflow.set_allowed_targets(ws, inbox_root.root_id, [archive_root.root_id])
    nested = inbox / "nested"
    nested.mkdir()
    document = nested / "scan.pdf"
    make_blank_pdf(document, page_count=1)
    workflow.scan(ws, inbox_root.root_id)
    draft = workflow.create_rule_draft(
        ws,
        scope_root_id=inbox_root.root_id,
        target_root_id=archive_root.root_id,
        predicates=[RulePredicate(field="document_type", operator="equals", values=["invoice"])],
        filename_template="{subject}",
        target_subdir="invoices",
    )
    workflow.mark_tested(ws, draft.rule_id)
    workflow.activate_rule(ws, draft.rule_id)
    run_id = mutation.preview_routing(ws, inbox_root.root_id)
    item = mutation.run_items(ws, run_id)[0]
    assert item.target_path is not None

    outside = tmp_path / "outside"
    outside.mkdir()
    nested.rename(outside / "nested")
    nested.symlink_to(outside / "nested")
    outside_file = outside / "nested" / "scan.pdf"
    outside_bytes = outside_file.read_bytes()

    result = mutation.apply(ws, run_id)

    assert result.committed == 0
    assert mutation.run_items(ws, run_id)[0].state is ItemState.BLOCKED
    assert outside_file.exists()
    assert outside_file.read_bytes() == outside_bytes
    assert not item.target_path.exists()


def test_symlinked_registered_root_blocks_mutation(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    inbox = tmp_path / "inbox"
    archive = tmp_path / "archive"
    inbox.mkdir()
    archive.mkdir()
    inbox_root = workflow.register_root(ws, inbox, role=RootRole.INBOX)
    archive_root = workflow.register_root(ws, archive, role=RootRole.ARCHIVE)
    workflow.set_allowed_targets(ws, inbox_root.root_id, [archive_root.root_id])
    document = inbox / "scan.pdf"
    make_blank_pdf(document, page_count=1)
    workflow.scan(ws, inbox_root.root_id)
    draft = workflow.create_rule_draft(
        ws,
        scope_root_id=inbox_root.root_id,
        target_root_id=archive_root.root_id,
        predicates=[RulePredicate(field="document_type", operator="equals", values=["invoice"])],
        filename_template="{subject}",
        target_subdir="invoices",
    )
    workflow.mark_tested(ws, draft.rule_id)
    workflow.activate_rule(ws, draft.rule_id)
    run_id = mutation.preview_routing(ws, inbox_root.root_id)
    item = mutation.run_items(ws, run_id)[0]
    assert item.target_path is not None

    real = tmp_path / "inbox-real"
    inbox.rename(real)
    inbox.symlink_to(real)
    original_bytes = (real / "scan.pdf").read_bytes()

    result = mutation.apply(ws, run_id)

    assert result.committed == 0
    assert mutation.run_items(ws, run_id)[0].state is ItemState.BLOCKED
    assert (real / "scan.pdf").read_bytes() == original_bytes
    assert not item.target_path.exists()
