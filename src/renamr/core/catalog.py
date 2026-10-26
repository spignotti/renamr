"""Catalog records: documents, revisions, locations, pages, facts, and reviews.

Content identity (a full SHA-256 hash) is separate from location identity (an
opaque location id per canonical path), so identical copies stay distinct while a
moved file keeps its content. A revision stores independent text and feature
cache keys plus durable retry state. Committing a move transfers the owning root.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from renamr.core.db import Database
from renamr.core.errors import CatalogError
from renamr.core.facts import (
    FactRecord,
    get_facts,
    open_review_items,
    record_review,
    set_facts,
)
from renamr.core.hashing import HASH_CHUNK_BYTES, Fingerprint, fingerprint, hash_file
from renamr.core.ids import new_id

__all__ = [
    "HASH_CHUNK_BYTES",
    "FactRecord",
    "Fingerprint",
    "IngestResult",
    "LocationRecord",
    "PageMethod",
    "PageRecord",
    "Presence",
    "QualityState",
    "RevisionRecord",
    "fingerprint",
    "get_facts",
    "get_location",
    "get_location_by_path",
    "get_pages",
    "get_revision",
    "hash_file",
    "ingest_file",
    "locations_for_revision",
    "locations_for_root",
    "mark_presence",
    "open_review_items",
    "record_review",
    "revision_has_pages",
    "set_facts",
    "set_pages",
    "update_location_path",
]


class Presence(StrEnum):
    """Observed presence of a file location."""

    PRESENT = "present"
    MISSING = "missing"


class QualityState(StrEnum):
    """Processing quality for a revision or page."""

    PENDING = "pending"
    OK = "ok"
    PARTIAL = "partial"
    LOW = "low"
    FAILED = "failed"
    INCOMPLETE = "incomplete"


class PageMethod(StrEnum):
    """How page text was obtained."""

    PDF_TEXT = "pdf_text"
    TEXT = "text"
    OCR = "ocr"
    VISION = "vision"
    NONE = "none"


class PageRecord(BaseModel):
    """One page of extracted text with provenance and quality."""

    model_config = ConfigDict(extra="forbid")

    page_number: int
    text: str
    method: PageMethod
    confidence: float | None = None
    rotation: int = 0
    quality: QualityState = QualityState.OK
    incomplete: bool = False


class RevisionRecord(BaseModel):
    """A content revision with its processing identity."""

    model_config = ConfigDict(extra="forbid")

    revision_id: str
    document_id: str
    content_hash: str
    size_bytes: int
    media_type: str
    extract_key: str = ""
    feature_key: str = ""
    quality_state: QualityState = QualityState.PENDING
    failed_stage: str = ""
    attempts: int = 0
    next_retry_at: str = ""
    error_category: str = ""


class LocationRecord(BaseModel):
    """A physical file location for a revision."""

    model_config = ConfigDict(extra="forbid")

    location_id: str
    revision_id: str
    root_id: str
    canonical_path: Path
    dev: int | None
    ino: int | None
    size_bytes: int
    mtime_ns: int | None
    presence: Presence


class IngestResult(BaseModel):
    """Outcome of ingesting one scanned file into the catalog."""

    model_config = ConfigDict(extra="forbid")

    revision_id: str
    location_id: str
    document_id: str
    revision_created: bool
    location_created: bool
    revision_changed: bool


def ingest_file(
    db: Database,
    *,
    root_id: str,
    path: Path,
    observed: Fingerprint,
    media_type: str,
) -> IngestResult:
    """Insert or update a document revision and its location.

    A new revision is created only for unseen content; an existing canonical path
    is relinked when its content changed, keeping the same document grouping.
    """
    with db.transaction() as connection:
        revision_row = _find_revision_by_hash(connection, observed.content_hash)
        location_row = connection.execute(
            "SELECT * FROM locations WHERE canonical_path = ?", (str(path),)
        ).fetchone()
        return _write_revision(
            connection,
            root_id=root_id,
            path=path,
            observed=observed,
            media_type=media_type,
            revision_row=revision_row,
            location_row=location_row,
        )


def _write_revision(
    connection: sqlite3.Connection,
    *,
    root_id: str,
    path: Path,
    observed: Fingerprint,
    media_type: str,
    revision_row: sqlite3.Row | None,
    location_row: sqlite3.Row | None,
) -> IngestResult:
    """Create or reuse a revision, then upsert its location row."""
    now = datetime.now(UTC).isoformat()
    prior_revision_id = None if location_row is None else str(location_row["revision_id"])
    revision_created = revision_row is None
    revision_id, document_id = _revision_identity(
        connection, revision_row, prior_revision_id, observed, media_type, now
    )
    revision_changed = prior_revision_id is not None and prior_revision_id != revision_id
    location_id, location_created = _upsert_location(
        connection, location_row, root_id, path, revision_id, observed, now
    )
    return IngestResult(
        revision_id=revision_id,
        location_id=location_id,
        document_id=document_id,
        revision_created=revision_created,
        location_created=location_created,
        revision_changed=revision_changed,
    )


def _revision_identity(
    connection: sqlite3.Connection,
    revision_row: sqlite3.Row | None,
    prior_revision_id: str | None,
    observed: Fingerprint,
    media_type: str,
    now: str,
) -> tuple[str, str]:
    """Return the (revision_id, document_id) for a new or reused revision."""
    if revision_row is not None:
        return str(revision_row["revision_id"]), str(revision_row["document_id"])
    document_id = _document_for_change(connection, prior_revision_id, now)
    revision_id = new_id("rev")
    connection.execute(
        "INSERT INTO revisions (revision_id, document_id, content_hash, size_bytes, "
        "media_type, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (revision_id, document_id, observed.content_hash, observed.size_bytes, media_type, now),
    )
    return revision_id, document_id


def _upsert_location(
    connection: sqlite3.Connection,
    location_row: sqlite3.Row | None,
    root_id: str,
    path: Path,
    revision_id: str,
    observed: Fingerprint,
    now: str,
) -> tuple[str, bool]:
    """Insert or update a location row, returning its id and whether it was new."""
    if location_row is None:
        location_id = new_id("loc")
        connection.execute(
            "INSERT INTO locations (location_id, revision_id, root_id, canonical_path, dev, ino, "
            "size_bytes, mtime_ns, presence, observed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, "
            "'present', ?)",
            (
                location_id,
                revision_id,
                root_id,
                str(path),
                observed.dev,
                observed.ino,
                observed.size_bytes,
                observed.mtime_ns,
                now,
            ),
        )
        return location_id, True
    location_id = str(location_row["location_id"])
    connection.execute(
        "UPDATE locations SET revision_id = ?, root_id = ?, dev = ?, ino = ?, size_bytes = ?, "
        "mtime_ns = ?, presence = 'present', observed_at = ? WHERE location_id = ?",
        (
            revision_id,
            root_id,
            observed.dev,
            observed.ino,
            observed.size_bytes,
            observed.mtime_ns,
            now,
            location_id,
        ),
    )
    return location_id, False


def get_revision(db: Database, revision_id: str) -> RevisionRecord:
    """Return a revision by id.

    Raises:
        CatalogError: If the revision is unknown.
    """
    with db.read() as connection:
        row = connection.execute(
            "SELECT * FROM revisions WHERE revision_id = ?", (revision_id,)
        ).fetchone()
    if row is None:
        raise CatalogError("unknown revision id")
    return _row_to_revision(row)


def get_location(db: Database, location_id: str) -> LocationRecord:
    """Return a location by id.

    Raises:
        CatalogError: If the location is unknown.
    """
    with db.read() as connection:
        row = connection.execute(
            "SELECT * FROM locations WHERE location_id = ?", (location_id,)
        ).fetchone()
    if row is None:
        raise CatalogError("unknown location id")
    return _row_to_location(row)


def get_location_by_path(db: Database, path: Path) -> LocationRecord | None:
    """Return the location for a canonical path, or None."""
    with db.read() as connection:
        row = connection.execute(
            "SELECT * FROM locations WHERE canonical_path = ?", (str(path),)
        ).fetchone()
    return None if row is None else _row_to_location(row)


def locations_for_revision(db: Database, revision_id: str) -> list[LocationRecord]:
    """Return every location pointing at a revision."""
    with db.read() as connection:
        rows = connection.execute(
            "SELECT * FROM locations WHERE revision_id = ? ORDER BY canonical_path",
            (revision_id,),
        ).fetchall()
    return [_row_to_location(row) for row in rows]


def locations_for_root(db: Database, root_id: str) -> list[LocationRecord]:
    """Return all locations registered under a root."""
    with db.read() as connection:
        rows = connection.execute(
            "SELECT * FROM locations WHERE root_id = ? ORDER BY canonical_path", (root_id,)
        ).fetchall()
    return [_row_to_location(row) for row in rows]


def revision_has_pages(db: Database, revision_id: str) -> bool:
    """Return whether a revision already has stored pages."""
    with db.read() as connection:
        row = connection.execute(
            "SELECT 1 FROM pages WHERE revision_id = ? LIMIT 1", (revision_id,)
        ).fetchone()
    return row is not None


def set_pages(db: Database, revision_id: str, pages: list[PageRecord]) -> None:
    """Replace all pages and search rows for a revision."""
    with db.transaction() as connection:
        connection.execute("DELETE FROM pages WHERE revision_id = ?", (revision_id,))
        connection.execute("DELETE FROM pages_fts WHERE revision_id = ?", (revision_id,))
        for page in pages:
            _insert_page(connection, revision_id, page)


def _insert_page(connection: sqlite3.Connection, revision_id: str, page: PageRecord) -> None:
    """Insert one page row and its FTS row."""
    connection.execute(
        "INSERT INTO pages (revision_id, page_number, text, method, confidence, rotation, "
        "quality_state, incomplete) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            revision_id,
            page.page_number,
            page.text,
            str(page.method),
            page.confidence,
            page.rotation,
            str(page.quality),
            int(page.incomplete),
        ),
    )
    connection.execute(
        "INSERT INTO pages_fts (text, revision_id, page_number) VALUES (?, ?, ?)",
        (page.text, revision_id, page.page_number),
    )


def get_pages(db: Database, revision_id: str) -> list[PageRecord]:
    """Return all pages for a revision ordered by page number."""
    with db.read() as connection:
        rows = connection.execute(
            "SELECT * FROM pages WHERE revision_id = ? ORDER BY page_number", (revision_id,)
        ).fetchall()
    return [_row_to_page(row) for row in rows]


def mark_presence(db: Database, location_id: str, presence: Presence) -> None:
    """Update the observed presence of a location."""
    now = datetime.now(UTC).isoformat()
    with db.transaction() as connection:
        connection.execute(
            "UPDATE locations SET presence = ?, observed_at = ? WHERE location_id = ?",
            (str(presence), now, location_id),
        )


def update_location_path(
    db: Database,
    location_id: str,
    *,
    path: Path,
    root_id: str,
    dev: int | None,
    ino: int | None,
    size_bytes: int,
    mtime_ns: int | None,
) -> None:
    """Relocate a location to a new canonical path and owning root after a move."""
    now = datetime.now(UTC).isoformat()
    with db.transaction() as connection:
        connection.execute(
            "UPDATE locations SET canonical_path = ?, root_id = ?, dev = ?, ino = ?, "
            "size_bytes = ?, mtime_ns = ?, presence = 'present', observed_at = ? "
            "WHERE location_id = ?",
            (str(path), root_id, dev, ino, size_bytes, mtime_ns, now, location_id),
        )


def _find_revision_by_hash(connection: sqlite3.Connection, content_hash: str) -> sqlite3.Row | None:
    """Return the revision row for a content hash, if any."""
    return connection.execute(
        "SELECT * FROM revisions WHERE content_hash = ?", (content_hash,)
    ).fetchone()


def _document_for_change(
    connection: sqlite3.Connection, prior_revision_id: str | None, now: str
) -> str:
    """Return an existing document for a changed path, else create a new one."""
    if prior_revision_id is not None:
        row = connection.execute(
            "SELECT document_id FROM revisions WHERE revision_id = ?", (prior_revision_id,)
        ).fetchone()
        if row is not None:
            return str(row["document_id"])
    document_id = new_id("doc")
    connection.execute(
        "INSERT INTO documents (document_id, created_at) VALUES (?, ?)", (document_id, now)
    )
    return document_id


def _row_to_revision(row: sqlite3.Row) -> RevisionRecord:
    """Build a RevisionRecord from a row."""
    return RevisionRecord(
        revision_id=str(row["revision_id"]),
        document_id=str(row["document_id"]),
        content_hash=str(row["content_hash"]),
        size_bytes=int(row["size_bytes"]),
        media_type=str(row["media_type"]),
        extract_key=str(row["extract_key"]),
        feature_key=str(row["feature_key"]),
        quality_state=QualityState(str(row["quality_state"])),
        failed_stage=str(row["failed_stage"]),
        attempts=int(row["attempts"]),
        next_retry_at=str(row["next_retry_at"] or ""),
        error_category=str(row["error_category"]),
    )


def _row_to_location(row: sqlite3.Row) -> LocationRecord:
    """Build a LocationRecord from a row."""
    return LocationRecord(
        location_id=str(row["location_id"]),
        revision_id=str(row["revision_id"]),
        root_id=str(row["root_id"]),
        canonical_path=Path(str(row["canonical_path"])),
        dev=row["dev"],
        ino=row["ino"],
        size_bytes=int(row["size_bytes"]),
        mtime_ns=row["mtime_ns"],
        presence=Presence(str(row["presence"])),
    )


def _row_to_page(row: sqlite3.Row) -> PageRecord:
    """Build a PageRecord from a row."""
    return PageRecord(
        page_number=int(row["page_number"]),
        text=str(row["text"]),
        method=PageMethod(str(row["method"])),
        confidence=None if row["confidence"] is None else float(row["confidence"]),
        rotation=int(row["rotation"]),
        quality=QualityState(str(row["quality_state"])),
        incomplete=bool(row["incomplete"]),
    )
