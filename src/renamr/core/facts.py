"""Typed document facts and the review queue.

Facts carry their supporting page and quote; review items record unresolved
identity, extraction, and operation questions. Both are keyed by revision so
feature and contract modules can attach concrete records without duplicating page
text.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict

from renamr.core.db import Database
from renamr.core.ids import new_id


class FactRecord(BaseModel):
    """A typed extracted fact with optional grounding evidence."""

    model_config = ConfigDict(extra="forbid")

    field: str
    value: str | None = None
    evidence_page: int | None = None
    evidence_quote: str | None = None
    extractor_version: str = ""
    validation_state: str = "validated"


def set_facts(db: Database, revision_id: str, facts: list[FactRecord]) -> None:
    """Replace all facts for a revision."""
    now = datetime.now(UTC).isoformat()
    with db.transaction() as connection:
        connection.execute("DELETE FROM facts WHERE revision_id = ?", (revision_id,))
        for fact in facts:
            connection.execute(
                "INSERT INTO facts (fact_id, revision_id, field, value, evidence_page, "
                "evidence_quote, extractor_version, validation_state, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    new_id("fact"),
                    revision_id,
                    fact.field,
                    fact.value,
                    fact.evidence_page,
                    fact.evidence_quote,
                    fact.extractor_version,
                    fact.validation_state,
                    now,
                ),
            )


def get_facts(db: Database, revision_id: str) -> list[FactRecord]:
    """Return all facts for a revision ordered by field."""
    with db.read() as connection:
        rows = connection.execute(
            "SELECT * FROM facts WHERE revision_id = ? ORDER BY field", (revision_id,)
        ).fetchall()
    return [_row_to_fact(row) for row in rows]


def record_review(
    db: Database,
    *,
    kind: str,
    detail: str = "",
    revision_id: str | None = None,
    location_id: str | None = None,
) -> str:
    """Record a review item, returning an existing open one for the same target.

    Deduplicated on ``(revision_id, kind)`` so repeated failures of the same
    revision never pile up duplicate open questions.
    """
    now = datetime.now(UTC).isoformat()
    review_id = new_id("review")
    with db.transaction() as connection:
        existing = connection.execute(
            "SELECT review_id FROM review_items WHERE revision_id IS ? AND location_id IS ? "
            "AND kind = ? AND status = 'open' ORDER BY created_at LIMIT 1",
            (revision_id, location_id, kind),
        ).fetchone()
        if existing is not None:
            return str(existing["review_id"])
        try:
            connection.execute(
                "INSERT INTO review_items (review_id, revision_id, location_id, kind, detail, "
                "status, created_at) VALUES (?, ?, ?, ?, ?, 'open', ?)",
                (review_id, revision_id, location_id, kind, detail, now),
            )
        except sqlite3.IntegrityError:
            row = connection.execute(
                "SELECT review_id FROM review_items WHERE revision_id IS ? AND location_id IS ? "
                "AND kind = ? AND status = 'open' LIMIT 1",
                (revision_id, location_id, kind),
            ).fetchone()
            if row is None:
                raise
            return str(row["review_id"])
    return review_id


def open_review_items(db: Database) -> list[dict[str, object]]:
    """Return open review items ordered oldest first."""
    with db.read() as connection:
        rows = connection.execute(
            "SELECT review_id, revision_id, location_id, kind, detail FROM review_items "
            "WHERE status = 'open' ORDER BY created_at"
        ).fetchall()
    return [dict(row) for row in rows]


def record_review_once(
    db: Database,
    *,
    kind: str,
    detail: str,
    revision_id: str | None = None,
    location_id: str | None = None,
) -> str:
    """Record a review item deduplicated on ``(kind, detail)``.

    Used for path-scoped observations (for example an oversized file) that have
    no revision or location row yet.
    """
    review_id = new_id("review")
    now = datetime.now(UTC).isoformat()
    with db.transaction() as connection:
        existing = connection.execute(
            "SELECT review_id FROM review_items WHERE kind = ? AND detail = ? AND status = 'open' "
            "ORDER BY created_at LIMIT 1",
            (kind, detail),
        ).fetchone()
        if existing is not None:
            return str(existing["review_id"])
        connection.execute(
            "INSERT INTO review_items (review_id, revision_id, location_id, kind, detail, "
            "status, created_at) VALUES (?, ?, ?, ?, ?, 'open', ?)",
            (review_id, revision_id, location_id, kind, detail, now),
        )
    return review_id


def _row_to_fact(row: sqlite3.Row) -> FactRecord:
    """Build a FactRecord from a row."""
    return FactRecord(
        field=str(row["field"]),
        value=None if row["value"] is None else str(row["value"]),
        evidence_page=row["evidence_page"],
        evidence_quote=None if row["evidence_quote"] is None else str(row["evidence_quote"]),
        extractor_version=str(row["extractor_version"]),
        validation_state=str(row["validation_state"]),
    )
