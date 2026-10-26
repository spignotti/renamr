"""P2 tests: incremental reconciliation, caching, and conservative observations."""

from __future__ import annotations

import shutil
from pathlib import Path

from renamr.core import catalog, operations, roots
from renamr.core.db import Database
from renamr.core.extract_runner import InProcessRunner
from renamr.core.reconcile import ScanOutcome, ScanStatus, scan_root
from renamr.core.roots import RootRole, register_root
from renamr.core.settings import Settings
from tests.unit.core_support import FakeExtractor, FakeOCR, make_blank_pdf, ocr_text


def _setup(tmp_path: Path) -> tuple[Database, str, Path]:
    db = Database(tmp_path / "state" / "catalog.sqlite3")
    db.initialize()
    library = tmp_path / "library"
    library.mkdir()
    root = register_root(db, path=library, role=RootRole.LIBRARY, state_dir=tmp_path / "state")
    return db, root.root_id, library


def _scan(
    db: Database, root_id: str, runner: InProcessRunner, extractor: FakeExtractor
) -> ScanOutcome:
    root = roots.get_root(db, root_id)
    return scan_root(
        db,
        root,
        runner=runner,
        extractor=extractor,
        settings=Settings(),
        wait_stable=False,
    )


def _reviews(db: Database) -> list[str]:
    return [str(item["kind"]) for item in catalog.open_review_items(db)]


def test_scan_extracts_and_caches_unchanged(tmp_path: Path) -> None:
    db, root_id, library = _setup(tmp_path)
    make_blank_pdf(library / "scan.pdf", page_count=1)
    ocr = FakeOCR([ocr_text("Rechnung von Beispiel", 0.9), ocr_text("unused", 0.9)])
    extractor = FakeExtractor()
    runner = InProcessRunner(ocr)

    first = _scan(db, root_id, runner, extractor)
    assert first.new_revisions == 1
    assert extractor.calls == 1
    assert ocr.calls == 1

    location = catalog.locations_for_root(db, root_id)[0]
    pages = catalog.get_pages(db, location.revision_id)
    assert pages and pages[0].text
    assert catalog.get_facts(db, location.revision_id)

    second = _scan(db, root_id, runner, extractor)
    assert second.unchanged == 1
    assert extractor.calls == 1
    assert ocr.calls == 1


def test_offline_root_is_unavailable_not_empty(tmp_path: Path) -> None:
    db, root_id, library = _setup(tmp_path)
    make_blank_pdf(library / "scan.pdf", page_count=1)
    runner = InProcessRunner(FakeOCR([ocr_text("text", 0.9)]))
    _scan(db, root_id, runner, FakeExtractor())

    moved = tmp_path / "library-moved"
    shutil.move(str(library), str(moved))
    outcome = _scan(db, root_id, runner, FakeExtractor())

    assert outcome.status is ScanStatus.UNAVAILABLE
    assert outcome.missing == 0
    location = catalog.locations_for_root(db, root_id)[0]
    assert catalog.get_location(db, location.location_id).presence is catalog.Presence.PRESENT


def test_missing_file_creates_review_not_deletion(tmp_path: Path) -> None:
    db, root_id, library = _setup(tmp_path)
    file = library / "scan.pdf"
    make_blank_pdf(file, page_count=1)
    runner = InProcessRunner(FakeOCR([ocr_text("text", 0.9)]))
    _scan(db, root_id, runner, FakeExtractor())

    file.unlink()
    outcome = _scan(db, root_id, runner, FakeExtractor())

    assert outcome.missing == 1
    assert "path_missing" in _reviews(db)
    revision_ids = {location.revision_id for location in catalog.locations_for_root(db, root_id)}
    assert revision_ids  # the document is still catalogued


def test_manual_move_becomes_pending_review_candidate(tmp_path: Path) -> None:
    db, root_id, library = _setup(tmp_path)
    original = library / "scan.pdf"
    make_blank_pdf(original, page_count=1)
    runner = InProcessRunner(FakeOCR([ocr_text("text", 0.9)]))
    _scan(db, root_id, runner, FakeExtractor())
    revision_id = catalog.locations_for_root(db, root_id)[0].revision_id

    shutil.move(str(original), str(library / "moved.pdf"))
    outcome = _scan(db, root_id, runner, FakeExtractor())

    assert outcome.missing == 1
    assert "possible_move" in _reviews(db)
    locations = catalog.locations_for_root(db, root_id)
    assert {location.revision_id for location in locations} == {revision_id}
    assert len(locations) == 2


def test_app_operations_are_not_training_signals(tmp_path: Path) -> None:
    db, root_id, library = _setup(tmp_path)
    file = library / "scan.pdf"
    make_blank_pdf(file, page_count=1)
    runner = InProcessRunner(FakeOCR([ocr_text("text", 0.9)]))
    _scan(db, root_id, runner, FakeExtractor())

    run_id = operations.create_run(db, kind="rename", root_id=root_id)
    operations.add_plan_item(
        db,
        run_id,
        source_path=file,
        target_path=library / "renamed.pdf",
        content_hash=catalog.fingerprint(file).content_hash,
        size_bytes=file.stat().st_size,
    )
    file.unlink()
    outcome = _scan(db, root_id, runner, FakeExtractor())

    assert outcome.missing == 1
    assert "possible_move" not in _reviews(db)
    assert "path_missing" not in _reviews(db)
