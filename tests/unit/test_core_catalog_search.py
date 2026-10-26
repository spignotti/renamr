"""P2 tests: catalog identities, page/fact storage, search, and export."""

from __future__ import annotations

import hashlib
from pathlib import Path

from renamr.benchmark_schema import DocumentFeatures, GroundingEvidence
from renamr.core import catalog
from renamr.core.catalog import FactRecord, PageMethod, PageRecord
from renamr.core.db import Database
from renamr.core.features import facts_to_features, features_to_facts
from renamr.core.roots import RootRole, register_root
from renamr.core.search import export_text, search

_TEXT = "Rechnung fuer Beispiel GmbH\nRechnungsdatum: 2026-03-14\nBetrag: 128,40 EUR"


def _setup(tmp_path: Path) -> tuple[Database, str, Path]:
    db = Database(tmp_path / "state" / "catalog.sqlite3")
    db.initialize()
    root_dir = tmp_path / "library"
    root_dir.mkdir()
    root = register_root(db, path=root_dir, role=RootRole.LIBRARY, state_dir=tmp_path / "state")
    return db, root.root_id, root_dir


def test_fingerprint_matches_hashlib(tmp_path: Path) -> None:
    file = tmp_path / "note.txt"
    file.write_text("synthetic")
    observed = catalog.fingerprint(file)
    assert observed.content_hash == hashlib.sha256(b"synthetic").hexdigest()
    assert observed.size_bytes == len("synthetic")


def test_ingest_reuses_unchanged_revision(tmp_path: Path) -> None:
    db, root_id, root_dir = _setup(tmp_path)
    file = root_dir / "note.txt"
    file.write_text(_TEXT)

    first = catalog.ingest_file(
        db, root_id=root_id, path=file, observed=catalog.fingerprint(file), media_type="text/plain"
    )
    second = catalog.ingest_file(
        db, root_id=root_id, path=file, observed=catalog.fingerprint(file), media_type="text/plain"
    )

    assert first.revision_created is True
    assert second.revision_created is False
    assert second.location_created is False
    assert second.revision_id == first.revision_id


def test_copies_share_revision_but_not_location(tmp_path: Path) -> None:
    db, root_id, root_dir = _setup(tmp_path)
    original = root_dir / "a.txt"
    copy = root_dir / "b.txt"
    original.write_text(_TEXT)
    copy.write_text(_TEXT)

    first = catalog.ingest_file(
        db,
        root_id=root_id,
        path=original,
        observed=catalog.fingerprint(original),
        media_type="text/plain",
    )
    second = catalog.ingest_file(
        db, root_id=root_id, path=copy, observed=catalog.fingerprint(copy), media_type="text/plain"
    )

    assert first.revision_id == second.revision_id
    assert first.location_id != second.location_id
    assert len(catalog.locations_for_revision(db, first.revision_id)) == 2


def test_content_change_creates_new_revision_same_document(tmp_path: Path) -> None:
    db, root_id, root_dir = _setup(tmp_path)
    file = root_dir / "note.txt"
    file.write_text(_TEXT)
    first = catalog.ingest_file(
        db, root_id=root_id, path=file, observed=catalog.fingerprint(file), media_type="text/plain"
    )

    file.write_text(_TEXT + "\nupdated line")
    second = catalog.ingest_file(
        db, root_id=root_id, path=file, observed=catalog.fingerprint(file), media_type="text/plain"
    )

    assert second.revision_created is True
    assert second.revision_changed is True
    assert second.revision_id != first.revision_id
    assert second.document_id == first.document_id
    assert catalog.get_location(db, second.location_id).revision_id == second.revision_id


def test_pages_search_and_export(tmp_path: Path) -> None:
    db, root_id, root_dir = _setup(tmp_path)
    file = root_dir / "note.txt"
    file.write_text(_TEXT)
    ingest = catalog.ingest_file(
        db, root_id=root_id, path=file, observed=catalog.fingerprint(file), media_type="text/plain"
    )
    pages = [
        PageRecord(page_number=1, text="Cover page", method=PageMethod.PDF_TEXT),
        PageRecord(page_number=2, text=_TEXT, method=PageMethod.OCR, confidence=0.81),
    ]
    catalog.set_pages(db, ingest.revision_id, pages)

    stored = catalog.get_pages(db, ingest.revision_id)
    assert [page.page_number for page in stored] == [1, 2]
    assert stored[1].confidence == 0.81

    hits = search(db, "Rechnung")
    assert hits and hits[0].page_number == 2
    assert search(db, "   ") == []

    destination = tmp_path / "export.txt"
    export_text(db, ingest.revision_id, destination)
    assert "Rechnung fuer Beispiel GmbH" in destination.read_text()
    assert destination.stat().st_mode & 0o777 == 0o600


def test_facts_roundtrip(tmp_path: Path) -> None:
    db, root_id, root_dir = _setup(tmp_path)
    file = root_dir / "note.txt"
    file.write_text(_TEXT)
    ingest = catalog.ingest_file(
        db, root_id=root_id, path=file, observed=catalog.fingerprint(file), media_type="text/plain"
    )
    features = DocumentFeatures(
        document_type="invoice",
        issuer="Beispiel GmbH",
        date="2026-03-14",
        subject="Rechnung",
        language="de",
        needs_review=False,
        evidence=[GroundingEvidence(page=1, source_text="Rechnung fuer Beispiel GmbH")],
    )
    catalog.set_facts(db, ingest.revision_id, features_to_facts(features))

    rebuilt = facts_to_features(catalog.get_facts(db, ingest.revision_id))
    assert rebuilt == features


def test_facts_to_features_fails_closed_on_missing_type() -> None:
    assert facts_to_features([FactRecord(field="issuer", value="X")]) is None
    assert (
        facts_to_features([FactRecord(field="evidence", evidence_page=None, evidence_quote=None)])
        is None
    )


def test_processing_stage_cache_keys(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from renamr.core import processing

    db, root_id, root_dir = _setup(tmp_path)
    file = root_dir / "note.txt"
    file.write_text(_TEXT)
    ingest = catalog.ingest_file(
        db, root_id=root_id, path=file, observed=catalog.fingerprint(file), media_type="text/plain"
    )
    catalog.set_pages(
        db,
        ingest.revision_id,
        [PageRecord(page_number=1, text=_TEXT, method=PageMethod.TEXT)],
    )
    extract_key = processing.text_extract_key(limits_signature="1:2:3:200")
    processing.record_text_result(db, ingest.revision_id, extract_key=extract_key, quality="ok")
    now = datetime.now(UTC)
    revision = catalog.get_revision(db, ingest.revision_id)

    assert (
        processing.plan_stage(
            revision, extract_key=extract_key, feature_key=None, has_pages=True, now=now
        )
        is processing.Stage.SKIP
    )
    feature_key = processing.feature_cache_key("model", "validated_prompt")
    assert (
        processing.plan_stage(
            revision, extract_key=extract_key, feature_key=feature_key, has_pages=True, now=now
        )
        is processing.Stage.FEATURES
    )
    processing.record_feature_result(db, ingest.revision_id, feature_key=feature_key)
    settled = catalog.get_revision(db, ingest.revision_id)
    assert (
        processing.plan_stage(
            settled, extract_key=extract_key, feature_key=feature_key, has_pages=True, now=now
        )
        is processing.Stage.SKIP
    )
    raised = processing.text_extract_key(limits_signature="999:2:3:200")
    assert (
        processing.plan_stage(
            settled, extract_key=raised, feature_key=feature_key, has_pages=True, now=now
        )
        is processing.Stage.EXTRACT
    )


def test_permanent_failure_is_durable_and_retry_resumes(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from renamr.core import processing

    db, root_id, root_dir = _setup(tmp_path)
    file = root_dir / "locked.pdf"
    file.write_text("%PDF")
    ingest = catalog.ingest_file(
        db,
        root_id=root_id,
        path=file,
        observed=catalog.fingerprint(file),
        media_type="application/pdf",
    )
    extract_key = processing.text_extract_key(limits_signature="1:2:3:200")
    processing.record_text_result(
        db,
        ingest.revision_id,
        extract_key=extract_key,
        quality="failed",
        error_category="encrypted",
        permanent=True,
    )
    now = datetime.now(UTC)
    revision = catalog.get_revision(db, ingest.revision_id)
    assert (
        processing.plan_stage(
            revision, extract_key=extract_key, feature_key=None, has_pages=False, now=now
        )
        is processing.Stage.SKIP
    )
    processing.retry_revision(db, ingest.revision_id)
    retried = catalog.get_revision(db, ingest.revision_id)
    assert (
        processing.plan_stage(
            retried, extract_key=extract_key, feature_key=None, has_pages=False, now=now
        )
        is processing.Stage.EXTRACT
    )
