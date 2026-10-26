"""Regressions: stale or failed feature stages cannot drive automatic routing."""

from __future__ import annotations

from pathlib import Path

from renamr.benchmark_schema import DocumentFeatures, RulePredicate
from renamr.core import catalog, mutation, workflow
from renamr.core.facts import FactRecord
from renamr.core.features import FeatureExtraction, FeatureValidation
from renamr.core.roots import Root
from renamr.core.workspace import Workspace
from tests.unit.acceptance_support import _roots, _scan
from tests.unit.core_support import FakeExtractor


class PartialExtractor:
    """Return validated-looking features marked incomplete."""

    def extract(self, request: object) -> FeatureExtraction:
        return FeatureExtraction(
            features=DocumentFeatures(
                document_type="invoice", subject="Rechnung", needs_review=False
            ),
            validation=FeatureValidation(ok=True),
            incomplete=True,
            blocked_reason="inference_failed",
            chunks=1,
        )


class FailedExtractor:
    """Return an explicit incomplete failure without features."""

    def extract(self, request: object) -> FeatureExtraction:
        return FeatureExtraction(
            validation=FeatureValidation(ok=False, reasons=["inference_failed"]),
            blocked_reason="inference_failed",
            incomplete=True,
            chunks=1,
        )


class BlockedExtractor:
    """Return a deterministic feature abstention."""

    def extract(self, request: object) -> FeatureExtraction:
        return FeatureExtraction(
            validation=FeatureValidation(ok=False, reasons=["model_flagged_review"]),
            blocked_reason="model_flagged_review",
            incomplete=True,
            chunks=1,
        )


def _prepared(tmp_path: Path) -> tuple[Workspace, Root, Root, Path]:
    workspace, inbox_root, _, archive_root = _roots(tmp_path)
    document = tmp_path / "Inbox" / "scan.txt"
    document.write_text("Rechnung von Beispiel")
    _scan(workspace, inbox_root.root_id, FakeExtractor(document_type="invoice"))
    rule = workflow.create_rule_draft(
        workspace,
        scope_root_id=inbox_root.root_id,
        target_root_id=archive_root.root_id,
        predicates=[RulePredicate(field="document_type", operator="equals", values=["invoice"])],
        filename_template="{subject}",
    )
    workflow.mark_tested(workspace, rule.rule_id)
    workflow.activate_rule(workspace, rule.rule_id)
    return workspace, inbox_root, archive_root, document


def _facts(workspace: Workspace, document: Path) -> dict[str, str | None]:
    location = catalog.get_location_by_path(workspace.db, document)
    assert location is not None
    return {
        fact.field: fact.value for fact in catalog.get_facts(workspace.db, location.revision_id)
    }


def test_partial_refresh_blocks_fresh_preview_but_retains_old_facts(tmp_path: Path) -> None:
    workspace, inbox_root, _, document = _prepared(tmp_path)
    workspace.model_digest = "new-model-digest"
    _scan(workspace, inbox_root.root_id, PartialExtractor())

    run_id = mutation.preview_routing(workspace, inbox_root.root_id)

    assert mutation.run_items(workspace, run_id) == []
    assert _facts(workspace, document)["document_type"] == "invoice"
    assert any(item["kind"] == "features_unresolved" for item in workflow.review_items(workspace))


def test_failed_refresh_blocks_fresh_preview(tmp_path: Path) -> None:
    workspace, inbox_root, _, document = _prepared(tmp_path)
    workspace.model_digest = "new-model-digest"
    _scan(workspace, inbox_root.root_id, FailedExtractor())

    run_id = mutation.preview_routing(workspace, inbox_root.root_id)

    assert mutation.run_items(workspace, run_id) == []
    assert _facts(workspace, document)["document_type"] == "invoice"


def test_pending_plan_blocks_after_partial_or_failed_refresh(tmp_path: Path) -> None:
    for extractor in (PartialExtractor(), FailedExtractor()):
        case = tmp_path / type(extractor).__name__
        case.mkdir()
        workspace, inbox_root, _, document = _prepared(case)
        run_id = mutation.preview_routing(workspace, inbox_root.root_id)
        assert len(mutation.run_items(workspace, run_id)) == 1
        workspace.model_digest = "new-model-digest"
        _scan(workspace, inbox_root.root_id, extractor)

        mutation.apply(workspace, run_id)

        assert [item.state for item in mutation.run_items(workspace, run_id)] == ["blocked"]
        assert document.exists()
        assert not (case / "Archive" / "Rechnung.txt").exists()


def test_stale_feature_cache_alone_blocks_routing(tmp_path: Path) -> None:
    workspace, inbox_root, _, _ = _prepared(tmp_path)
    workspace.model_digest = "new-model-digest"

    run_id = mutation.preview_routing(workspace, inbox_root.root_id)

    assert mutation.run_items(workspace, run_id) == []


def test_blocked_refresh_keeps_old_and_user_confirmed_facts_private(tmp_path: Path) -> None:
    workspace, inbox_root, _, document = _prepared(tmp_path)
    location = catalog.get_location_by_path(workspace.db, document)
    assert location is not None
    catalog.set_facts(
        workspace.db,
        location.revision_id,
        [
            *catalog.get_facts(workspace.db, location.revision_id),
            FactRecord(
                field="issuer", value="User Confirmed AG", validation_state="user_confirmed"
            ),
        ],
    )
    workspace.model_digest = "new-model-digest"
    _scan(workspace, inbox_root.root_id, BlockedExtractor())

    run_id = mutation.preview_routing(workspace, inbox_root.root_id)

    assert mutation.run_items(workspace, run_id) == []
    facts = {
        (fact.field, fact.value, fact.validation_state)
        for fact in catalog.get_facts(workspace.db, location.revision_id)
    }
    assert ("issuer", "User Confirmed AG", "user_confirmed") in facts
    assert ("document_type", "invoice", "validated") in facts
