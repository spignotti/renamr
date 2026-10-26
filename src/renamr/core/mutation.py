"""Preview, apply, recover, and undo workflows over a :class:`Workspace`.

Every writer path acquires the same cross-process lock, reconciles the journal
before new work, and revalidates allowed target roots, symlink boundaries, and the
active rule revision at mutation time (not from a stale preview snapshot).
"""

from __future__ import annotations

from pathlib import Path

from renamr.benchmark_schema import DocumentFeatures
from renamr.core import catalog, history, operations, roots, rules
from renamr.core.catalog import Presence, QualityState
from renamr.core.errors import CoreError, PathBoundaryError, PreconditionError, RuleError
from renamr.core.journal import FaultInjector
from renamr.core.roots import Root
from renamr.core.runitem import RunGuard, RunItem
from renamr.core.workspace import Workspace


def preview_routing(ws: Workspace, root_id: str) -> str:
    """Plan routing for one root and persist a preview run.

    Only active-rule matches on documents with validated, non-review facts and
    clean quality are planned; everything else stays in place with a review item.
    """
    root = roots.get_root(ws.db, root_id)
    with ws.lock():
        run_id = operations.create_run(
            ws.db, kind="rename", root_id=root_id, rule_policy_version="rules-v1"
        )
        active = rules.active_rules(ws.db)
        for location in catalog.locations_for_root(ws.db, root.root_id):
            _plan_location(ws, root, location, active, run_id)
    return run_id


def apply(
    ws: Workspace, run_id: str, *, fault: FaultInjector | None = None
) -> operations.ApplyResult:
    """Recover the journal, then apply the run under the writer lock."""
    run = operations.get_run(ws.db, run_id)
    guard = _routing_guard(ws, run)
    with ws.lock() as lock:
        operations.recover_run(ws.db, run_id, lock=lock, guard=guard)
        return operations.apply_run(ws.db, run_id, lock=lock, fault=fault, guard=guard)


def recover(ws: Workspace, run_id: str) -> operations.RecoveryResult:
    """Reconcile a run's journal under the writer lock."""
    run = operations.get_run(ws.db, run_id)
    guard = _routing_guard(ws, run)
    with ws.lock() as lock:
        return operations.recover_run(ws.db, run_id, lock=lock, guard=guard)


def undo(ws: Workspace, run_id: str) -> history.UndoOutcome:
    """Reverse the committed items of a run under the writer lock."""
    guard = _boundary_guard(ws)
    with ws.lock() as lock:
        return history.undo_run(ws.db, run_id, lock=lock, guard=guard)


def restore(ws: Workspace, backup_path: Path) -> None:
    """Restore the catalog from a verified backup under the writer lock.

    The restore runs through SQLite under the same exclusive writer lock as the
    other mutation workflows, so no other writer is active while the backup is
    copied into the live database.
    """
    with ws.lock() as lock:
        ws.db.restore_from(backup_path, lock=lock)


def legacy_undo(
    ws: Workspace, preview: history.LegacyUndoPreview, *, confirmed_indices: set[int]
) -> history.UndoOutcome:
    """Apply confirmed legacy reversals under the enforcing boundary guard."""
    guard = _boundary_guard(ws)
    with ws.lock() as lock:
        return history.apply_legacy_undo(
            ws.db, preview, confirmed_indices=confirmed_indices, lock=lock, guard=guard
        )


def history_runs(ws: Workspace, *, limit: int = 50) -> list[dict[str, object]]:
    """Return recent runs newest first."""
    return operations.list_runs(ws.db, limit=limit)


def run_items(ws: Workspace, run_id: str) -> list[RunItem]:
    """Return the items of a run."""
    return operations.run_items(ws.db, run_id)


def _boundary_guard(ws: Workspace) -> RunGuard:
    """Return a guard keeping the source and target inside live registered roots."""

    def guard(item: RunItem) -> None:
        _require_registered(ws, item.source_path, what="source")
        if item.target_path is not None:
            _require_registered(ws, item.target_path, what="target")

    return guard


def _require_registered(ws: Workspace, path: Path, *, what: str) -> None:
    """Block a path that is not inside a live registered root right now.

    The root registry is read inside the guarded callback, under the held writer
    lock, so a root registered, removed, or replaced by a symlink after planning
    is judged at mutation time.
    """
    try:
        roots.require_within_registered_roots(ws.db, path, what=what)
    except PathBoundaryError:
        raise PreconditionError(f"{what} is outside its registered root") from None


def _routing_guard(ws: Workspace, run: dict[str, object]) -> RunGuard:
    """Return a guard that rechecks allowed targets and active rule revisions.

    Allowed targets, the active rule revision, and the registered roots are read
    at call time, under the held writer lock, never from a stale pre-lock snapshot.
    """
    root_id = run.get("root_id")
    source_root_id = root_id if isinstance(root_id, str) and root_id else None

    def guard(item: RunItem) -> None:
        _check_rule(ws, item)
        _require_registered(ws, item.source_path, what="source")
        target = item.target_path
        if target is None:
            return
        _require_registered(ws, target, what="target")
        if source_root_id is not None:
            _require_allowed_target(ws, source_root_id, target)

    return guard


def _require_allowed_target(ws: Workspace, source_root_id: str, target: Path) -> None:
    """Block a target that is not inside an allowed target root."""
    allowed = roots.get_root(ws.db, source_root_id).allowed_target_ids
    for target_id in allowed:
        try:
            roots.require_within_root(roots.get_root(ws.db, target_id), target)
        except PathBoundaryError:
            continue
        return
    raise PreconditionError("target is not an allowed destination")


def _check_rule(ws: Workspace, item: RunItem) -> None:
    """Block an item whose rule is no longer the active revision."""
    if not item.rule_id:
        return
    try:
        current = rules.get_rule(ws.db, item.rule_id)
    except RuleError:
        raise PreconditionError("rule_missing") from None
    if current.status is not rules.RuleStatus.ACTIVE or current.revision != item.rule_revision:
        raise PreconditionError("rule_revision_stale")


def _plan_location(
    ws: Workspace,
    root: Root,
    location: catalog.LocationRecord,
    active: list[rules.Rule],
    run_id: str,
) -> None:
    """Plan one location or record a review item."""
    if location.presence is Presence.MISSING:
        return
    revision = catalog.get_revision(ws.db, location.revision_id)
    if revision.quality_state not in (QualityState.OK, QualityState.LOW):
        catalog.record_review(
            ws.db,
            kind="quality_incomplete",
            detail=str(revision.quality_state),
            revision_id=revision.revision_id,
            location_id=location.location_id,
        )
        return
    from renamr.core.features import facts_to_features

    features = facts_to_features(catalog.get_facts(ws.db, revision.revision_id))
    if features is None or features.needs_review:
        catalog.record_review(
            ws.db,
            kind="features_unresolved",
            detail="pending_review",
            revision_id=revision.revision_id,
            location_id=location.location_id,
        )
        return
    decision = _decide(ws, root, location, active)
    if decision.state == "conflict":
        catalog.record_review(
            ws.db,
            kind="rule_conflict",
            detail=decision.reason,
            revision_id=revision.revision_id,
            location_id=location.location_id,
        )
        return
    if decision.state != "routed" or decision.target_root_id is None:
        return
    _plan_routed(ws, root, location, revision, decision, features, run_id)


def _decide(
    ws: Workspace, root: Root, location: catalog.LocationRecord, active: list[rules.Rule]
) -> rules.RoutingDecision:
    """Run deterministic rule selection for one location."""
    relative = _relative(location.canonical_path, root.canonical_path)
    fact_map = _facts_for_routing(catalog.get_facts(ws.db, location.revision_id))
    return rules.select_rule(
        active, scope_root_id=root.root_id, relative_path=relative, facts=fact_map
    )


def _plan_routed(
    ws: Workspace,
    root: Root,
    location: catalog.LocationRecord,
    revision: catalog.RevisionRecord,
    decision: rules.RoutingDecision,
    features: DocumentFeatures,
    run_id: str,
) -> None:
    """Persist one routed rename item after validating the target."""
    try:
        roots.require_allowed_target(ws.db, root.root_id, decision.target_root_id or "")
        target_root = roots.get_root(ws.db, decision.target_root_id or "")
        target_dir = roots.resolve_subpath(target_root, decision.target_subdir)
    except CoreError as exc:
        catalog.record_review(
            ws.db,
            kind="target_rejected",
            detail=exc.category,
            revision_id=revision.revision_id,
            location_id=location.location_id,
        )
        return
    base = rules.render_base_name(decision.filename_template or "", _filename_fields(features))
    target_path = target_dir / f"{base}{location.canonical_path.suffix.lower()}"
    observed = _indexed_fingerprint(ws, location, revision)
    if observed is None:
        return
    operations.add_plan_item(
        ws.db,
        run_id,
        source_path=location.canonical_path,
        target_path=target_path,
        content_hash=observed.content_hash,
        size_bytes=observed.size_bytes,
        revision_id=revision.revision_id,
        location_id=location.location_id,
        rule_id=decision.rule_id,
        rule_revision=decision.rule_revision,
        annotation=decision.reason,
        source_dev=observed.dev,
        source_ino=observed.ino,
        source_mtime_ns=observed.mtime_ns,
    )


def _indexed_fingerprint(
    ws: Workspace, location: catalog.LocationRecord, revision: catalog.RevisionRecord
) -> catalog.Fingerprint | None:
    """Require the source bytes to match the revision that supplied the facts."""
    try:
        observed = catalog.fingerprint(location.canonical_path)
    except OSError:
        observed = None
    if observed is not None and observed.content_hash == revision.content_hash:
        return observed
    catalog.record_review(
        ws.db,
        kind="source_changed_since_scan",
        detail="rescan_required",
        revision_id=revision.revision_id,
        location_id=location.location_id,
    )
    return None


def _filename_fields(features: DocumentFeatures) -> dict[str, str]:
    """Map validated metadata to the supported filename placeholders."""
    return {
        "date": features.date or "",
        "sender": features.issuer or "",
        "subject": features.subject or "",
        "document_type": features.document_type,
    }


def _relative(path: Path, root: Path) -> str:
    """Return a POSIX relative path of ``path`` under ``root``."""
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.name


def _facts_for_routing(facts: list[catalog.FactRecord]) -> dict[str, list[str]]:
    """Group scalar fact values by field for rule matching."""
    grouped: dict[str, list[str]] = {}
    for fact in facts:
        if fact.field == "evidence" or fact.value is None:
            continue
        grouped.setdefault(fact.field, []).append(fact.value)
    return grouped
