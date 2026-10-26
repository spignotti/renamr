"""Conservative rule drafts learned from confirmed examples.

Draft generation is deterministic and never activates anything. Every candidate
is evaluated against confirmed positives, explicit negatives, and currently
active rules before the terminal can test or activate it. One-off labels without
a confirmed destination do not become routing rules.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from renamr.benchmark_schema import RulePredicate
from renamr.core import catalog, examples, roots, rules
from renamr.core.db import Database
from renamr.core.roots import Root

_DEFAULT_TEMPLATE = "{date}_{subject}"
_MAX_CANDIDATES = 20


class RuleImpact(BaseModel):
    """Counts shown before a draft is tested or activated."""

    model_config = ConfigDict(extra="forbid")

    matching_documents: int = 0
    positive_examples: int = 0
    negative_examples: int = 0
    conflicts: int = 0
    rule_id: str | None = None


class RuleCandidate(BaseModel):
    """One deterministic draft candidate with its example impact."""

    model_config = ConfigDict(extra="forbid")

    scope_root_id: str
    target_root_id: str
    target_subdir: str = ""
    predicates: list[RulePredicate]
    filename_template: str = _DEFAULT_TEMPLATE
    example_ids: list[str] = []
    impact: RuleImpact


def propose_candidates(db: Database, *, limit: int = _MAX_CANDIDATES) -> list[RuleCandidate]:
    """Build bounded draft candidates from confirmed destination examples."""
    grouped: dict[tuple[str, str, str, str], list[examples.Example]] = {}
    for example in examples.list_examples(db):
        if example.source == str(examples.ExampleSource.NEGATIVE):
            continue
        destination = _destination(db, example)
        if destination is None:
            continue
        scope_root_id, target_root_id, target_subdir, document_type = destination
        key = (scope_root_id, target_root_id, target_subdir, document_type)
        grouped.setdefault(key, []).append(example)
    candidates: list[RuleCandidate] = []
    for key, members in list(grouped.items())[:limit]:
        scope_root_id, target_root_id, target_subdir, document_type = key
        predicates = _predicates(members, document_type)
        candidate = RuleCandidate(
            scope_root_id=scope_root_id,
            target_root_id=target_root_id,
            target_subdir=target_subdir,
            predicates=predicates,
            example_ids=[item.example_id for item in members],
            impact=RuleImpact(),
        )
        candidates.append(candidate.model_copy(update={"impact": evaluate(db, candidate)}))
    return candidates


def create_draft_from_candidate(db: Database, candidate: RuleCandidate) -> rules.Rule:
    """Persist one candidate as a draft rule; never activate it."""
    return rules.create_rule_draft(
        db,
        scope_root_id=candidate.scope_root_id,
        target_root_id=candidate.target_root_id,
        predicates=candidate.predicates,
        filename_template=candidate.filename_template,
        target_subdir=candidate.target_subdir,
        provenance="learned_from_confirmed_examples",
    )


def evaluate(db: Database, candidate: RuleCandidate) -> RuleImpact:
    """Evaluate a candidate against catalog, examples, and active rules."""
    rule = rules.Rule(
        rule_id=candidate.example_ids[0] if candidate.example_ids else "candidate",
        revision=1,
        scope_root_id=candidate.scope_root_id,
        target_root_id=candidate.target_root_id,
        target_subdir=candidate.target_subdir,
        predicates=candidate.predicates,
        filename_template=candidate.filename_template,
        status=rules.RuleStatus.ACTIVE,
    )
    impact = RuleImpact(matching_documents=_matching_documents(db, rule))
    active = rules.active_rules(db)
    for example in examples.list_examples(db):
        features = _example_features(db, example)
        if not rules.rule_matches(rule, features):
            continue
        if example.source == str(examples.ExampleSource.NEGATIVE):
            impact.negative_examples += 1
            continue
        impact.positive_examples += 1
        decision = rules.select_rule(
            active,
            scope_root_id=candidate.scope_root_id,
            relative_path="example",
            facts=features,
        )
        if decision.state == "conflict" or (
            decision.state == "routed" and decision.target_root_id != candidate.target_root_id
        ):
            impact.conflicts += 1
    return impact


def active_rule_fingerprint(db: Database) -> str:
    """Return a stable fingerprint of the active rule revisions."""
    parts = [
        f"{rule.rule_id}:{rule.revision}:{rule.target_root_id}:{rule.target_subdir}"
        for rule in sorted(rules.active_rules(db), key=lambda item: item.rule_id)
    ]
    return "|".join(parts)


def _destination(db: Database, example: examples.Example) -> tuple[str, str, str, str] | None:
    """Return (scope root, target root, subdir, document type) for one example."""
    document_type = _payload(example, "document_type") or _revision_field(
        db, example.revision_id, "document_type"
    )
    if not document_type:
        return None
    target_path = _payload(example, "new_path")
    if not target_path:
        return None
    target_root = roots.containing_root(db, Path(target_path))
    if target_root is None:
        return None
    old_path = _payload(example, "old_path")
    if old_path:
        scope = roots.containing_root(db, Path(old_path))
    else:
        location = _location(db, example)
        scope = None if location is None else roots.get_root(db, location.root_id)
    if scope is None:
        return None
    subdir = _relative_parent(target_root, Path(target_path))
    return scope.root_id, target_root.root_id, subdir, document_type


def _predicates(members: list[examples.Example], document_type: str) -> list[RulePredicate]:
    """Use document type plus one stable second field when it is shared."""
    predicates = [RulePredicate(field="document_type", operator="equals", values=[document_type])]
    for field in ("issuer", "language"):
        values = [value for item in members if (value := _payload(item, field))]
        if len(set(values)) == 1:
            predicates.append(RulePredicate(field=field, operator="equals", values=[values[0]]))
            break
    return predicates


def _example_features(db: Database, example: examples.Example) -> dict[str, list[str]]:
    """Return the confirmed fields of an example for predicate matching."""
    features: dict[str, list[str]] = {}
    for field in ("document_type", "issuer", "language", "entity_id"):
        value = _payload(example, field) or _revision_field(db, example.revision_id, field)
        if value:
            features[field] = [value]
    return features


def _matching_documents(db: Database, rule: rules.Rule) -> int:
    """Count catalogued locations that the rule's scope and facts would match."""
    count = 0
    for location in catalog.locations_for_root(db, rule.scope_root_id):
        revision = catalog.get_revision(db, location.revision_id)
        if revision.quality_state not in (catalog.QualityState.OK, catalog.QualityState.LOW):
            continue
        grouped: dict[str, list[str]] = {}
        for fact in catalog.get_facts(db, revision.revision_id):
            if fact.value is not None and fact.field != "evidence":
                grouped.setdefault(fact.field, []).append(fact.value)
        if rules.rule_matches(rule, grouped):
            count += 1
    return count


def _payload(example: examples.Example, name: str) -> str | None:
    """Return a non-empty string payload field."""
    value = example.payload.get(name)
    return value if isinstance(value, str) and value else None


def _revision_field(db: Database, revision_id: str | None, field: str) -> str | None:
    """Return a stored fact value for an example revision."""
    if revision_id is None:
        return None
    for fact in catalog.get_facts(db, revision_id):
        if fact.field == field and fact.value:
            return fact.value
    return None


def _location(db: Database, example: examples.Example) -> catalog.LocationRecord | None:
    """Return the example location, or None when it has no stable location."""
    if example.location_id is None:
        return None
    try:
        return catalog.get_location(db, example.location_id)
    except catalog.CatalogError:
        return None


def _relative_parent(root: Root, path: Path) -> str:
    """Return a path's parent relative to a root."""
    try:
        relative = path.relative_to(root.canonical_path)
    except ValueError:
        return ""
    parent = relative.parent.as_posix()
    return "" if parent == "." else parent
