"""Read/scan/rule workflows that operate on a :class:`Workspace`.

Every workflow that writes the catalog, or that performs OCR/inference, runs
under the shared cross-process writer lock so only one writer is active at a
time. Read-only helpers take no lock.
"""

from __future__ import annotations

from pathlib import Path

from renamr.benchmark_schema import RulePredicate
from renamr.core import catalog, processing, reconcile, roots, rules
from renamr.core.catalog import PageRecord
from renamr.core.extract import ExtractorLimits
from renamr.core.reconcile import ScanOutcome
from renamr.core.roots import Root, RootRole
from renamr.core.search import DEFAULT_LIMIT, SearchHit
from renamr.core.search import export_text as _export_text
from renamr.core.search import search as _search
from renamr.core.workspace import Workspace


def register_root(
    ws: Workspace,
    path: Path,
    *,
    role: RootRole = RootRole.INBOX,
    description: str = "",
    trusted_examples: bool = False,
    automation_enabled: bool = False,
    allowed_targets: list[str] | None = None,
) -> Root:
    """Register a root after validating every precondition, under the lock."""
    with ws.lock():
        return roots.register_root(
            ws.db,
            path=path,
            role=role,
            state_dir=ws.state_dir,
            description=description,
            trusted_examples=trusted_examples,
            automation_enabled=automation_enabled,
            allowed_targets=allowed_targets,
        )


def list_roots(ws: Workspace) -> list[Root]:
    """Return all registered roots."""
    return roots.list_roots(ws.db)


def get_root(ws: Workspace, root_id: str) -> Root:
    """Return a registered root by id."""
    return roots.get_root(ws.db, root_id)


def set_allowed_targets(ws: Workspace, source_root_id: str, target_root_ids: list[str]) -> None:
    """Replace the allowed target set for a source root, under the lock."""
    with ws.lock():
        roots.set_allowed_targets(ws.db, source_root_id, target_root_ids)


def limits(ws: Workspace) -> ExtractorLimits:
    """Return extraction limits derived from settings."""
    return reconcile.limits_from_settings(ws.settings())


def scan(ws: Workspace, root_id: str, *, wait_stable: bool = False) -> ScanOutcome:
    """Scan one registered root incrementally under the writer lock."""
    root = roots.get_root(ws.db, root_id)
    with ws.lock():
        return reconcile.scan_root(
            ws.db,
            root,
            runner=ws.extraction_runner(),
            extractor=ws.extractor,
            settings=ws.settings(),
            wait_stable=wait_stable,
            model_digest=ws.model_digest,
        )


def get_pages(ws: Workspace, revision_id: str) -> list[PageRecord]:
    """Return a revision's pages."""
    return catalog.get_pages(ws.db, revision_id)


def search(ws: Workspace, query: str, *, limit: int = DEFAULT_LIMIT) -> list[SearchHit]:
    """Search indexed page text."""
    return _search(ws.db, query, limit=limit)


def export_text(ws: Workspace, revision_id: str, destination: Path) -> Path:
    """Export a revision's text to an explicit private file."""
    return _export_text(ws.db, revision_id, destination)


def review_items(ws: Workspace) -> list[dict[str, object]]:
    """Return open review items."""
    return catalog.open_review_items(ws.db)


def retry_revision(ws: Workspace, revision_id: str) -> None:
    """Clear retry/cache state so the next scan redoes the failed stage."""
    with ws.lock():
        processing.retry_revision(ws.db, revision_id)


def create_rule_draft(
    ws: Workspace,
    *,
    scope_root_id: str,
    target_root_id: str,
    predicates: list[RulePredicate],
    filename_template: str,
    scope_subtree: str = "",
    target_subdir: str = "",
    priority: int = 0,
    provenance: str = "",
) -> rules.Rule:
    """Validate and store a new rule revision in the draft state, under the lock."""
    with ws.lock():
        return rules.create_rule_draft(
            ws.db,
            scope_root_id=scope_root_id,
            target_root_id=target_root_id,
            predicates=predicates,
            filename_template=filename_template,
            scope_subtree=scope_subtree,
            target_subdir=target_subdir,
            priority=priority,
            provenance=provenance,
        )


def mark_tested(ws: Workspace, rule_id: str) -> rules.Rule:
    """Mark a draft rule tested, under the lock."""
    with ws.lock():
        return rules.mark_tested(ws.db, rule_id)


def activate_rule(ws: Workspace, rule_id: str) -> rules.Rule:
    """Activate a tested rule, under the lock."""
    with ws.lock():
        return rules.activate_rule(ws.db, rule_id)


def list_rules(ws: Workspace) -> list[rules.Rule]:
    """Return current rule revisions."""
    return rules.list_rules(ws.db)


def active_rules(ws: Workspace) -> list[rules.Rule]:
    """Return active rule revisions."""
    return rules.active_rules(ws.db)


def rule_history(ws: Workspace, rule_id: str) -> list[rules.Rule]:
    """Return the revision history of a rule."""
    return rules.rule_history(ws.db, rule_id)
