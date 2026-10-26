"""Ensure routing decisions never rebase stale facts onto changed source bytes."""

from pathlib import Path

import pytest

from renamr.benchmark_schema import RulePredicate
from renamr.core import mutation, workflow
from renamr.core.extract_runner import InProcessRunner
from renamr.core.roots import Root, RootRole
from renamr.core.workspace import Workspace
from tests.unit.core_support import FakeExtractor


def _indexed_source(tmp_path: Path) -> tuple[Workspace, Root, Path]:
    workspace = Workspace.bootstrap(tmp_path / "state")
    workspace.runner = InProcessRunner()
    workspace.extractor = FakeExtractor()
    inbox, library = tmp_path / "inbox", tmp_path / "library"
    inbox.mkdir()
    library.mkdir()
    source_root = workflow.register_root(workspace, inbox, role=RootRole.INBOX)
    target_root = workflow.register_root(workspace, library, role=RootRole.LIBRARY)
    workflow.set_allowed_targets(workspace, source_root.root_id, [target_root.root_id])
    document = inbox / "document.txt"
    document.write_text("Rechnung von Beispiel", encoding="utf-8")
    workflow.scan(workspace, source_root.root_id)
    rule = workflow.create_rule_draft(
        workspace,
        scope_root_id=source_root.root_id,
        target_root_id=target_root.root_id,
        predicates=[RulePredicate(field="document_type", operator="equals", values=["invoice"])],
        filename_template="{subject}",
    )
    workflow.mark_tested(workspace, rule.rule_id)
    workflow.activate_rule(workspace, rule.rule_id)
    return workspace, source_root, document


@pytest.mark.parametrize("change", ["edited", "removed"])
def test_preview_rejects_source_changes_since_indexing(tmp_path: Path, change: str) -> None:
    """Require a fresh catalog revision before planning any changed source."""
    workspace, root, document = _indexed_source(tmp_path)
    if change == "edited":
        document.write_text("A different document with unrelated contents.", encoding="utf-8")
    else:
        document.unlink()

    run_id = mutation.preview_routing(workspace, root.root_id)

    assert mutation.run_items(workspace, run_id) == []
    reviews = workflow.review_items(workspace)
    assert any(item["kind"] == "source_changed_since_scan" for item in reviews)
    if change == "edited":
        assert (
            document.read_text(encoding="utf-8") == "A different document with unrelated contents."
        )
    else:
        assert not document.exists()
