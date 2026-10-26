"""P5 tests: confirmed grouping, source-backed facts, calculations, and conflicts."""

from __future__ import annotations

from pathlib import Path

import pytest

from renamr.core import catalog, contracts, processing, workflow
from renamr.core.catalog import PageRecord, QualityState
from renamr.core.errors import CoreError
from renamr.core.extract_runner import InProcessRunner
from renamr.core.roots import RootRole
from renamr.core.settings import Settings
from renamr.core.workspace import Workspace

_INITIAL = """Beispiel Versicherung AG
Versicherungsschein
Anbieter: Beispiel Versicherung AG
Vertragsbeginn: 2026-01-01
Mindestlaufzeit: 12 Monate
Kündigungsfrist: 30 Tage
Beitrag: 120,00 EUR monatlich
Ausgestellt am: 2025-12-15
"""

_AMENDMENT = """Nachtrag zum Versicherungsvertrag
Anbieter: Beispiel Versicherung AG
Änderung ab: 2026-06-01
Beitrag: 130,00 EUR monatlich
Ausgestellt am: 2026-05-20
"""

_CANCELLATION = """Kündigungsbestätigung
Beispiel Versicherung AG
Ausgestellt am: 2026-11-05
Vertragsende: 2026-12-31
"""


def _workspace(tmp_path: Path) -> tuple[Workspace, str, Path]:
    workspace = Workspace.bootstrap(tmp_path / "state", config_path=tmp_path / "config.toml")
    library = tmp_path / "library"
    folder = library / "contract-a"
    folder.mkdir(parents=True)
    (folder / "initial.txt").write_text(_INITIAL)
    (folder / "amendment.txt").write_text(_AMENDMENT)
    (folder / "cancellation.txt").write_text(_CANCELLATION)
    root = workflow.register_root(workspace, library, role=RootRole.LIBRARY)
    workspace.runner = InProcessRunner()
    workspace.extractor = None
    workflow.scan(workspace, root.root_id)
    return workspace, root.root_id, folder


def _value(facts: list[contracts.ContractFactRecord], field: str) -> contracts.ContractFactRecord:
    return next(item for item in facts if item.field == field)


def test_grouping_is_explicit_and_stable(tmp_path: Path) -> None:
    workspace, root_id, _ = _workspace(tmp_path)

    preview = contracts.preview_groups(workspace.db, root_id)
    assert len(preview) == 1
    assert preview[0].contract_id is None

    created = workflow.configure_contracts(workspace, root_id, confirmed=True)
    assert len(created) == 1
    first_id = created[0].contract_id
    again = workflow.configure_contracts(workspace, root_id, confirmed=True)

    assert again[0].contract_id == first_id
    assert again[0].alias == created[0].alias


def test_initial_amendment_and_cancellation_are_source_backed(tmp_path: Path) -> None:
    workspace, root_id, folder = _workspace(tmp_path)
    contract = workflow.configure_contracts(workspace, root_id, confirmed=True)[0]

    result = workflow.refresh_contracts(workspace, contract_ids=[contract.contract_id])
    facts = contracts.get_contract_facts(workspace.db, contract.contract_id)

    assert result.conflicts == 0
    assert _value(facts, "provider").value == "Beispiel Versicherung AG"
    assert _value(facts, "contract_type").value == "insurance"
    assert _value(facts, "start_date").value == "2026-01-01"
    assert _value(facts, "minimum_term_months").value == "12"
    assert _value(facts, "minimum_end_date").value == "2026-12-31"
    assert _value(facts, "end_date").value == "2026-12-31"
    assert _value(facts, "notice_deadline").value == "2026-12-01"
    assert _value(facts, "cancellation_confirmed").value == "true"
    assert _value(facts, "status").value == "cancelled"
    assert _value(facts, "cost_amount").value == "130.00"
    assert _value(facts, "cost_amount").relationship == "amendment"
    assert _value(facts, "cost_amount").effective_date == "2026-06-01"
    assert _value(facts, "end_date").relationship == "cancellation"

    provider = _value(facts, "provider")
    assert provider.source_page == 1
    assert provider.evidence_quote is not None
    assert provider.evidence_quote in (folder / "initial.txt").read_text()


def test_conflicting_initial_values_stay_conflicting(tmp_path: Path) -> None:
    workspace, root_id, folder = _workspace(tmp_path)
    (folder / "second-initial.txt").write_text(
        _INITIAL.replace("Vertragsbeginn: 2026-01-01", "Vertragsbeginn: 2026-02-01")
    )
    workflow.scan(workspace, root_id)
    contract = workflow.configure_contracts(workspace, root_id, confirmed=True)[0]

    result = workflow.refresh_contracts(workspace, contract_ids=[contract.contract_id])
    facts = contracts.get_contract_facts(workspace.db, contract.contract_id)

    assert result.conflicts >= 1
    assert _value(facts, "start_date").origin == "conflicting"
    assert _value(facts, "start_date").value is None


def test_incomplete_ocr_cannot_confirm_contract_facts(tmp_path: Path) -> None:
    workspace = Workspace.bootstrap(tmp_path / "state", config_path=tmp_path / "config.toml")
    library = tmp_path / "library"
    folder = library / "contract-a"
    folder.mkdir(parents=True)
    file = folder / "scan.txt"
    file.write_text("Beispiel Versicherung AG\nAnbieter: Beispiel\n")
    root = workflow.register_root(workspace, library, role=RootRole.LIBRARY)
    ingest = catalog.ingest_file(
        workspace.db,
        root_id=root.root_id,
        path=file,
        observed=catalog.fingerprint(file),
        media_type="text/plain",
    )
    catalog.set_pages(
        workspace.db,
        ingest.revision_id,
        [
            PageRecord(
                page_number=1,
                text=file.read_text(),
                method=catalog.PageMethod.OCR,
                confidence=0.2,
                quality=QualityState.LOW,
                incomplete=True,
            )
        ],
    )
    processing.record_text_result(
        workspace.db, ingest.revision_id, extract_key="synthetic", quality="low"
    )
    contract = workflow.configure_contracts(workspace, root.root_id, confirmed=True)[0]

    result = workflow.refresh_contracts(workspace, contract_ids=[contract.contract_id])
    facts = contracts.get_contract_facts(workspace.db, contract.contract_id)

    assert result.incomplete_documents == 1
    assert _value(facts, "provider").origin == "unknown"
    assert any(
        item["kind"] == "contract_source_incomplete" for item in workflow.review_items(workspace)
    )


def test_user_confirmed_fact_survives_refresh(tmp_path: Path) -> None:
    workspace, root_id, _ = _workspace(tmp_path)
    contract = workflow.configure_contracts(workspace, root_id, confirmed=True)[0]
    workflow.refresh_contracts(workspace, contract_ids=[contract.contract_id])
    workspace.extractor = None
    workspace.save_settings(Settings(endpoint="http://127.0.0.1:11434"))

    workflow.set_contract_fact(workspace, contract.contract_id, "provider", "Lokal Bestätigt AG")
    workflow.refresh_contracts(workspace, contract_ids=[contract.contract_id])
    facts = contracts.get_contract_facts(workspace.db, contract.contract_id)

    assert _value(facts, "provider").value == "Lokal Bestätigt AG"
    assert _value(facts, "provider").origin == "user_confirmed"


def test_manual_membership_exception(tmp_path: Path) -> None:
    workspace, root_id, _ = _workspace(tmp_path)
    contract = workflow.configure_contracts(workspace, root_id, confirmed=True)[0]
    exception = tmp_path / "library" / "exceptions"
    exception.mkdir()
    (exception / "letter.txt").write_text(_INITIAL)
    workflow.scan(workspace, root_id)

    member = workflow.assign_contract_member(
        workspace, contract.contract_id, root_id=root_id, relative_path="exceptions"
    )

    assert member.relative_path == "exceptions"
    with pytest.raises(CoreError):
        workflow.assign_contract_member(
            workspace, contract.contract_id, root_id=root_id, relative_path="../escape"
        )
