"""Read/scan/rule workflows that operate on a :class:`Workspace`.

Every workflow that writes the catalog, or that performs OCR/inference, runs
under the shared cross-process writer lock so only one writer is active at a
time. Read-only helpers take no lock.
"""

from __future__ import annotations

from pathlib import Path

from renamr.benchmark_schema import RulePredicate
from renamr.core import (
    catalog,
    contracts,
    dialog,
    examples,
    folders,
    learning,
    onboarding,
    placement,
    processing,
    reconcile,
    review,
    roots,
    rule_maintenance,
    rules,
    scheduler,
    structure,
)
from renamr.core.catalog import PageRecord
from renamr.core.extract import ExtractorLimits
from renamr.core.reconcile import ScanOutcome
from renamr.core.roots import Root, RootRole
from renamr.core.search import DEFAULT_LIMIT, SearchHit
from renamr.core.search import export_text as _export_text
from renamr.core.search import search as _search
from renamr.core.settings import Settings, save_settings
from renamr.core.workspace import Workspace
from renamr.local_transport import OutputMode


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


def scan(
    ws: Workspace,
    root_id: str,
    *,
    wait_stable: bool = False,
    budget: reconcile.ScanBudget | None = None,
) -> ScanOutcome:
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
            budget=budget,
        )


def get_pages(ws: Workspace, revision_id: str) -> list[PageRecord]:
    """Return a revision's pages."""
    return catalog.get_pages(ws.db, revision_id)


def search(ws: Workspace, query: str, *, limit: int = DEFAULT_LIMIT) -> list[SearchHit]:
    """Search indexed page text."""
    return _search(ws.db, query, limit=limit)


def export_text(
    ws: Workspace, revision_id: str, destination: Path, *, overwrite: bool = False
) -> Path:
    """Export a revision's text to an explicit private file without clobbering."""
    return _export_text(ws.db, revision_id, destination, overwrite=overwrite)


def review_items(ws: Workspace) -> list[dict[str, object]]:
    """Return open review items."""
    return catalog.open_review_items(ws.db)


def retry_revision(ws: Workspace, revision_id: str) -> None:
    """Clear retry/cache state so the next scan redoes the failed stage."""
    with ws.lock():
        processing.retry_revision(ws.db, revision_id)


def retry_deferred_features(ws: Workspace, root_ids: list[str]) -> int:
    """Clear budget-deferred feature markers for selected roots before retry."""
    with ws.lock():
        return processing.retry_deferred_features(ws.db, root_ids)


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


def revise_rule(ws: Workspace, rule_id: str, **changes: object) -> rules.Rule:
    """Append a rule revision under the writer lock."""
    with ws.lock():
        return rules.revise_rule(ws.db, rule_id, **changes)


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


def set_settings(ws: Workspace, settings: Settings) -> None:
    """Persist typed database-owned settings under the writer lock."""
    with ws.lock():
        save_settings(ws.db, settings)


def update_root(
    ws: Workspace,
    root_id: str,
    *,
    description: str | None = None,
    alias: str | None = None,
    trusted_examples: bool | None = None,
    automation_enabled: bool | None = None,
) -> Root:
    """Update root description/alias/trust under the writer lock."""
    with ws.lock():
        return roots.update_root(
            ws.db,
            root_id,
            description=description,
            alias=alias,
            trusted_examples=trusted_examples,
            automation_enabled=automation_enabled,
        )


def set_folder_override(
    ws: Workspace,
    root_id: str,
    *,
    relative_path: str,
    description: str | None = None,
    alias: str | None = None,
    filename_template: str | None = None,
    target_root_id: str | None = None,
    target_subdir: str | None = None,
) -> folders.FolderOverride:
    """Persist one folder description/naming override under the writer lock."""
    with ws.lock():
        return folders.set_folder_override(
            ws.db,
            root_id,
            relative_path=relative_path,
            description=description,
            alias=alias,
            filename_template=filename_template,
            target_root_id=target_root_id,
            target_subdir=target_subdir,
        )


def observe_manual_moves(ws: Workspace) -> list[examples.ManualMoveObservation]:
    """Classify possible moves under the writer lock without confirming any."""
    with ws.lock():
        return examples.observe_manual_moves(ws.db)


def preview_placement(
    ws: Workspace,
    review_id: str,
    *,
    target_root_id: str,
    target_subdir: str = "",
    filename: str | None = None,
) -> placement.PlacementPreview:
    """Preview one explicit destination choice without changing anything."""
    return placement.preview_placement(
        ws.db,
        review_id,
        target_root_id=target_root_id,
        target_subdir=target_subdir,
        filename=filename,
    )


def placement_targets(ws: Workspace, review_id: str) -> list[Root]:
    """Return allowed target roots for one reviewed document."""
    return placement.placement_targets(ws.db, review_id)


def apply_placement(
    ws: Workspace,
    review_id: str,
    *,
    target_root_id: str,
    target_subdir: str = "",
    filename: str | None = None,
) -> placement.PlacementResult:
    """Apply one confirmed destination move through the journal."""
    return placement.apply_placement(
        ws,
        review_id,
        target_root_id=target_root_id,
        target_subdir=target_subdir,
        filename=filename,
    )


def seed_trusted_examples(
    ws: Workspace, root_ids: list[str] | None = None
) -> onboarding.TrustedSeedResult:
    """Seed auditable placement examples from trusted scopes under the lock."""
    with ws.lock():
        return onboarding.seed_trusted_examples(ws.db, root_ids=root_ids)


def resolve_label(ws: Workspace, review_id: str, *, fields: dict[str, str]) -> examples.Example:
    """Store a one-off confirmed label under the writer lock."""
    with ws.lock():
        return review.resolve_label(ws.db, review_id, fields=fields)


def resolve_negative(ws: Workspace, review_id: str, *, fields: dict[str, str]) -> examples.Example:
    """Record a rejected pattern as a negative example under the writer lock."""
    with ws.lock():
        return review.resolve_negative(ws.db, review_id, fields=fields)


def resolve_manual_move(ws: Workspace, review_id: str, *, accept: bool) -> examples.Example | None:
    """Confirm or reject a unique manual move under the writer lock."""
    with ws.lock():
        return review.resolve_manual_move(ws.db, review_id, accept=accept)


def resolve_naming(ws: Workspace, review_id: str, *, template: str) -> rules.Rule:
    """Replace a rule template with a tested-required draft revision."""
    with ws.lock():
        return review.resolve_naming(ws.db, review_id, template=template)


def dismiss_review(ws: Workspace, review_id: str, *, note: str = "") -> None:
    """Resolve one review item without an example, under the writer lock."""
    with ws.lock():
        review.dismiss_review(ws.db, review_id, note=note)


def propose_rule_candidates(ws: Workspace) -> list[learning.RuleCandidate]:
    """Return deterministic draft candidates from confirmed examples."""
    return learning.propose_candidates(ws.db)


def create_rule_from_candidate(ws: Workspace, candidate: learning.RuleCandidate) -> rules.Rule:
    """Persist a learned candidate as a draft rule under the writer lock."""
    with ws.lock():
        return learning.create_draft_from_candidate(ws.db, candidate)


def disable_rule(ws: Workspace, rule_id: str, *, reason: str = "") -> rules.Rule:
    """Disable a rule under the writer lock."""
    with ws.lock():
        return rule_maintenance.disable_rule(ws.db, rule_id, reason=reason)


def rollback_rule(ws: Workspace, rule_id: str, *, revision: int | None = None) -> rules.Rule:
    """Roll a rule back to an earlier revision under the writer lock."""
    with ws.lock():
        return rule_maintenance.rollback_rule(ws.db, rule_id, revision=revision)


def propose_structure(ws: Workspace, draft: structure.StructureDraft) -> structure.StructurePlan:
    """Persist a structure draft without changing files, under the writer lock."""
    with ws.lock():
        return structure.propose_structure(ws.db, draft)


def apply_structure(ws: Workspace, plan_id: str) -> structure.StructureApplyResult:
    """Apply a structure plan through the journal under the writer lock."""
    return structure.apply_structure(ws, plan_id)


def propose_dialog(
    ws: Workspace,
    *,
    message: str,
    completer: dialog.DialogCompleter,
    model: str,
    root_ids: list[str],
    output_mode: OutputMode = "validated_prompt",
    keep_alive: str | None = None,
    history: list[str] | None = None,
) -> dialog.DialogProposal:
    """Ask for and persist a bounded local proposal under the writer lock."""
    with ws.lock():
        return dialog.propose(
            ws.db,
            message=message,
            completer=completer,
            model=model,
            root_ids=root_ids,
            output_mode=output_mode,
            keep_alive=keep_alive,
            history=history,
        )


def apply_dialog(
    ws: Workspace, proposal_id: str, *, confirm: bool, decision: str = ""
) -> dict[str, object]:
    """Apply a confirmed local proposal under the writer lock."""
    with ws.lock():
        return dialog.apply_proposal(ws.db, proposal_id, confirm=confirm, decision=decision)


def configure_contracts(
    ws: Workspace, root_id: str, *, confirmed: bool, base_path: str | None = None
) -> list[contracts.ContractRecord]:
    """Confirm the folder contract convention under the writer lock."""
    with ws.lock():
        return contracts.configure_contracts(
            ws.db, root_id, confirmed=confirmed, base_path=base_path
        )


def create_contract(
    ws: Workspace,
    *,
    root_id: str,
    relative_path: str,
    alias: str = "",
    description: str = "",
) -> contracts.ContractRecord:
    """Create one explicit manual contract group under the writer lock."""
    with ws.lock():
        return contracts.create_contract(
            ws.db,
            root_id=root_id,
            relative_path=relative_path,
            alias=alias,
            description=description,
        )


def assign_contract_member(
    ws: Workspace, contract_id: str, *, root_id: str, relative_path: str
) -> contracts.ContractMember:
    """Add an explicit contract membership exception under the writer lock."""
    with ws.lock():
        return contracts.assign_member(
            ws.db, contract_id, root_id=root_id, relative_path=relative_path
        )


def set_contract_alias(ws: Workspace, contract_id: str, alias: str) -> contracts.ContractRecord:
    """Set a safe local contract alias under the writer lock."""
    with ws.lock():
        return contracts.set_alias(ws.db, contract_id, alias)


def refresh_contracts(
    ws: Workspace, *, root_id: str | None = None, contract_ids: list[str] | None = None
) -> contracts.ContractRefreshResult:
    """Refresh source-backed contract facts under the writer lock."""
    with ws.lock():
        return contracts.refresh_contracts(ws.db, root_id=root_id, contract_ids=contract_ids)


def set_contract_fact(
    ws: Workspace, contract_id: str, field: str, value: str
) -> contracts.ContractFactRecord:
    """Record a user-confirmed contract fact under the writer lock."""
    with ws.lock():
        return contracts.set_user_fact(ws.db, contract_id, field, value)


def run_service_tick(ws: Workspace) -> scheduler.TickResult:
    """Run one bounded scheduled reconciliation tick."""
    return scheduler.run_tick(ws)


def pause_service(ws: Workspace) -> None:
    """Pause scheduled ticks under the writer lock."""
    with ws.lock():
        scheduler.pause_service(ws.db)


def resume_service(ws: Workspace) -> None:
    """Resume scheduled ticks under the writer lock."""
    with ws.lock():
        scheduler.resume_service(ws.db)
