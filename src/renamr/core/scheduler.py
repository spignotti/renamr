"""Bounded background reconciliation and the user-level macOS LaunchAgent.

A tick takes the global writer lock non-blockingly, recovers the journal, scans
metadata cheaply, runs features only when work exists, observes manual moves, and
routes only inbox roots with explicit automation enabled. LaunchAgent rendering
has no side effect; install/remove require an explicit call and only touch this
application's own label and plist.
"""

from __future__ import annotations

import json
import os
import plistlib
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from renamr.core import catalog, examples, mutation, operations, processing, reconcile, roots
from renamr.core.db import Database
from renamr.core.errors import CoreError, InferenceError
from renamr.core.features import FeatureExtractor
from renamr.core.inference import LocalInference, verify_model
from renamr.core.lock import WriterLock
from renamr.core.process import OwnedServer
from renamr.core.roots import RootRole
from renamr.core.rules import Rule
from renamr.core.settings import ServiceBudgets, Settings, get_setting, set_setting
from renamr.core.workspace import Workspace

SERVICE_STATE_KEY = "service_state"
LAUNCH_AGENT_LABEL = "com.renamr.reconcile"
_TICK_RUN_LIMIT = 20


class TickResult(BaseModel):
    """Sanitized outcome of one scheduled reconciliation tick."""

    model_config = ConfigDict(extra="forbid")

    status: str
    roots_scanned: int = 0
    documents: int = 0
    pages: int = 0
    model_calls: int = 0
    deferred: int = 0
    reviews: int = 0
    routes_committed: int = 0
    routes_blocked: int = 0
    observations: int = 0
    duration_s: float = 0.0
    error: str = ""


class ServiceStatus(BaseModel):
    """Pause/tick/backlog status for the service and CLI."""

    model_config = ConfigDict(extra="forbid")

    paused: bool = False
    last_tick_at: str = ""
    last_status: str = ""
    last_error: str = ""
    open_reviews: int = 0
    pending_features: int = 0


class LaunchAgentStatus(BaseModel):
    """Installed/loaded state of this application's LaunchAgent."""

    model_config = ConfigDict(extra="forbid")

    label: str = LAUNCH_AGENT_LABEL
    plist_path: Path
    installed: bool
    loaded: bool


Launcher = Callable[[list[str]], int]


def run_tick(ws: Workspace) -> TickResult:
    """Run one bounded tick under the non-blocking global writer lock."""
    state = service_state(ws.db)
    if state.get("paused"):
        return TickResult(status="paused")
    lock = ws.lock()
    if not lock.acquire(blocking=False):
        return TickResult(status="busy")
    started = time.monotonic()
    try:
        return _tick_locked(ws, started, lock)
    except (CoreError, OSError) as exc:
        _record_state(ws.db, "blocked", type(exc).__name__)
        return TickResult(status="blocked", error=type(exc).__name__)
    finally:
        lock.release()


def _tick_locked(ws: Workspace, started: float, lock: WriterLock) -> TickResult:
    """Perform one tick while the caller holds the writer lock."""
    settings = ws.settings()
    budgets = settings.service
    _recover_journal(ws, lock)
    roots_list = roots.list_roots(ws.db)
    result = TickResult(status="completed")
    used_docs = used_pages = used_models = 0
    for root in roots_list:
        remaining = _budget(budgets, used_docs, used_pages, used_models, started)
        if remaining is None:
            break
        outcome = reconcile.scan_root(
            ws.db,
            root,
            runner=ws.extraction_runner(),
            extractor=None,
            settings=settings,
            wait_stable=True,
            budget=remaining,
        )
        _collect(result, outcome)
        used_docs += min(budgets.max_documents, outcome.new_revisions + outcome.changed)
        used_pages += outcome.pages_extracted
        result.roots_scanned += 1
    if _need_features(ws, settings) and used_models < budgets.max_model_calls:
        used_models += _feature_pass(ws, settings, result, used_pages, started)
    result.observations = len(examples.observe_manual_moves(ws.db))
    _auto_route(ws, settings, result, lock)
    result.duration_s = round(time.monotonic() - started, 3)
    if result.error or result.deferred:
        result.status = "partial"
    _record_state(ws.db, result.status, result.error)
    return result


def _feature_pass(
    ws: Workspace,
    settings: Settings,
    result: TickResult,
    used_pages: int,
    started: float,
) -> int:
    """Run one bounded feature pass with an owned/reused local server."""
    server = OwnedServer(endpoint=settings.endpoint, startup_timeout=15)
    try:
        server.start_if_needed()
        inference = LocalInference.from_settings(settings, role="small")
        info = verify_model(inference, settings.model_roles.small)
        if not info.digest:
            raise InferenceError("model_digest_unavailable")
        ws.model_digest = info.digest
        ws.extractor = _feature_extractor(settings, inference)
        return _run_feature_roots(ws, settings, result, used_pages, started)
    except (OSError, RuntimeError, TimeoutError, CoreError, InferenceError):
        result.error = "inference_unavailable"
        return 0
    finally:
        if server.started_by_us:
            server.stop()


def _run_feature_roots(
    ws: Workspace,
    settings: Settings,
    result: TickResult,
    used_pages: int,
    started: float,
) -> int:
    """Scan roots for features with the remaining model/page/time budget."""
    used_models = 0
    for root in roots.list_roots(ws.db):
        remaining = _budget(
            settings.service,
            0,
            used_pages,
            used_models,
            started,
            model_calls=settings.service.max_model_calls - used_models,
        )
        if remaining is None:
            break
        outcome = reconcile.scan_root(
            ws.db,
            root,
            runner=ws.extraction_runner(),
            extractor=ws.extractor,
            settings=settings,
            wait_stable=False,
            model_digest=ws.model_digest,
            budget=remaining,
        )
        _collect(result, outcome)
        used_models += outcome.model_calls
        used_pages += outcome.pages_extracted
    return used_models


def _auto_route(ws: Workspace, settings: Settings, result: TickResult, lock: WriterLock) -> None:
    """Apply active rules only for inbox roots with explicit automation enabled."""
    if not any(rule.status.value == "active" for rule in _active_rules(ws)):
        return
    for root in roots.list_roots(ws.db):
        if root.role is not RootRole.INBOX or not root.automation_enabled:
            continue
        run_id = operations.create_run(ws.db, kind="rename", root_id=root.root_id)
        planned = mutation.plan_routing_locked(ws, root.root_id, run_id)
        if not planned:
            continue
        run = operations.get_run(ws.db, run_id)
        outcome = operations.apply_run(
            ws.db,
            run_id,
            lock=lock,
            guard=mutation.routing_guard(ws, run),
        )
        result.routes_committed += outcome.committed
        result.routes_blocked += outcome.blocked + outcome.failed


def _recover_journal(ws: Workspace, lock: WriterLock) -> None:
    """Reconcile non-terminal items before any new tick work."""
    for run in operations.list_runs(ws.db, limit=_TICK_RUN_LIMIT):
        kind = str(run["kind"])
        guard = mutation.routing_guard(ws, run) if kind == "rename" else mutation.boundary_guard(ws)
        operations.recover_run(ws.db, str(run["run_id"]), lock=lock, guard=guard)


def _budget(
    settings: ServiceBudgets,
    used_docs: int,
    used_pages: int,
    used_models: int,
    started: float,
    *,
    model_calls: int | None = None,
) -> reconcile.ScanBudget | None:
    """Return a remaining tick budget, or None when no work may start."""
    elapsed = time.monotonic() - started
    remaining_seconds = settings.max_seconds - elapsed
    docs = settings.max_documents - used_docs
    pages = settings.max_pages - used_pages
    models = settings.max_model_calls - used_models if model_calls is None else model_calls
    if remaining_seconds <= 0 or docs <= 0 or pages <= 0 or models <= 0:
        return None
    return reconcile.ScanBudget(
        max_documents=docs,
        max_model_calls=models,
        max_pages=pages,
        max_seconds=remaining_seconds,
    )


def _collect(result: TickResult, outcome: reconcile.ScanOutcome) -> None:
    """Add sanitized outcome counts to a tick result."""
    result.documents += outcome.new_revisions + outcome.changed
    result.pages += outcome.pages_extracted
    result.model_calls += outcome.model_calls
    result.deferred += outcome.deferred
    result.reviews += outcome.reviews


def _need_features(ws: Workspace, settings: Settings) -> bool:
    """Return whether any catalogued revision needs a routine feature pass.

    Budget-deferred revisions stay visible in service status but are retried by a
    manual ``run`` or an explicit retry, not by every scheduled tick.
    """
    prefix = (
        f"{settings.model_roles.small}|{settings.output_mode}|{processing.FEATURE_PROMPT_VERSION}|"
    )
    for root in roots.list_roots(ws.db):
        for location in catalog.locations_for_root(ws.db, root.root_id):
            revision = catalog.get_revision(ws.db, location.revision_id)
            if revision.quality_state not in (catalog.QualityState.OK, catalog.QualityState.LOW):
                continue
            if revision.failed_stage in ("features", "features_deferred"):
                continue
            if not revision.feature_key.startswith(prefix):
                return True
    return False


def _feature_extractor(settings: Settings, inference: LocalInference) -> FeatureExtractor:
    """Build the small-role routine feature extractor."""
    from renamr.core.features import LocalFeatureExtractor

    return LocalFeatureExtractor(
        inference,
        settings.model_roles.small,
        output_mode=settings.output_mode,
        max_chars=settings.feature_max_chars,
    )


def _active_rules(ws: Workspace) -> list[Rule]:
    """Return active rule revisions."""
    from renamr.core import rules

    return list(rules.active_rules(ws.db))


def service_state(db: Database) -> dict[str, object]:
    """Return the persisted service state."""
    raw = get_setting(db, SERVICE_STATE_KEY)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def service_status(ws: Workspace) -> ServiceStatus:
    """Return pause/tick/backlog status with no document content."""
    state = service_state(ws.db)
    with ws.db.read() as connection:
        reviews = connection.execute(
            "SELECT count(*) FROM review_items WHERE status = 'open'"
        ).fetchone()[0]
    pending = _pending_features(ws)
    return ServiceStatus(
        paused=bool(state.get("paused", False)),
        last_tick_at=str(state.get("last_tick_at", "")),
        last_status=str(state.get("last_status", "")),
        last_error=str(state.get("last_error", "")),
        open_reviews=int(reviews),
        pending_features=pending,
    )


def pause_service(db: Database) -> None:
    """Pause scheduled ticks without unloading the LaunchAgent."""
    _update_state(db, {"paused": True})


def resume_service(db: Database) -> None:
    """Resume scheduled ticks."""
    _update_state(db, {"paused": False})


def _record_state(db: Database, status: str, error: str) -> None:
    """Record a sanitized tick result."""
    _update_state(
        db,
        {
            "last_tick_at": datetime.now(UTC).isoformat(),
            "last_status": status,
            "last_error": error,
        },
    )


def _update_state(db: Database, updates: dict[str, object]) -> None:
    """Merge and persist service state."""
    state = service_state(db)
    state.update(updates)
    set_setting(db, SERVICE_STATE_KEY, json.dumps(state, sort_keys=True))


def _pending_features(ws: Workspace) -> int:
    """Count revisions whose small-role feature key is not current."""
    settings = ws.settings()
    prefix = (
        f"{settings.model_roles.small}|{settings.output_mode}|{processing.FEATURE_PROMPT_VERSION}|"
    )
    count = 0
    for root in roots.list_roots(ws.db):
        for location in catalog.locations_for_root(ws.db, root.root_id):
            revision = catalog.get_revision(ws.db, location.revision_id)
            if revision.quality_state in (catalog.QualityState.OK, catalog.QualityState.LOW):
                if revision.failed_stage == "features_deferred":
                    count += 1
                elif not revision.feature_key.startswith(prefix):
                    count += 1
    return count


def render_agent_plist(*, python: str, state_dir: Path, interval_s: int = 60) -> bytes:
    """Render the LaunchAgent plist without writing or loading it."""
    payload = {
        "Label": LAUNCH_AGENT_LABEL,
        "ProgramArguments": [
            python,
            "-m",
            "renamr.service_worker",
            "--state-dir",
            str(Path(state_dir)),
        ],
        "RunAtLoad": True,
        "StartInterval": int(interval_s),
        "ThrottleInterval": 10,
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "Nice": 10,
    }
    return plistlib.dumps(payload, sort_keys=True)


def agent_plist_path(home: Path | None = None) -> Path:
    """Return this application's own plist path."""
    base = Path.home() if home is None else Path(home)
    return base / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"


def install_agent(
    state_dir: Path,
    *,
    home: Path | None = None,
    python: str | None = None,
    interval_s: int = 60,
    launcher: Launcher | None = None,
) -> Path:
    """Write and bootstrap this application's LaunchAgent after explicit confirmation."""
    plist = agent_plist_path(home)
    plist.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    data = render_agent_plist(
        python=python or sys.executable, state_dir=state_dir, interval_s=interval_s
    )
    from renamr.core.paths import write_private_file

    write_private_file(plist, data, require_private_parent=False)
    run = launcher or _launchctl
    domain = f"gui/{os.getuid()}"
    _ = run(["launchctl", "bootout", domain, str(plist)])
    code = run(["launchctl", "bootstrap", domain, str(plist)])
    if code != 0:
        raise CoreError("launchctl could not load the service job")
    return plist


def remove_agent(*, home: Path | None = None, launcher: Launcher | None = None) -> bool:
    """Unload and remove only this application's own job and plist."""
    plist = agent_plist_path(home)
    run = launcher or _launchctl
    if plist.exists():
        _ = run(["launchctl", "bootout", f"gui/{os.getuid()}", str(plist)])
        plist.unlink(missing_ok=True)
        return True
    return False


def agent_status(
    *, home: Path | None = None, launcher: Launcher | None = None
) -> LaunchAgentStatus:
    """Return installed/loaded state for this application's own job."""
    plist = agent_plist_path(home)
    run = launcher or _launchctl
    loaded = False
    if plist.exists():
        loaded = run(["launchctl", "print", f"gui/{os.getuid()}/{LAUNCH_AGENT_LABEL}"]) == 0
    return LaunchAgentStatus(plist_path=plist, installed=plist.exists(), loaded=loaded)


def _launchctl(arguments: list[str]) -> int:
    """Run one launchctl command without printing raw output."""
    try:
        return subprocess.run(arguments, capture_output=True, check=False).returncode
    except OSError:
        return 1
