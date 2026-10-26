"""Deterministic, source-backed contract field parsing from stored page text.

Only explicit labels, dates, amounts, and bounded enums are recognized. Every
value carries the page and verbatim line that supports it. Unsupported wording
stays unknown rather than being guessed, and no legal interpretation is made.
"""

from __future__ import annotations

import re
from datetime import date

from pydantic import BaseModel, ConfigDict

from renamr.core.dates import find_document_date, parse_document_date

CONTRACT_FIELDS = (
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

_CONTRACT_TYPES = {
    "employment": ("arbeitsvertrag", "employment contract", "arbeitgeber"),
    "tenancy": ("mietvertrag", "tenancy agreement", "mietsache"),
    "insurance": ("versicherungsschein", "insurance policy", "versicherungsbeginn"),
    "service": ("dienstleistungsvertrag", "service agreement", "wartungsvertrag"),
}
CONTRACT_TYPES = frozenset(_CONTRACT_TYPES)
_START_LABELS = (
    "vertragsbeginn",
    "beschaeftigungsbeginn",
    "beschäftigungsbeginn",
    "mietbeginn",
    "versicherungsbeginn",
    "start date",
    "commencement",
    "beginnt am",
)
_END_LABELS = ("vertragsende", "end date", "laufzeit bis", "contract end", "endet am")
_ISSUE_LABELS = ("ausgestellt am", "ausstellungsdatum", "erstellt am", "date of issue", "datum")
_EFFECTIVE_LABELS = ("aenderung ab", "änderung ab", "gueltig ab", "gültig ab", "effective")
_PROVIDER_LABELS = (
    "anbieter",
    "vertragspartner",
    "arbeitgeber",
    "vermieter",
    "provider",
    "issuer",
)
_MINIMUM_LABELS = ("mindestlaufzeit", "mindestvertragslaufzeit", "minimum term")
_RENEWAL_LABELS = ("verlaengerung", "verlängerung", "renewal")
_NOTICE_LABELS = ("kuendigungsfrist", "kündigungsfrist", "notice period")
_COST_LABELS = (
    "miete",
    "beitrag",
    "kosten",
    "preis",
    "praemie",
    "prämie",
    "fee",
    "cost",
    "premium",
)
_CANCEL_LABELS = ("kuendigungsbestaetigung", "kündigungsbestätigung", "cancellation confirmation")
_AMENDMENT_LABELS = ("nachtrag", "amendment")
_REFERENCE_LABELS = (
    "vertragsnummer",
    "vertragsnr",
    "contract number",
    "contract no",
    "contract no.",
    "referenznummer",
    "reference number",
    "police number",
    "versicherungsnummer",
    "kundennummer",
    "customer number",
)
_AMOUNT = re.compile(r"(\d{1,3}(?:[.\s]\d{3})*(?:,\d{2})|\d+(?:\.\d{2})?)\s*(EUR|GBP|USD|CHF)")
_DURATION = re.compile(r"(\d+)\s*(monat|monate|months?|jahr|jahre|years?)")
_DEADLINE = re.compile(r"(\d+)\s*(tag|tage|days?|woche|wochen|weeks?)")
_REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9./_-]{2,39}")


class ExtractedField(BaseModel):
    """One explicit or deterministically calculated field with its source line."""

    model_config = ConfigDict(extra="forbid")

    field: str
    value: str
    page: int
    quote: str
    origin: str = "explicit"


class ExtractedReference(BaseModel):
    """One private, source-grounded contract/reference number."""

    model_config = ConfigDict(extra="forbid")

    value: str
    page: int
    quote: str


class ContractDocumentFacts(BaseModel):
    """All parsed fields and relationship metadata for one document."""

    model_config = ConfigDict(extra="forbid")

    revision_id: str
    kind: str
    document_date: date | None = None
    effective_date: date | None = None
    complete: bool = True
    fields: list[ExtractedField] = []
    references: list[ExtractedReference] = []


def parse_contract_document(
    revision_id: str,
    pages: list[tuple[int, str]],
    *,
    complete: bool,
) -> ContractDocumentFacts:
    """Parse one document from (page number, text) pairs without inference."""
    kind = _kind(pages)
    fields: list[ExtractedField] = []
    references: list[ExtractedReference] = []
    if complete:
        fields.extend(_explicit_fields(pages))
        references = _explicit_references(pages)
        _add_contract_type(fields, kind, pages)
        _add_calculated(fields)
    issue = _labeled_date(pages, _ISSUE_LABELS)
    effective = _labeled_date(pages, _EFFECTIVE_LABELS)
    return ContractDocumentFacts(
        revision_id=revision_id,
        kind=kind,
        document_date=issue,
        effective_date=effective,
        complete=complete,
        fields=fields,
        references=references,
    )


def _kind(pages: list[tuple[int, str]]) -> str:
    """Classify a document from bounded keywords."""
    text = " ".join(text for _, text in pages).lower()
    if any(label in text for label in _CANCEL_LABELS):
        return "cancellation"
    if any(label in text for label in _AMENDMENT_LABELS):
        return "amendment"
    for kind, markers in _CONTRACT_TYPES.items():
        if any(marker in text for marker in markers):
            return kind
    return "other"


def _explicit_fields(pages: list[tuple[int, str]]) -> list[ExtractedField]:
    """Collect explicit provider, dates, terms, costs, and flags."""
    fields: list[ExtractedField] = []
    provider = _labeled_text(pages, _PROVIDER_LABELS)
    if provider is not None:
        fields.append(provider.model_copy(update={"field": "provider"}))
    start = _labeled_date_field(pages, _START_LABELS, "start_date")
    if start is not None:
        fields.append(start)
    end = _labeled_date_field(pages, _END_LABELS, "end_date")
    if end is not None:
        fields.append(end)
    minimum = _labeled_duration(pages, _MINIMUM_LABELS, "minimum_term_months")
    if minimum is not None:
        fields.append(minimum)
    notice = _labeled_deadline(pages, _NOTICE_LABELS, "notice_period_days")
    if notice is not None:
        fields.append(notice)
    renewal = _renewal(pages)
    if renewal is not None:
        fields.append(renewal)
    costs = _costs(pages)
    fields.extend(costs)
    if any(label in " ".join(text.lower() for _, text in pages) for label in _CANCEL_LABELS):
        page, quote = _first_line(pages, _CANCEL_LABELS) or (1, "cancellation confirmation")
        fields.append(
            ExtractedField(field="cancellation_confirmed", value="true", page=page, quote=quote)
        )
    return fields


def _add_contract_type(
    fields: list[ExtractedField], kind: str, pages: list[tuple[int, str]]
) -> None:
    """Record a bounded contract type for an initial contract document."""
    if kind in ("amendment", "cancellation"):
        return
    page, quote = _first_line(pages, ("",)) or (1, kind)
    fields.append(
        ExtractedField(
            field="contract_type",
            value=kind if kind in CONTRACT_TYPES else "other",
            page=page,
            quote=quote[:300],
        )
    )


def _explicit_references(pages: list[tuple[int, str]]) -> list[ExtractedReference]:
    """Return unique labeled reference numbers grounded in the chain text."""
    found: dict[str, ExtractedReference] = {}
    for page, text in pages:
        for line in text.splitlines():
            stripped = line.strip()
            lowered = stripped.lower()
            for label in _REFERENCE_LABELS:
                index = lowered.find(label)
                if index < 0:
                    continue
                match = _REFERENCE.search(stripped[index + len(label) :].lstrip(" :=—-"))
                if match is None:
                    continue
                value = match.group(0).rstrip(".,;")
                key = reference_key(value)
                if key and key not in found:
                    found[key] = ExtractedReference(value=value, page=page, quote=stripped[:300])
                break
    return list(found.values())


def reference_key(value: str) -> str:
    """Return the normalized comparison key for a private reference value."""
    return " ".join(value.casefold().split())


def _labeled_text(pages: list[tuple[int, str]], labels: tuple[str, ...]) -> ExtractedField | None:
    """Return the text after one explicit label."""
    found = _first_line(pages, labels)
    if found is None:
        return None
    page, quote = found
    lowered = quote.lower()
    for label in labels:
        index = lowered.find(label)
        if index < 0:
            continue
        value = quote[index + len(label) :].lstrip(" :=—-")
        value = value.strip()
        if value:
            return ExtractedField(field="value", value=value[:120], page=page, quote=quote[:300])
    return None


def _labeled_date(pages: list[tuple[int, str]], labels: tuple[str, ...]) -> date | None:
    """Return an explicit labeled date, never a filesystem date."""
    found = _first_line(pages, labels)
    if found is None:
        return None
    return parse_document_date(found[1])


def _labeled_date_field(
    pages: list[tuple[int, str]], labels: tuple[str, ...], field: str
) -> ExtractedField | None:
    """Return one labeled ISO/German date with its source line."""
    found = _first_line(pages, labels)
    if found is None:
        return None
    parsed = parse_document_date(found[1])
    if parsed is None:
        return None
    return ExtractedField(
        field=field, value=parsed.isoformat(), page=found[0], quote=found[1][:300]
    )


def _labeled_duration(
    pages: list[tuple[int, str]], labels: tuple[str, ...], field: str
) -> ExtractedField | None:
    """Return a minimum term in whole months."""
    found = _first_line(pages, labels)
    if found is None:
        return None
    match = _DURATION.search(found[1].lower())
    if match is None:
        return None
    months = int(match.group(1)) * (12 if _is_years(match.group(2)) else 1)
    return ExtractedField(field=field, value=str(months), page=found[0], quote=found[1][:300])


def _labeled_deadline(
    pages: list[tuple[int, str]], labels: tuple[str, ...], field: str
) -> ExtractedField | None:
    """Return a notice period only when expressed in days or weeks."""
    found = _first_line(pages, labels)
    if found is None:
        return None
    match = _DEADLINE.search(found[1].lower())
    if match is None:
        return None
    days = int(match.group(1)) * (7 if match.group(2).startswith("wo") else 1)
    return ExtractedField(field=field, value=str(days), page=found[0], quote=found[1][:300])


def _renewal(pages: list[tuple[int, str]]) -> ExtractedField | None:
    """Return a bounded automatic-renewal label when explicit."""
    found = _first_line(pages, _RENEWAL_LABELS)
    if found is None:
        return None
    match = _DURATION.search(found[1].lower())
    if match is None:
        return ExtractedField(field="renewal", value="stated", page=found[0], quote=found[1][:300])
    months = int(match.group(1)) * (12 if _is_years(match.group(2)) else 1)
    return ExtractedField(
        field="renewal",
        value=f"automatic_{months}_months",
        page=found[0],
        quote=found[1][:300],
    )


def _costs(pages: list[tuple[int, str]]) -> list[ExtractedField]:
    """Return amount, currency, and interval from one explicit cost line."""
    for page, text in pages:
        for line in text.splitlines():
            lowered = line.lower()
            if not any(label in lowered for label in _COST_LABELS):
                continue
            match = _AMOUNT.search(line)
            if match is None:
                continue
            amount = _normalize_amount(match.group(1))
            currency = match.group(2).upper()
            fields = [
                ExtractedField(field="cost_amount", value=amount, page=page, quote=line[:300]),
                ExtractedField(field="cost_currency", value=currency, page=page, quote=line[:300]),
            ]
            interval = _interval(lowered)
            if interval is not None:
                fields.append(
                    ExtractedField(
                        field="cost_interval", value=interval, page=page, quote=line[:300]
                    )
                )
            return fields
    return []


def _add_calculated(fields: list[ExtractedField]) -> None:
    """Add the two supported deterministic calculations, never legal advice."""
    values = {item.field: item for item in fields}
    start = values.get("start_date")
    minimum = values.get("minimum_term_months")
    if start is not None and minimum is not None:
        from renamr.core.dates import add_months, days_before

        end = days_before(add_months(date.fromisoformat(start.value), int(minimum.value)), 1)
        fields.append(
            ExtractedField(
                field="minimum_end_date",
                value=end.isoformat(),
                page=start.page,
                quote=start.quote,
                origin="calculated",
            )
        )
    end = values.get("end_date")
    notice = values.get("notice_period_days")
    if end is not None and notice is not None:
        from renamr.core.dates import days_before

        deadline = days_before(date.fromisoformat(end.value), int(notice.value))
        fields.append(
            ExtractedField(
                field="notice_deadline",
                value=deadline.isoformat(),
                page=end.page,
                quote=end.quote,
                origin="calculated",
            )
        )


def _first_line(pages: list[tuple[int, str]], labels: tuple[str, ...]) -> tuple[int, str] | None:
    """Return the first line containing one label with its page."""
    for page, text in pages:
        for line in text.splitlines():
            stripped = line.strip()
            if stripped and any(label in stripped.lower() for label in labels):
                return page, stripped
    return None


def _normalize_amount(value: str) -> str:
    """Normalize a German or plain decimal amount to a dot decimal string."""
    if "," in value:
        return value.replace(".", "").replace(" ", "").replace(",", ".")
    return value.replace(" ", "")


def _interval(lowered: str) -> str | None:
    """Return a bounded cost interval label."""
    if "monat" in lowered or "month" in lowered:
        return "monthly"
    if "jahr" in lowered or "year" in lowered or "annual" in lowered:
        return "yearly"
    return None


def _is_years(unit: str) -> bool:
    """Return whether a duration unit means years."""
    return unit.startswith(("jahr", "year"))


def labeled_issue_date(pages: list[tuple[int, str]]) -> date | None:
    """Return a labeled issue date or None."""
    return _labeled_date(pages, _ISSUE_LABELS)


def first_text_date(pages: list[tuple[int, str]]) -> tuple[date, str] | None:
    """Return the first parseable date line in the document."""
    for _, text in pages:
        found = find_document_date(text)
        if found is not None:
            return found
    return None
