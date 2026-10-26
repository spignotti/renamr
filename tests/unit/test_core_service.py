"""P6 tests: bounded scan budgets, service ticks, and LaunchAgent lifecycle."""

from __future__ import annotations

import plistlib
import subprocess
import sys
import time
from pathlib import Path
from typing import ClassVar

from renamr.benchmark_schema import DocumentFeatures, RulePredicate
from renamr.core import catalog, processing, reconcile, scheduler, workflow
from renamr.core.errors import InferenceError
from renamr.core.extract_runner import InProcessRunner
from renamr.core.features import LocalFeatureExtractor
from renamr.core.roots import Root, RootRole
from renamr.core.settings import ServiceBudgets, Settings
from renamr.core.workspace import Workspace
from renamr.local_transport import StructuredResult
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
        f"{settings.model_roles.small}|{settings.output_mode}|{processing.FEATURE_PROMPT_VERSION}|"
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


class _CountingCompleter:
    """Offline completer that counts calls and always fails."""

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, *args: object, **kwargs: object) -> StructuredResult[DocumentFeatures]:
        self.calls += 1
        raise InferenceError("synthetic_unavailable")


def test_feature_chunk_budget_is_enforced_within_one_document(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    root, inbox = _inbox(workspace, tmp_path)
    workspace.save_settings(Settings(stability_window_s=0))
    (inbox / "long.txt").write_text("Synthetic invoice data. " * 50)
    workspace.runner = InProcessRunner()
    completer = _CountingCompleter()
    workspace.extractor = LocalFeatureExtractor(
        completer, "synthetic", max_chars=200, prompt_overhead=50
    )

    outcome = workflow.scan(
        workspace,
        root.root_id,
        budget=reconcile.ScanBudget(max_model_calls=1, max_pages=50),
    )

    assert completer.calls == 1
    assert outcome.model_calls == 1
    assert outcome.deferred == 1
    location = catalog.locations_for_root(workspace.db, root.root_id)[0]
    revision = catalog.get_revision(workspace.db, location.revision_id)
    assert revision.failed_stage == "features_deferred"
    assert any(item["kind"] == "feature_deferred" for item in workflow.review_items(workspace))

    workspace.extractor = FakeExtractor(document_type="invoice", evidence=False)
    workflow.retry_deferred_features(workspace, [root.root_id])
    workflow.scan(workspace, root.root_id)

    facts = {
        fact.field: fact.value for fact in catalog.get_facts(workspace.db, revision.revision_id)
    }
    assert facts["document_type"] == "invoice"
    assert not any(item["kind"] == "feature_deferred" for item in workflow.review_items(workspace))


def test_deferred_first_root_does_not_starve_second_root(tmp_path: Path, monkeypatch) -> None:
    workspace = _workspace(tmp_path)
    workspace.save_settings(
        Settings(
            stability_window_s=0,
            service=ServiceBudgets(max_documents=4, max_model_calls=4, max_pages=1),
        )
    )
    first, second = tmp_path / "a-first", tmp_path / "b-second"
    first.mkdir()
    second.mkdir()
    first_root = workflow.register_root(workspace, first, role=RootRole.INBOX)
    second_root = workflow.register_root(workspace, second, role=RootRole.INBOX)
    make_text_pdf(first / "large.pdf", ["one", "two", "three"])
    (second / "small.txt").write_text("small")
    workspace.runner = InProcessRunner()
    monkeypatch.setattr(scheduler, "_feature_pass", lambda *args, **kwargs: 0)

    for _ in range(3):
        result = scheduler.run_tick(workspace)
        assert result.roots_scanned == 2
        assert result.deferred >= 1

    assert catalog.get_location_by_path(workspace.db, second / "small.txt") is not None
    assert catalog.get_location_by_path(workspace.db, first / "large.pdf") is None
    assert any(item["kind"] == "scan_budget_deferred" for item in workflow.review_items(workspace))
    assert first_root.root_id and second_root.root_id


class _FakeServer:
    """Fake owned server that records lifecycle events."""

    instances: ClassVar[list[_FakeServer]] = []

    def __init__(self, *, started_by_us: bool = True, **kwargs: object) -> None:
        self.events: list[str] = []
        self.started_by_us = started_by_us
        _FakeServer.instances.append(self)

    def start_if_needed(self) -> bool:
        self.events.append("start")
        return self.started_by_us

    def stop(self) -> None:
        self.events.append("stop")


def _run_feature_pass(workspace: Workspace) -> scheduler.TickResult:
    tick = scheduler.TickResult(status="completed")
    used = scheduler._feature_pass(
        workspace,
        workspace.settings(),
        tick,
        0,
        time.monotonic(),
    )
    assert isinstance(used, int)
    return tick


def test_feature_pass_stops_owned_server_on_verification_failure(
    tmp_path: Path, monkeypatch
) -> None:
    workspace = _workspace(tmp_path)
    _FakeServer.instances.clear()
    monkeypatch.setattr(scheduler, "OwnedServer", _FakeServer)
    monkeypatch.setattr(scheduler.LocalInference, "from_settings", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        scheduler,
        "verify_model",
        lambda *args, **kwargs: (_ for _ in ()).throw(InferenceError("synthetic")),
    )

    result = _run_feature_pass(workspace)

    assert result.error == "inference_unavailable"
    assert _FakeServer.instances[-1].events == ["start", "stop"]


def test_feature_pass_stops_owned_server_on_extraction_failure(tmp_path: Path, monkeypatch) -> None:
    from types import SimpleNamespace

    workspace = _workspace(tmp_path)
    _FakeServer.instances.clear()
    monkeypatch.setattr(scheduler, "OwnedServer", _FakeServer)
    monkeypatch.setattr(scheduler.LocalInference, "from_settings", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        scheduler, "verify_model", lambda *args, **kwargs: SimpleNamespace(digest="d")
    )
    monkeypatch.setattr(
        scheduler,
        "_run_feature_roots",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("synthetic")),
    )

    result = _run_feature_pass(workspace)

    assert result.error == "inference_unavailable"
    assert _FakeServer.instances[-1].events == ["start", "stop"]


def test_feature_pass_stops_owned_server_after_normal_exit(tmp_path: Path, monkeypatch) -> None:
    from types import SimpleNamespace

    workspace = _workspace(tmp_path)
    _FakeServer.instances.clear()
    monkeypatch.setattr(scheduler, "OwnedServer", _FakeServer)
    monkeypatch.setattr(scheduler.LocalInference, "from_settings", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        scheduler, "verify_model", lambda *args, **kwargs: SimpleNamespace(digest="d")
    )
    monkeypatch.setattr(scheduler, "_run_feature_roots", lambda *args, **kwargs: 0)

    _run_feature_pass(workspace)

    assert _FakeServer.instances[-1].events == ["start", "stop"]


def test_feature_pass_does_not_stop_external_server(tmp_path: Path, monkeypatch) -> None:
    workspace = _workspace(tmp_path)
    _FakeServer.instances.clear()
    monkeypatch.setattr(scheduler.LocalInference, "from_settings", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        scheduler,
        "verify_model",
        lambda *args, **kwargs: (_ for _ in ()).throw(InferenceError("synthetic")),
    )
    monkeypatch.setattr(
        scheduler,
        "OwnedServer",
        lambda **kwargs: _FakeServer(started_by_us=False, **kwargs),
    )

    _run_feature_pass(workspace)

    assert _FakeServer.instances[-1].events == ["start"]


def test_service_worker_reports_busy_promptly_while_lock_is_held(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    _inbox(workspace, tmp_path)
    lock = workspace.lock()
    assert lock.acquire() is True
    started = time.monotonic()
    try:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "renamr.service_worker",
                "--state-dir",
                str(workspace.state_dir),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    finally:
        lock.release()

    assert time.monotonic() - started < 10
    assert completed.returncode == 0
    assert '"status": "busy"' in completed.stdout

    idle = subprocess.run(
        [
            sys.executable,
            "-m",
            "renamr.service_worker",
            "--state-dir",
            str(workspace.state_dir),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert idle.returncode == 0
    assert '"status": "busy"' not in idle.stdout
    assert '"status": "completed"' in idle.stdout or '"status": "partial"' in idle.stdout


def test_run_tick_reuses_a_held_lock_without_reacquiring(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    _inbox(workspace, tmp_path)
    lock = workspace.lock()
    assert lock.acquire() is True
    try:
        result = scheduler.run_tick(workspace, lock=lock)
        assert lock.held is True
        assert result.status in ("completed", "partial")
    finally:
        lock.release()
    assert lock.held is False
