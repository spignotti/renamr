"""Local full-text search over page text and explicit text export.

Search uses the FTS5 ``unicode61`` index (no German stemming/compound expansion
in the initial release). Export writes private UTF-8 text to an explicit
destination; it never modifies original source bytes.
"""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from renamr.core.catalog import get_revision
from renamr.core.db import Database
from renamr.core.errors import CatalogError
from renamr.core.paths import write_private_file

DEFAULT_LIMIT = 50
MAX_LIMIT = 500
_SNIPPET_TOKENS = 12
_TOKEN = re.compile(r"\S+")


class SearchHit(BaseModel):
    """One matching page with a bounded highlight snippet."""

    model_config = ConfigDict(extra="forbid")

    revision_id: str
    page_number: int
    snippet: str


def search(db: Database, query: str, *, limit: int = DEFAULT_LIMIT) -> list[SearchHit]:
    """Search indexed page text.

    Args:
        db: Open catalog database.
        query: Free text; tokens are quoted so no FTS operator is interpreted.
        limit: Maximum hits (clamped to ``MAX_LIMIT``).

    Returns:
        Hits ordered by relevance.
    """
    expression = fts_expression(query)
    if expression is None:
        return []
    bounded = max(1, min(limit, MAX_LIMIT))
    with db.read() as connection:
        rows = connection.execute(
            "SELECT revision_id, page_number, "
            "snippet(pages_fts, 0, '<', '>', '...', ?) AS snippet "
            "FROM pages_fts WHERE pages_fts MATCH ? ORDER BY rank LIMIT ?",
            (_SNIPPET_TOKENS, expression, bounded),
        ).fetchall()
    return [
        SearchHit(
            revision_id=str(row["revision_id"]),
            page_number=int(row["page_number"]),
            snippet=str(row["snippet"]),
        )
        for row in rows
    ]


def fts_expression(query: str) -> str | None:
    """Build a safe FTS5 phrase expression from free text.

    Args:
        query: Raw user text.

    Returns:
        A quoted token expression, or None when the query has no tokens.
    """
    tokens = _TOKEN.findall(query)
    if not tokens:
        return None
    return " ".join(f'"{token.replace(chr(34), "")}"' for token in tokens)


def document_text(db: Database, revision_id: str) -> str:
    """Return the full extracted text of a revision with page markers."""
    from renamr.core.catalog import get_pages

    revision = get_revision(db, revision_id)
    pages = get_pages(db, revision_id)
    if not pages:
        raise CatalogError("revision has no extracted pages")
    blocks = [f"[[PAGE {page.page_number}]]\n{page.text}" for page in pages]
    header = f"# revision {revision.revision_id} ({revision.media_type})\n"
    return header + "\n\n".join(blocks)


def export_text(
    db: Database, revision_id: str, destination: Path, *, overwrite: bool = False
) -> Path:
    """Write a revision's text to an explicit private UTF-8 file.

    An existing unrelated destination is refused unless ``overwrite`` is true,
    and a registered source original is never rewritten.

    Args:
        db: Open catalog database.
        revision_id: Revision to export.
        destination: Explicit output path (created privately, ``0600``).
        overwrite: Allow replacing an existing non-source file after confirmation.

    Returns:
        The destination path.

    Raises:
        CatalogError: If the destination is a registered source original.
        StateError: If an occupied destination is not overwriteable.
    """
    target = Path(destination).expanduser()
    resolved = target.resolve()
    with db.read() as connection:
        source = connection.execute(
            "SELECT 1 FROM locations WHERE canonical_path = ? LIMIT 1", (str(resolved),)
        ).fetchone()
    if source is not None:
        raise CatalogError("refusing to rewrite a registered source original as text export")
    if target.exists() and not overwrite:
        raise CatalogError(
            "export destination already exists; choose a new path or use --overwrite"
        )
    text = document_text(db, revision_id)
    write_private_file(
        target, text.encode("utf-8"), require_private_parent=False, overwrite=overwrite
    )
    return target
