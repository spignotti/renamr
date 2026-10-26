"""Folder-based contract groups, source-backed facts, and deterministic aggregation.

Group membership is explicit and stable. Facts are read from complete stored page
text, carry page/quote evidence, and keep explicit, calculated, user-confirmed,
unknown, and conflicting origins. An amendment overrides only with an explicit
effective date; a cancellation confirmation sets lifecycle state only from its own
text. No legal-authority claim is made.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from renamr.core import catalog, contract_aggregate, folders, roots
from renamr.core.catalog import QualityState
from renamr.core.contract_fields import (
    CONTRACT_FIELDS,
    ContractDocumentFacts,
    parse_contract_document,
)
from renamr.core.contract_types import (
    CONTRACT_TYPES,
    CURRENCIES,
    INTERVALS,
    STATUSES,
    ContractFactRecord,
    ContractMember,
    ContractRecord,
    ContractRefreshResult,
    GroupEntry,
)
from renamr.core.dates import parse_document_date
from renamr.core.db import Database
from renamr.core.errors import CoreError, RootError
from renamr.core.facts import record_review_once
from renamr.core.ids import new_id
from renamr.core.sanitize import is_safe_alias, is_safe_label, safe_alias


def preview_groups(db: Database, root_id: str) -> list[GroupEntry]:
    """Return the explicit child-folder convention without creating anything."""
    root = roots.get_root(db, root_id)
    existing = _members_by_path(db, root.root_id)
    aliases = {item.contract_id: item.alias for item in list_contracts(db)}
    return [
        GroupEntry(
            relative_path=folder,
            contract_id=existing.get(folder),
            alias=aliases.get(existing.get(folder, ""), ""),
        )
        for folder in folders.discover_folders(root)
    ]


def configure_contracts(db: Database, root_id: str, *, confirmed: bool) -> list[ContractRecord]:
    """Confirm the each-child-folder convention and create stable contract ids.

    Raises:
        CoreError: If confirmation is missing.
        RootError: If the root or a folder is invalid.
    """
    if not confirmed:
        raise CoreError("contract grouping requires explicit confirmation")
    root = roots.get_root(db, root_id)
    existing = _members_by_path(db, root.root_id)
    for folder in folders.discover_folders(root):
        if folder in existing:
            continue
        contract_id = new_id("ctr")
        alias = _unique_alias(db, safe_alias(folder))
        now = datetime.now(UTC).isoformat()
        with db.transaction() as connection:
            connection.execute(
                "INSERT INTO contracts (contract_id, alias, description, state, created_at) "
                "VALUES (?, ?, ?, 'configured', ?)",
                (contract_id, alias, folder, now),
            )
            connection.execute(
                "INSERT INTO contract_members (contract_id, root_id, relative_path, created_at) "
                "VALUES (?, ?, ?, ?)",
                (contract_id, root.root_id, folder, now),
            )
    return [item for item in list_contracts(db) if _in_root(item, root.root_id)]


def assign_member(
    db: Database, contract_id: str, *, root_id: str, relative_path: str
) -> ContractMember:
    """Add an explicit manual membership (exception) for a contract."""
    roots.get_root(db, root_id)
    _require_contract(db, contract_id)
    relative = folders.validate_relative_path(relative_path)
    if not relative:
        raise RootError("manual membership must name a folder or file")
    with db.read() as connection:
        clash = connection.execute(
            "SELECT contract_id FROM contract_members WHERE root_id = ? AND relative_path = ?",
            (root_id, relative),
        ).fetchone()
    if clash is not None and str(clash["contract_id"]) != contract_id:
        raise CoreError("path already belongs to another contract")
    now = datetime.now(UTC).isoformat()
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO contract_members (contract_id, root_id, relative_path, created_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT (contract_id, root_id, relative_path) DO NOTHING",
            (contract_id, root_id, relative, now),
        )
    return ContractMember(root_id=root_id, relative_path=relative)


def set_alias(db: Database, contract_id: str, alias: str) -> ContractRecord:
    """Set a safe, unique local alias used by the read-only MCP surface."""
    _require_contract(db, contract_id)
    cleaned = safe_alias(alias)
    if not cleaned or not is_safe_alias(cleaned):
        raise CoreError("contract alias is not safe")
    with db.read() as connection:
        clash = connection.execute(
            "SELECT contract_id FROM contracts WHERE alias = ? AND contract_id != ?",
            (cleaned, contract_id),
        ).fetchone()
    if clash is not None:
        raise CoreError("contract alias is already used")
    with db.transaction() as connection:
        connection.execute(
            "UPDATE contracts SET alias = ? WHERE contract_id = ?", (cleaned, contract_id)
        )
    return get_contract(db, contract_id)


def list_contracts(db: Database) -> list[ContractRecord]:
    """Return every contract with its explicit members."""
    with db.read() as connection:
        rows = connection.execute("SELECT * FROM contracts ORDER BY alias").fetchall()
        members = connection.execute(
            "SELECT * FROM contract_members ORDER BY root_id, relative_path"
        ).fetchall()
    grouped: dict[str, list[ContractMember]] = {}
    for row in members:
        grouped.setdefault(str(row["contract_id"]), []).append(
            ContractMember(root_id=str(row["root_id"]), relative_path=str(row["relative_path"]))
        )
    return [
        ContractRecord(
            contract_id=str(row["contract_id"]),
            alias=str(row["alias"] or ""),
            description=str(row["description"]),
            state=str(row["state"]),
            members=grouped.get(str(row["contract_id"]), []),
        )
        for row in rows
    ]


def get_contract(db: Database, contract_id: str) -> ContractRecord:
    """Return one contract or raise."""
    for contract in list_contracts(db):
        if contract.contract_id == contract_id:
            return contract
    raise CoreError("unknown contract id")


def refresh_contracts(
    db: Database, *, root_id: str | None = None, contract_ids: list[str] | None = None
) -> ContractRefreshResult:
    """Read complete page text and rewrite each contract's evidence-backed facts."""
    selected = list_contracts(db)
    if contract_ids is not None:
        selected = [item for item in selected if item.contract_id in contract_ids]
    if root_id is not None:
        selected = [item for item in selected if _in_root(item, root_id)]
    result = ContractRefreshResult()
    for contract in selected:
        outcome = _refresh_one(db, contract)
        result.contracts += 1
        result.facts += outcome.facts
        result.incomplete_documents += outcome.incomplete_documents
        result.conflicts += outcome.conflicts
        result.skipped += outcome.skipped
    return result


def _refresh_one(db: Database, contract: ContractRecord) -> ContractRefreshResult:
    """Refresh one contract and persist its facts."""
    documents: list[ContractDocumentFacts] = []
    result = ContractRefreshResult(contracts=1)
    for member in contract.members:
        for revision_id in _member_revisions(db, member):
            stored_pages = catalog.get_pages(db, revision_id)
            complete = _document_complete(db, revision_id, stored_pages)
            pages = [(page.page_number, page.text) for page in stored_pages]
            parsed = parse_contract_document(revision_id, pages, complete=complete)
            documents.append(parsed)
            if not complete:
                result.incomplete_documents += 1
                record_review_once(
                    db,
                    kind="contract_source_incomplete",
                    detail=revision_id,
                    revision_id=revision_id,
                )
    if not documents:
        result.skipped += 1
        user_facts = contract_aggregate.user_facts(db, contract.contract_id)
        contract_aggregate.write_facts(
            db,
            contract.contract_id,
            contract_aggregate.merge_user(
                contract_aggregate.unknown_facts(contract.contract_id), user_facts
            ),
        )
        return result
    facts, conflicts = contract_aggregate.aggregate(db, contract.contract_id, documents)
    user_facts = contract_aggregate.user_facts(db, contract.contract_id)
    merged = contract_aggregate.merge_user(facts, user_facts)
    result.facts = contract_aggregate.write_facts(db, contract.contract_id, merged)
    result.conflicts = len(conflicts)
    for conflict in sorted(conflicts):
        record_review_once(
            db,
            kind="contract_fact_conflict",
            detail=f"{contract.contract_id}:{conflict}",
        )
    return result


def get_contract_facts(db: Database, contract_id: str) -> list[ContractFactRecord]:
    """Return all facts for one contract ordered by field."""
    return contract_aggregate.get_contract_facts(db, contract_id)


def set_user_fact(db: Database, contract_id: str, field: str, value: str) -> ContractFactRecord:
    """Record one locally confirmed typed fact, preserving it across refreshes."""
    _require_contract(db, contract_id)
    cleaned = validate_fact(field, value)
    now = datetime.now(UTC).isoformat()
    with db.transaction() as connection:
        connection.execute(
            "DELETE FROM contract_facts WHERE contract_id = ? AND field = ? "
            "AND fact_state = 'user_confirmed'",
            (contract_id, field),
        )
        connection.execute(
            "INSERT INTO contract_facts (fact_id, contract_id, field, value, fact_state, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, 'user_confirmed', ?, ?)",
            (new_id("fact"), contract_id, field, cleaned, now, now),
        )
    return ContractFactRecord(
        contract_id=contract_id, field=field, value=cleaned, origin="user_confirmed"
    )


def validate_fact(field: str, value: str) -> str:
    """Validate and normalize one typed fact value.

    Raises:
        CoreError: If the field is unknown or the value is not typed/safe.
    """
    text = value.strip()
    if field not in CONTRACT_FIELDS:
        raise CoreError("unknown contract field")
    if field in ("start_date", "end_date", "minimum_end_date", "notice_deadline"):
        parsed = parse_document_date(text)
        if parsed is None:
            raise CoreError("contract date is invalid")
        return parsed.isoformat()
    if field in ("minimum_term_months", "notice_period_days"):
        if not text.isdigit():
            raise CoreError("contract number is invalid")
        return text
    if field == "cost_amount":
        try:
            return f"{float(text.replace(',', '.')):.2f}"
        except ValueError:
            raise CoreError("contract amount is invalid") from None
    if field == "cost_currency":
        return _enum(text.upper(), CURRENCIES, "contract currency")
    if field == "cost_interval":
        return _enum(text, INTERVALS, "contract interval")
    if field == "contract_type":
        return _enum(text, CONTRACT_TYPES, "contract type")
    if field == "status":
        return _enum(text, STATUSES, "contract status")
    if field == "cancellation_confirmed":
        return _enum(text, frozenset({"true", "false"}), "cancellation flag")
    if field == "renewal":
        if not text.startswith("automatic_") and text != "stated":
            raise CoreError("renewal value is not supported")
        return text
    if not is_safe_label(text):
        raise CoreError("contract label is not safe")
    return text


def _enum(value: str, allowed: frozenset[str], label: str) -> str:
    """Return an allowed enum value or raise."""
    if value not in allowed:
        raise CoreError(f"{label} is not supported")
    return value


def _member_revisions(db: Database, member: ContractMember) -> list[str]:
    """Return unique revision ids under a member path."""
    root = roots.get_root(db, member.root_id)
    found: dict[str, bool] = {}
    for location in catalog.locations_for_root(db, root.root_id):
        relative = _relative(location.canonical_path, root.canonical_path)
        if not _within(relative, member.relative_path):
            continue
        found[catalog.get_revision(db, location.revision_id).revision_id] = True
    return list(found)


def _document_complete(db: Database, revision_id: str, pages: list[catalog.PageRecord]) -> bool:
    """Return whether every page is complete, readable, and free of weak OCR."""
    revision = catalog.get_revision(db, revision_id)
    if revision.quality_state is not QualityState.OK or not pages:
        return False
    if not any(page.text.strip() for page in pages):
        return False
    return all(page.quality is QualityState.OK and not page.incomplete for page in pages)


def _within(relative: str, member: str) -> bool:
    """Return whether a file is inside a member folder or is the member file."""
    if not member:
        return True
    return relative == member or relative.startswith(f"{member}/")


def _relative(path: Path, root: Path) -> str:
    """Return a POSIX relative path."""
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.name


def _members_by_path(db: Database, root_id: str) -> dict[str, str]:
    """Return relative path to contract id for one root."""
    with db.read() as connection:
        rows = connection.execute(
            "SELECT relative_path, contract_id FROM contract_members WHERE root_id = ?",
            (root_id,),
        ).fetchall()
    return {str(row["relative_path"]): str(row["contract_id"]) for row in rows}


def _in_root(contract: ContractRecord, root_id: str) -> bool:
    """Return whether a contract has a member in a root."""
    return any(item.root_id == root_id for item in contract.members)


def _unique_alias(db: Database, base: str) -> str:
    """Return a unique safe alias derived from a folder name."""
    used = {item.alias for item in list_contracts(db)}
    if base not in used:
        return base
    index = 2
    while f"{base}-{index}" in used:
        index += 1
    return f"{base}-{index}"


def _require_contract(db: Database, contract_id: str) -> None:
    """Raise when a contract id is unknown."""
    with db.read() as connection:
        row = connection.execute(
            "SELECT 1 FROM contracts WHERE contract_id = ?", (contract_id,)
        ).fetchone()
    if row is None:
        raise CoreError("unknown contract id")
