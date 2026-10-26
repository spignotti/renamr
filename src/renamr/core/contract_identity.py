"""Stable contract identity, nested scopes, moves, and the entity bridge.

Member identities stay with the contract when folders or manual member files
move. References are extracted only from complete contract source documents and
are matched to a new document only when they resolve to one contract. Private
reference values never leave the local database through MCP or logs.
"""

from __future__ import annotations

import hashlib
import os
import re
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from renamr.core import catalog, folders, roots
from renamr.core.catalog import QualityState
from renamr.core.contract_fields import ContractDocumentFacts, reference_key
from renamr.core.contract_types import ContractReference
from renamr.core.db import Database
from renamr.core.errors import CoreError
from renamr.core.facts import FactRecord, record_review_once, set_facts
from renamr.core.ids import new_id
from renamr.core.roots import Root

_AMBIGUOUS = "contract_entity_ambiguous"
_MAX_SCAN_ENTRIES = 20_000


class EntityBridgeResult(BaseModel):
    """Counts from one entity bridge pass without private values."""

    model_config = ConfigDict(extra="forbid")

    entities: int = 0
    ambiguous: int = 0


def scope_base(db: Database, root_id: str, requested: str | None) -> str:
    """Return a validated root-relative contract scope base path."""
    if requested is not None:
        return folders.validate_relative_path(requested)
    with db.read() as connection:
        row = connection.execute(
            "SELECT base_path FROM contract_scopes WHERE root_id = ?", (root_id,)
        ).fetchone()
    return "" if row is None else str(row["base_path"])


def store_scope(db: Database, root_id: str, base_path: str) -> None:
    """Persist the confirmed base path for one root's contract convention."""
    now = datetime.now(UTC).isoformat()
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO contract_scopes (root_id, base_path, created_at) VALUES (?, ?, ?) "
            "ON CONFLICT (root_id) DO UPDATE SET base_path = excluded.base_path",
            (root_id, base_path, now),
        )


def member_identity(root: Root, relative_path: str) -> tuple[int | None, int | None]:
    """Return the device and inode of a member folder/file, when it exists."""
    candidate = root.canonical_path.joinpath(*relative_path.split("/"))
    try:
        info = candidate.stat()
    except OSError:
        return None, None
    return info.st_dev, info.st_ino


def ensure_member_identity(db: Database, root: Root, relative_path: str) -> None:
    """Fill missing or stale member identity for an existing path."""
    dev, ino = member_identity(root, relative_path)
    if dev is None or ino is None:
        return
    with db.transaction() as connection:
        connection.execute(
            "UPDATE contract_members SET dev = ?, ino = ? WHERE root_id = ? AND relative_path = ?",
            (dev, ino, root.root_id, relative_path),
        )


def require_unclaimed_path(
    db: Database, root_id: str, relative_path: str, *, contract_id: str | None = None
) -> None:
    """Raise when a path already belongs to a different contract."""
    with db.read() as connection:
        row = connection.execute(
            "SELECT contract_id FROM contract_members WHERE root_id = ? AND relative_path = ?",
            (root_id, relative_path),
        ).fetchone()
    if row is not None and str(row["contract_id"]) != contract_id:
        raise CoreError("path already belongs to another contract")


def replace_references(
    db: Database, contract_id: str, documents: list[ContractDocumentFacts]
) -> int:
    """Replace a contract's grounded private reference numbers."""
    found: dict[str, ContractReference] = {}
    for document in documents:
        if not document.complete:
            continue
        for reference in document.references:
            key = reference_key(reference.value)
            if not key or key in found:
                continue
            found[key] = ContractReference(
                contract_id=contract_id,
                value=reference.value,
                value_key=key,
                source_revision_id=document.revision_id,
                source_page=reference.page,
            )
    with db.transaction() as connection:
        connection.execute("DELETE FROM contract_references WHERE contract_id = ?", (contract_id,))
        for item in found.values():
            connection.execute(
                "INSERT INTO contract_references (reference_id, contract_id, value, value_key, "
                "source_revision_id, source_page, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    new_id("ref"),
                    contract_id,
                    item.value,
                    item.value_key,
                    item.source_revision_id,
                    item.source_page,
                    datetime.now(UTC).isoformat(),
                ),
            )
    return len(found)


def list_references(db: Database, contract_id: str | None = None) -> list[ContractReference]:
    """Return stored private references for local inspection and tests."""
    query = (
        "SELECT contract_id, value, value_key, source_revision_id, source_page "
        "FROM contract_references"
    )
    params: tuple[object, ...] = ()
    if contract_id is not None:
        query += " WHERE contract_id = ?"
        params = (contract_id,)
    query += " ORDER BY contract_id, value_key"
    with db.read() as connection:
        rows = connection.execute(query, params).fetchall()
    return [
        ContractReference(
            contract_id=str(row["contract_id"]),
            value=str(row["value"]),
            value_key=str(row["value_key"]),
            source_revision_id=str(row["source_revision_id"]),
            source_page=int(row["source_page"]),
        )
        for row in rows
    ]


def bridge_entities(db: Database) -> EntityBridgeResult:
    """Attach stable contract entity ids from memberships and unique references."""
    unique, ambiguous = _unique_references(db)
    result = EntityBridgeResult(ambiguous=ambiguous)
    memberships = _memberships(db)
    for location in _all_locations(db):
        revision = catalog.get_revision(db, location.revision_id)
        if revision.quality_state not in (QualityState.OK, QualityState.LOW):
            continue
        pages = catalog.get_pages(db, revision.revision_id)
        candidates = _document_candidates(db, location, memberships, unique, pages)
        if len(candidates) > 1:
            record_review_once(
                db,
                kind=_AMBIGUOUS,
                detail=f"{location.revision_id}:{','.join(sorted(candidates))}",
                revision_id=revision.revision_id,
                location_id=location.location_id,
            )
            result.ambiguous += 1
            continue
        if len(candidates) == 1 and _set_entity(
            db, revision.revision_id, next(iter(candidates)), _entity_state(pages, unique)
        ):
            result.entities += 1
    return result


def _unique_references(db: Database) -> tuple[dict[str, str], int]:
    """Return references that resolve to one contract, and ambiguity count."""
    by_key: dict[str, set[str]] = {}
    for item in list_references(db):
        by_key.setdefault(item.value_key, set()).add(item.contract_id)
    unique: dict[str, str] = {}
    ambiguous = 0
    for key, contract_ids in by_key.items():
        if len(contract_ids) == 1:
            unique[key] = next(iter(contract_ids))
        else:
            ambiguous += 1
            record_review_once(db, kind=_AMBIGUOUS, detail=_ambiguity_detail(key, contract_ids))
    return unique, ambiguous


def _document_candidates(
    db: Database,
    location: catalog.LocationRecord,
    memberships: list[tuple[str, str, str]],
    unique: dict[str, str],
    pages: list[catalog.PageRecord],
) -> set[str]:
    """Return contracts supported by membership and unique grounded references."""
    candidates = set(_membership_contracts(db, location, memberships))
    if pages:
        text = reference_key(" ".join(page.text for page in pages))
        candidates.update(
            contract_id
            for key, contract_id in unique.items()
            if key and _contains_reference(text, key)
        )
    return candidates


def _entity_state(pages: list[catalog.PageRecord], unique: dict[str, str]) -> str:
    """Return whether a reference match (or only membership) supplied identity."""
    if not pages:
        return "contract_membership"
    text = reference_key(" ".join(page.text for page in pages))
    if any(key and _contains_reference(text, key) for key in unique):
        return "contract_reference"
    return "contract_membership"


def _contains_reference(text: str, key: str) -> bool:
    """Return whether a normalized key occurs as a bounded token."""
    pattern = rf"(?<![A-Za-z0-9]){re.escape(key)}(?![A-Za-z0-9])"
    return re.search(pattern, text) is not None


def reconcile_members(db: Database) -> int:
    """Update member paths whose folder/file moved while the contract id stayed."""
    updated = 0
    roots_list = roots.list_roots(db)
    with db.read() as connection:
        rows = connection.execute("SELECT * FROM contract_members").fetchall()
    for row in rows:
        root = next((item for item in roots_list if item.root_id == str(row["root_id"])), None)
        if root is None:
            continue
        relative = str(row["relative_path"])
        current = root.canonical_path.joinpath(*relative.split("/"))
        dev = row["dev"]
        ino = row["ino"]
        if current.exists():
            if dev is None or ino is None:
                ensure_member_identity(db, root, relative)
                updated += 1
            continue
        if dev is None or ino is None:
            continue
        found = _find_identity([item.canonical_path for item in roots_list], int(dev), int(ino))
        if found is None:
            continue
        found_root = roots.containing_root(db, found)
        if found_root is None:
            continue
        new_relative = found.relative_to(found_root.canonical_path).as_posix()
        with db.transaction() as connection:
            connection.execute(
                "UPDATE contract_members SET root_id = ?, relative_path = ?, dev = ?, ino = ? "
                "WHERE contract_id = ? AND root_id = ? AND relative_path = ?",
                (
                    found_root.root_id,
                    new_relative,
                    dev,
                    ino,
                    str(row["contract_id"]),
                    str(row["root_id"]),
                    relative,
                ),
            )
        updated += 1
    return updated


def remap_members(db: Database, root_id: str, mappings: list[tuple[str, str]]) -> int:
    """Remap member paths and the scope base after an approved structure plan."""
    updated = 0
    with db.read() as connection:
        member_rows = connection.execute(
            "SELECT contract_id, relative_path FROM contract_members WHERE root_id = ?",
            (root_id,),
        ).fetchall()
        scope_row = connection.execute(
            "SELECT base_path FROM contract_scopes WHERE root_id = ?", (root_id,)
        ).fetchone()
    for row in member_rows:
        mapped = _mapped_relative(str(row["relative_path"]), mappings)
        if mapped is None or mapped == str(row["relative_path"]):
            continue
        root = roots.get_root(db, root_id)
        dev, ino = member_identity(root, mapped)
        with db.transaction() as connection:
            connection.execute(
                "UPDATE contract_members SET relative_path = ?, dev = ?, ino = ? "
                "WHERE contract_id = ? AND root_id = ? AND relative_path = ?",
                (mapped, dev, ino, str(row["contract_id"]), root_id, str(row["relative_path"])),
            )
        updated += 1
    if scope_row is not None:
        mapped_base = _mapped_relative(str(scope_row["base_path"]), mappings)
        if mapped_base is not None and mapped_base != str(scope_row["base_path"]):
            store_scope(db, root_id, mapped_base)
    return updated


def remap_moved_path(db: Database, *, old_path: Path, new_path: Path, target_root_id: str) -> int:
    """Keep a stable membership when a manual member file/folder moves."""
    target_root = roots.get_root(db, target_root_id)
    try:
        new_relative = new_path.relative_to(target_root.canonical_path).as_posix()
    except ValueError:
        return 0
    with db.read() as connection:
        rows = connection.execute("SELECT * FROM contract_members").fetchall()
    updated = 0
    for row in rows:
        root = roots.get_root(db, str(row["root_id"]))
        current = root.canonical_path.joinpath(*str(row["relative_path"]).split("/"))
        if current != old_path:
            continue
        dev, ino = member_identity(target_root, new_relative)
        with db.transaction() as connection:
            connection.execute(
                "UPDATE contract_members SET root_id = ?, relative_path = ?, dev = ?, ino = ? "
                "WHERE contract_id = ? AND root_id = ? AND relative_path = ?",
                (
                    target_root.root_id,
                    new_relative,
                    dev,
                    ino,
                    str(row["contract_id"]),
                    str(row["root_id"]),
                    str(row["relative_path"]),
                ),
            )
        updated += 1
    return updated


def _set_entity(db: Database, revision_id: str, contract_id: str, state: str) -> bool:
    """Set a contract entity fact unless a user-confirmed label already exists."""
    facts = catalog.get_facts(db, revision_id)
    existing = [item for item in facts if item.field == "entity_id"]
    if any(item.validation_state == "user_confirmed" for item in existing):
        return False
    if len(existing) == 1 and existing[0].value == contract_id:
        return False
    kept = [item for item in facts if item.field != "entity_id"]
    set_facts(
        db,
        revision_id,
        [
            *kept,
            FactRecord(
                field="entity_id",
                value=contract_id,
                validation_state=state,
                extractor_version="contract_entity_bridge",
            ),
        ],
    )
    return True


def _memberships(db: Database) -> list[tuple[str, str, str]]:
    """Return (contract id, root id, relative path) memberships."""
    with db.read() as connection:
        rows = connection.execute(
            "SELECT contract_id, root_id, relative_path FROM contract_members"
        ).fetchall()
    return [
        (str(row["contract_id"]), str(row["root_id"]), str(row["relative_path"])) for row in rows
    ]


def _membership_contracts(
    db: Database, location: catalog.LocationRecord, memberships: list[tuple[str, str, str]]
) -> set[str]:
    """Return contracts whose member folder/file contains a location."""
    root = roots.get_root(db, location.root_id)
    relative = location.canonical_path.relative_to(root.canonical_path).as_posix()
    found = set()
    for contract_id, root_id, member in memberships:
        if root_id != location.root_id or not member:
            continue
        if relative == member or relative.startswith(f"{member}/"):
            found.add(contract_id)
    return found


def _all_locations(db: Database) -> list[catalog.LocationRecord]:
    """Return every present catalog location in stable order."""
    found: list[catalog.LocationRecord] = []
    for root in roots.list_roots(db):
        for location in catalog.locations_for_root(db, root.root_id):
            if location.presence is catalog.Presence.PRESENT:
                found.append(location)
    return found


def _ambiguity_detail(key: str, contract_ids: set[str]) -> str:
    """Return an opaque ambiguity label without the private reference value."""
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
    return f"{digest}:{','.join(sorted(contract_ids))}"


def _find_identity(root_paths: list[Path], dev: int, ino: int) -> Path | None:
    """Find a path with an exact device/inode identity under known roots."""
    seen = 0
    for root_path in root_paths:
        for current, dirs, files in os.walk(root_path, topdown=True, followlinks=False):
            dirs[:] = [name for name in dirs if not name.startswith(".")]
            for name in [*dirs, *files]:
                seen += 1
                if seen > _MAX_SCAN_ENTRIES:
                    return None
                candidate = Path(current) / name
                try:
                    info = candidate.stat(follow_symlinks=False)
                except OSError:
                    continue
                if info.st_dev == dev and info.st_ino == ino:
                    return candidate
    return None


def _mapped_relative(relative: str, mappings: list[tuple[str, str]]) -> str | None:
    """Return a mapped relative path, or None when no mapping applies."""
    for source, target in sorted(mappings, key=lambda item: len(item[0]), reverse=True):
        source_path = source.strip("/")
        target_path = target.strip("/")
        if not source_path:
            continue
        if relative == source_path:
            return target_path
        if relative.startswith(f"{source_path}/"):
            remainder = relative[len(source_path) + 1 :]
            return f"{target_path}/{remainder}".strip("/")
    return None
