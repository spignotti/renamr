"""Incremental scan and reconciliation of a registered root.

Unchanged stat-identified content is reused without re-hashing or re-extracting.
Text extraction and feature extraction use independent cache keys, so a changed
model or prompt reruns only the feature stage. Permanent failures are durable
until the content changes or an explicit retry; transient failures back off.
Hidden/system directories are pruned, and only an active (unconsumed) own
operation suppresses a path. A missing or unmounted root is never marked deleted.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from renamr.core import catalog, feature_stage, processing
from renamr.core.budget import BudgetTracker, ScanBudget
from renamr.core.catalog import Presence, QualityState
from renamr.core.db import Database
from renamr.core.extract import ExtractorLimits, detect_media_type, estimate_page_count
from renamr.core.extract_runner import ExtractionResult, ExtractionRunner, SubprocessRunner
from renamr.core.facts import record_review_once
from renamr.core.features import FeatureExtractor
from renamr.core.hashing import exceeds_limit, fingerprint, stat_matches
from renamr.core.paths import is_within
from renamr.core.roots import Root, list_roots
from renamr.core.settings import Settings, load_settings

SUPPORTED_SUFFIXES = frozenset(
    {".pdf", ".txt", ".md", ".text", ".csv", ".log"}
    | {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif", ".webp"}
)
_PERMANENT_ERRORS = frozenset({"encrypted", "unreadable", "unsupported_type", "empty_document"})
_SKIPPED_DIR_NAMES = frozenset({".git", ".svn", ".hg", "node_modules", "__pycache__", "Library"})


class ScanStatus(StrEnum):
    """Overall outcome of one root scan."""

    OK = "ok"
    UNAVAILABLE = "unavailable"
    PARTIAL = "partial"


class ScanOutcome(BaseModel):
    """Bounded scan statistics and sanitized error codes."""

    model_config = ConfigDict(extra="forbid")

    root_id: str
    status: ScanStatus
    discovered: int = 0
    new_revisions: int = 0
    changed: int = 0
    unchanged: int = 0
    extracted: int = 0
    features_run: int = 0
    model_calls: int = 0
    pages_extracted: int = 0
    deferred: int = 0
    budget_exhausted: bool = False
    missing: int = 0
    reviews: int = 0
    skipped: int = 0
    oversized: int = 0
    errors: list[str] = []


def scan_root(
    db: Database,
    root: Root,
    *,
    runner: ExtractionRunner | None = None,
    extractor: FeatureExtractor | None = None,
    limits: ExtractorLimits | None = None,
    settings: Settings | None = None,
    wait_stable: bool = True,
    model_digest: str = "",
    budget: ScanBudget | None = None,
) -> ScanOutcome:
    """Scan one registered root incrementally and update the catalog.

    An unavailable root is reported without deletions. Tests inject
    :class:`InProcessRunner`; production uses the bounded subprocess runner. A
    budget stops work between documents and defers a file whose page count does
    not fit the remaining page budget.
    """
    effective = settings or load_settings(db)
    bounds = limits or limits_from_settings(effective)
    extraction = runner or SubprocessRunner()
    if not root.canonical_path.is_dir():
        return ScanOutcome(root_id=root.root_id, status=ScanStatus.UNAVAILABLE)
    keys = _scan_keys(effective, extractor, bounds, model_digest)
    outcome = ScanOutcome(root_id=root.root_id, status=ScanStatus.OK)
    tracker = (
        BudgetTracker(budget, allow_models=extractor is not None) if budget is not None else None
    )
    seen, new_hashes = _scan_candidates(
        db, root, extraction, extractor, bounds, keys, outcome, tracker, wait_stable, effective
    )
    _reconcile_missing(db, root, seen, new_hashes, outcome)
    if outcome.reviews or outcome.errors or outcome.oversized or outcome.deferred:
        outcome.status = ScanStatus.PARTIAL
    return outcome


class _ScanKeys(BaseModel):
    """Precomputed cache keys for one scan."""

    model_config = ConfigDict(extra="forbid")

    extract: str
    feature: str | None


def limits_from_settings(settings: Settings) -> ExtractorLimits:
    """Return extraction limits derived from settings."""
    return ExtractorLimits(
        max_pages=settings.max_pages,
        max_render_pixels=settings.max_render_pixels,
        max_file_bytes=settings.max_file_bytes,
        timeout_s=settings.extraction_timeout_s,
    )


def _scan_keys(
    settings: Settings,
    extractor: FeatureExtractor | None,
    limits: ExtractorLimits,
    model_digest: str,
) -> _ScanKeys:
    """Build the independent text and feature cache keys for a scan."""
    extract_key = processing.text_extract_key(limits_signature=limits.signature())
    feature_key = None
    if extractor is not None:
        feature_key = processing.feature_cache_key(
            _routine_model(settings),
            settings.output_mode,
            model_digest=model_digest,
            budget_signature=feature_budget_signature(settings),
        )
    return _ScanKeys(extract=extract_key, feature=feature_key)


def _routine_model(settings: Settings) -> str:
    """Return the configured routine classification model (small role)."""
    return settings.model_roles.small


def feature_budget_signature(settings: Settings) -> str:
    """Return the cache signature for the model request character budget."""
    return str(settings.feature_max_chars)


def _scan_candidates(
    db: Database,
    root: Root,
    extraction: ExtractionRunner,
    extractor: FeatureExtractor | None,
    bounds: ExtractorLimits,
    keys: _ScanKeys,
    outcome: ScanOutcome,
    tracker: BudgetTracker | None,
    wait_stable: bool,
    settings: Settings,
) -> tuple[set[str], dict[str, list[str]]]:
    """Walk candidates under one root and stop between documents at a budget."""
    seen: set[str] = set()
    new_hashes: dict[str, list[str]] = {}
    for path in _iter_candidates(root.canonical_path, list_roots(db)):
        if path.suffix.lower() not in SUPPORTED_SUFFIXES:
            outcome.skipped += 1
            continue
        if wait_stable and _recently_modified(path, settings.stability_window_s):
            outcome.skipped += 1
            continue
        if tracker is not None and tracker.exhausted():
            outcome.budget_exhausted = True
            break
        if tracker is not None and _defer_large(db, path, tracker, outcome):
            seen.add(str(path))
            continue
        seen.add(str(path))
        before = (outcome.pages_extracted, outcome.model_calls)
        worked = _scan_candidate(
            db, root, path, extraction, extractor, bounds, keys, outcome, new_hashes, tracker
        )
        if tracker is not None and worked:
            tracker.record(outcome, before)
    outcome.discovered = len(seen)
    return seen, new_hashes


def _scan_candidate(
    db: Database,
    root: Root,
    path: Path,
    runner: ExtractionRunner,
    extractor: FeatureExtractor | None,
    limits: ExtractorLimits,
    keys: _ScanKeys,
    outcome: ScanOutcome,
    new_hashes: dict[str, list[str]],
    tracker: BudgetTracker | None,
) -> bool:
    """Ingest one candidate (with a stat fast path) and process it.

    Returns:
        Whether extraction/feature work was performed for this candidate.
    """
    try:
        info = path.stat()
    except OSError:
        outcome.skipped += 1
        return False
    if exceeds_limit(info.st_size, limits.max_file_bytes):
        _record_oversized(db, path, outcome)
        return False
    existing = catalog.get_location_by_path(db, path)
    revision_id, created, changed, new_location = _resolve_revision(
        db, root, path, existing, outcome
    )
    if created:
        outcome.new_revisions += 1
    elif changed:
        outcome.changed += 1
    if created or new_location:
        revision = catalog.get_revision(db, revision_id)
        new_hashes.setdefault(revision.content_hash, []).append(str(path))
    stage = processing.plan_stage(
        catalog.get_revision(db, revision_id),
        extract_key=keys.extract,
        feature_key=keys.feature,
        has_pages=catalog.revision_has_pages(db, revision_id),
        now=datetime.now(UTC),
    )
    if stage is processing.Stage.SKIP:
        outcome.unchanged += 1
        return False
    _process_stage(db, path, revision_id, stage, runner, extractor, limits, keys, outcome, tracker)
    return True


def _process_stage(
    db: Database,
    path: Path,
    revision_id: str,
    stage: processing.Stage,
    runner: ExtractionRunner,
    extractor: FeatureExtractor | None,
    limits: ExtractorLimits,
    keys: _ScanKeys,
    outcome: ScanOutcome,
    tracker: BudgetTracker | None,
) -> None:
    """Run the extraction and/or feature stage for one revision."""
    if stage is processing.Stage.EXTRACT:
        result = runner.run(path, limits)
        _store_extraction(db, revision_id, result, keys.extract, outcome)
        if result.pages and keys.feature is not None:
            feature_stage.run_features(
                db, revision_id, result.pages, extractor, keys.feature, outcome, tracker
            )
        return
    pages = catalog.get_pages(db, revision_id)
    if keys.feature is not None:
        feature_stage.run_features(
            db, revision_id, pages, extractor, keys.feature, outcome, tracker
        )


def _resolve_revision(
    db: Database,
    root: Root,
    path: Path,
    existing: catalog.LocationRecord | None,
    outcome: ScanOutcome,
) -> tuple[str, bool, bool, bool]:
    """Return (revision id, created, changed, new location), hashing only when needed."""
    if existing is not None and _stat_unchanged(path, existing):
        return existing.revision_id, False, False, False
    observed = fingerprint(path)
    ingest = catalog.ingest_file(
        db, root_id=root.root_id, path=path, observed=observed, media_type=detect_media_type(path)
    )
    return (
        ingest.revision_id,
        ingest.revision_created,
        ingest.revision_changed,
        ingest.location_created,
    )


def _stat_unchanged(path: Path, location: catalog.LocationRecord) -> bool:
    """Return whether a path still matches its stored stat fingerprint."""
    try:
        return stat_matches(
            path,
            dev=location.dev,
            ino=location.ino,
            size_bytes=location.size_bytes,
            mtime_ns=location.mtime_ns,
        )
    except OSError:
        return False


def _store_extraction(
    db: Database, revision_id: str, result: ExtractionResult, extract_key: str, outcome: ScanOutcome
) -> None:
    """Persist extracted pages and the text-stage outcome."""
    catalog.set_pages(db, revision_id, result.pages)
    outcome.pages_extracted += len(result.pages)
    for code in result.errors:
        _note(outcome.errors, code)
    quality = _quality(result, outcome)
    if result.pages or not result.errors:
        processing.record_text_result(db, revision_id, extract_key=extract_key, quality=quality)
        outcome.extracted += 1
        return
    category = result.errors[0]
    processing.record_text_result(
        db,
        revision_id,
        extract_key=extract_key,
        quality="failed",
        error_category=category,
        permanent=category in _PERMANENT_ERRORS,
    )
    catalog.record_review(db, kind="extraction_failed", detail=category, revision_id=revision_id)
    outcome.reviews += 1


def _quality(result: ExtractionResult, outcome: ScanOutcome) -> str:
    """Return an explicit quality state for a stored extraction."""
    if not result.pages:
        return QualityState.FAILED.value if result.errors else QualityState.OK.value
    if result.incomplete:
        return QualityState.PARTIAL.value
    if any(page.quality is QualityState.FAILED for page in result.pages):
        return QualityState.PARTIAL.value
    if any(page.quality is QualityState.LOW for page in result.pages):
        return QualityState.LOW.value
    return QualityState.OK.value


def _defer_large(db: Database, path: Path, tracker: BudgetTracker, outcome: ScanOutcome) -> bool:
    """Defer a changed file whose page count exceeds the remaining page budget."""
    existing = catalog.get_location_by_path(db, path)
    if existing is not None and _stat_unchanged(path, existing):
        return False
    room = tracker.page_room()
    if room is None:
        return False
    estimate = estimate_page_count(path)
    if estimate <= room:
        return False
    label = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:16]
    record_review_once(db, kind="scan_budget_deferred", detail=f"{label}|{estimate}")
    outcome.deferred += 1
    outcome.budget_exhausted = True
    return True


def _record_oversized(db: Database, path: Path, outcome: ScanOutcome) -> None:
    """Record a durable review for a file over the byte limit without hashing it."""
    label = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:16]
    record_review_once(db, kind="oversized_file", detail=label)
    outcome.oversized += 1


def _iter_candidates(root_path: Path, registered: list[Root]) -> Iterator[Path]:
    """Yield candidate files, pruning hidden/system/symlink and nested roots."""
    skip_roots = [root.canonical_path for root in registered if root.canonical_path != root_path]
    for current, dirs, files in os.walk(root_path, topdown=True, followlinks=False):
        directory = Path(current)
        dirs[:] = [name for name in sorted(dirs) if not _skip_dir(directory / name, skip_roots)]
        for name in sorted(files):
            candidate = directory / name
            if candidate.is_symlink() or name.startswith(".") or name.endswith(".icloud"):
                continue
            yield candidate


def _skip_dir(path: Path, skip_roots: list[Path]) -> bool:
    """Return whether a directory should not be descended into."""
    if path.is_symlink() or path.name.startswith("."):
        return True
    if path.name in _SKIPPED_DIR_NAMES:
        return True
    return any(is_within(path, other) for other in skip_roots)


def _recently_modified(path: Path, window_s: float) -> bool:
    """Return whether a file was modified within the stability window."""
    try:
        age = datetime.now(UTC).timestamp() - path.stat().st_mtime
    except OSError:
        return True
    return age < window_s


def _reconcile_missing(
    db: Database,
    root: Root,
    seen: set[str],
    new_hashes: dict[str, list[str]],
    outcome: ScanOutcome,
) -> None:
    """Mark absent locations missing and record conservative review items."""
    for location in catalog.locations_for_root(db, root.root_id):
        canonical = str(location.canonical_path)
        if canonical in seen or location.presence is Presence.MISSING:
            continue
        if Path(canonical).exists():
            continue
        catalog.mark_presence(db, location.location_id, Presence.MISSING)
        outcome.missing += 1
        if _active_own_operation(db, canonical):
            continue
        revision = catalog.get_revision(db, location.revision_id)
        moved_elsewhere = any(
            item.location_id != location.location_id and item.presence is Presence.PRESENT
            for item in catalog.locations_for_revision(db, revision.revision_id)
        )
        kind = (
            "possible_move"
            if new_hashes.get(revision.content_hash) or moved_elsewhere
            else "path_missing"
        )
        catalog.record_review(
            db,
            kind=kind,
            detail="pending_review",
            revision_id=location.revision_id,
            location_id=location.location_id,
        )
        outcome.reviews += 1


def _active_own_operation(db: Database, canonical_path: str) -> bool:
    """Return whether an unconsumed own operation still covers a path."""
    with db.read() as connection:
        row = connection.execute(
            "SELECT 1 FROM run_items WHERE (source_path = ? OR target_path = ?) "
            "AND state NOT IN ('committed', 'noop', 'blocked', 'failed') LIMIT 1",
            (canonical_path, canonical_path),
        ).fetchone()
    return row is not None


def _note(errors: list[str], code: str) -> None:
    """Append a sanitized error code once."""
    if code not in errors:
        errors.append(code)
