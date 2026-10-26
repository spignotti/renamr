"""Read-only MCP export policy and typed fact projection.

Permission is checked on every request from the database. Only contracts with a
safe local alias and at least one explicit grant are discoverable; only the
granted fields are projected. Evidence quotes, paths, full text, contract ids,
and free-form model strings never cross this boundary.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from renamr.core import contracts
from renamr.core.contracts import ContractFactRecord
from renamr.core.dates import parse_document_date
from renamr.core.db import Database
from renamr.core.errors import CoreError
from renamr.core.sanitize import is_safe_alias, is_safe_label

MCP_FIELDS: tuple[str, ...] = (
    "provider",
    "contract_type",
    "start_date",
    "minimum_term_months",
    "renewal",
    "notice_period_days",
    "end_date",
    "cancellation_confirmed",
    "cost_amount",
    "cost_currency",
    "cost_interval",
    "status",
    "minimum_end_date",
    "notice_deadline",
)
_EXPORT_ORIGINS = frozenset({"explicit", "calculated", "user_confirmed"})
_TYPES = frozenset({"employment", "tenancy", "insurance", "service", "other"})
_STATUSES = frozenset({"active", "ended", "cancelled", "unknown"})
_INTERVALS = frozenset({"monthly", "yearly"})
_CURRENCIES = frozenset({"EUR", "GBP", "USD", "CHF"})


class ExportedFact(BaseModel):
    """One typed, normalized fact with a non-private verification state."""

    model_config = ConfigDict(extra="forbid")

    value: str | int | float | bool | None = None
    verification: str


class ContractExport(BaseModel):
    """Text and structured channels share this exact whitelist."""

    model_config = ConfigDict(extra="forbid")

    available: bool = True
    alias: str = ""
    facts: dict[str, ExportedFact] = {}


class ContractDiscovery(BaseModel):
    """Bounded discovery of contracts with at least one granted field."""

    model_config = ConfigDict(extra="forbid")

    contracts: list[ContractExport] = []
    limit: int = 50


class GrantRecord(BaseModel):
    """One local grant for CLI display."""

    model_config = ConfigDict(extra="forbid")

    contract_id: str
    alias: str
    field: str
    granted: bool


def grant_field(db: Database, contract_id: str, field: str) -> GrantRecord:
    """Grant one enumerated field for a safe-aliased contract.

    Raises:
        CoreError: If the contract, alias, or field is not allowed.
    """
    contract = contracts.get_contract(db, contract_id)
    if field not in MCP_FIELDS:
        raise CoreError("field is not in the MCP export allowlist")
    if not contract.alias or not is_safe_alias(contract.alias):
        raise CoreError("set a safe local alias before granting export access")
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO mcp_permissions (permission_id, contract_id, field, granted, granted_at) "
            "VALUES (?, ?, ?, 1, datetime('now')) ON CONFLICT (contract_id, field) "
            "DO UPDATE SET granted = 1, granted_at = excluded.granted_at",
            (f"perm_{contract_id}_{field}", contract_id, field),
        )
    return GrantRecord(contract_id=contract_id, alias=contract.alias, field=field, granted=True)


def revoke_field(db: Database, contract_id: str, field: str) -> GrantRecord:
    """Revoke one field; the next request sees the change without a restart."""
    contract = contracts.get_contract(db, contract_id)
    with db.transaction() as connection:
        connection.execute(
            "UPDATE mcp_permissions SET granted = 0 WHERE contract_id = ? AND field = ?",
            (contract_id, field),
        )
    return GrantRecord(contract_id=contract_id, alias=contract.alias, field=field, granted=False)


def list_grants(db: Database) -> list[GrantRecord]:
    """Return local grants with contract ids for the owner's CLI."""
    aliases = {item.contract_id: item.alias for item in contracts.list_contracts(db)}
    with db.read() as connection:
        rows = connection.execute(
            "SELECT contract_id, field, granted FROM mcp_permissions "
            "WHERE granted = 1 ORDER BY contract_id, field"
        ).fetchall()
    return [
        GrantRecord(
            contract_id=str(row["contract_id"]),
            alias=aliases.get(str(row["contract_id"]), ""),
            field=str(row["field"]),
            granted=bool(row["granted"]),
        )
        for row in rows
    ]


def discover(db: Database, *, limit: int = 50) -> ContractDiscovery:
    """Return bounded aliases that have at least one live grant."""
    granted = list_grants(db)
    aliases: dict[str, str] = {}
    for grant in granted:
        if grant.alias and is_safe_alias(grant.alias):
            aliases[grant.contract_id] = grant.alias
    entries = [
        ContractExport(available=True, alias=alias, facts={})
        for alias in sorted(set(aliases.values()))[: max(1, min(limit, 50))]
    ]
    return ContractDiscovery(contracts=entries, limit=limit)


def export(db: Database, alias: str) -> ContractExport:
    """Project granted facts for one alias; denial is identical for unknown/hidden."""
    contract = _contract_by_alias(db, alias)
    if contract is None:
        return ContractExport(available=False, alias="", facts={})
    grants = {
        item.field
        for item in list_grants(db)
        if item.contract_id == contract.contract_id and item.granted
    }
    if not grants:
        return ContractExport(available=False, alias="", facts={})
    facts = {item.field: item for item in contracts.get_contract_facts(db, contract.contract_id)}
    projected: dict[str, ExportedFact] = {}
    for field in sorted(grants):
        fact = facts.get(field)
        if fact is None:
            projected[field] = ExportedFact(verification="unknown")
            continue
        if fact.origin in ("unknown", "conflicting"):
            projected[field] = ExportedFact(verification=fact.origin)
            continue
        if fact.origin not in _EXPORT_ORIGINS:
            continue
        normalized = _normalize(fact)
        if normalized is None:
            projected[field] = ExportedFact(verification="unavailable")
            continue
        projected[field] = ExportedFact(value=normalized, verification=fact.origin)
    return ContractExport(available=True, alias=contract.alias, facts=projected)


def _contract_by_alias(db: Database, alias: str) -> contracts.ContractRecord | None:
    """Return a safe-aliased contract, or None for any mismatch."""
    if not alias or not is_safe_alias(alias):
        return None
    for contract in contracts.list_contracts(db):
        if contract.alias == alias and is_safe_alias(contract.alias):
            return contract
    return None


def _normalize(fact: ContractFactRecord) -> str | int | float | bool | None:
    """Normalize one fact to a bounded date/number/enum/label value."""
    if fact.value is None:
        return None
    value = fact.value.strip()
    field = fact.field
    if field in ("start_date", "end_date", "minimum_end_date", "notice_deadline"):
        parsed = parse_document_date(value)
        return None if parsed is None else parsed.isoformat()
    if field in ("minimum_term_months", "notice_period_days"):
        return int(value) if value.isdigit() else None
    if field == "cost_amount":
        try:
            return round(float(value), 2)
        except ValueError:
            return None
    if field == "cancellation_confirmed":
        return True if value == "true" else False if value == "false" else None
    if field == "contract_type":
        return value if value in _TYPES else None
    if field == "status":
        return value if value in _STATUSES else None
    if field == "cost_interval":
        return value if value in _INTERVALS else None
    if field == "cost_currency":
        return value if value in _CURRENCIES else None
    if field == "renewal":
        return value if value == "stated" or value.startswith("automatic_") else None
    if field == "provider":
        return value if is_safe_label(value) and len(value) <= 80 else None
    return None
