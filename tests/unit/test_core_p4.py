"""P4 tests: review learning, manual moves, dialog confirmation, and structure apply."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import cast

import pytest
from pydantic import BaseModel

from renamr.benchmark_schema import (
    FeatureValidation,
    RuleDraft,
    RulePredicate,
)
from renamr.core import dialog, examples, learning, mutation, review, rules, structure, workflow
from renamr.core.errors import CoreError, RuleError
from renamr.core.extract_runner import InProcessRunner
from renamr.core.features import FeatureExtraction, FeatureRequest
from renamr.core.roots import Root, RootRole
from renamr.core.settings import Settings
from renamr.core.workspace import Workspace
from renamr.local_transport import ChatMessage, ChatMetrics, OutputMode, StructuredResult
from tests.unit.core_support import FakeExtractor


class BlockingExtractor:
    """Extractor that deterministically blocks and opens a review item."""

    def extract(self, request: FeatureRequest) -> FeatureExtraction:
        return FeatureExtraction(
            validation=FeatureValidation(ok=False, reasons=["model_flagged_review"]),
            blocked_reason="model_flagged_review",
            incomplete=True,
        )


class FakeDialogCompleter:
    """Return one canned dialog response."""

    def __init__(self, response: dialog.DialogResponse) -> None:
        self.response = response
        self.calls = 0

    def complete[ResultT: BaseModel](
        self,
        model: str,
        messages: Sequence[ChatMessage],
        schema: type[ResultT],
        *,
        output_mode: OutputMode | None = None,
        images: Sequence[str] = (),
        require_vision: bool = False,
        keep_alive: str | None = None,
    ) -> StructuredResult[ResultT]:
        self.calls += 1
        return StructuredResult[ResultT](
            data=cast("ResultT", self.response),
            metrics=ChatMetrics(),
            output_mode=output_mode or "validated_prompt",
        )


def _workspace(tmp_path: Path) -> Workspace:
    return Workspace.bootstrap(tmp_path / "state", config_path=tmp_path / "config.toml")


def _roots(workspace: Workspace, tmp_path: Path) -> tuple[Root, Root]:
    inbox, archive = tmp_path / "inbox", tmp_path / "archive"
    inbox.mkdir()
    archive.mkdir()
    inbox_root = workflow.register_root(workspace, inbox, role=RootRole.INBOX)
    archive_root = workflow.register_root(workspace, archive, role=RootRole.ARCHIVE)
    workflow.set_allowed_targets(workspace, inbox_root.root_id, [archive_root.root_id])
    return inbox_root, archive_root


def _label_review(workspace: Workspace, tmp_path: Path) -> tuple[str, str]:
    inbox_root, _ = _roots(workspace, tmp_path)
    (tmp_path / "inbox" / "note.txt").write_text("Beispiel Rechnung")
    workspace.runner = InProcessRunner()
    workspace.extractor = BlockingExtractor()
    workflow.scan(workspace, inbox_root.root_id)
    question = next(
        item for item in review.review_questions(workspace.db) if item.kind == "feature_blocked"
    )
    return cast("str", question.review_id), cast("str", question.revision_id)


def test_label_creates_example_but_never_a_rule(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    review_id, _ = _label_review(workspace, tmp_path)

    example = workflow.resolve_label(
        workspace,
        review_id,
        fields={"document_type": "invoice", "issuer": "Beispiel GmbH", "date": "2026-03-14"},
    )

    assert example.source == "user_label"
    assert workflow.list_rules(workspace) == []
    assert examples.list_examples(workspace.db)[0].payload["document_type"] == "invoice"


def test_unique_manual_move_is_confirmed_as_example(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    inbox_root, _ = _roots(workspace, tmp_path)
    document = tmp_path / "inbox" / "note.txt"
    document.write_text("Rechnung von Beispiel")
    workspace.runner = InProcessRunner()
    workspace.extractor = FakeExtractor(document_type="invoice")
    workflow.scan(workspace, inbox_root.root_id)

    document.rename(tmp_path / "inbox" / "renamed.txt")
    workflow.scan(workspace, inbox_root.root_id)
    observations = workflow.observe_manual_moves(workspace)
    assert [item.status for item in observations] == ["candidate"]

    question = next(
        item for item in review.review_questions(workspace.db) if item.kind == "possible_move"
    )
    example = workflow.resolve_manual_move(workspace, question.review_id, accept=True)

    assert example is not None
    assert example.source == "manual_move"
    assert workflow.list_rules(workspace) == []


def test_ambiguous_move_does_not_become_an_example(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    inbox_root, _ = _roots(workspace, tmp_path)
    document = tmp_path / "inbox" / "note.txt"
    document.write_text("same bytes")
    workspace.runner = InProcessRunner()
    workspace.extractor = FakeExtractor(document_type="invoice")
    workflow.scan(workspace, inbox_root.root_id)

    document.unlink()
    (tmp_path / "inbox" / "copy-a.txt").write_text("same bytes")
    (tmp_path / "inbox" / "copy-b.txt").write_text("same bytes")
    workflow.scan(workspace, inbox_root.root_id)
    observations = workflow.observe_manual_moves(workspace)

    assert any(item.status == "ambiguous" for item in observations)
    assert examples.list_examples(workspace.db) == []


def test_own_move_is_not_a_training_example(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    inbox_root, archive_root = _roots(workspace, tmp_path)
    document = tmp_path / "inbox" / "note.txt"
    document.write_text("Rechnung von Beispiel")
    workspace.runner = InProcessRunner()
    workspace.extractor = FakeExtractor(document_type="invoice")
    workflow.scan(workspace, inbox_root.root_id)
    draft = workflow.create_rule_draft(
        workspace,
        scope_root_id=inbox_root.root_id,
        target_root_id=archive_root.root_id,
        predicates=[RulePredicate(field="document_type", operator="equals", values=["invoice"])],
        filename_template="{date}_{subject}",
    )
    workflow.mark_tested(workspace, draft.rule_id)
    workflow.activate_rule(workspace, draft.rule_id)
    run_id = mutation.preview_routing(workspace, inbox_root.root_id)
    assert mutation.apply(workspace, run_id).committed == 1

    moved = next((tmp_path / "archive").iterdir())
    moved.rename(moved.with_name("user-name.txt"))
    workflow.scan(workspace, archive_root.root_id)
    observations = workflow.observe_manual_moves(workspace)

    assert observations == []
    assert examples.list_examples(workspace.db) == []


def test_missing_naming_value_stays_in_review(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    inbox_root, archive_root = _roots(workspace, tmp_path)
    (tmp_path / "inbox" / "note.txt").write_text("Rechnung ohne Datum")
    workspace.runner = InProcessRunner()
    workspace.extractor = FakeExtractor(document_type="invoice", date=None)
    workflow.scan(workspace, inbox_root.root_id)
    draft = workflow.create_rule_draft(
        workspace,
        scope_root_id=inbox_root.root_id,
        target_root_id=archive_root.root_id,
        predicates=[RulePredicate(field="document_type", operator="equals", values=["invoice"])],
        filename_template="{date}_{subject}",
    )
    workflow.mark_tested(workspace, draft.rule_id)
    workflow.activate_rule(workspace, draft.rule_id)

    run_id = mutation.preview_routing(workspace, inbox_root.root_id)

    assert mutation.run_items(workspace, run_id) == []
    kinds = {item["kind"] for item in workflow.review_items(workspace)}
    assert "naming_field_missing" in kinds


def test_learning_proposes_a_draft_after_a_confirmed_move(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    inbox_root, _ = _roots(workspace, tmp_path)
    document = tmp_path / "inbox" / "note.txt"
    document.write_text("Rechnung von Beispiel")
    workspace.runner = InProcessRunner()
    workspace.extractor = FakeExtractor(document_type="invoice")
    workflow.scan(workspace, inbox_root.root_id)
    document.rename(tmp_path / "inbox" / "renamed.txt")
    workflow.scan(workspace, inbox_root.root_id)
    workflow.observe_manual_moves(workspace)
    question = next(
        item for item in review.review_questions(workspace.db) if item.kind == "possible_move"
    )
    workflow.resolve_manual_move(workspace, question.review_id, accept=True)

    candidates = learning.propose_candidates(workspace.db)

    assert candidates
    assert candidates[0].predicates[0].values == ["invoice"]
    assert candidates[0].impact.positive_examples >= 1


def _rule_response(inbox_id: str, archive_id: str) -> dialog.DialogResponse:
    return dialog.DialogResponse(
        intent="propose_rule",
        explanation="Route invoices to the archive.",
        rule=RuleDraft(
            scope_root_id=inbox_id,
            target_root_id=archive_id,
            target_subdir="invoices",
            predicates=[
                RulePredicate(field="document_type", operator="equals", values=["invoice"])
            ],
            filename_template="{date}_{subject}",
        ),
    )


def test_dialog_requires_confirmation_and_creates_only_a_draft(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    inbox_root, archive_root = _roots(workspace, tmp_path)
    completer = FakeDialogCompleter(_rule_response(inbox_root.root_id, archive_root.root_id))
    proposal = dialog.propose(
        workspace.db,
        message="Put invoices in the archive.",
        completer=completer,
        model="gemma4:e4b",
        root_ids=[inbox_root.root_id],
    )

    with pytest.raises(CoreError, match="confirmation"):
        dialog.apply_proposal(workspace.db, proposal.proposal_id, confirm=False)

    outcome = dialog.apply_proposal(workspace.db, proposal.proposal_id, confirm=True)
    created = rules.get_rule(workspace.db, str(outcome["rule_id"]))
    assert created.status is rules.RuleStatus.DRAFT


def test_dialog_rejects_unknown_root_and_stale_context(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    inbox_root, archive_root = _roots(workspace, tmp_path)
    unknown = _rule_response("root_missing", archive_root.root_id)
    with pytest.raises(CoreError):
        dialog.propose(
            workspace.db,
            message="Bad ids.",
            completer=FakeDialogCompleter(unknown),
            model="gemma4:e4b",
            root_ids=[inbox_root.root_id],
        )

    completer = FakeDialogCompleter(_rule_response(inbox_root.root_id, archive_root.root_id))
    proposal = dialog.propose(
        workspace.db,
        message="Route invoices.",
        completer=completer,
        model="gemma4:e4b",
        root_ids=[inbox_root.root_id],
    )
    workflow.set_settings(workspace, Settings(languages=["en"]))
    with pytest.raises(CoreError, match="stale"):
        dialog.apply_proposal(workspace.db, proposal.proposal_id, confirm=True)


def test_structure_moves_only_explicit_mapping_and_leaves_hidden_files(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    library = tmp_path / "library"
    (library / "old").mkdir(parents=True)
    (library / "keep").mkdir()
    (library / "old" / "doc.txt").write_text("old document")
    (library / "old" / ".hidden.txt").write_text("hidden")
    (library / "keep" / "other.txt").write_text("other document")
    root = workflow.register_root(workspace, library, role=RootRole.LIBRARY)
    workspace.runner = InProcessRunner()
    workspace.extractor = FakeExtractor(document_type="invoice")
    workflow.scan(workspace, root.root_id)

    plan = workflow.propose_structure(
        workspace,
        structure.StructureDraft(
            root_id=root.root_id,
            mappings=[structure.FolderMapping(source="old", target="new")],
        ),
    )
    result = workflow.apply_structure(workspace, plan.plan_id)

    assert result.state == "applied"
    assert (library / "new" / "doc.txt").exists()
    assert (library / "keep" / "other.txt").exists()
    assert (library / "old" / ".hidden.txt").exists()
    assert (library / "old").is_dir()


def test_structure_blocks_when_active_rules_changed(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    library = tmp_path / "library"
    (library / "old").mkdir(parents=True)
    (library / "old" / "doc.txt").write_text("old document")
    root = workflow.register_root(workspace, library, role=RootRole.LIBRARY)
    plan = workflow.propose_structure(
        workspace,
        structure.StructureDraft(
            root_id=root.root_id,
            mappings=[structure.FolderMapping(source="old", target="new")],
        ),
    )
    inbox, archive = tmp_path / "inbox", tmp_path / "archive"
    inbox.mkdir()
    archive.mkdir()
    inbox_root = workflow.register_root(workspace, inbox, role=RootRole.INBOX)
    archive_root = workflow.register_root(workspace, archive, role=RootRole.ARCHIVE)
    workflow.set_allowed_targets(workspace, inbox_root.root_id, [archive_root.root_id])
    draft = workflow.create_rule_draft(
        workspace,
        scope_root_id=inbox_root.root_id,
        target_root_id=archive_root.root_id,
        predicates=[RulePredicate(field="document_type", operator="equals", values=["invoice"])],
        filename_template="{subject}",
    )
    workflow.mark_tested(workspace, draft.rule_id)
    workflow.activate_rule(workspace, draft.rule_id)

    with pytest.raises(RuleError, match="changed"):
        workflow.apply_structure(workspace, plan.plan_id)
    assert (library / "old" / "doc.txt").exists()


def test_negative_example_is_counted_and_never_a_candidate(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    inbox_root, _ = _roots(workspace, tmp_path)
    first = tmp_path / "inbox" / "note.txt"
    first.write_text("Rechnung von Beispiel")
    workspace.runner = InProcessRunner()
    workspace.extractor = FakeExtractor(document_type="invoice")
    workflow.scan(workspace, inbox_root.root_id)
    first.rename(tmp_path / "inbox" / "renamed.txt")
    workflow.scan(workspace, inbox_root.root_id)
    workflow.observe_manual_moves(workspace)
    question = next(
        item for item in review.review_questions(workspace.db) if item.kind == "possible_move"
    )
    workflow.resolve_manual_move(workspace, question.review_id, accept=True)

    second = tmp_path / "inbox" / "rejected.txt"
    second.write_text("Keine Rechnung")
    workspace.extractor = BlockingExtractor()
    workflow.scan(workspace, inbox_root.root_id)
    blocked = next(
        item for item in review.review_questions(workspace.db) if item.kind == "feature_blocked"
    )
    workflow.resolve_negative(workspace, blocked.review_id, fields={"document_type": "invoice"})

    candidates = learning.propose_candidates(workspace.db)

    assert candidates
    assert any(item.impact.negative_examples >= 1 for item in candidates)
    assert all(item.example_ids for item in candidates)


class _Crash(Exception):
    """Injected structure failure."""


def test_structure_recovers_a_crash_before_marking_applied(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    library = tmp_path / "library"
    (library / "old").mkdir(parents=True)
    (library / "old" / "doc.txt").write_text("old document")
    root = workflow.register_root(workspace, library, role=RootRole.LIBRARY)
    workspace.runner = InProcessRunner()
    workspace.extractor = FakeExtractor(document_type="invoice")
    workflow.scan(workspace, root.root_id)
    plan = workflow.propose_structure(
        workspace,
        structure.StructureDraft(
            root_id=root.root_id,
            mappings=[structure.FolderMapping(source="old", target="new")],
        ),
    )

    def fault(where: str) -> None:
        if where == "after_stage":
            raise _Crash(where)

    with pytest.raises(_Crash):
        structure.apply_structure(workspace, plan.plan_id, fault=fault)
    result = workflow.apply_structure(workspace, plan.plan_id)

    assert result.state == "applied"
    assert (library / "new" / "doc.txt").exists()
    assert not (library / "old" / "doc.txt").exists()
