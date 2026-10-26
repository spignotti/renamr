"""Deterministic contract fact aggregation, calculations, and persistence.

Explicit values are merged conservatively; amendments apply only with their own
effective date; cancellations apply only with their own document/effective date;
user-confirmed values survive refreshes. Unsupported calculations stay unknown.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from renamr.core.contract_fields import CONTRACT_FIELDS, ContractDocumentFacts, ExtractedField
from renamr.core.contract_types import (
    CONTRACT_TYPES,
    ContractFactRecord,
)
from renamr.core.dates import add_months, days_before
from renamr.core.db import Database
from renamr.core.facts import record_review_once
from renamr.core.ids import new_id


def aggregate(
    db: Database, contract_id: str, documents: list[ContractDocumentFacts]
) -> tuple[list[ContractFactRecord], list[str]]:
    """Aggregate initial, amendment, cancellation, and calculated facts."""
    current: dict[str, ContractFactRecord] = {}
    conflicts: set[str] = set()
    for document in sorted(
        documents, key=lambda item: (item.document_date or date.min, item.revision_id)
    ):
        if not document.complete:
            continue
        if document.kind == "amendment":
            _merge_amendment(db, contract_id, document, current, conflicts)
            continue
        if document.kind == "cancellation":
            _merge_cancellation(contract_id, document, current, conflicts)
            continue
        for field in document.fields:
            _merge_initial(contract_id, document, field, current, conflicts)
    _apply_status(contract_id, documents, current, conflicts)
    _apply_calculations(current)
    for field in set(conflicts):
        current[field] = ContractFactRecord(
            contract_id=contract_id, field=field, value=None, origin="conflicting"
        )
    for name in CONTRACT_FIELDS:
        if name not in current:
            current[name] = ContractFactRecord(
                contract_id=contract_id, field=name, value=None, origin="unknown"
            )
    return list(current.values()), list(conflicts)


def _merge_initial(
    contract_id: str,
    document: ContractDocumentFacts,
    field: ExtractedField,
    current: dict[str, ContractFactRecord],
    conflicts: set[str],
) -> None:
    """Merge one initial-contract field, detecting competing values."""
    existing = current.get(field.field)
    if existing is not None and existing.value not in (None, field.value):
        conflicts.add(field.field)
        return
    if field.field == "contract_type":
        value = document.kind if document.kind in CONTRACT_TYPES else "other"
    else:
        value = field.value
    current[field.field] = _fact(contract_id, field.field, value, field, document)


def _merge_amendment(
    db: Database,
    contract_id: str,
    document: ContractDocumentFacts,
    current: dict[str, ContractFactRecord],
    conflicts: set[str],
) -> None:
    """Override fields only with an amendment's explicit effective date."""
    if document.effective_date is None:
        if document.fields:
            record_review_once(
                db,
                kind="contract_relationship_unsupported",
                detail=document.revision_id,
                revision_id=document.revision_id,
            )
        return
    effective = document.effective_date.isoformat()
    for field in document.fields:
        previous = current.get(field.field)
        if previous is not None and previous.effective_date and previous.effective_date > effective:
            continue
        if field.field == "contract_type":
            value = document.kind if document.kind in CONTRACT_TYPES else "other"
        else:
            value = field.value
        current[field.field] = _fact(contract_id, field.field, value, field, document)
        current[field.field].relationship = "amendment"
        current[field.field].effective_date = effective
    _ = conflicts


def _merge_cancellation(
    contract_id: str,
    document: ContractDocumentFacts,
    current: dict[str, ContractFactRecord],
    conflicts: set[str],
) -> None:
    """Apply an explicit cancellation confirmation that carries its own date."""
    if document.document_date is None and document.effective_date is None:
        return
    effective = document.effective_date or document.document_date
    for field in document.fields:
        if field.field not in ("end_date", "cancellation_confirmed"):
            continue
        current[field.field] = _fact(contract_id, field.field, field.value, field, document)
        current[field.field].relationship = "cancellation"
        current[field.field].effective_date = None if effective is None else effective.isoformat()
    _ = conflicts


def _apply_status(
    contract_id: str,
    documents: list[ContractDocumentFacts],
    current: dict[str, ContractFactRecord],
    conflicts: set[str],
) -> None:
    """Set only an explicit cancellation status, otherwise unknown."""
    cancelled = current.get("cancellation_confirmed")
    if cancelled is not None and cancelled.value == "true":
        current["status"] = ContractFactRecord(
            contract_id=contract_id,
            field="status",
            value="cancelled",
            origin="explicit",
            relationship="cancellation_confirmation",
        )
    elif "status" not in current:
        current["status"] = ContractFactRecord(
            contract_id=contract_id, field="status", value="unknown", origin="unknown"
        )
    _ = (documents, conflicts)


def _apply_calculations(current: dict[str, ContractFactRecord]) -> None:
    """Add supported deterministic dates from explicit inputs only."""
    start = _value(current, "start_date")
    minimum = _value(current, "minimum_term_months")
    if start is not None and minimum is not None:
        try:
            end = days_before(add_months(date.fromisoformat(start), int(minimum)), 1)
            current["minimum_end_date"] = _calculated(
                current["minimum_end_date"] if "minimum_end_date" in current else None,
                current.get("start_date"),
                "minimum_end_date",
                end.isoformat(),
                "start_plus_minimum_term_minus_one_day",
            )
        except (ValueError, KeyError):
            pass
    end_value = _value(current, "end_date")
    notice = _value(current, "notice_period_days")
    if end_value is not None and notice is not None:
        try:
            deadline = days_before(date.fromisoformat(end_value), int(notice))
            current["notice_deadline"] = _calculated(
                current["notice_deadline"] if "notice_deadline" in current else None,
                current.get("end_date"),
                "notice_deadline",
                deadline.isoformat(),
                "end_minus_notice_period",
            )
        except ValueError:
            pass


def _fact(
    contract_id: str,
    name: str,
    value: str,
    field: ExtractedField,
    document: ContractDocumentFacts,
) -> ContractFactRecord:
    """Build one explicit fact with its source provenance."""
    return ContractFactRecord(
        contract_id=contract_id,
        field=name,
        value=value,
        origin=field.origin,
        source_revision_id=document.revision_id,
        source_page=field.page,
        evidence_quote=field.quote,
        source_document_type=document.kind,
    )


def _calculated(
    existing: ContractFactRecord | None,
    source: ContractFactRecord | None,
    name: str,
    value: str,
    formula: str,
) -> ContractFactRecord:
    """Build a calculated fact referencing the input evidence."""
    return ContractFactRecord(
        contract_id=source.contract_id if source is not None else "unknown",
        field=name,
        value=value,
        origin="calculated",
        source_revision_id=None if source is None else source.source_revision_id,
        source_page=None if source is None else source.source_page,
        evidence_quote=None if source is None else source.evidence_quote,
        relationship=formula,
    )


def write_facts(db: Database, contract_id: str, facts: list[ContractFactRecord]) -> int:
    """Replace all facts for a contract with the supplied merged set."""
    now = datetime.now(UTC).isoformat()
    with db.transaction() as connection:
        connection.execute(
            "DELETE FROM contract_facts WHERE contract_id = ?",
            (contract_id,),
        )
        for fact in facts:
            connection.execute(
                "INSERT INTO contract_facts (fact_id, contract_id, field, value, fact_state, "
                "source_revision_id, source_page, evidence_quote, source_document_type, "
                "effective_date, relationship, created_at, updated_at) VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    new_id("fact"),
                    contract_id,
                    fact.field,
                    fact.value,
                    fact.origin,
                    fact.source_revision_id,
                    fact.source_page,
                    fact.evidence_quote,
                    fact.source_document_type,
                    fact.effective_date,
                    fact.relationship,
                    now,
                    now,
                ),
            )
    return len(facts)


def get_contract_facts(db: Database, contract_id: str) -> list[ContractFactRecord]:
    """Return all facts for one contract ordered by field."""
    with db.read() as connection:
        rows = connection.execute(
            "SELECT * FROM contract_facts WHERE contract_id = ? ORDER BY field", (contract_id,)
        ).fetchall()
    return [
        ContractFactRecord(
            contract_id=str(row["contract_id"]),
            field=str(row["field"]),
            value=None if row["value"] is None else str(row["value"]),
            origin=str(row["fact_state"]),
            source_revision_id=None
            if row["source_revision_id"] is None
            else str(row["source_revision_id"]),
            source_page=None if row["source_page"] is None else int(row["source_page"]),
            evidence_quote=None if row["evidence_quote"] is None else str(row["evidence_quote"]),
            source_document_type=None
            if row["source_document_type"] is None
            else str(row["source_document_type"]),
            effective_date=None if row["effective_date"] is None else str(row["effective_date"]),
            relationship=None if row["relationship"] is None else str(row["relationship"]),
        )
        for row in rows
    ]


def user_facts(db: Database, contract_id: str) -> list[ContractFactRecord]:
    """Return user-confirmed rows."""
    return [item for item in get_contract_facts(db, contract_id) if item.origin == "user_confirmed"]


def merge_user(
    facts: list[ContractFactRecord], user_facts: list[ContractFactRecord]
) -> list[ContractFactRecord]:
    """Let user-confirmed rows replace computed rows for the same field."""
    by_field = {item.field: item for item in facts}
    for item in user_facts:
        by_field[item.field] = item
    return list(by_field.values())


def unknown_facts(contract_id: str) -> list[ContractFactRecord]:
    """Return unknown rows for a contract with no readable documents."""
    return [
        ContractFactRecord(contract_id=contract_id, field=name, value=None, origin="unknown")
        for name in CONTRACT_FIELDS
    ]


def _value(current: dict[str, ContractFactRecord], field: str) -> str | None:
    """Return a current non-conflicting value."""
    fact = current.get(field)
    if fact is None or fact.origin in ("conflicting", "unknown"):
        return None
    return fact.value
