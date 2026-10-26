"""End-to-end tests for the workspace context and workflow modules."""

from __future__ import annotations

from pathlib import Path

from renamr.benchmark_schema import RulePredicate
from renamr.core import mutation, workflow
from renamr.core.db import Database
from renamr.core.extract_runner import InProcessRunner
from renamr.core.journal import ItemState
from renamr.core.roots import RootRole
from renamr.core.workspace import Workspace
from tests.unit.core_support import FakeExtractor, FakeOCR, make_blank_pdf, ocr_text


def _workspace(tmp_path: Path) -> Workspace:
    return Workspace.bootstrap(tmp_path / "state", config_path=tmp_path / "config.toml")


def test_bootstrap_creates_private_state_and_bootstrap(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)

    assert workspace.state_dir.stat().st_mode & 0o777 == 0o700
    assert (tmp_path / "config.toml").exists()
    reopened = Workspace.open(config_path=tmp_path / "config.toml")
    assert reopened.state_dir == workspace.state_dir


def _activate_invoice_rule(workspace: Workspace, source_id: str, target_id: str) -> None:
    """Activate the known invoice rule used by the complete workflow test."""
    draft = workflow.create_rule_draft(
        workspace,
        scope_root_id=source_id,
        target_root_id=target_id,
        predicates=[RulePredicate(field="document_type", operator="equals", values=["invoice"])],
        filename_template="{date}_{subject}",
        target_subdir="invoices",
    )
    workflow.mark_tested(workspace, draft.rule_id)
    workflow.activate_rule(workspace, draft.rule_id)


def _assert_search_export(workspace: Workspace, tmp_path: Path) -> None:
    """Verify that a catalog hit exports its stored text."""
    hits = workflow.search(workspace, "Rechnung")
    assert hits
    destination = tmp_path / "export.txt"
    workflow.export_text(workspace, hits[0].revision_id, destination)
    assert "Rechnung" in destination.read_text()


def test_scan_route_apply_search_undo(tmp_path: Path) -> None:
    state = tmp_path / "state"
    ocr = FakeOCR(
        [ocr_text("Rechnung von Beispiel", 0.92), ocr_text("Rechnung von Beispiel", 0.92)]
    )
    extractor = FakeExtractor(document_type="invoice")
    workspace = Workspace(
        db=_bootstrap_db(state),
        state_dir=state,
        extractor=extractor,
        runner=InProcessRunner(ocr),
    )
    inbox = tmp_path / "inbox"
    archive = tmp_path / "archive"
    inbox.mkdir()
    archive.mkdir()
    inbox_root = workflow.register_root(workspace, inbox, role=RootRole.INBOX)
    archive_root = workflow.register_root(workspace, archive, role=RootRole.ARCHIVE)
    workflow.set_allowed_targets(workspace, inbox_root.root_id, [archive_root.root_id])

    document = inbox / "scan.pdf"
    make_blank_pdf(document, page_count=1)
    original_hash = _hash(document)

    scan = workflow.scan(workspace, inbox_root.root_id)
    assert scan.new_revisions == 1
    assert extractor.calls == 1

    _activate_invoice_rule(workspace, inbox_root.root_id, archive_root.root_id)

    run_id = mutation.preview_routing(workspace, inbox_root.root_id)
    assert len(mutation.run_items(workspace, run_id)) == 1

    result = mutation.apply(workspace, run_id)
    assert result.committed == 1
    assert not document.exists()
    moved = archive / "invoices" / f"2026-03-14_Rechnung{document.suffix}"
    assert moved.exists()
    assert _hash(moved) == original_hash

    _assert_search_export(workspace, tmp_path)

    assert mutation.history_runs(workspace)
    outcome = mutation.undo(workspace, run_id)
    assert outcome.reversed == 1
    assert document.exists()
    assert _hash(document) == original_hash


def test_routing_without_rules_plans_nothing(tmp_path: Path) -> None:
    state = tmp_path / "state"
    workspace = Workspace(
        db=_bootstrap_db(state),
        state_dir=state,
        extractor=FakeExtractor(),
        runner=InProcessRunner(FakeOCR([ocr_text("Rechnung von Beispiel", 0.9)])),
    )
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    root = workflow.register_root(workspace, inbox, role=RootRole.INBOX)
    make_blank_pdf(inbox / "scan.pdf", page_count=1)
    workflow.scan(workspace, root.root_id)

    run_id = mutation.preview_routing(workspace, root.root_id)
    assert mutation.run_items(workspace, run_id) == []
    assert (inbox / "scan.pdf").exists()


def test_apply_blocks_when_rule_revision_changed(tmp_path: Path) -> None:
    from renamr.core.rules import RuleStatus, set_rule_status

    state = tmp_path / "state"
    workspace = Workspace(
        db=_bootstrap_db(state),
        state_dir=state,
        extractor=FakeExtractor(document_type="invoice"),
        runner=InProcessRunner(
            FakeOCR([ocr_text("Rechnung von Beispiel", 0.92), ocr_text("Rechnung", 0.92)])
        ),
    )
    inbox = tmp_path / "inbox"
    archive = tmp_path / "archive"
    inbox.mkdir()
    archive.mkdir()
    inbox_root = workflow.register_root(workspace, inbox, role=RootRole.INBOX)
    archive_root = workflow.register_root(workspace, archive, role=RootRole.ARCHIVE)
    workflow.set_allowed_targets(workspace, inbox_root.root_id, [archive_root.root_id])
    document = inbox / "scan.pdf"
    make_blank_pdf(document, page_count=1)
    workflow.scan(workspace, inbox_root.root_id)

    draft = workflow.create_rule_draft(
        workspace,
        scope_root_id=inbox_root.root_id,
        target_root_id=archive_root.root_id,
        predicates=[RulePredicate(field="document_type", operator="equals", values=["invoice"])],
        filename_template="{date}_{subject}",
        target_subdir="invoices",
    )
    workflow.mark_tested(workspace, draft.rule_id)
    workflow.activate_rule(workspace, draft.rule_id)
    run_id = mutation.preview_routing(workspace, inbox_root.root_id)
    assert len(mutation.run_items(workspace, run_id)) == 1

    set_rule_status(workspace.db, draft.rule_id, RuleStatus.DISABLED)
    mutation.apply(workspace, run_id)

    assert mutation.run_items(workspace, run_id)[0].state is ItemState.BLOCKED
    assert document.exists()


def _bootstrap_db(state: Path) -> Database:
    db = Database(state / "catalog.sqlite3")
    db.initialize()
    return db


def _hash(path: Path) -> str:
    from renamr.core import catalog

    return catalog.hash_file(path)
