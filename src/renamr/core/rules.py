"""Frozen bounded routing rules with deterministic precedence and conflicts.

Rules use the frozen grammar: a scope root plus optional subtree, an integer
priority, at most eight conjunction predicates over ``document_type``, ``issuer``,
``entity_id``, or ``language`` with ``equals``/``in``, an allowed target root and
relative subdirectory, and a bounded filename template. Every mutation appends an
immutable revision; only active rules participate in routing.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from renamr.benchmark_schema import RuleDraft, RulePredicate
from renamr.core.db import Database
from renamr.core.errors import RuleError
from renamr.core.ids import new_id
from renamr.core.settings import validate_filename_template
from renamr.files import sanitize_component

MAX_FILENAME_BYTES = 255
_TEMPLATE_PLACEHOLDERS = ("date", "sender", "subject", "document_type")


class RuleStatus(StrEnum):
    """Lifecycle state of a rule revision."""

    DRAFT = "draft"
    TESTED = "tested"
    ACTIVE = "active"
    DISABLED = "disabled"
    SUPERSEDED = "superseded"


class Rule(BaseModel):
    """One immutable rule revision."""

    model_config = ConfigDict(extra="forbid")

    rule_id: str
    revision: int
    scope_root_id: str
    scope_subtree: str = ""
    priority: int = 0
    predicates: list[RulePredicate]
    target_root_id: str
    target_subdir: str = ""
    filename_template: str
    status: RuleStatus
    provenance: str = ""
    created_at: str = ""


class RoutingDecision(BaseModel):
    """Deterministic routing outcome for one document."""

    model_config = ConfigDict(extra="forbid")

    state: str
    rule_id: str | None = None
    rule_revision: int | None = None
    target_root_id: str | None = None
    target_subdir: str = ""
    filename_template: str | None = None
    reason: str = ""


def create_rule_draft(
    db: Database,
    *,
    scope_root_id: str,
    target_root_id: str,
    predicates: list[RulePredicate],
    filename_template: str,
    scope_subtree: str = "",
    target_subdir: str = "",
    priority: int = 0,
    provenance: str = "",
) -> Rule:
    """Validate and store a new rule revision in the draft state.

    Raises:
        RuleError: If the rule violates the frozen grammar.
    """
    draft = _validate_draft(
        scope_root_id=scope_root_id,
        scope_subtree=scope_subtree,
        priority=priority,
        predicates=predicates,
        target_root_id=target_root_id,
        target_subdir=target_subdir,
        filename_template=filename_template,
    )
    _require_roots(db, draft.scope_root_id, draft.target_root_id)
    rule = Rule(
        rule_id=new_id("rule"),
        revision=1,
        scope_root_id=draft.scope_root_id,
        scope_subtree=draft.scope_subtree or "",
        priority=draft.priority,
        predicates=draft.predicates,
        target_root_id=draft.target_root_id,
        target_subdir=draft.target_subdir,
        filename_template=draft.filename_template,
        status=RuleStatus.DRAFT,
        provenance=provenance,
        created_at=datetime.now(UTC).isoformat(),
    )
    _persist(db, rule)
    return rule


def revise_rule(db: Database, rule_id: str, **changes: object) -> Rule:
    """Append a new revision copying the current rule with overrides applied.

    Raises:
        RuleError: If the rule is unknown or the revision is invalid.
    """
    current = get_rule(db, rule_id)
    merged = current.model_dump()
    merged.update(changes)
    draft = _validate_draft(
        scope_root_id=str(merged["scope_root_id"]),
        scope_subtree=str(merged["scope_subtree"]),
        priority=int(merged["priority"]),
        predicates=list(merged["predicates"]),
        target_root_id=str(merged["target_root_id"]),
        target_subdir=str(merged["target_subdir"]),
        filename_template=str(merged["filename_template"]),
    )
    _require_roots(db, draft.scope_root_id, draft.target_root_id)
    provenance_value = merged.get("provenance")
    rule = Rule(
        rule_id=rule_id,
        revision=current.revision + 1,
        scope_root_id=draft.scope_root_id,
        scope_subtree=draft.scope_subtree or "",
        priority=draft.priority,
        predicates=draft.predicates,
        target_root_id=draft.target_root_id,
        target_subdir=draft.target_subdir,
        filename_template=draft.filename_template,
        status=RuleStatus(str(merged["status"])),
        provenance=current.provenance if provenance_value is None else str(provenance_value),
        created_at=datetime.now(UTC).isoformat(),
    )
    _persist(db, rule)
    return rule


def set_rule_status(db: Database, rule_id: str, status: RuleStatus, *, reason: str = "") -> Rule:
    """Append a new revision that changes only the rule status."""
    return revise_rule(db, rule_id, status=str(status), provenance=reason or None)


def activate_rule(db: Database, rule_id: str) -> Rule:
    """Activate a tested rule.

    Raises:
        RuleError: If the rule has not been tested.
    """
    current = get_rule(db, rule_id)
    if current.status not in (RuleStatus.TESTED, RuleStatus.ACTIVE):
        raise RuleError("a rule must be tested before it can be activated")
    return set_rule_status(db, rule_id, RuleStatus.ACTIVE)


def mark_tested(db: Database, rule_id: str) -> Rule:
    """Mark a draft rule as tested so it may be activated."""
    return set_rule_status(db, rule_id, RuleStatus.TESTED)


def get_rule(db: Database, rule_id: str) -> Rule:
    """Return the current revision of a rule.

    Raises:
        RuleError: If the rule id is unknown.
    """
    with db.read() as connection:
        row = connection.execute("SELECT * FROM rules WHERE rule_id = ?", (rule_id,)).fetchone()
    if row is None:
        raise RuleError("unknown rule id")
    return _row_to_rule(row)


def list_rules(db: Database) -> list[Rule]:
    """Return the current revision of every rule ordered by id."""
    with db.read() as connection:
        rows = connection.execute("SELECT * FROM rules ORDER BY rule_id").fetchall()
    return [_row_to_rule(row) for row in rows]


def active_rules(db: Database) -> list[Rule]:
    """Return the current revision of every active rule."""
    with db.read() as connection:
        rows = connection.execute(
            "SELECT * FROM rules WHERE status = ? ORDER BY rule_id", (str(RuleStatus.ACTIVE),)
        ).fetchall()
    return [_row_to_rule(row) for row in rows]


def rule_history(db: Database, rule_id: str) -> list[Rule]:
    """Return every stored revision of a rule, oldest first."""
    with db.read() as connection:
        rows = connection.execute(
            "SELECT * FROM rule_revisions WHERE rule_id = ? ORDER BY revision", (rule_id,)
        ).fetchall()
    return [_row_to_rule(row) for row in rows]


def rule_matches(rule: Rule, facts: dict[str, list[str]]) -> bool:
    """Return whether every predicate matches the given facts (conjunction)."""
    return all(_predicate_matches(predicate, facts) for predicate in rule.predicates)


def select_rule(
    rules: list[Rule],
    *,
    scope_root_id: str,
    relative_path: str,
    facts: dict[str, list[str]],
) -> RoutingDecision:
    """Choose the winning active rule, detecting equally-ranked conflicts.

    Precedence is deeper scope subtree, then higher priority, then rule id. Two
    top-ranked candidates that tie on specificity and priority but target
    different destinations are a conflict and route nothing.

    Args:
        rules: Candidate rules (active filtering is re-checked here).
        scope_root_id: Document's root.
        relative_path: Document path relative to its root.
        facts: Field name to list of observed values.

    Returns:
        A routed, no-match, or conflict decision.
    """
    applicable = [
        rule
        for rule in rules
        if rule.status is RuleStatus.ACTIVE
        and rule.scope_root_id == scope_root_id
        and _within_subtree(relative_path, rule.scope_subtree)
        and rule_matches(rule, facts)
    ]
    if not applicable:
        return RoutingDecision(state="no_match", reason="no_applicable_rule")
    ranked = sorted(applicable, key=_rank_key)
    best = ranked[0]
    if len(ranked) > 1 and _rank_key(ranked[1])[:2] == _rank_key(best)[:2]:
        second = ranked[1]
        if (second.target_root_id, second.target_subdir) != (
            best.target_root_id,
            best.target_subdir,
        ):
            return RoutingDecision(state="conflict", reason="equally_ranked_rules_conflict")
    return RoutingDecision(
        state="routed",
        rule_id=best.rule_id,
        rule_revision=best.revision,
        target_root_id=best.target_root_id,
        target_subdir=best.target_subdir,
        filename_template=best.filename_template,
        reason="active_rule_match",
    )


def render_base_name(
    template: str, values: dict[str, str], *, max_bytes: int = MAX_FILENAME_BYTES
) -> str:
    """Render a filename base with sanitized components and a UTF-8 byte bound.

    Args:
        template: Validated template using the frozen placeholder set.
        values: Placeholder values (missing values become sanitized fallbacks).
        max_bytes: Maximum UTF-8 byte length of the rendered name.

    Returns:
        A sanitized, byte-bounded filename base without extension.
    """
    safe = {
        placeholder: sanitize_component(values.get(placeholder, "") or "")
        for placeholder in _TEMPLATE_PLACEHOLDERS
    }
    rendered = template.format(**safe).strip(" .")
    if not rendered:
        rendered = "Unknown"
    return _byte_trim(rendered, max_bytes)


def validate_target_subdir(value: str) -> str:
    """Validate a relative target subdirectory, returning it normalized.

    Raises:
        RuleError: If the value is absolute or contains traversal/backslashes.
    """
    try:
        RuleDraft(
            scope_root_id="x",
            predicates=[RulePredicate(field="document_type", operator="equals", values=["x"])],
            target_root_id="x",
            target_subdir=value,
            filename_template="{subject}",
        )
    except ValueError as exc:
        raise RuleError(f"invalid target subdirectory: {exc}") from None
    return value.strip().strip("/")


def _validate_draft(
    *,
    scope_root_id: str,
    scope_subtree: str,
    priority: int,
    predicates: list[RulePredicate],
    target_root_id: str,
    target_subdir: str,
    filename_template: str,
) -> RuleDraft:
    """Validate the complete rule grammar via the frozen RuleDraft model."""
    try:
        validate_filename_template(filename_template)
        return RuleDraft(
            scope_root_id=scope_root_id,
            scope_subtree=scope_subtree or None,
            priority=priority,
            predicates=predicates,
            target_root_id=target_root_id,
            target_subdir=target_subdir,
            filename_template=filename_template,
        )
    except ValueError as exc:
        raise RuleError(f"rule violates the frozen grammar: {exc}") from None


def _require_roots(db: Database, *root_ids: str) -> None:
    """Raise when a referenced root id is unknown."""
    with db.read() as connection:
        for root_id in dict.fromkeys(root_ids):
            row = connection.execute("SELECT 1 FROM roots WHERE root_id = ?", (root_id,)).fetchone()
            if row is None:
                raise RuleError("rule references an unknown root id")


def _predicate_matches(predicate: RulePredicate, facts: dict[str, list[str]]) -> bool:
    """Return whether one predicate matches observed fact values."""
    values = {value.casefold() for value in facts.get(predicate.field, [])}
    if not values:
        return False
    wanted = [value.casefold() for value in predicate.values]
    if predicate.operator == "equals":
        return len(wanted) == 1 and wanted[0] in values
    return any(item in values for item in wanted)


def _within_subtree(relative_path: str, subtree: str) -> bool:
    """Return whether a relative path is inside an optional scope subtree."""
    normalized = relative_path.strip("/")
    scope = subtree.strip("/")
    if not scope:
        return True
    return normalized == scope or normalized.startswith(f"{scope}/")


def _rank_key(rule: Rule) -> tuple[int, int, str]:
    """Return the deterministic precedence key for a rule."""
    depth = len([part for part in rule.scope_subtree.split("/") if part])
    return (-depth, -rule.priority, rule.rule_id)


def _byte_trim(value: str, max_bytes: int) -> str:
    """Trim a string to a UTF-8 byte length without splitting a character."""
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    trimmed = encoded[:max_bytes].decode("utf-8", errors="ignore").rstrip(" ._-")
    return trimmed or "Unknown"


def _persist(db: Database, rule: Rule) -> None:
    """Write a rule revision to the current pointer and the history table."""
    predicates_json = _dump_predicates(rule.predicates)
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO rules (rule_id, revision, scope_root_id, scope_subtree, priority, "
            "predicates, target_root_id, target_subdir, filename_template, status, provenance, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (rule_id) DO UPDATE SET revision = excluded.revision, "
            "scope_root_id = excluded.scope_root_id, scope_subtree = excluded.scope_subtree, "
            "priority = excluded.priority, predicates = excluded.predicates, "
            "target_root_id = excluded.target_root_id, target_subdir = excluded.target_subdir, "
            "filename_template = excluded.filename_template, status = excluded.status, "
            "provenance = excluded.provenance, updated_at = excluded.updated_at",
            (
                rule.rule_id,
                rule.revision,
                rule.scope_root_id,
                rule.scope_subtree,
                rule.priority,
                predicates_json,
                rule.target_root_id,
                rule.target_subdir,
                rule.filename_template,
                str(rule.status),
                rule.provenance,
                rule.created_at,
                rule.created_at,
            ),
        )
        connection.execute(
            "INSERT INTO rule_revisions (rule_id, revision, scope_root_id, scope_subtree, "
            "priority, predicates, target_root_id, target_subdir, filename_template, status, "
            "provenance, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                rule.rule_id,
                rule.revision,
                rule.scope_root_id,
                rule.scope_subtree,
                rule.priority,
                predicates_json,
                rule.target_root_id,
                rule.target_subdir,
                rule.filename_template,
                str(rule.status),
                rule.provenance,
                rule.created_at,
            ),
        )


def _dump_predicates(predicates: list[RulePredicate]) -> str:
    """Serialize predicates to a compact JSON string."""
    return json.dumps([predicate.model_dump() for predicate in predicates], sort_keys=True)


def _load_predicates(raw: str) -> list[RulePredicate]:
    """Deserialize predicates, failing closed on corrupt stored data."""
    try:
        data = json.loads(raw)
        return [RulePredicate.model_validate(item) for item in data]
    except (ValueError, TypeError):
        raise RuleError("stored rule predicates are invalid") from None


def _row_to_rule(row: sqlite3.Row) -> Rule:
    """Build a Rule from a database row."""
    return Rule(
        rule_id=str(row["rule_id"]),
        revision=int(row["revision"]),
        scope_root_id=str(row["scope_root_id"]),
        scope_subtree=str(row["scope_subtree"]),
        priority=int(row["priority"]),
        predicates=_load_predicates(str(row["predicates"])),
        target_root_id=str(row["target_root_id"]),
        target_subdir=str(row["target_subdir"]),
        filename_template=str(row["filename_template"]),
        status=RuleStatus(str(row["status"])),
        provenance=str(row["provenance"]),
        created_at=str(row["created_at"]),
    )
