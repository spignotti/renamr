"""Bounded feature-stage execution and partial-result handling."""

from __future__ import annotations

from typing import TYPE_CHECKING

from renamr.core import catalog, processing
from renamr.core.catalog import PageRecord
from renamr.core.db import Database
from renamr.core.facts import record_review_once
from renamr.core.features import FeatureExtractor, FeatureRequest, features_to_facts

if TYPE_CHECKING:
    from renamr.core.budget import BudgetTracker
    from renamr.core.reconcile import ScanOutcome


def run_features(
    db: Database,
    revision_id: str,
    pages: list[PageRecord],
    extractor: FeatureExtractor | None,
    feature_key: str,
    outcome: ScanOutcome,
    tracker: BudgetTracker | None,
) -> None:
    """Run the feature stage and record only complete, validated results."""
    if extractor is None:
        return
    extraction = extractor.extract(
        FeatureRequest(
            page_texts=[page.text for page in pages],
            max_model_calls=None if tracker is None else tracker.model_room(),
            deadline_monotonic=None if tracker is None else tracker.deadline(),
        )
    )
    outcome.features_run += 1
    outcome.model_calls += extraction.calls if extraction.calls > 0 else max(1, extraction.chunks)
    if extraction.features is not None and not extraction.incomplete:
        catalog.set_facts(db, revision_id, features_to_facts(extraction.features))
        processing.record_feature_result(db, revision_id, feature_key=feature_key)
        _resolve_feature_reviews(db, revision_id)
        return
    reason = extraction.blocked_reason or "incomplete_features"
    if extraction.features is not None:
        if extraction.incomplete:
            _record_partial_features(db, revision_id, feature_key, reason, outcome)
        return
    if extraction.incomplete and _is_budget_reason(reason):
        _record_budget_deferral(db, revision_id, feature_key, reason, outcome)
        return
    if extraction.incomplete and reason.startswith("inference_"):
        processing.record_feature_result(
            db, revision_id, feature_key=feature_key, error_category=reason
        )
        return
    processing.record_feature_result(db, revision_id, feature_key=feature_key)
    catalog.record_review(db, kind="feature_blocked", detail=reason, revision_id=revision_id)
    outcome.reviews += 1


def _record_partial_features(
    db: Database,
    revision_id: str,
    feature_key: str,
    reason: str,
    outcome: ScanOutcome,
) -> None:
    """Keep a partial-success feature result unresolved and non-routable."""
    if _is_budget_reason(reason):
        processing.record_feature_deferred(db, revision_id, feature_key=feature_key)
    elif reason.startswith("inference_"):
        processing.record_feature_result(
            db, revision_id, feature_key=feature_key, error_category=reason
        )
    else:
        processing.record_feature_deferred(db, revision_id, feature_key=feature_key)
    record_review_once(
        db,
        kind="feature_incomplete",
        detail=f"{revision_id}|{reason}",
        revision_id=revision_id,
    )
    outcome.deferred += 1
    outcome.budget_exhausted = _is_budget_reason(reason)


def _record_budget_deferral(
    db: Database,
    revision_id: str,
    feature_key: str,
    reason: str,
    outcome: ScanOutcome,
) -> None:
    """Record a bounded-budget deferral with an actionable review."""
    processing.record_feature_deferred(db, revision_id, feature_key=feature_key)
    record_review_once(
        db,
        kind="feature_deferred",
        detail=f"{revision_id}|{reason}",
        revision_id=revision_id,
    )
    outcome.deferred += 1
    outcome.budget_exhausted = True


def _is_budget_reason(reason: str) -> bool:
    """Return whether a blocked reason is a request/time budget deferral."""
    return reason in ("feature_budget_exhausted", "feature_time_budget_exhausted")


def _resolve_feature_reviews(db: Database, revision_id: str) -> None:
    """Resolve open feature questions after a complete, validated retry."""
    with db.transaction() as connection:
        connection.execute(
            "UPDATE review_items SET status = 'resolved' WHERE revision_id = ? "
            "AND status = 'open' AND kind IN ('feature_deferred', 'feature_incomplete', "
            "'feature_blocked', 'features_unresolved')",
            (revision_id,),
        )
