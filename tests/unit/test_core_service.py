"""P6 tests: bounded scan budgets, service ticks, and LaunchAgent lifecycle."""

from __future__ import annotations

import plistlib
import subprocess
import sys
from pathlib import Path

from renamr.benchmark_schema import RulePredicate
from renamr.core import catalog, processing, reconcile, scheduler, workflow
from renamr.core.extract_runner import InProcessRunner
from renamr.core.roots import Root, RootRole
from renamr.core.settings import Settings
from renamr.core.workspace import Workspace
from tests.unit.core_support import FakeExtractor, make_text_pdf


def _workspace(tmp_path: Path) -> Workspace:
    return Workspace.bootstrap(tmp_path / "state", config_path=tmp_path / "config.toml")


def _inbox(workspace: Workspace, tmp_path: Path) -> tuple[Root, Path]:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    root = workflow.register_root(workspace, inbox, role=RootRole.INBOX, automation_enabled=True)
    return root, inbox


def _workspace_with_facts(tmp_path: Path) -> tuple[Workspace, Root, Path]:
    workspace = _workspace(tmp_path)
    root, inbox = _inbox(workspace, tmp_path)
    archive = tmp_path / "archive"
    archive.mkdir()
    archive_root = workflow.register_root(workspace, archive, role=RootRole.ARCHIVE)
    workflow.set_allowed_targets(workspace, root.root_id, [archive_root.root_id])
    workspace.save_settings(Settings(stability_window_s=0))
    (inbox / "note.txt").write_text("Rechnung von Beispiel")
    workspace.runner = InProcessRunner()
    workspace.extractor = FakeExtractor(document_type="invoice")
    workflow.scan(workspace, root.root_id)
    _settle_features(workspace)
    draft = workflow.create_rule_draft(
        workspace,
        scope_root_id=root.root_id,
        target_root_id=archive_root.root_id,
        predicates=[RulePredicate(field="document_type", operator="equals", values=["invoice"])],
        filename_template="{date}_{subject}",
        target_subdir="invoices",
    )
    workflow.mark_tested(workspace, draft.rule_id)
    workflow.activate_rule(workspace, draft.rule_id)
    return workspace, root, inbox


def _settle_features(workspace: Workspace) -> None:
    """Mark facts current for the configured model so a tick needs no server."""
    settings = workspace.settings()
    prefix = (
        f"{settings.model_roles.main}|{settings.output_mode}|{processing.FEATURE_PROMPT_VERSION}|"
    )
    for root in workflow.list_roots(workspace):
        for location in catalog.locations_for_root(workspace.db, root.root_id):
            processing.record_feature_result(
                workspace.db, location.revision_id, feature_key=f"{prefix}synthetic"
            )


def test_budget_limits_documents_and_reports_exhaustion(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    root, inbox = _inbox(workspace, tmp_path)
    for index in range(4):
        (inbox / f"doc-{index}.txt").write_text(f"document {index}")
    workspace.runner = InProcessRunner()

    outcome = workflow.scan(
        workspace,
        root.root_id,
        budget=reconcile.ScanBudget(max_documents=2, max_model_calls=0, max_pages=10),
    )

    assert outcome.budget_exhausted is True
    assert outcome.new_revisions == 2


def test_large_file_is_deferred_to_manual_retry(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    root, inbox = _inbox(workspace, tmp_path)
    make_text_pdf(inbox / "a-large.pdf", ["one", "two", "three"])
    (inbox / "b-small.txt").write_text("small")
    workspace.runner = InProcessRunner()

    outcome = workflow.scan(
        workspace,
        root.root_id,
        budget=reconcile.ScanBudget(max_documents=4, max_model_calls=0, max_pages=1),
    )

    assert outcome.deferred >= 1
    assert outcome.budget_exhausted is True
    kinds = {item["kind"] for item in workflow.review_items(workspace)}
    assert "scan_budget_deferred" in kinds
    assert (inbox / "b-small.txt").exists()


def test_tick_routes_enabled_inbox_and_skips_unchanged_model_work(tmp_path: Path) -> None:
    workspace, _root, _inbox = _workspace_with_facts(tmp_path)

    result = scheduler.run_tick(workspace)

    assert result.status in ("completed", "partial")
    assert result.routes_committed == 1
    assert (tmp_path / "archive" / "invoices" / "2026-03-14_Rechnung.txt").exists()
    second = scheduler.run_tick(workspace)
    assert second.routes_committed == 0


def test_tick_busy_and_paused(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    workflow.pause_service(workspace)
    assert scheduler.run_tick(workspace).status == "paused"
    workflow.resume_service(workspace)

    lock = workspace.lock()
    lock.acquire()
    try:
        assert scheduler.run_tick(workspace).status == "busy"
    finally:
        lock.release()


def test_tick_observes_manual_move(tmp_path: Path) -> None:
    workspace, _root, inbox = _workspace_with_facts(tmp_path)
    moved = inbox / "note.txt"
    moved.rename(inbox / "renamed.txt")

    result = scheduler.run_tick(workspace)

    assert result.observations == 1


def test_service_worker_prints_only_sanitized_counts(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    workflow.pause_service(workspace)
    secret = tmp_path / "inbox"
    secret.mkdir()
    (secret / "PRIVATE-NAME.txt").write_text("private body")

    completed = subprocess.run(
        [sys.executable, "-m", "renamr.service_worker", "--state-dir", str(workspace.state_dir)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0
    assert '"paused"' in completed.stdout
    assert "PRIVATE-NAME" not in completed.stdout
    assert "private body" not in completed.stdout


def test_launch_agent_render_has_no_side_effect(tmp_path: Path) -> None:
    data = scheduler.render_agent_plist(
        python="/usr/bin/python3", state_dir=tmp_path / "state", interval_s=60
    )
    payload = plistlib.loads(data)

    assert payload["Label"] == scheduler.LAUNCH_AGENT_LABEL
    assert payload["RunAtLoad"] is True
    assert payload["StartInterval"] == 60
    assert payload["ProgramArguments"][:3] == [
        "/usr/bin/python3",
        "-m",
        "renamr.service_worker",
    ]
    assert not scheduler.agent_plist_path(tmp_path).exists()


def test_launch_agent_install_status_remove_temp_home(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def launcher(arguments: list[str]) -> int:
        calls.append(arguments)
        return 0

    home = tmp_path / "home"
    plist = scheduler.install_agent(
        tmp_path / "state", home=home, python="/usr/bin/python3", launcher=launcher
    )

    assert plist.exists()
    assert plist.stat().st_mode & 0o777 == 0o600
    assert any(item[:2] == ["launchctl", "bootstrap"] for item in calls)
    status = scheduler.agent_status(home=home, launcher=launcher)
    assert status.installed is True
    assert status.loaded is True

    foreign = home / "Library" / "LaunchAgents" / "com.example.other.plist"
    foreign.write_text("other")
    assert scheduler.remove_agent(home=home, launcher=launcher) is True
    assert not plist.exists()
    assert foreign.exists()
