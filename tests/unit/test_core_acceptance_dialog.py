from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from renamr.benchmark_schema import RulePredicate
from renamr.core import (
    catalog,
    dialog,
    dialog_context,
    dialog_render,
    mutation,
    review,
    workflow,
)
from renamr.core.errors import CoreError, RootError
from renamr.core.roots import RootRole
from tests.unit.acceptance_support import FakeDialogCompleter, _roots, _scan
from tests.unit.core_support import FakeExtractor


def test_dialog_context_is_real_tree_and_history_is_bounded(tmp_path: Path) -> None:
    workspace, inbox_root, _, archive_root = _roots(tmp_path)
    (tmp_path / "Inbox" / "Invoices").mkdir()
    (tmp_path / "Inbox" / "Invoices" / "scan.txt").write_text("Rechnung von Beispiel")
    _scan(workspace, inbox_root.root_id, FakeExtractor(document_type="invoice"))

    context = dialog_context.build_context(workspace.db, root_ids=[inbox_root.root_id])
    assert "folder=Invoices docs=1 invoice:1" in context
    assert "location=loc_" in context
    with pytest.raises(RootError):
        dialog_context.build_context(workspace.db, root_ids=["root_missing"])

    response = dialog.DialogResponse(intent="explain", explanation="read only")
    completer = FakeDialogCompleter(response)
    dialog.propose(
        workspace.db,
        message="No, use the other folder.",
        completer=completer,
        model="maint:1",
        root_ids=[inbox_root.root_id],
        history=["user=Route invoices intent=propose_rule"],
    )
    assert "Recent ephemeral turns" in completer.messages[-1].content
    assert "user=Route invoices" in completer.messages[-1].content
    assert archive_root.root_id


def test_dialog_renders_validated_rule_payload_and_counts(tmp_path: Path) -> None:
    workspace, inbox_root, _, archive_root = _roots(tmp_path)
    response = dialog.DialogResponse(
        intent="propose_rule",
        explanation="Misleading explanation.",
        rule=dialog.RuleDraft(
            scope_root_id=inbox_root.root_id,
            target_root_id=archive_root.root_id,
            target_subdir="Invoices",
            predicates=[
                RulePredicate(field="document_type", operator="equals", values=["invoice"])
            ],
            filename_template="{date}_{subject}",
        ),
    )
    proposal = dialog.propose(
        workspace.db,
        message="Route invoices.",
        completer=FakeDialogCompleter(response),
        model="maint:1",
        root_ids=[inbox_root.root_id],
    )

    lines = dialog_render.describe_proposal(workspace.db, proposal)

    rendered = "\n".join(lines)
    assert f"target_root={archive_root.root_id}" in rendered
    assert "target_folder=Invoices" in rendered
    assert "predicates=document_type equals invoice" in rendered
    assert "affected_documents=" in rendered
    assert "Misleading explanation" not in rendered


def test_dialog_label_proposal_is_stale_after_facts_change(tmp_path: Path) -> None:
    workspace, inbox_root, _, archive_root = _roots(tmp_path)
    (tmp_path / "Inbox" / "scan.txt").write_text("Rechnung von Beispiel")
    _scan(workspace, inbox_root.root_id, FakeExtractor(document_type="invoice"))
    mutation.preview_routing(workspace, inbox_root.root_id)
    question = next(
        item for item in review.review_questions(workspace.db) if item.kind == "unmatched_placement"
    )
    location = catalog.get_location(workspace.db, cast("str", question.location_id))
    response = dialog.DialogResponse(
        intent="label_documents",
        labels=[dialog.LabelDraft(location_id=location.location_id, document_type="receipt")],
    )
    proposal = dialog.propose(
        workspace.db,
        message="This is a receipt.",
        completer=FakeDialogCompleter(response),
        model="maint:1",
        root_ids=[inbox_root.root_id],
    )
    workflow.resolve_label(
        workspace, question.review_id, fields={"document_type": "invoice", "issuer": "Beispiel"}
    )

    with pytest.raises(CoreError, match="stale"):
        dialog.apply_proposal(workspace.db, proposal.proposal_id, confirm=True)
    assert archive_root.root_id


def test_pending_route_blocks_when_higher_priority_rule_appears(tmp_path: Path) -> None:
    workspace, inbox_root, _, archive_root = _roots(tmp_path)
    second = tmp_path / "Archive2"
    second.mkdir()
    second_root = workflow.register_root(workspace, second, role=RootRole.ARCHIVE)
    workflow.set_allowed_targets(
        workspace,
        inbox_root.root_id,
        [archive_root.root_id, second_root.root_id],
    )
    source = tmp_path / "Inbox" / "scan.txt"
    source.write_text("Rechnung von Beispiel")
    _scan(workspace, inbox_root.root_id, FakeExtractor(document_type="invoice"))
    first_rule = workflow.create_rule_draft(
        workspace,
        scope_root_id=inbox_root.root_id,
        target_root_id=archive_root.root_id,
        predicates=[RulePredicate(field="document_type", operator="equals", values=["invoice"])],
        filename_template="{subject}",
    )
    workflow.mark_tested(workspace, first_rule.rule_id)
    workflow.activate_rule(workspace, first_rule.rule_id)
    run_id = mutation.preview_routing(workspace, inbox_root.root_id)

    better = workflow.create_rule_draft(
        workspace,
        scope_root_id=inbox_root.root_id,
        target_root_id=second_root.root_id,
        predicates=[RulePredicate(field="document_type", operator="equals", values=["invoice"])],
        filename_template="{subject}",
        priority=10,
    )
    workflow.mark_tested(workspace, better.rule_id)
    workflow.activate_rule(workspace, better.rule_id)

    mutation.apply(workspace, run_id)

    items = mutation.run_items(workspace, run_id)
    assert [item.state for item in items] == ["blocked"]
    assert source.exists()
    assert not (tmp_path / "Archive" / "Rechnung").exists()
