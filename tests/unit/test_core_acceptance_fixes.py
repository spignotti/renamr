"""Regression tests for the approved v2 acceptance corrections."""

from __future__ import annotations

import os
from collections.abc import Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import cast

import pytest
from pydantic import BaseModel
from typer.testing import CliRunner

import renamr.cli.chat as chat_module
import renamr.cli.onboard as onboard_module
from renamr.benchmark_schema import RulePredicate
from renamr.cli import app
from renamr.core import (
    catalog,
    contracts,
    dialog,
    dialog_context,
    dialog_render,
    examples,
    learning,
    mutation,
    onboarding,
    paths,
    review,
    rules,
    structure,
    workflow,
)
from renamr.core.errors import CatalogError, CoreError, RootError
from renamr.core.extract_runner import InProcessRunner
from renamr.core.features import FeatureExtractor
from renamr.core.inference import LocalInference
from renamr.core.mcp_gateway import MCP_FIELDS
from renamr.core.roots import Root, RootRole
from renamr.core.settings import ModelRoles, Settings
from renamr.core.workspace import Workspace
from renamr.local_transport import ChatMessage, ChatMetrics, OutputMode, StructuredResult
from tests.unit.core_support import FakeExtractor

runner = CliRunner()


class FakeDialogCompleter:
    """Return one canned dialog response and capture its messages."""

    def __init__(self, response: dialog.DialogResponse) -> None:
        self.response = response
        self.messages: list[ChatMessage] = []

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
        self.messages = list(messages)
        return StructuredResult[ResultT](
            data=cast("ResultT", self.response),
            metrics=ChatMetrics(),
            output_mode=output_mode or "validated_prompt",
        )


def _workspace(tmp_path: Path) -> Workspace:
    return Workspace.bootstrap(tmp_path / "state", config_path=tmp_path / "config.toml")


def _roots(tmp_path: Path) -> tuple[Workspace, Root, Root, Root]:
    workspace = _workspace(tmp_path)
    inbox, library, archive = tmp_path / "Inbox", tmp_path / "Library", tmp_path / "Archive"
    for folder in (inbox, library, archive):
        folder.mkdir()
    inbox_root = workflow.register_root(workspace, inbox, role=RootRole.INBOX)
    library_root = workflow.register_root(workspace, library, role=RootRole.LIBRARY)
    archive_root = workflow.register_root(workspace, archive, role=RootRole.ARCHIVE)
    workflow.set_allowed_targets(
        workspace, inbox_root.root_id, [library_root.root_id, archive_root.root_id]
    )
    return workspace, inbox_root, library_root, archive_root


def _scan(workspace: Workspace, root_id: str, extractor: FeatureExtractor | None = None) -> None:
    workspace.runner = InProcessRunner()
    workspace.extractor = extractor
    workflow.scan(workspace, root_id)


def test_untrusted_scope_does_not_seed_placement_examples(tmp_path: Path) -> None:
    workspace, inbox_root, library_root, _ = _roots(tmp_path)
    target = tmp_path / "Library" / "Invoices" / "invoice.txt"
    target.parent.mkdir()
    target.write_text("Rechnung von Beispiel")
    _scan(workspace, library_root.root_id, FakeExtractor(document_type="invoice"))
    workflow.update_root(workspace, library_root.root_id, trusted_examples=False)

    assert (
        onboarding.seed_trusted_examples(workspace.db, root_ids=[library_root.root_id]).examples
        == 0
    )
    workflow.update_root(workspace, library_root.root_id, trusted_examples=True)
    assert (
        onboarding.seed_trusted_examples(workspace.db, root_ids=[library_root.root_id]).examples > 0
    )
    assert inbox_root.root_id


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


def test_init_with_new_config_uses_default_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    default_state = tmp_path / "default-state"
    monkeypatch.setattr(paths, "default_state_dir", lambda env=None: default_state)
    config = tmp_path / "new" / "config.toml"

    result = runner.invoke(app, ["init", "--config", str(config)])

    assert result.exit_code == 0, result.output
    assert config.exists()
    assert (default_state / "catalog.sqlite3").is_file()


def test_sharing_export_refuses_registered_source_and_overwrite_confirm(
    tmp_path: Path,
) -> None:
    workspace, inbox_root, _, _ = _roots(tmp_path)
    source = tmp_path / "Inbox" / "scan.txt"
    source.write_text("Rechnung von Beispiel")
    _scan(workspace, inbox_root.root_id, FakeExtractor(document_type="invoice"))
    location = catalog.get_location_by_path(workspace.db, source)
    assert location is not None
    destination = tmp_path / "export.txt"
    destination.write_text("ORIGINAL")
    config = tmp_path / "config.toml"

    refused = runner.invoke(
        app,
        [
            "document",
            "export",
            location.revision_id,
            "--to",
            str(destination),
            "--config",
            str(config),
        ],
    )
    assert refused.exit_code == 1
    assert destination.read_text() == "ORIGINAL"

    overwritten = runner.invoke(
        app,
        [
            "document",
            "export",
            location.revision_id,
            "--to",
            str(destination),
            "--overwrite",
            "--yes",
            "--config",
            str(config),
        ],
    )
    assert overwritten.exit_code == 0, overwritten.output
    assert "[[PAGE 1]]" in destination.read_text()


def test_onboarding_classifies_selected_scope_and_seeds_trusted_examples(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, config = tmp_path / "state", tmp_path / "config.toml"
    Workspace.bootstrap(state, config_path=config)
    library = tmp_path / "Library"
    (library / "Invoices").mkdir(parents=True)
    (library / "Invoices" / "invoice.txt").write_text("Rechnung von Beispiel")
    for name in ("Inbox", "Archive"):
        (tmp_path / name).mkdir()
    roles: list[str] = []

    @contextmanager
    def fake_session(settings: Settings, *, role: str):
        roles.append(role)
        yield object()

    def fake_key(workspace: Workspace, settings: Settings, inference: object, *, role: str) -> None:
        roles.append(f"verify:{role}")
        workspace.model_digest = "synthetic-digest"

    def fake_extractor(settings: Settings, inference: object, *, role: str) -> FakeExtractor:
        roles.append(f"extractor:{role}")
        return FakeExtractor(document_type="invoice", issuer="Beispiel GmbH")

    monkeypatch.setattr(onboard_module, "inference_session", fake_session)
    monkeypatch.setattr(onboard_module, "verify_and_key", fake_key)
    monkeypatch.setattr(onboard_module, "feature_extractor", fake_extractor)

    result = runner.invoke(
        app,
        [
            "onboard",
            "--inbox",
            str(tmp_path / "Inbox"),
            "--library",
            str(library),
            "--archive",
            str(tmp_path / "Archive"),
            "--no-questions",
            "--config",
            str(config),
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert roles[:3] == ["small", "verify:small", "extractor:small"]
    workspace = Workspace.open(config_path=config)
    location = catalog.get_location_by_path(workspace.db, library / "Invoices" / "invoice.txt")
    assert location is not None
    facts = {
        item.field: item.value for item in catalog.get_facts(workspace.db, location.revision_id)
    }
    assert facts["document_type"] == "invoice"
    seeded = [
        item for item in examples.list_examples(workspace.db) if item.source == "trusted_placement"
    ]
    assert seeded
    candidates = learning.propose_candidates(workspace.db)
    assert candidates
    inbox_root = next(
        item for item in workflow.list_roots(workspace) if item.role is RootRole.INBOX
    )
    library_root = next(
        item for item in workflow.list_roots(workspace) if item.role is RootRole.LIBRARY
    )
    assert candidates[0].scope_root_id == inbox_root.root_id
    assert candidates[0].target_root_id == library_root.root_id
    assert candidates[0].target_subdir == "Invoices"


def test_library_only_onboarding_preserves_unrelated_inbox_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, inbox_root, _, archive_root = _roots(tmp_path)
    workflow.set_allowed_targets(workspace, inbox_root.root_id, [archive_root.root_id])
    workflow.update_root(workspace, inbox_root.root_id, automation_enabled=True)
    extra = tmp_path / "ExtraLibrary"
    extra.mkdir()

    assert inbox_root.root_id
    result = runner.invoke(
        app,
        [
            "onboard",
            "--library",
            str(extra),
            "--no-catalog",
            "--no-features",
            "--config",
            str(tmp_path / "config.toml"),
        ],
    )

    assert result.exit_code == 0, result.stdout
    current = workflow.get_root(workspace, inbox_root.root_id)
    assert current.allowed_target_ids == [archive_root.root_id]
    assert current.automation_enabled is True

    existing_library = next(
        item
        for item in workflow.list_roots(workspace)
        if item.role is RootRole.LIBRARY and item.canonical_path == (tmp_path / "Library").resolve()
    )
    assert existing_library.trusted_examples is False
    onboard_existing = runner.invoke(
        app,
        [
            "onboard",
            "--library",
            str(tmp_path / "Library"),
            "--no-catalog",
            "--no-features",
            "--config",
            str(tmp_path / "config.toml"),
        ],
    )
    assert onboard_existing.exit_code == 0, onboard_existing.stdout
    assert workflow.get_root(workspace, existing_library.root_id).trusted_examples is False


def test_unmatched_question_is_deduplicated_and_explicit_destination_moves_once(
    tmp_path: Path,
) -> None:
    workspace, inbox_root, _, archive_root = _roots(tmp_path)
    source = tmp_path / "Inbox" / "scan.txt"
    source.write_text("Rechnung von Beispiel")
    _scan(workspace, inbox_root.root_id, FakeExtractor(document_type="invoice"))

    first = mutation.preview_routing(workspace, inbox_root.root_id)
    second = mutation.preview_routing(workspace, inbox_root.root_id)
    assert mutation.run_items(workspace, first) == []
    assert mutation.run_items(workspace, second) == []
    open_items = [
        item for item in workflow.review_items(workspace) if item["kind"] == "unmatched_placement"
    ]
    assert len(open_items) == 1
    review_id = str(open_items[0]["review_id"])

    preview = workflow.preview_placement(
        workspace,
        review_id,
        target_root_id=archive_root.root_id,
        target_subdir="Invoices",
    )
    assert preview.source_path == source
    assert preview.filename == "scan.txt"
    assert preview.target_path == tmp_path / "Archive" / "Invoices" / "scan.txt"

    result = workflow.apply_placement(
        workspace,
        review_id,
        target_root_id=archive_root.root_id,
        target_subdir="Invoices",
    )

    assert result.committed == 1
    assert not source.exists()
    assert preview.target_path.read_text() == "Rechnung von Beispiel"
    example = next(
        item for item in examples.list_examples(workspace.db) if item.source == "user_destination"
    )
    assert example.payload["scope_root_id"] == inbox_root.root_id
    assert workflow.list_rules(workspace) == []
    candidates = learning.propose_candidates(workspace.db)
    assert any(item.target_subdir == "Invoices" for item in candidates)


def test_repeated_destination_choices_create_a_testable_draft_only(tmp_path: Path) -> None:
    workspace, inbox_root, _, archive_root = _roots(tmp_path)
    for name in ("first.txt", "second.txt"):
        (tmp_path / "Inbox" / name).write_text(f"Rechnung {name}")
    _scan(workspace, inbox_root.root_id, FakeExtractor(document_type="invoice"))
    mutation.preview_routing(workspace, inbox_root.root_id)

    first = next(
        item for item in review.review_questions(workspace.db) if item.kind == "unmatched_placement"
    )
    workflow.apply_placement(
        workspace,
        first.review_id,
        target_root_id=archive_root.root_id,
        target_subdir="Invoices",
    )
    assert workflow.list_rules(workspace) == []

    _scan(workspace, inbox_root.root_id, FakeExtractor(document_type="invoice"))
    mutation.preview_routing(workspace, inbox_root.root_id)
    second = next(
        item for item in review.review_questions(workspace.db) if item.kind == "unmatched_placement"
    )
    workflow.apply_placement(
        workspace,
        second.review_id,
        target_root_id=archive_root.root_id,
        target_subdir="Invoices",
    )

    drafts = workflow.list_rules(workspace)
    assert len(drafts) == 1
    assert drafts[0].status is rules.RuleStatus.DRAFT
    assert workflow.active_rules(workspace) == []


def test_cli_review_destination_flags_preview_and_apply(tmp_path: Path) -> None:
    workspace, inbox_root, _, archive_root = _roots(tmp_path)
    source = tmp_path / "Inbox" / "scan.txt"
    source.write_text("Rechnung von Beispiel")
    _scan(workspace, inbox_root.root_id, FakeExtractor(document_type="invoice"))
    mutation.preview_routing(workspace, inbox_root.root_id)
    question = next(
        item for item in review.review_questions(workspace.db) if item.kind == "unmatched_placement"
    )

    result = runner.invoke(
        app,
        [
            "review",
            "--review-id",
            question.review_id,
            "--destination-root",
            archive_root.root_id,
            "--destination-folder",
            "Invoices",
            "--yes",
            "--config",
            str(tmp_path / "config.toml"),
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert not source.exists()
    assert (tmp_path / "Archive" / "Invoices" / "scan.txt").exists()


def test_learning_counts_different_destinations_and_scopes_as_conflicts(tmp_path: Path) -> None:
    workspace, inbox_root, library_root, archive_root = _roots(tmp_path)
    target = tmp_path / "Archive" / "Invoices" / "x.txt"
    examples.add_example(
        workspace.db,
        source=examples.ExampleSource.USER_DESTINATION,
        revision_id=None,
        location_id=None,
        payload={
            "document_type": "invoice",
            "new_path": str(target),
            "scope_root_id": inbox_root.root_id,
        },
    )
    examples.add_example(
        workspace.db,
        source=examples.ExampleSource.USER_DESTINATION,
        revision_id=None,
        location_id=None,
        payload={
            "document_type": "invoice",
            "new_path": str(tmp_path / "Archive" / "Receipts" / "x.txt"),
            "scope_root_id": inbox_root.root_id,
        },
    )
    examples.add_example(
        workspace.db,
        source=examples.ExampleSource.USER_DESTINATION,
        revision_id=None,
        location_id=None,
        payload={
            "document_type": "invoice",
            "new_path": str(target),
            "scope_root_id": library_root.root_id,
        },
    )
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


def test_invalid_legacy_template_blocks_before_state_or_roots(tmp_path: Path) -> None:
    inbox = tmp_path / "Inbox"
    inbox.mkdir()
    legacy = tmp_path / "legacy.toml"
    legacy.write_text(
        f'language = "de"\nfilename_template = "{{date}}_{{unknown}}"\n'
        f'[[inbox]]\npath = "{inbox}"\n'
        '[llm]\nmodel = "ollama/gemma4:e4b"\napi_base = "http://127.0.0.1:11434"\n'
    )
    config, state = tmp_path / "config.toml", tmp_path / "state"

    result = runner.invoke(
        app,
        [
            "init",
            "--legacy-config",
            str(legacy),
            "--config",
            str(config),
            "--state-dir",
            str(state),
            "--apply",
        ],
    )

    assert result.exit_code == 1
    assert not (state / "catalog.sqlite3").exists()
    assert not config.exists()
    assert legacy.read_text().count("{unknown}") == 1


def test_missing_explicit_config_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "default-state"))
    missing = tmp_path / "missing.toml"

    result = runner.invoke(app, ["run", "--config", str(missing)])

    assert result.exit_code == 1
    assert str(missing) in str(result.exception) or str(missing) in result.output
    assert not (tmp_path / "default-state").exists()


def test_export_refuses_existing_destination_and_source_original(tmp_path: Path) -> None:
    workspace, inbox_root, _, _ = _roots(tmp_path)
    source = tmp_path / "Inbox" / "scan.txt"
    source.write_text("Rechnung von Beispiel")
    _scan(workspace, inbox_root.root_id, FakeExtractor(document_type="invoice"))
    location = catalog.get_location_by_path(workspace.db, source)
    assert location is not None

    destination = tmp_path / "export.txt"
    destination.write_text("ORIGINAL USER CONTENT")
    os.chmod(destination, 0o640)
    with pytest.raises(CatalogError, match="already exists"):
        workflow.export_text(workspace, location.revision_id, destination)
    assert destination.read_text() == "ORIGINAL USER CONTENT"
    assert destination.stat().st_mode & 0o777 == 0o640

    with pytest.raises(CatalogError, match="source original"):
        workflow.export_text(workspace, location.revision_id, source)
    assert source.read_text() == "Rechnung von Beispiel"

    workflow.export_text(workspace, location.revision_id, destination, overwrite=True)
    assert "[[PAGE 1]]" in destination.read_text()


def test_routine_feature_key_uses_small_role(tmp_path: Path) -> None:
    workspace, inbox_root, _, _ = _roots(tmp_path)
    (tmp_path / "Inbox" / "scan.txt").write_text("Rechnung von Beispiel")
    workspace.save_settings(
        Settings(model_roles=ModelRoles(main="main:1", small="small:1", maintenance="maint:1"))
    )
    _scan(workspace, inbox_root.root_id, FakeExtractor(document_type="invoice"))
    location = catalog.get_location_by_path(workspace.db, tmp_path / "Inbox" / "scan.txt")
    assert location is not None
    revision = catalog.get_revision(workspace.db, location.revision_id)
    assert revision.feature_key.startswith("small:1|")


def test_chat_turn_uses_maintenance_role(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, inbox_root, _, _ = _roots(tmp_path)
    workspace.save_settings(
        Settings(model_roles=ModelRoles(main="main:1", small="small:1", maintenance="maint:1"))
    )
    captured: dict[str, object] = {}
    proposal = dialog.DialogProposal(
        proposal_id="proposal_test",
        intent="explain",
        payload={"response": dialog.DialogResponse(intent="explain").model_dump(mode="json")},
        status="proposed",
        decision="",
        created_at="2026-01-01T00:00:00+00:00",
    )

    def fake_propose(*args: object, **kwargs: object) -> dialog.DialogProposal:
        captured.update(kwargs)
        return proposal

    monkeypatch.setattr(chat_module.workflow, "propose_dialog", fake_propose)
    monkeypatch.setattr(chat_module.dialog_render, "describe_proposal", lambda *args: [])

    summary = chat_module._turn(
        workspace,
        "explain",
        cast("LocalInference", object()),
        [inbox_root.root_id],
        apply=False,
        yes=False,
        history=[],
    )

    assert captured["model"] == "maint:1"
    assert captured["keep_alive"] == workspace.settings().model_keep_alive.maintenance
    assert "intent=explain" in summary
