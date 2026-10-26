"""Extraction and feature cache keys plus durable retry state.

Text extraction and feature extraction have independent cache keys so a model or
prompt change reuses cached page text and reruns only the feature stage. A stage
is skipped only when its key is current; permanent failures are cached until the
content changes or an explicit retry is requested, and transient failures back
off with a bounded attempt budget.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import StrEnum

from renamr.core.catalog import RevisionRecord
from renamr.core.db import Database
from renamr.core.settings import Settings
from renamr.local_transport import OutputMode

CACHE_SCHEMA_VERSION = 1
TEXT_EXTRACTOR_VERSION = "extract-v3"
FEATURE_PROMPT_VERSION = "features-v3"
MAX_ATTEMPTS = 5
_BACKOFF_BASE_SECONDS = 30
_BACKOFF_CAP_SECONDS = 3600


class Stage(StrEnum):
    """The next processing stage for a revision."""

    SKIP = "skip"
    EXTRACT = "extract"
    FEATURES = "features"


def text_extract_key(
    extractor_version: str = TEXT_EXTRACTOR_VERSION,
    schema_version: int = CACHE_SCHEMA_VERSION,
    *,
    limits_signature: str = "",
) -> str:
    """Return the page-text cache key.

    Includes the extraction-limit signature so that raising a file/page/pixel
    limit resumes work instead of leaving a truncated result marked current.
    """
    return f"{extractor_version}|schema{schema_version}|{limits_signature}"


def feature_cache_key(
    model: str,
    output_mode: OutputMode,
    *,
    prompt_version: str = FEATURE_PROMPT_VERSION,
    model_digest: str = "",
    schema_version: int = CACHE_SCHEMA_VERSION,
    budget_signature: str = "",
) -> str:
    """Return the feature cache key including prompt version, digest, and budget."""
    return (
        f"{model}|{output_mode}|{prompt_version}|{model_digest}"
        f"|schema{schema_version}|{budget_signature}"
    )


def plan_stage(
    revision: RevisionRecord,
    *,
    extract_key: str,
    feature_key: str | None,
    has_pages: bool,
    now: datetime,
) -> Stage:
    """Decide the next processing stage for a revision."""
    if revision.failed_stage == "text":
        return _retry_or_skip(revision, now, Stage.EXTRACT)
    if revision.extract_key != extract_key or not has_pages:
        return Stage.EXTRACT
    if revision.failed_stage == "features_deferred":
        if feature_key is None or revision.error_category == feature_key:
            return Stage.SKIP
        return Stage.FEATURES
    if revision.failed_stage == "features_blocked":
        if feature_key is None or revision.feature_key == feature_key:
            return Stage.SKIP
        return Stage.FEATURES
    if feature_key is not None and revision.feature_key != feature_key:
        if revision.failed_stage == "features":
            return _retry_or_skip(revision, now, Stage.FEATURES)
        return Stage.FEATURES
    return Stage.SKIP


def in_backoff(revision: RevisionRecord, now: datetime) -> bool:
    """Return whether a revision is waiting out a transient-failure backoff."""
    due = _due(revision)
    return due is not None and now < due


def backoff_until(attempts: int, now: datetime) -> str:
    """Return the ISO timestamp of the next allowed attempt after a failure."""
    seconds = min(_BACKOFF_BASE_SECONDS * (2 ** max(0, attempts - 1)), _BACKOFF_CAP_SECONDS)
    return (now + timedelta(seconds=seconds)).isoformat()


def exhausted(attempts: int) -> bool:
    """Return whether a transient-failure attempt budget is spent."""
    return attempts >= MAX_ATTEMPTS


def record_text_result(
    db: Database,
    revision_id: str,
    *,
    extract_key: str,
    quality: str,
    error_category: str = "",
    permanent: bool = False,
    now: datetime | None = None,
) -> None:
    """Persist a text-extraction outcome.

    On success the extract key becomes current. On failure the key stays stale and
    the failed stage is recorded: a permanent failure or an exhausted budget never
    auto-retries, a transient failure backs off.
    """
    if quality != "failed":
        _update(
            db,
            revision_id,
            {
                "extract_key": extract_key,
                "quality_state": quality,
                "failed_stage": "",
                "attempts": 0,
                "next_retry_at": "",
                "error_category": "",
            },
        )
        return
    attempts = _attempts(db, revision_id) + 1
    terminal = permanent or exhausted(attempts)
    _update(
        db,
        revision_id,
        {
            "quality_state": "failed",
            "failed_stage": "text",
            "attempts": attempts,
            "next_retry_at": "" if terminal else backoff_until(attempts, now or datetime.now(UTC)),
            "error_category": error_category,
        },
    )


def record_feature_result(
    db: Database,
    revision_id: str,
    *,
    feature_key: str,
    error_category: str = "",
    permanent: bool = False,
    now: datetime | None = None,
) -> None:
    """Persist a feature-extraction outcome.

    A valid result (including a grounded abstention) caches the key so unchanged
    content is not re-inferred. A transport/parse failure keeps the old key,
    records the failed stage, and backs off.
    """
    if not error_category:
        _update(
            db,
            revision_id,
            {
                "feature_key": feature_key,
                "failed_stage": "",
                "attempts": 0,
                "next_retry_at": "",
                "error_category": "",
            },
        )
        return
    attempts = _attempts(db, revision_id) + 1
    terminal = permanent or exhausted(attempts)
    _update(
        db,
        revision_id,
        {
            "failed_stage": "features",
            "attempts": attempts,
            "next_retry_at": "" if terminal else backoff_until(attempts, now or datetime.now(UTC)),
            "error_category": error_category,
        },
    )


def retry_revision(db: Database, revision_id: str) -> None:
    """Clear retry/cache state so the next scan redoes the failed stage."""
    from renamr.core import catalog

    revision = catalog.get_revision(db, revision_id)
    fields: dict[str, object] = {
        "failed_stage": "",
        "attempts": 0,
        "next_retry_at": "",
        "error_category": "",
    }
    if revision.failed_stage == "text":
        fields["extract_key"] = ""
        fields["quality_state"] = "pending"
    else:
        fields["feature_key"] = ""
    _update(db, revision_id, fields)


def record_feature_deferred(db: Database, revision_id: str, *, feature_key: str) -> None:
    """Record a non-failure deferral that retries only after the budget changes.

    The feature key is not marked current, so facts cannot be mistaken for a
    complete extraction. The stored key lets a later scan skip the same deferred
    work until the model, prompt, or budget signature changes.
    """
    _update(
        db,
        revision_id,
        {
            "failed_stage": "features_deferred",
            "error_category": feature_key,
            "next_retry_at": "",
            "attempts": 0,
        },
    )


def record_feature_blocked(
    db: Database, revision_id: str, *, feature_key: str, reason: str
) -> None:
    """Record a complete feature abstention while keeping prior facts private.

    The current key is recorded so unchanged content does not retry on every
    tick, but the revision is not eligible for automatic routing.
    """
    _update(
        db,
        revision_id,
        {
            "feature_key": feature_key,
            "failed_stage": "features_blocked",
            "error_category": reason,
            "next_retry_at": "",
            "attempts": 0,
        },
    )


def feature_state_ready(
    revision: RevisionRecord, settings: Settings, *, model_digest: str = ""
) -> bool:
    """Return whether stored facts may drive automatic routing.

    A failed, deferred, blocked, backing-off, or stale-cache feature stage is
    never routing-eligible, even when older facts remain available for local
    inspection.
    """
    if revision.failed_stage or revision.next_retry_at or not revision.feature_key:
        return False
    prefix = f"{settings.model_roles.small}|{settings.output_mode}|{FEATURE_PROMPT_VERSION}|"
    if not revision.feature_key.startswith(prefix):
        return False
    if model_digest:
        expected = feature_cache_key(
            settings.model_roles.small,
            settings.output_mode,
            model_digest=model_digest,
            budget_signature=str(settings.feature_max_chars),
        )
        return revision.feature_key == expected
    return True


def retry_deferred_features(db: Database, root_ids: list[str]) -> int:
    """Clear deferred feature markers for selected roots before a manual retry."""
    from renamr.core import catalog

    retried = 0
    for root_id in dict.fromkeys(root_ids):
        for location in catalog.locations_for_root(db, root_id):
            revision = catalog.get_revision(db, location.revision_id)
            if revision.failed_stage != "features_deferred":
                continue
            retry_revision(db, revision.revision_id)
            retried += 1
    return retried


def _retry_or_skip(revision: RevisionRecord, now: datetime, stage: Stage) -> Stage:
    """Return the stage when its backoff is due, else ``SKIP`` for a terminal one."""
    due = _due(revision)
    if due is None:
        return Stage.SKIP
    return stage if now >= due else Stage.SKIP


def _due(revision: RevisionRecord) -> datetime | None:
    """Parse a revision's next-retry timestamp, or None when terminal/unset."""
    if not revision.next_retry_at:
        return None
    try:
        return datetime.fromisoformat(revision.next_retry_at)
    except ValueError:
        return None


def _attempts(db: Database, revision_id: str) -> int:
    """Return the stored attempt count for a revision."""
    from renamr.core import catalog

    return catalog.get_revision(db, revision_id).attempts


def _update(db: Database, revision_id: str, fields: dict[str, object]) -> None:
    """Apply an allowlisted set of revision columns in one short transaction."""
    from renamr.core import catalog

    allowed = (
        "extract_key",
        "feature_key",
        "quality_state",
        "failed_stage",
        "attempts",
        "next_retry_at",
        "error_category",
    )
    filtered = {key: value for key, value in fields.items() if key in allowed}
    if not filtered:
        return
    revision = catalog.get_revision(db, revision_id)
    values = {name: getattr(revision, name) for name in allowed}
    values.update(filtered)
    with db.transaction() as connection:
        connection.execute(
            "UPDATE revisions SET extract_key = ?, feature_key = ?, quality_state = ?, "
            "failed_stage = ?, attempts = ?, next_retry_at = ?, error_category = ? "
            "WHERE revision_id = ?",
            (
                values["extract_key"],
                values["feature_key"],
                values["quality_state"],
                values["failed_stage"],
                values["attempts"],
                values["next_retry_at"],
                values["error_category"],
                revision_id,
            ),
        )
