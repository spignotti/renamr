"""Rule disable, rollback, and revision maintenance without deleting history."""

from __future__ import annotations

from renamr.core import rules
from renamr.core.db import Database
from renamr.core.errors import RuleError


def disable_rule(db: Database, rule_id: str, *, reason: str = "") -> rules.Rule:
    """Disable the current revision, keeping its history."""
    return rules.set_rule_status(db, rule_id, rules.RuleStatus.DISABLED, reason=reason)


def rollback_rule(db: Database, rule_id: str, *, revision: int | None = None) -> rules.Rule:
    """Append a revision copied from an earlier one and activate that copy.

    Raises:
        RuleError: If the requested revision does not exist or is the current one.
    """
    history = rules.rule_history(db, rule_id)
    if not history:
        raise RuleError("unknown rule id")
    current = history[-1]
    target = _target_revision(history, revision)
    if target.revision == current.revision:
        raise RuleError("rollback needs an earlier revision")
    return rules.revise_rule(
        db,
        rule_id,
        scope_root_id=target.scope_root_id,
        scope_subtree=target.scope_subtree,
        priority=target.priority,
        predicates=target.predicates,
        target_root_id=target.target_root_id,
        target_subdir=target.target_subdir,
        filename_template=target.filename_template,
        status=str(rules.RuleStatus.ACTIVE),
        provenance=f"rollback_to_revision_{target.revision}",
    )


def maintenance_report(db: Database) -> dict[str, int]:
    """Return counts of rules by lifecycle state and example totals."""
    counts: dict[str, int] = {}
    for rule in rules.list_rules(db):
        counts[str(rule.status)] = counts.get(str(rule.status), 0) + 1
    with db.read() as connection:
        examples = connection.execute("SELECT count(*) FROM review_examples").fetchone()[0]
        observations = connection.execute(
            "SELECT count(*) FROM manual_move_observations WHERE status = 'candidate'"
        ).fetchone()[0]
    counts["examples"] = int(examples)
    counts["move_candidates"] = int(observations)
    return counts


def _target_revision(history: list[rules.Rule], revision: int | None) -> rules.Rule:
    """Return the requested revision, or the revision before the current one."""
    if revision is None:
        return history[-2] if len(history) >= 2 else history[-1]
    for item in history:
        if item.revision == revision:
            return item
    raise RuleError("requested rule revision does not exist")
