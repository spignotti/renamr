"""Regression tests: scan caching, durable failures, taxonomy, and chunking."""

from __future__ import annotations

import inspect
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import cast

import pytest

from renamr.benchmark_schema import DocumentFeatures, DocumentType, GroundingEvidence
from renamr.core import catalog, hashing, workflow
from renamr.core.db import Database
from renamr.core.extract import ExtractionResult, ExtractorLimits
from renamr.core.extract_runner import ExtractionRunner, InProcessRunner, SubprocessRunner
from renamr.core.features import (
    FeatureExtractor,
    FeatureRequest,
    LocalFeatureExtractor,
    build_chunk_text,
    chunk_pages,
    facts_to_features,
    features_to_facts,
)
from renamr.core.roots import Root, RootRole
from renamr.core.settings import Settings
from renamr.core.workspace import Workspace
from renamr.local_transport import ChatMessage, ChatMetrics, OutputMode, StructuredResult
from tests.unit.core_support import (
    FakeExtractor,
    FakeOCR,
    make_blank_pdf,
    make_encrypted_pdf,
    make_text_pdf,
    ocr_text,
)

NEW_CATEGORIES = (
    "payslip",
    "application",
    "enrolment certificate",
    "medical correspondence",
    "official correspondence",
)


class CountingRunner:
    """Wrap a runner and count how often it is invoked."""

    def __init__(self, inner: ExtractionRunner) -> None:
        self.inner = inner
        self.calls = 0

    def run(self, path: Path, limits: ExtractorLimits) -> ExtractionResult:
        self.calls += 1
        return self.inner.run(path, limits)


class StubRunner:
    """Return one canned extraction result and count invocations."""

    def __init__(self, result: ExtractionResult) -> None:
        self.result = result
        self.calls = 0

    def run(self, path: Path, limits: ExtractorLimits) -> ExtractionResult:
        self.calls += 1
        return self.result


class FakeInference:
    """Return canned feature types grounded in the requested chunk text."""

    def __init__(self, types: list[str]) -> None:
        self._types = types
        self.calls = 0

    def complete(
        self,
        model: str,
        messages: Sequence[ChatMessage],
        schema: type[DocumentFeatures],
        *,
        output_mode: OutputMode | None = None,
        images: Sequence[str] = (),
        require_vision: bool = False,
    ) -> StructuredResult[DocumentFeatures]:
        content = messages[1].content
        current_page = 1
        page = 1
        quote = ""
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("[[PAGE"):
                token = stripped.removeprefix("[[PAGE").strip().split()[0].rstrip("]")
                current_page = int(token)
            elif stripped and not quote:
                quote = stripped
                page = current_page
        document_type = self._types[min(self.calls, len(self._types) - 1)]
        self.calls += 1
        data = DocumentFeatures(
            document_type=cast("DocumentType", document_type),
            needs_review=False,
            evidence=[GroundingEvidence(page=page, source_text=quote[:20])],
        )
        return StructuredResult(
            data=data,
            metrics=ChatMetrics(),
            output_mode=output_mode if output_mode is not None else "validated_prompt",
        )


def _workspace(
    tmp_path: Path,
    *,
    runner: ExtractionRunner | None = None,
    extractor: FeatureExtractor | None = None,
) -> Workspace:
    state = tmp_path / "state"
    db = Database(state / "catalog.sqlite3")
    db.initialize()
    return Workspace(db=db, state_dir=state, extractor=extractor, runner=runner)


def _inbox(ws: Workspace, tmp_path: Path) -> tuple[Path, Root]:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    return inbox, workflow.register_root(ws, inbox, role=RootRole.INBOX)


def _count_hashes(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    counter = {"n": 0}
    real = hashing.hash_file

    def counting(path: Path) -> str:
        counter["n"] += 1
        return real(path)

    monkeypatch.setattr(hashing, "hash_file", counting)
    return counter


def test_scan_does_not_rehash_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    counter = _count_hashes(monkeypatch)
    ocr = FakeOCR([ocr_text("Rechnung von Beispiel", 0.92), ocr_text("Rechnung", 0.92)])
    ws = _workspace(tmp_path, runner=InProcessRunner(ocr), extractor=FakeExtractor())
    inbox, root = _inbox(ws, tmp_path)
    make_blank_pdf(inbox / "scan.pdf", page_count=1)

    workflow.scan(ws, root.root_id)
    first = counter["n"]
    assert first >= 1

    outcome = workflow.scan(ws, root.root_id)
    assert outcome.unchanged == 1
    assert counter["n"] == first


def test_permanent_failure_is_not_repeated(tmp_path: Path) -> None:
    runner = CountingRunner(InProcessRunner())
    ws = _workspace(tmp_path, runner=runner)
    inbox, root = _inbox(ws, tmp_path)
    make_encrypted_pdf(inbox / "locked.pdf")

    first = workflow.scan(ws, root.root_id)
    assert "encrypted" in first.errors
    assert runner.calls == 1

    second = workflow.scan(ws, root.root_id)
    assert runner.calls == 1
    assert second.unchanged + second.skipped >= 1


def test_transient_failure_backs_off(tmp_path: Path) -> None:
    result = ExtractionResult(
        media_type="application/pdf",
        pages=[],
        page_count=1,
        incomplete=True,
        errors=["worker_invalid_output"],
    )
    runner = StubRunner(result)
    ws = _workspace(tmp_path, runner=runner)
    inbox, root = _inbox(ws, tmp_path)
    make_text_pdf(inbox / "note.pdf", ["payload"])

    workflow.scan(ws, root.root_id)
    assert runner.calls == 1

    workflow.scan(ws, root.root_id)
    assert runner.calls == 1


def test_hidden_and_system_directories_are_pruned(tmp_path: Path) -> None:
    ws = _workspace(tmp_path, runner=InProcessRunner(FakeOCR([])))
    inbox, root = _inbox(ws, tmp_path)
    (inbox / ".hidden").mkdir()
    (inbox / ".git").mkdir()
    make_text_pdf(inbox / "visible.pdf", ["hello"])
    make_text_pdf(inbox / ".hidden" / "secret.pdf", ["hidden"])
    make_text_pdf(inbox / ".git" / "config.pdf", ["git"])

    outcome = workflow.scan(ws, root.root_id)

    assert outcome.discovered == 1


def test_oversized_file_records_review_without_hashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    counter = _count_hashes(monkeypatch)
    ws = _workspace(tmp_path, runner=InProcessRunner(FakeOCR([])))
    ws.save_settings(Settings(max_file_bytes=4))
    inbox, root = _inbox(ws, tmp_path)
    (inbox / "big.txt").write_text("x" * 200)

    outcome = workflow.scan(ws, root.root_id)

    assert outcome.oversized == 1
    assert counter["n"] == 0
    assert "oversized_file" in {str(item["kind"]) for item in workflow.review_items(ws)}


def test_hash_file_has_no_partial_digest_option() -> None:
    assert "max_bytes" not in inspect.signature(hashing.hash_file).parameters
    assert "max_bytes" not in inspect.signature(hashing.fingerprint).parameters


def test_own_move_is_not_suppressed_forever(tmp_path: Path) -> None:
    from renamr.benchmark_schema import RulePredicate
    from renamr.core import mutation

    ocr = FakeOCR([ocr_text("Rechnung von Beispiel", 0.92), ocr_text("Rechnung", 0.92)])
    ws = _workspace(
        tmp_path, runner=InProcessRunner(ocr), extractor=FakeExtractor(document_type="invoice")
    )
    inbox = tmp_path / "inbox"
    archive = tmp_path / "archive"
    inbox.mkdir()
    archive.mkdir()
    inbox_root = workflow.register_root(ws, inbox, role=RootRole.INBOX)
    archive_root = workflow.register_root(ws, archive, role=RootRole.ARCHIVE)
    workflow.set_allowed_targets(ws, inbox_root.root_id, [archive_root.root_id])
    document = inbox / "scan.pdf"
    make_blank_pdf(document, page_count=1)
    workflow.scan(ws, inbox_root.root_id)
    draft = workflow.create_rule_draft(
        ws,
        scope_root_id=inbox_root.root_id,
        target_root_id=archive_root.root_id,
        predicates=[RulePredicate(field="document_type", operator="equals", values=["invoice"])],
        filename_template="{subject}",
        target_subdir="invoices",
    )
    workflow.mark_tested(ws, draft.rule_id)
    workflow.activate_rule(ws, draft.rule_id)
    run_id = mutation.preview_routing(ws, inbox_root.root_id)
    mutation.apply(ws, run_id)

    moved = catalog.locations_for_root(ws.db, archive_root.root_id)[0].canonical_path
    user_path = moved.with_name("user-renamed.pdf")
    moved.rename(user_path)
    workflow.scan(ws, archive_root.root_id)

    assert "possible_move" in {str(item["kind"]) for item in workflow.review_items(ws)}


def test_taxonomy_includes_production_categories() -> None:
    for document_type in NEW_CATEGORIES:
        features = DocumentFeatures(
            document_type=cast("DocumentType", document_type), needs_review=False
        )
        assert features.document_type == document_type
    with pytest.raises(ValueError):
        DocumentFeatures.model_validate({"document_type": "receiptt", "needs_review": False})


def test_payslip_round_trips_through_facts() -> None:
    features = DocumentFeatures(document_type="payslip", needs_review=False)
    assert facts_to_features(features_to_facts(features)) == features


def test_chunk_pages_splits_within_a_long_page() -> None:
    text = "A" * 250
    chunks = chunk_pages([text], 60)

    assert len(chunks) >= 2
    assert all(item.page_number == 1 for chunk in chunks for item in chunk.slices)
    offsets = [item.offset for chunk in chunks for item in chunk.slices]
    assert offsets == sorted(offsets)
    assert offsets[0] == 0
    rebuilt = "".join(item.text for chunk in chunks for item in chunk.slices)
    assert rebuilt == text
    assert "offset=" in build_chunk_text(chunks[1])


def test_local_extractor_processes_multiple_chunks() -> None:
    texts = ["A" * 300, "B" * 300]
    inference = FakeInference(["invoice", "invoice", "invoice", "invoice"])
    extractor = LocalFeatureExtractor(inference, "model", max_chars=80, prompt_overhead=0)

    result = extractor.extract(FeatureRequest(page_texts=texts))

    assert result.features is not None
    assert result.chunks >= 2
    assert result.incomplete is False


def test_conflicting_chunks_stay_in_review() -> None:
    texts = ["A" * 300, "B" * 300]
    inference = FakeInference(["invoice", "payslip"])
    extractor = LocalFeatureExtractor(inference, "model", max_chars=80, prompt_overhead=0)

    result = extractor.extract(FeatureRequest(page_texts=texts))

    assert result.features is None
    assert result.blocked_reason == "conflicting_chunks"
    assert result.incomplete is True


def test_in_process_runner_reports_limits(tmp_path: Path) -> None:
    pdf = make_text_pdf(tmp_path / "note.pdf", ["payload"])
    result = InProcessRunner().run(pdf, ExtractorLimits())
    assert result.limits_report["mode"] == "in_process"
    assert result.limits_report["wall_clock"] == "not_enforced"


def test_subprocess_failure_reports_limits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pdf = make_text_pdf(tmp_path / "note.pdf", ["payload"])

    def timeout(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired(cmd="worker", timeout=1)

    monkeypatch.setattr(subprocess, "run", timeout)
    result = SubprocessRunner().run(pdf, ExtractorLimits())

    assert result.errors == ["timeout"]
    assert result.limits_report["outcome"] == "timeout"


def test_repeated_failure_dedupes_review(tmp_path: Path) -> None:
    runner = CountingRunner(InProcessRunner())
    ws = _workspace(tmp_path, runner=runner)
    inbox, root = _inbox(ws, tmp_path)
    make_encrypted_pdf(inbox / "locked.pdf")

    workflow.scan(ws, root.root_id)
    first = len(workflow.review_items(ws))
    revision_id = catalog.locations_for_root(ws.db, root.root_id)[0].revision_id

    workflow.retry_revision(ws, revision_id)
    workflow.scan(ws, root.root_id)

    assert len(workflow.review_items(ws)) == first


def test_enabling_extractor_later_reruns_only_features(tmp_path: Path) -> None:
    ocr = FakeOCR([ocr_text("Rechnung von Beispiel", 0.92), ocr_text("Rechnung", 0.92)])
    runner = CountingRunner(InProcessRunner(ocr))
    ws = _workspace(tmp_path, runner=runner, extractor=None)
    inbox, root = _inbox(ws, tmp_path)
    make_blank_pdf(inbox / "scan.pdf", page_count=1)

    workflow.scan(ws, root.root_id)
    assert runner.calls == 1
    revision_id = catalog.locations_for_root(ws.db, root.root_id)[0].revision_id
    assert catalog.get_facts(ws.db, revision_id) == []

    ws.extractor = FakeExtractor(document_type="payslip")
    workflow.scan(ws, root.root_id)

    assert runner.calls == 1  # no re-OCR of unchanged content
    facts = {fact.field: fact.value for fact in catalog.get_facts(ws.db, revision_id)}
    assert facts["document_type"] == "payslip"
