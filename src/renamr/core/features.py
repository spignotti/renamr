"""Feature extraction boundary: bounded prompts, validation, and caching keys.

Document text is treated as untrusted data. Extracted values are validated
against stored page text before they become facts. Oversized documents are split
into chunks that keep original page numbers and byte offsets, and every request
fits the character budget including prompt overhead. Conflicting values across
chunks stay in review instead of silently keeping the first chunk.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Protocol, cast, get_args

from pydantic import BaseModel, ConfigDict

from renamr.benchmark_schema import (
    FEATURE_SYSTEM_PROMPT,
    DocumentFeatures,
    DocumentType,
    FeatureValidation,
    GroundingEvidence,
    validate_features,
)
from renamr.core.catalog import FactRecord
from renamr.core.processing import (
    FEATURE_PROMPT_VERSION,
    TEXT_EXTRACTOR_VERSION,
    feature_cache_key,
    text_extract_key,
)
from renamr.local_transport import ChatMessage, OutputMode, StructuredResult

__all__ = [
    "FEATURE_PROMPT_VERSION",
    "TEXT_EXTRACTOR_VERSION",
    "FeatureExtraction",
    "FeatureExtractor",
    "FeatureRequest",
    "LocalFeatureExtractor",
    "PageChunk",
    "PageSlice",
    "build_chunk_text",
    "chunk_pages",
    "facts_to_features",
    "feature_cache_key",
    "features_to_facts",
    "text_extract_key",
]

MAX_EVIDENCE = 8
_SLICE_OVERHEAD = 24
_SCALAR_FIELDS = ("document_type", "issuer", "date", "subject", "language", "needs_review")


class PageSlice(BaseModel):
    """A contiguous slice of one page, with its original 1-based page number."""

    model_config = ConfigDict(extra="forbid")

    page_number: int
    offset: int
    text: str


class PageChunk(BaseModel):
    """A set of page slices that together fit one model request budget."""

    model_config = ConfigDict(extra="forbid")

    slices: list[PageSlice]


class FeatureRequest(BaseModel):
    """Input for one feature extraction: original page texts in order."""

    model_config = ConfigDict(extra="forbid")

    page_texts: list[str]
    language: str | None = None
    filename: str | None = None
    max_model_calls: int | None = None
    deadline_monotonic: float | None = None


class FeatureExtraction(BaseModel):
    """Validated feature extraction with explicit invalidity reasons."""

    model_config = ConfigDict(extra="forbid")

    features: DocumentFeatures | None = None
    validation: FeatureValidation
    blocked_reason: str | None = None
    incomplete: bool = False
    chunks: int = 1
    calls: int = 0


class FeatureExtractor(Protocol):
    """Extracts validated features from page text."""

    def extract(self, request: FeatureRequest) -> FeatureExtraction:
        """Return validated features or an explicit blocked result."""
        ...


class FeatureCompleter(Protocol):
    """Performs one validated structured completion for feature extraction."""

    def complete(
        self,
        model: str,
        messages: Sequence[ChatMessage],
        schema: type[DocumentFeatures],
        *,
        output_mode: OutputMode | None = None,
        images: Sequence[str] = (),
        require_vision: bool = False,
    ) -> StructuredResult[DocumentFeatures]:
        """Return validated features for one bounded request."""
        ...


def chunk_pages(page_texts: Sequence[str], max_chars: int, *, overhead: int = 0) -> list[PageChunk]:
    """Split page text into request-sized chunks, keeping page numbers and offsets.

    A single over-long page is split within the page. Each slice carries the
    original 1-based page number and its character offset so evidence stays
    addressable.

    Args:
        page_texts: Page texts in page order.
        max_chars: Total character budget per request including overhead.
        overhead: Non-page characters reserved for the prompt and schema.

    Returns:
        One or more chunks; always at least one chunk.
    """
    budget = max(1, max_chars - overhead)
    chunks: list[PageChunk] = []
    current: list[PageSlice] = []
    used = 0
    for page_number, text in enumerate(page_texts, start=1):
        offset = 0
        while offset < len(text):
            room = budget - used - _SLICE_OVERHEAD
            if room <= 0:
                chunks.append(PageChunk(slices=current))
                current, used = [], 0
                room = budget - _SLICE_OVERHEAD
            piece = text[offset : offset + max(1, room)]
            current.append(PageSlice(page_number=page_number, offset=offset, text=piece))
            used += len(piece) + _SLICE_OVERHEAD
            offset += len(piece)
    if current:
        chunks.append(PageChunk(slices=current))
    return chunks or [PageChunk(slices=[])]


def build_chunk_text(chunk: PageChunk) -> str:
    """Render a chunk with markers carrying the original page number and offset."""
    blocks: list[str] = []
    for page_slice in chunk.slices:
        if page_slice.offset:
            marker = f"[[PAGE {page_slice.page_number} offset={page_slice.offset}]]"
        else:
            marker = f"[[PAGE {page_slice.page_number}]]"
        blocks.append(f"{marker}\n{page_slice.text}")
    return "\n\n".join(blocks)


class LocalFeatureExtractor:
    """Feature extractor backed by a guarded local inference adapter."""

    def __init__(
        self,
        inference: FeatureCompleter,
        model: str,
        *,
        output_mode: OutputMode | None = None,
        max_chars: int = 12_000,
        prompt_overhead: int = 1_500,
    ) -> None:
        self._inference = inference
        self._model = model
        self._output_mode: OutputMode | None = output_mode
        self._max_chars = max_chars
        self._overhead = prompt_overhead

    def extract(self, request: FeatureRequest) -> FeatureExtraction:
        """Extract, validate, and merge features across bounded chunks.

        The request may cap model calls or set a monotonic deadline. Work stops
        before the next chunk exceeds either bound; an incomplete chunk set never
        yields routable features.
        """
        page_texts = list(request.page_texts)
        if not any(text.strip() for text in page_texts):
            return _blocked("no_extractable_text", incomplete=True)
        chunks = chunk_pages(page_texts, self._max_chars, overhead=self._overhead)
        if request.max_model_calls is not None and request.max_model_calls <= 0:
            return _blocked(
                "feature_budget_exhausted", incomplete=True, chunks=len(chunks), calls=0
            )
        results, calls, budget_reason = _run_chunks(
            self._inference,
            self._model,
            self._output_mode,
            chunks,
            request,
        )
        if budget_reason is not None:
            return _blocked(budget_reason, incomplete=True, chunks=len(chunks), calls=calls)
        return _interpret_chunks(page_texts, chunks, results, calls)


def _run_chunks(
    inference: FeatureCompleter,
    model: str,
    output_mode: OutputMode | None,
    chunks: list[PageChunk],
    request: FeatureRequest,
) -> tuple[list[FeatureExtraction], int, str | None]:
    """Run chunks within the request's model-call and time budgets."""
    results: list[FeatureExtraction] = []
    calls = 0
    for chunk in chunks:
        if request.max_model_calls is not None and calls >= request.max_model_calls:
            return results, calls, "feature_budget_exhausted"
        if (
            request.deadline_monotonic is not None
            and time.monotonic() >= request.deadline_monotonic
        ):
            return results, calls, "feature_time_budget_exhausted"
        results.append(_complete_chunk(inference, model, output_mode, chunk))
        calls += 1
    return results, calls, None


def _interpret_chunks(
    page_texts: list[str],
    chunks: list[PageChunk],
    results: list[FeatureExtraction],
    calls: int,
) -> FeatureExtraction:
    """Validate chunk results and reject any partial or conflicting success."""
    valid = [item.features for item in results if item.features is not None]
    blocked = next((item.blocked_reason for item in results if item.blocked_reason), None)
    if not valid:
        return _blocked(
            blocked or "extraction_blocked", incomplete=True, chunks=len(chunks), calls=calls
        )
    if len(results) != len(chunks):
        return _blocked("incomplete_chunks", incomplete=True, chunks=len(chunks), calls=calls)
    merged = _merge_sets([item for item in valid if item is not None])
    if merged is None:
        return _blocked("conflicting_chunks", incomplete=True, chunks=len(chunks), calls=calls)
    validation = validate_features(merged, page_texts)
    if not validation.ok or validation.unsafe:
        reason = validation.reasons[0] if validation.reasons else "invalid_features"
        return FeatureExtraction(
            validation=validation,
            blocked_reason=reason,
            incomplete=True,
            chunks=len(chunks),
            calls=calls,
        )
    partial = blocked is not None or any(item.incomplete for item in results)
    if partial:
        reason = blocked or "incomplete_chunks"
        return FeatureExtraction(
            validation=FeatureValidation(ok=False, reasons=[reason]),
            blocked_reason=reason,
            incomplete=True,
            chunks=len(chunks),
            calls=calls,
        )
    return FeatureExtraction(
        features=merged,
        validation=validation,
        incomplete=False,
        chunks=len(chunks),
        calls=calls,
    )


def _complete_chunk(
    inference: FeatureCompleter,
    model: str,
    output_mode: OutputMode | None,
    chunk: PageChunk,
) -> FeatureExtraction:
    """Run one bounded chunk through the local model and validate it."""
    messages = [
        ChatMessage(role="system", content=FEATURE_SYSTEM_PROMPT),
        ChatMessage(role="user", content=build_chunk_text(chunk)),
    ]
    try:
        result = inference.complete(model, messages, DocumentFeatures, output_mode=output_mode)
    except Exception as exc:
        reason = getattr(exc, "category", "inference_failed")
        return _blocked(f"inference_{reason}", incomplete=True)
    validation = validate_features(result.data, _page_text_map(chunk))
    if not validation.ok or validation.unsafe:
        reason = validation.reasons[0] if validation.reasons else "invalid_features"
        return FeatureExtraction(validation=validation, blocked_reason=reason, incomplete=True)
    return FeatureExtraction(features=result.data, validation=validation)


def _page_text_map(chunk: PageChunk) -> list[str]:
    """Return page texts indexed by original page number for one chunk."""
    page_count = max((item.page_number for item in chunk.slices), default=0)
    texts = ["" for _ in range(page_count)]
    for item in chunk.slices:
        texts[item.page_number - 1] += item.text
    return texts


def _merge_sets(sets: list[DocumentFeatures]) -> DocumentFeatures | None:
    """Merge validated chunk features, or None when scalar values conflict."""
    base = sets[0]
    merged: dict[str, object] = {}
    for field in _SCALAR_FIELDS:
        values = {getattr(item, field) for item in sets if getattr(item, field) is not None}
        if len(values) > 1:
            return None
        merged[field] = getattr(base, field) if values else None
    evidence: list[GroundingEvidence] = []
    for item in sets:
        for entry in item.evidence:
            if entry not in evidence:
                evidence.append(entry)
    merged["evidence"] = evidence[:MAX_EVIDENCE]
    return DocumentFeatures.model_validate(merged)


def _blocked(
    reason: str, *, incomplete: bool, chunks: int = 1, calls: int = 0
) -> FeatureExtraction:
    """Return an explicit blocked extraction."""
    return FeatureExtraction(
        validation=FeatureValidation(ok=False, reasons=[reason]),
        blocked_reason=reason,
        incomplete=incomplete,
        chunks=chunks,
        calls=calls,
    )


def features_to_facts(
    features: DocumentFeatures, *, validation_state: str = "validated"
) -> list[FactRecord]:
    """Convert validated features into typed fact records."""
    facts: list[FactRecord] = []
    for field in _SCALAR_FIELDS:
        value = getattr(features, field)
        if isinstance(value, bool):
            text_value: str | None = "true" if value else "false"
        elif value is None:
            text_value = None
        else:
            text_value = str(value)
        facts.append(
            FactRecord(
                field=field,
                value=text_value,
                extractor_version=TEXT_EXTRACTOR_VERSION,
                validation_state=validation_state,
            )
        )
    for evidence in features.evidence[:MAX_EVIDENCE]:
        facts.append(
            FactRecord(
                field="evidence",
                evidence_page=evidence.page,
                evidence_quote=evidence.source_text,
                extractor_version=TEXT_EXTRACTOR_VERSION,
                validation_state=validation_state,
            )
        )
    return facts


def facts_to_features(facts: Sequence[FactRecord]) -> DocumentFeatures | None:
    """Rebuild features from stored facts, or None when incomplete or invalid."""
    scalars: dict[str, str | None] = {}
    evidence: list[GroundingEvidence] = []
    for fact in facts:
        if fact.field == "evidence":
            if fact.evidence_page is None or not fact.evidence_quote:
                return None
            evidence.append(
                GroundingEvidence(page=fact.evidence_page, source_text=fact.evidence_quote)
            )
        elif fact.field in _SCALAR_FIELDS:
            scalars[fact.field] = fact.value
    document_type = _as_document_type(scalars.get("document_type"))
    if document_type is None:
        return None
    try:
        return DocumentFeatures(
            document_type=document_type,
            issuer=scalars.get("issuer"),
            date=scalars.get("date"),
            subject=scalars.get("subject"),
            language=scalars.get("language"),
            needs_review=scalars.get("needs_review") == "true",
            evidence=evidence[:MAX_EVIDENCE],
        )
    except ValueError:
        return None


def _as_document_type(value: str | None) -> DocumentType | None:
    """Return a valid document type literal or None."""
    if value is None or value not in get_args(DocumentType):
        return None
    return cast("DocumentType", value)
