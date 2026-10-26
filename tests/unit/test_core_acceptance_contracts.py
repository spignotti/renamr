from __future__ import annotations

from pathlib import Path

from renamr.benchmark_schema import RulePredicate
from renamr.core import (
    catalog,
    contracts,
    examples,
    learning,
    review,
    structure,
    workflow,
)
from renamr.core.mcp_gateway import MCP_FIELDS
from renamr.core.workspace import Workspace
from tests.unit.acceptance_support import _roots, _scan


def test_entity_predicate_is_learned_from_confirmed_destination(tmp_path: Path) -> None:
    workspace, inbox_root, _, archive_root = _roots(tmp_path)
    examples.add_example(
        workspace.db,
        source=examples.ExampleSource.USER_DESTINATION,
        revision_id=None,
        location_id=None,
        payload={
            "document_type": "insurance policy",
            "entity_id": "ctr_stable_1",
            "new_path": str(tmp_path / "Archive" / "Insurance" / "policy.txt"),
            "scope_root_id": inbox_root.root_id,
        },
    )

    candidates = learning.propose_candidates(workspace.db)

    assert candidates
    assert candidates[0].target_root_id == archive_root.root_id
    assert any(
        predicate.field == "entity_id" and predicate.values == ["ctr_stable_1"]
        for predicate in candidates[0].predicates
    )


def test_manual_entity_label_survives_contract_bridge(tmp_path: Path) -> None:
    workspace, _, library_root, _ = _roots(tmp_path)
    document = tmp_path / "Library" / "contracts" / "a" / "initial.txt"
    document.parent.mkdir(parents=True)
    document.write_text("Vertragsnummer: V-1\nAnbieter: Beispiel AG\n")
    _scan(workspace, library_root.root_id)
    contract = workflow.configure_contracts(
        workspace, library_root.root_id, confirmed=True, base_path="contracts"
    )[0]
    location = catalog.get_location_by_path(workspace.db, document)
    assert location is not None
    facts = catalog.get_facts(workspace.db, location.revision_id)
    catalog.set_facts(
        workspace.db,
        location.revision_id,
        [
            *[item for item in facts if item.field != "entity_id"],
            catalog.FactRecord(
                field="entity_id", value="manual-entity", validation_state="user_confirmed"
            ),
        ],
    )

    workflow.refresh_contracts(workspace)

    entity = next(
        item
        for item in catalog.get_facts(workspace.db, location.revision_id)
        if item.field == "entity_id"
    )
    assert entity.value == "manual-entity"
    assert entity.validation_state == "user_confirmed"
    assert contract.contract_id


def _add_destination_example(
    workspace: Workspace, target: Path, scope_root_id: str, document_type: str = "invoice"
) -> None:
    examples.add_example(
        workspace.db,
        source=examples.ExampleSource.USER_DESTINATION,
        revision_id=None,
        location_id=None,
        payload={
            "document_type": document_type,
            "new_path": str(target),
            "scope_root_id": scope_root_id,
        },
    )


def test_learning_counts_different_destinations_and_scopes_as_conflicts(tmp_path: Path) -> None:
    workspace, inbox_root, library_root, archive_root = _roots(tmp_path)
    target = tmp_path / "Archive" / "Invoices" / "x.txt"
    _add_destination_example(workspace, target, inbox_root.root_id)
    _add_destination_example(
        workspace, tmp_path / "Archive" / "Receipts" / "x.txt", inbox_root.root_id
    )
    _add_destination_example(workspace, target, library_root.root_id)
    examples.add_example(
        workspace.db,
        source=examples.ExampleSource.NEGATIVE,
        revision_id=None,
        location_id=None,
        payload={"document_type": "invoice"},
    )
    candidate = learning.RuleCandidate(
        scope_root_id=inbox_root.root_id,
        target_root_id=archive_root.root_id,
        target_subdir="Invoices",
        predicates=[RulePredicate(field="document_type", operator="equals", values=["invoice"])],
        impact=learning.RuleImpact(),
    )

    impact = learning.evaluate(workspace.db, candidate)

    assert impact.positive_examples == 1
    assert impact.conflicts == 2
    assert impact.negative_examples == 1


def test_para_nested_contract_scope_and_archived_same_provider_group(
    tmp_path: Path,
) -> None:
    workspace, _, library_root, archive_root = _roots(tmp_path)
    folder = tmp_path / "Library" / "Personal" / "Contracts" / "Contract A"
    folder.mkdir(parents=True)
    (folder / "initial.txt").write_text(
        "Versicherungsschein\nAnbieter: Beispiel Versicherung AG\n"
        "Vertragsbeginn: 2026-01-01\nMindestlaufzeit: 12 Monate\n"
        "Ausgestellt am: 2025-12-15\n"
    )
    (folder / "amendment.txt").write_text(
        "Nachtrag zum Versicherungsvertrag\nAnbieter: Beispiel Versicherung AG\n"
        "Änderung ab: 2026-06-01\nAusgestellt am: 2026-05-20\n"
    )
    (folder / "cancellation.txt").write_text(
        "Kündigungsbestätigung\nBeispiel Versicherung AG\n"
        "Ausgestellt am: 2026-11-05\nVertragsende: 2026-12-31\n"
    )
    archived = tmp_path / "Archive" / "Contracts" / "Contract A"
    archived.mkdir(parents=True)
    (archived / "initial.txt").write_text(
        "Versicherungsschein\nAnbieter: Beispiel Versicherung AG\n"
        "Vertragsbeginn: 2026-01-01\nAusgestellt am: 2025-12-15\n"
    )
    _scan(workspace, library_root.root_id)
    _scan(workspace, archive_root.root_id)

    active = workflow.configure_contracts(
        workspace, library_root.root_id, confirmed=True, base_path="Personal/Contracts"
    )[0]
    archived_group = workflow.configure_contracts(
        workspace, archive_root.root_id, confirmed=True, base_path="Contracts"
    )[0]
    workflow.refresh_contracts(workspace)

    active_facts = {
        item.field: item for item in contracts.get_contract_facts(workspace.db, active.contract_id)
    }
    archived_facts = {
        item.field: item
        for item in contracts.get_contract_facts(workspace.db, archived_group.contract_id)
    }
    assert active.contract_id != archived_group.contract_id
    assert active_facts["status"].value == "cancelled"
    assert active_facts["minimum_end_date"].value == "2026-12-31"
    assert archived_facts["provider"].value == "Beispiel Versicherung AG"
    assert archived_facts["status"].origin == "unknown"


def test_contract_entity_bridge_matches_only_unique_grounded_references(
    tmp_path: Path,
) -> None:
    workspace, inbox_root, library_root, _ = _roots(tmp_path)
    documents = {
        "contracts/a/initial.txt": "Vertragsnummer: V-2026-001\nAnbieter: Beispiel AG\n",
        "contracts/b/initial.txt": "Vertragsnummer: V-2026-002\nAnbieter: Beispiel AG\n",
    }
    for relative, text in documents.items():
        target = tmp_path / "Library" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    inbox_document = tmp_path / "Inbox" / "new.txt"
    inbox_document.write_text("Vertragsnummer: V-2026-001\n")
    _scan(workspace, library_root.root_id)
    _scan(workspace, inbox_root.root_id)

    created = workflow.configure_contracts(
        workspace, library_root.root_id, confirmed=True, base_path="contracts"
    )
    contract_a = next(item for item in created if item.members[0].relative_path == "contracts/a")
    contract_b = next(item for item in created if item.members[0].relative_path == "contracts/b")
    workflow.refresh_contracts(workspace)

    def entity_for(path: Path) -> str | None:
        location = catalog.get_location_by_path(workspace.db, path)
        assert location is not None
        return next(
            (
                fact.value
                for fact in catalog.get_facts(workspace.db, location.revision_id)
                if fact.field == "entity_id"
            ),
            None,
        )

    assert entity_for(tmp_path / "Library" / "contracts" / "a" / "initial.txt") == (
        contract_a.contract_id
    )
    assert entity_for(tmp_path / "Library" / "contracts" / "b" / "initial.txt") == (
        contract_b.contract_id
    )
    assert entity_for(inbox_document) == contract_a.contract_id
    assert "reference" not in MCP_FIELDS
    assert "entity_id" not in MCP_FIELDS


def test_shared_reference_is_ambiguous_and_does_not_bridge(tmp_path: Path) -> None:
    workspace, inbox_root, library_root, _ = _roots(tmp_path)
    for folder in ("a", "b"):
        target = tmp_path / "Library" / "contracts" / folder / "initial.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"Kundennummer: K-999\nVertragsnummer: V-{folder}\n")
    shared = tmp_path / "Inbox" / "shared.txt"
    shared.write_text("Kundennummer: K-999\n")
    _scan(workspace, library_root.root_id)
    _scan(workspace, inbox_root.root_id)
    workflow.configure_contracts(
        workspace, library_root.root_id, confirmed=True, base_path="contracts"
    )
    workflow.refresh_contracts(workspace)

    location = catalog.get_location_by_path(workspace.db, shared)
    assert location is not None
    assert not any(
        fact.field == "entity_id" for fact in catalog.get_facts(workspace.db, location.revision_id)
    )
    assert any(
        item["kind"] == "contract_entity_ambiguous" for item in workflow.review_items(workspace)
    )


def test_nested_contract_scope_and_structure_move_keep_stable_id(tmp_path: Path) -> None:
    workspace, _, library_root, archive_root = _roots(tmp_path)
    for folder in ("A", "B"):
        target = tmp_path / "Library" / "Personal" / "Contracts" / folder / "initial.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"Vertragsnummer: {folder}\nAnbieter: Beispiel AG\n")
    archived = tmp_path / "Archive" / "Contracts" / "A" / "initial.txt"
    archived.parent.mkdir(parents=True, exist_ok=True)
    archived.write_text("Vertragsnummer: A\nAnbieter: Beispiel AG\n")
    _scan(workspace, library_root.root_id)
    _scan(workspace, archive_root.root_id)

    created = workflow.configure_contracts(
        workspace, library_root.root_id, confirmed=True, base_path="Personal/Contracts"
    )
    assert {item.members[0].relative_path for item in created} == {
        "Personal/Contracts/A",
        "Personal/Contracts/B",
    }
    contract_a = next(item for item in created if item.members[0].relative_path.endswith("/A"))
    archived_groups = workflow.configure_contracts(
        workspace, archive_root.root_id, confirmed=True, base_path="Contracts"
    )
    assert archived_groups[0].contract_id != contract_a.contract_id

    plan = workflow.propose_structure(
        workspace,
        structure.StructureDraft(
            root_id=library_root.root_id,
            mappings=[
                structure.FolderMapping(
                    source="Personal/Contracts/A", target="Personal/Contracts/A-renamed"
                )
            ],
        ),
    )
    workflow.apply_structure(workspace, plan.plan_id)

    current = contracts.get_contract(workspace.db, contract_a.contract_id)
    assert current.members[0].relative_path == "Personal/Contracts/A-renamed"


def test_confirmed_manual_move_keeps_file_membership(tmp_path: Path) -> None:
    workspace, _, library_root, archive_root = _roots(tmp_path)
    document = tmp_path / "Library" / "Docs" / "contract.txt"
    document.parent.mkdir()
    document.write_text("Vertragsnummer: V-1\n")
    _scan(workspace, library_root.root_id)
    contract = workflow.create_contract(
        workspace,
        root_id=library_root.root_id,
        relative_path="Docs/contract.txt",
        alias="manual-member",
    )
    document.rename(tmp_path / "Archive" / "contract.txt")
    _scan(workspace, library_root.root_id)
    _scan(workspace, archive_root.root_id)
    workflow.observe_manual_moves(workspace)
    question = next(
        item
        for item in review.review_questions(workspace.db)
        if item.kind in ("possible_move", "path_missing")
    )
    workflow.resolve_manual_move(workspace, question.review_id, accept=True)

    current = contracts.get_contract(workspace.db, contract.contract_id)
    assert current.members[0].root_id == archive_root.root_id
    assert current.members[0].relative_path == "contract.txt"


def test_confirmed_manual_folder_move_keeps_contract_id(tmp_path: Path) -> None:
    workspace, _, library_root, archive_root = _roots(tmp_path)
    folder = tmp_path / "Library" / "Contracts" / "A"
    folder.mkdir(parents=True)
    (folder / "initial.txt").write_text("Vertragsnummer: V-1\n")
    _scan(workspace, library_root.root_id)
    contract = workflow.configure_contracts(
        workspace, library_root.root_id, confirmed=True, base_path="Contracts"
    )[0]
    folder.rename(tmp_path / "Archive" / "A")
    _scan(workspace, library_root.root_id)
    _scan(workspace, archive_root.root_id)
    workflow.observe_manual_moves(workspace)
    question = next(
        item
        for item in review.review_questions(workspace.db)
        if item.kind in ("possible_move", "path_missing")
    )
    workflow.resolve_manual_move(workspace, question.review_id, accept=True)

    current = contracts.get_contract(workspace.db, contract.contract_id)
    assert current.contract_id == contract.contract_id
    assert current.members[0].root_id == archive_root.root_id
    assert current.members[0].relative_path == "A"


def test_unlabeled_or_longer_reference_does_not_bridge(tmp_path: Path) -> None:
    workspace, inbox_root, library_root, _ = _roots(tmp_path)
    contract_doc = tmp_path / "Library" / "contracts" / "a" / "initial.txt"
    contract_doc.parent.mkdir(parents=True)
    contract_doc.write_text("Vertragsnummer: V-2026-001\nAnbieter: Beispiel AG\n")
    _scan(workspace, library_root.root_id)
    workflow.configure_contracts(
        workspace, library_root.root_id, confirmed=True, base_path="contracts"
    )
    workflow.refresh_contracts(workspace)
    candidates = {
        "unlabeled.txt": "Reference V-2026-001 appears without a label.\n",
        "longer.txt": "Vertragsnummer: V-2026-0012\n",
        "embedded.txt": "Kundennummer: XX V-2026-001 YY\n",
    }
    for name, text in candidates.items():
        (tmp_path / "Inbox" / name).write_text(text)
    _scan(workspace, inbox_root.root_id)
    workflow.refresh_contracts(workspace)

    for name in candidates:
        location = catalog.get_location_by_path(workspace.db, tmp_path / "Inbox" / name)
        assert location is not None
        assert not any(
            fact.field == "entity_id"
            for fact in catalog.get_facts(workspace.db, location.revision_id)
        )
    member_location = catalog.get_location_by_path(workspace.db, contract_doc)
    assert member_location is not None
    assert any(
        fact.field == "entity_id"
        for fact in catalog.get_facts(workspace.db, member_location.revision_id)
    )
