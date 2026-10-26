from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import cast

import pytest

import renamr.cli.chat as chat_module
import renamr.cli.onboard as onboard_module
from renamr.cli import app
from renamr.core import (
    catalog,
    dialog,
    examples,
    learning,
    mutation,
    onboarding,
    paths,
    review,
    rules,
    workflow,
)
from renamr.core.errors import CatalogError
from renamr.core.inference import LocalInference
from renamr.core.roots import RootRole
from renamr.core.settings import ModelRoles, Settings
from renamr.core.workspace import Workspace
from tests.unit.acceptance_support import _roots, _scan, runner
from tests.unit.core_support import FakeExtractor


def test_main_flow_trusted_examples_to_tested_active_rule(tmp_path: Path) -> None:
    workspace, inbox_root, library_root, _ = _roots(tmp_path)
    existing = tmp_path / "Library" / "Invoices" / "invoice.txt"
    existing.parent.mkdir()
    existing.write_text("Rechnung von Beispiel")
    _scan(workspace, library_root.root_id, FakeExtractor(document_type="invoice"))
    workflow.update_root(workspace, library_root.root_id, trusted_examples=True)
    onboarding.seed_trusted_examples(workspace.db, root_ids=[library_root.root_id])
    candidate = learning.propose_candidates(workspace.db)[0]
    draft = workflow.create_rule_from_candidate(workspace, candidate)
    assert draft.status is rules.RuleStatus.DRAFT
    workflow.mark_tested(workspace, draft.rule_id)
    active = workflow.activate_rule(workspace, draft.rule_id)
    assert active.status is rules.RuleStatus.ACTIVE

    incoming = tmp_path / "Inbox" / "new.txt"
    incoming.write_text("Rechnung von Beispiel")
    _scan(workspace, inbox_root.root_id, FakeExtractor(document_type="invoice"))
    run_id = mutation.preview_routing(workspace, inbox_root.root_id)
    assert len(mutation.run_items(workspace, run_id)) == 1
    mutation.apply(workspace, run_id)

    assert not incoming.exists()
    moved = list((tmp_path / "Library" / "Invoices").iterdir())
    assert [item.name for item in moved if item.name != "invoice.txt"]
    renamed = next(item for item in moved if item.name != "invoice.txt")
    assert renamed.read_text() == "Rechnung von Beispiel"


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
