"""Shared contract record types and bounded value sets."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

CONTRACT_TYPES = frozenset({"employment", "tenancy", "insurance", "service", "other"})
STATUSES = frozenset({"active", "ended", "cancelled", "unknown"})
INTERVALS = frozenset({"monthly", "yearly"})
CURRENCIES = frozenset({"EUR", "GBP", "USD", "CHF"})


class ContractMember(BaseModel):
    """One explicit contract membership path relative to a root."""

    model_config = ConfigDict(extra="forbid")

    root_id: str
    relative_path: str


class ContractRecord(BaseModel):
    """A stable contract identity, alias, and members."""

    model_config = ConfigDict(extra="forbid")

    contract_id: str
    alias: str
    description: str
    state: str
    members: list[ContractMember] = []


class GroupEntry(BaseModel):
    """One child folder and the contract that already owns it, if any."""

    model_config = ConfigDict(extra="forbid")

    relative_path: str
    contract_id: str | None = None
    alias: str = ""


class ContractFactRecord(BaseModel):
    """One typed contract fact and its provenance."""

    model_config = ConfigDict(extra="forbid")

    contract_id: str
    field: str
    value: str | None
    origin: str
    source_revision_id: str | None = None
    source_page: int | None = None
    evidence_quote: str | None = None
    source_document_type: str | None = None
    effective_date: str | None = None
    relationship: str | None = None


class ContractRefreshResult(BaseModel):
    """Summary of one contract refresh without document content."""

    model_config = ConfigDict(extra="forbid")

    contracts: int = 0
    facts: int = 0
    incomplete_documents: int = 0
    conflicts: int = 0
    skipped: int = 0
    references: int = 0
    entities: int = 0


class ContractReference(BaseModel):
    """One private reference number with its stable contract identity."""

    model_config = ConfigDict(extra="forbid")

    contract_id: str
    value: str
    value_key: str
    source_revision_id: str
    source_page: int
