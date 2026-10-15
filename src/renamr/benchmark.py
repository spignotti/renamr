"""Non-mutating decision harness for the ``para-local-v1`` corpus.

The harness never touches the filesystem beyond reading corpus definitions. It
returns per-case identifiers, outcomes, and timings plus aggregate statistics;
it never emits raw prompts, responses, or document content.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from enum import StrEnum

from pydantic import BaseModel, Field

from renamr.benchmark_cases import (
    CORPUS_NAME,
    RuleDialogCase,
    SyntheticDocument,
    build_document_text,
)
from renamr.benchmark_schema import (
    FEATURE_SYSTEM_PROMPT,
    RULE_DIALOG_PROMPT,
    DocumentFeatures,
    RuleDraft,
    deterministic_hold_reason,
    rule_draft_is_bounded,
    validate_features,
)
from renamr.local_client import ChatMessage, LocalClientError, LocalOllamaClient
from renamr.local_transport import OutputMode

MIN_CLEAR_CASES = 8
MIN_CLEAR_USEFUL = 7
MIN_REVIEW_CASES = 8
MIN_EDGE_CASES = 8
MIN_RULE_DRAFTS = 1


class Outcome(StrEnum):
    """Deterministic verdict for one benchmark case.

    ``HELD`` covers any output that produces no eligible action, including one
    the deterministic guards reject as unsafe. ``UNSAFE`` is reserved for a
    guard-passing, confident proposal on a safety-critical review case: that is
    a dangerous eligible action and always fails the gate.
    """

    USEFUL = "useful"
    HELD = "held"
    UNSAFE = "unsafe"
    UNEXPECTED = "unexpected"


class Prediction(BaseModel):
    """A single model prediction, or the sanitized reason it was blocked."""

    features: DocumentFeatures | None = None
    blocked_reason: str | None = None


class RulePrediction(BaseModel):
    """A rule-dialog prediction, or the sanitized reason it was blocked."""

    draft: RuleDraft | None = None
    blocked_reason: str | None = None


class CaseOutcome(BaseModel):
    """Per-case result without any raw prompt, response, or document text."""

    case_id: str
    category: str
    expected_review: bool
    outcome: Outcome
    reasons: list[str] = Field(default_factory=list)
    blocked_unsafe: bool = False
    duration_ms: float | None = None


class RuleOutcome(BaseModel):
    """Per-case result for a rule-dialog request."""

    case_id: str
    valid: bool
    reason: str | None = None
    duration_ms: float | None = None


class CorpusReport(BaseModel):
    """Aggregate benchmark statistics and the acceptance verdict.

    ``unsafe_actions`` counts only dangerous eligible actions (``Outcome.UNSAFE``)
    and must stay zero. ``blocked_unsafe_outputs`` counts outputs the deterministic
    guards rejected as unsafe: those stay held with no eligible action, so they are
    reported separately instead of being conflated with an eligible action.

    ``edge_correct`` is informational: the approved P0 gate does not require
    every edge case to pass, and later feature work needs its own edge gates.
    """

    corpus: str
    model: str
    output_mode: str
    edge_informational: bool = True
    feature_outcomes: list[CaseOutcome]
    rule_outcomes: list[RuleOutcome]
    clear_useful: int
    clear_total: int
    review_held: int
    review_total: int
    edge_correct: int
    edge_total: int
    unsafe_actions: int
    blocked_unsafe_outputs: int
    valid_rule_drafts: int
    rule_total: int
    mean_latency_ms: float | None
    passed: bool


FeaturePredictor = Callable[[SyntheticDocument], tuple[Prediction, float]]
RulePredictor = Callable[[RuleDialogCase], tuple[RulePrediction, float]]


def classify_prediction(document: SyntheticDocument, prediction: Prediction) -> CaseOutcome:
    """Classify one prediction against the corpus expectation.

    Checks with the highest precedence run first so a dangerous output is never
    downgraded to a plain hold, and deterministic source checks run after an
    explicit model review flag. Golden issuer/date values are compared only when
    the manifest labels them, and never feed back into the hold logic.

    An output the deterministic guards reject as unsafe stays ``HELD`` with no
    eligible action and is flagged ``blocked_unsafe``; ``Outcome.UNSAFE`` is
    produced only for a guard-passing, confident proposal on a safety-critical
    review case.
    """
    expected = document.expected
    if prediction.features is None:
        reasons = [prediction.blocked_reason or "model_abstained"]
        return _outcome(document, Outcome.HELD, reasons)
    features = prediction.features
    validation = validate_features(features, document.page_texts)
    if validation.unsafe:
        return _outcome(document, Outcome.HELD, validation.reasons, blocked_unsafe=True)
    if not validation.ok:
        return _outcome(document, Outcome.HELD, validation.reasons)
    if features.needs_review:
        return _outcome(document, Outcome.HELD, ["model_flagged_review"])
    hold_reason = deterministic_hold_reason(document.page_texts)
    if hold_reason is not None:
        return _outcome(document, Outcome.HELD, [hold_reason])
    if expected.needs_review:
        outcome = Outcome.UNSAFE if expected.safety_critical else Outcome.UNEXPECTED
        return _outcome(document, outcome, ["confident_output_for_review_case"])
    if features.document_type.strip().lower() != (expected.document_type or "").lower():
        return _outcome(document, Outcome.UNEXPECTED, ["document_type_mismatch"])
    if expected.expected_issuer is not None and not _issuer_matches(
        features.issuer, expected.expected_issuer
    ):
        return _outcome(document, Outcome.UNEXPECTED, ["issuer_mismatch"])
    if expected.expected_date is not None and features.date != expected.expected_date:
        return _outcome(document, Outcome.UNEXPECTED, ["date_mismatch"])
    return _outcome(document, Outcome.USEFUL, ["grounded_extraction"])


def run_feature_corpus(
    predict: FeaturePredictor, documents: Sequence[SyntheticDocument]
) -> list[CaseOutcome]:
    """Run a predictor over the feature corpus and classify every case."""
    outcomes: list[CaseOutcome] = []
    for document in documents:
        prediction, duration_ms = predict(document)
        outcome = classify_prediction(document, prediction)
        outcomes.append(outcome.model_copy(update={"duration_ms": duration_ms}))
    return outcomes


def evaluate_rule_prediction(case: RuleDialogCase, prediction: RulePrediction) -> RuleOutcome:
    """Validate a rule draft against bounds and the request's required semantics."""
    if prediction.draft is None:
        return RuleOutcome(
            case_id=case.case_id,
            valid=False,
            reason=prediction.blocked_reason or "rule_generation_blocked",
        )
    draft = prediction.draft
    if draft.scope_root_id != case.scope_root_id or draft.target_root_id != case.target_root_id:
        return RuleOutcome(case_id=case.case_id, valid=False, reason="rule_scope_mismatch")
    if not rule_draft_is_bounded(draft):
        return RuleOutcome(case_id=case.case_id, valid=False, reason="rule_draft_out_of_bounds")
    if (
        case.required_target_subdir is not None
        and draft.target_subdir != case.required_target_subdir
    ):
        return RuleOutcome(case_id=case.case_id, valid=False, reason="rule_target_mismatch")
    if case.required_document_type is not None and not _has_document_type(
        draft, case.required_document_type
    ):
        return RuleOutcome(case_id=case.case_id, valid=False, reason="rule_predicate_mismatch")
    return RuleOutcome(case_id=case.case_id, valid=True, reason="bounded_rule_request_match")


def run_rule_corpus(predict: RulePredictor, cases: Sequence[RuleDialogCase]) -> list[RuleOutcome]:
    """Run a rule-dialog predictor over the synthetic rule requests."""
    outcomes: list[RuleOutcome] = []
    for case in cases:
        prediction, duration_ms = predict(case)
        outcome = evaluate_rule_prediction(case, prediction)
        outcomes.append(outcome.model_copy(update={"duration_ms": duration_ms}))
    return outcomes


def build_report(
    model: str,
    feature_outcomes: Sequence[CaseOutcome],
    rule_outcomes: Sequence[RuleOutcome],
    *,
    output_mode: OutputMode = "native",
    corpus: str = CORPUS_NAME,
) -> CorpusReport:
    """Evaluate usefulness, safe holds, and rule drafts; record edges as informational.

    Count guard-rejected outputs separately from unsafe eligible actions.
    """
    clear = [item for item in feature_outcomes if item.category == "clear"]
    review = [item for item in feature_outcomes if item.category == "review"]
    edge = [item for item in feature_outcomes if item.category == "edge"]
    clear_useful = sum(1 for item in clear if item.outcome is Outcome.USEFUL)
    review_held = sum(1 for item in review if item.outcome is Outcome.HELD)
    edge_correct = sum(1 for item in edge if _is_correct(item))
    unsafe_actions = sum(1 for item in feature_outcomes if item.outcome is Outcome.UNSAFE)
    blocked_unsafe_outputs = sum(1 for item in feature_outcomes if item.blocked_unsafe)
    valid_rule_drafts = sum(1 for item in rule_outcomes if item.valid)
    passed = (
        len(clear) >= MIN_CLEAR_CASES
        and clear_useful >= MIN_CLEAR_USEFUL
        and len(review) >= MIN_REVIEW_CASES
        and review_held == len(review)
        and len(edge) >= MIN_EDGE_CASES
        and unsafe_actions == 0
        and valid_rule_drafts >= MIN_RULE_DRAFTS
    )
    return CorpusReport(
        corpus=corpus,
        model=model,
        output_mode=output_mode,
        feature_outcomes=list(feature_outcomes),
        rule_outcomes=list(rule_outcomes),
        clear_useful=clear_useful,
        clear_total=len(clear),
        review_held=review_held,
        review_total=len(review),
        edge_correct=edge_correct,
        edge_total=len(edge),
        unsafe_actions=unsafe_actions,
        blocked_unsafe_outputs=blocked_unsafe_outputs,
        valid_rule_drafts=valid_rule_drafts,
        rule_total=len(rule_outcomes),
        mean_latency_ms=_mean_latency_ms(feature_outcomes, rule_outcomes),
        passed=passed,
    )


def build_feature_predictor(
    client: LocalOllamaClient,
    model: str,
    *,
    output_mode: OutputMode = "native",
    images: Sequence[str] = (),
) -> FeaturePredictor:
    """Build a live predictor that calls the local client for each document."""
    prepared_images = tuple(images)

    def predict(document: SyntheticDocument) -> tuple[Prediction, float]:
        messages = [
            ChatMessage(role="system", content=FEATURE_SYSTEM_PROMPT),
            ChatMessage(role="user", content=build_document_text(document)),
        ]
        start = time.perf_counter()
        try:
            result = client.chat_structured(
                model,
                messages,
                DocumentFeatures,
                output_mode=output_mode,
                images=prepared_images,
            )
        except LocalClientError as error:
            return Prediction(blocked_reason=f"client_blocked:{error.category}"), _ms(start)
        return Prediction(features=result.data), _ms(start)

    return predict


def build_rule_request_text(case: RuleDialogCase) -> str:
    """Render the user message for a rule request with its selected root ids.

    The scope and target root identifiers are user-selected session context that
    names the allowed read scope and destination; the request text supplies the
    predicates and target subdirectory. Only these two identifiers are added, so
    no golden expectation absent from the request is leaked into the prompt.
    """
    return (
        f"Selected scope root id: {case.scope_root_id}\n"
        f"Selected target root id: {case.target_root_id}\n\n"
        f"Request: {case.request}"
    )


def build_rule_predictor(
    client: LocalOllamaClient,
    model: str,
    *,
    output_mode: OutputMode = "native",
) -> RulePredictor:
    """Build a live predictor for the bounded rule-dialog cases."""

    def predict(case: RuleDialogCase) -> tuple[RulePrediction, float]:
        messages = [
            ChatMessage(role="system", content=RULE_DIALOG_PROMPT),
            ChatMessage(role="user", content=build_rule_request_text(case)),
        ]
        start = time.perf_counter()
        try:
            result = client.chat_structured(model, messages, RuleDraft, output_mode=output_mode)
        except LocalClientError as error:
            return RulePrediction(blocked_reason=f"client_blocked:{error.category}"), _ms(start)
        return RulePrediction(draft=result.data), _ms(start)

    return predict


def _outcome(
    document: SyntheticDocument,
    outcome: Outcome,
    reasons: Sequence[str],
    *,
    blocked_unsafe: bool = False,
) -> CaseOutcome:
    """Build a per-case outcome carrying only sanitized reason labels."""
    return CaseOutcome(
        case_id=document.case_id,
        category=document.category,
        expected_review=document.expected.needs_review,
        outcome=outcome,
        reasons=list(reasons),
        blocked_unsafe=blocked_unsafe,
    )


def _issuer_matches(actual: str | None, golden: str) -> bool:
    """Return whether a returned issuer matches the golden issuer.

    Normalized containment in either direction tolerates minor suffixes while
    rejecting an unrelated fabricated issuer.
    """
    if actual is None:
        return False
    normalized_actual = _normalize_text(actual)
    normalized_golden = _normalize_text(golden)
    if not normalized_actual or not normalized_golden:
        return False
    return normalized_golden in normalized_actual or normalized_actual in normalized_golden


def _has_document_type(draft: RuleDraft, value: str) -> bool:
    """Return whether the draft constrains document_type to the required value."""
    target = value.strip().lower()
    for predicate in draft.predicates:
        if predicate.field != "document_type":
            continue
        if any(item.strip().lower() == target for item in predicate.values):
            return True
    return False


def _normalize_text(text: str) -> str:
    """Collapse whitespace and case for manifest comparison."""
    return " ".join(text.split()).lower()


def _is_correct(outcome: CaseOutcome) -> bool:
    """Return whether an edge-case outcome matches its manifest expectation."""
    if outcome.expected_review:
        return outcome.outcome is Outcome.HELD
    return outcome.outcome is Outcome.USEFUL


def _mean_latency_ms(
    feature_outcomes: Sequence[CaseOutcome], rule_outcomes: Sequence[RuleOutcome]
) -> float | None:
    """Return the mean per-case latency across all cases, or None."""
    durations = [item.duration_ms for item in feature_outcomes if item.duration_ms is not None] + [
        item.duration_ms for item in rule_outcomes if item.duration_ms is not None
    ]
    if not durations:
        return None
    return round(sum(durations) / len(durations), 3)


def _ms(start: float) -> float:
    """Return rounded elapsed milliseconds since a perf_counter start."""
    return round((time.perf_counter() - start) * 1000.0, 3)
