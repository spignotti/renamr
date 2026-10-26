"""Focused review questions and their explicit resolutions.

Resolving a review records a one-off label or confirms one observed manual move.
It never activates a broad rule: generalization stays a separate draft/test/
activate workflow. Unsupported or still-ambiguous cases remain open.
"""

from __future__ import annotations

import sqlite3

from pydantic import BaseModel, ConfigDict

from renamr.core import catalog, examples, rules
from renamr.core.catalog import FactRecord
from renamr.core.db import Database
from renamr.core.errors import CatalogError
from renamr.core.facts import set_facts

_LABEL_FIELDS = ("document_type", "issuer", "date", "subject", "language", "entity_id")
_MOVE_CONFIRM = "manual_move"
_NAMING = "naming_field_missing"


class ReviewQuestion(BaseModel):
    """A focused local question for one open review item."""

    model_config = ConfigDict(extra="forbid")

    review_id: str
    kind: str
    detail: str
    question: str
    revision_id: str | None = None
    location_id: str | None = None


def review_questions(db: Database) -> list[ReviewQuestion]:
    """Return open review items with a focused question each."""
    questions: list[ReviewQuestion] = []
    for item in catalog.open_review_items(db):
        kind = str(item["kind"])
        questions.append(
            ReviewQuestion(
                review_id=str(item["review_id"]),
                kind=kind,
                detail=str(item["detail"]),
                question=_question(kind, str(item["detail"])),
                revision_id=_optional(item.get("revision_id")),
                location_id=_optional(item.get("location_id")),
            )
        )
    return questions


def resolve_label(
    db: Database,
    review_id: str,
    *,
    fields: dict[str, str],
    source: examples.ExampleSource = examples.ExampleSource.USER_LABEL,
) -> examples.Example:
    """Store confirmed labels as one-off facts plus a separate example.

    Raises:
        CatalogError: If the review or its revision is unknown, or a field is
            unsupported.
    """
    review = _review(db, review_id)
    revision_id = _optional(review["revision_id"])
    if revision_id is None:
        raise CatalogError("review has no document revision to label")
    cleaned = _clean_fields(fields)
    _replace_confirmed_facts(db, revision_id, cleaned)
    example = examples.add_example(
        db,
        source=source,
        revision_id=revision_id,
        location_id=_optional(review["location_id"]),
        label=str(review["kind"]),
        payload=cleaned,
    )
    _resolve(db, review_id)
    return example


def resolve_negative(db: Database, review_id: str, *, fields: dict[str, str]) -> examples.Example:
    """Record one rejected pattern as a negative example, not a rule.

    Raises:
        CatalogError: If the review or fields are invalid.
    """
    review = _review(db, review_id)
    cleaned = _clean_fields(fields)
    example = examples.add_example(
        db,
        source=examples.ExampleSource.NEGATIVE,
        revision_id=_optional(review["revision_id"]),
        location_id=_optional(review["location_id"]),
        label="negative",
        payload=cleaned,
    )
    _resolve(db, review_id)
    return example


def resolve_manual_move(db: Database, review_id: str, *, accept: bool) -> examples.Example | None:
    """Confirm or reject one unique manual-move candidate.

    Ambiguous, copied, offline, and app-generated moves are refused and stay
    open; they never become examples.

    Raises:
        CatalogError: If the review is unknown or the candidate is not unique.
    """
    review = _review(db, review_id)
    observation = _observation_for(db, review)
    if observation is None:
        raise CatalogError("no unique manual move is available for this review")
    if not accept:
        _set_observation_status(db, observation.observation_id, "rejected")
        _resolve(db, review_id)
        return None
    if observation.status != "candidate":
        raise CatalogError("this move is not a unique confirmed candidate")
    example = examples.confirm_manual_move(db, observation.observation_id)
    _resolve(db, review_id)
    return example


def resolve_naming(db: Database, review_id: str, *, template: str) -> rules.Rule:
    """Replace a rule's template after the user explicitly chooses one.

    The replacement is a draft revision, so it still has to be tested and
    activated before it can route anything.

    Raises:
        CatalogError: If the review is not a naming question or the detail is
            malformed.
    """
    review = _review(db, review_id)
    if str(review["kind"]) != _NAMING:
        raise CatalogError("review is not a naming-field question")
    detail = str(review["detail"])
    rule_id = detail.split("|", 1)[0]
    if not rule_id:
        raise CatalogError("naming review has no rule reference")
    revised = rules.revise_rule(
        db, rule_id, filename_template=template, status=str(rules.RuleStatus.DRAFT)
    )
    _resolve(db, review_id)
    return revised


def dismiss_review(db: Database, review_id: str, *, note: str = "") -> None:
    """Mark one review item resolved without creating an example."""
    _ = note
    _resolve(db, review_id)


def _question(kind: str, detail: str) -> str:
    """Return a focused question for a review kind."""
    if kind in (
        "features_unresolved",
        "feature_blocked",
        "extraction_failed",
        "quality_incomplete",
    ):
        return "What document type, issuer, date, and subject are supported by the page text?"
    if kind in ("possible_move", "path_missing"):
        return "Was this file moved manually to the observed location?"
    if kind == _NAMING:
        return (
            "The naming template needs a value that is not confirmed. Supply the field or "
            "choose a template that omits it."
        )
    if kind == "rule_conflict":
        return "Two active rules are equally specific. Which destination should win?"
    if kind == "source_changed_since_scan":
        return "The source changed after indexing. Rescan before planning this file?"
    if kind == "target_rejected":
        return "The planned destination is not allowed. Register/allow it or leave the file."
    if kind in ("operation_blocked", "operation_failed", "undo_refused"):
        return f"The file operation was refused ({detail}). Inspect and resolve manually."
    return "Review this item and choose an explicit action."


def _review(db: Database, review_id: str) -> sqlite3.Row:
    """Return one review row or raise."""
    with db.read() as connection:
        row = connection.execute(
            "SELECT * FROM review_items WHERE review_id = ?", (review_id,)
        ).fetchone()
    if row is None:
        raise CatalogError("unknown review id")
    return row


def _observation_for(db: Database, review: sqlite3.Row) -> examples.ManualMoveObservation | None:
    """Return the latest observation for a review's missing location."""
    revision_id = _optional(review["revision_id"])
    location_id = _optional(review["location_id"])
    if revision_id is None or location_id is None:
        return None
    examples.observe_manual_moves(db)
    for observation in examples.list_observations(db):
        if observation.revision_id == revision_id and observation.old_location_id == location_id:
            return observation
    return None


def _set_observation_status(db: Database, observation_id: str, status: str) -> None:
    """Update one observation's verdict."""
    with db.transaction() as connection:
        connection.execute(
            "UPDATE manual_move_observations SET status = ? WHERE observation_id = ?",
            (status, observation_id),
        )


def _replace_confirmed_facts(db: Database, revision_id: str, fields: dict[str, str]) -> None:
    """Replace only the confirmed scalar facts and keep other evidence."""
    kept = [
        fact
        for fact in catalog.get_facts(db, revision_id)
        if fact.field not in fields or fact.field == "evidence"
    ]
    confirmed = [
        FactRecord(field=name, value=value, validation_state="user_confirmed")
        for name, value in fields.items()
    ]
    set_facts(db, revision_id, [*kept, *confirmed])


def _clean_fields(fields: dict[str, str]) -> dict[str, str]:
    """Validate and trim confirmed label fields."""
    cleaned: dict[str, str] = {}
    for name, value in fields.items():
        if name not in _LABEL_FIELDS:
            raise CatalogError(f"unsupported label field: {name}")
        stripped = value.strip()
        if len(stripped) > 200:
            raise CatalogError("label value is too long")
        if stripped:
            cleaned[name] = stripped
    if not cleaned:
        raise CatalogError("at least one confirmed label is required")
    return cleaned


def _resolve(db: Database, review_id: str) -> None:
    """Mark a review item resolved."""
    with db.transaction() as connection:
        connection.execute(
            "UPDATE review_items SET status = 'resolved' WHERE review_id = ? AND status = 'open'",
            (review_id,),
        )


def _optional(value: object) -> str | None:
    """Return a non-empty string or None."""
    return None if value is None else str(value)
