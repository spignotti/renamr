"""P3 tests: frozen rule grammar, precedence, conflicts, and rendering."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import pytest

from renamr.benchmark_schema import RulePredicate
from renamr.core.db import Database
from renamr.core.errors import RuleError
from renamr.core.roots import RootRole, register_root
from renamr.core.rules import (
    Rule,
    RuleStatus,
    activate_rule,
    create_rule_draft,
    get_rule,
    list_rules,
    mark_tested,
    render_base_name,
    revise_rule,
    rule_history,
    select_rule,
    validate_target_subdir,
)


def _db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "state" / "catalog.sqlite3")
    db.initialize()
    return db


def _root_ids(tmp_path: Path, db: Database) -> tuple[str, str]:
    inbox = tmp_path / "inbox"
    archive = tmp_path / "archive"
    inbox.mkdir(exist_ok=True)
    archive.mkdir(exist_ok=True)
    inbox_root = register_root(db, path=inbox, role=RootRole.INBOX, state_dir=tmp_path / "state")
    archive_root = register_root(
        db, path=archive, role=RootRole.ARCHIVE, state_dir=tmp_path / "state"
    )
    return inbox_root.root_id, archive_root.root_id


def _predicate(
    *values: str,
    field: Literal["document_type", "issuer", "entity_id", "language"] = "document_type",
) -> RulePredicate:
    return RulePredicate(
        field=field, operator="equals" if len(values) == 1 else "in", values=list(values)
    )


def _rule(
    *,
    rule_id: str,
    subtree: str = "",
    priority: int = 0,
    subdir: str = "",
    root: str = "root-archive",
    scope: str = "root-inbox",
    status: RuleStatus = RuleStatus.ACTIVE,
    predicates: list[RulePredicate] | None = None,
) -> Rule:
    return Rule(
        rule_id=rule_id,
        revision=1,
        scope_root_id=scope,
        scope_subtree=subtree,
        priority=priority,
        predicates=predicates or [_predicate("invoice")],
        target_root_id=root,
        target_subdir=subdir,
        filename_template="{date}_{subject}",
        status=status,
    )


def test_create_and_activate_rule(tmp_path: Path) -> None:
    db = _db(tmp_path)
    inbox_id, archive_id = _root_ids(tmp_path, db)
    draft = create_rule_draft(
        db,
        scope_root_id=inbox_id,
        target_root_id=archive_id,
        predicates=[_predicate("invoice")],
        filename_template="{date}_{subject}",
        target_subdir="invoices",
    )
    assert draft.status is RuleStatus.DRAFT
    assert list_rules(db)[0].rule_id == draft.rule_id
    with pytest.raises(RuleError, match="tested before"):
        activate_rule(db, draft.rule_id)

    mark_tested(db, draft.rule_id)
    active = activate_rule(db, draft.rule_id)
    assert active.status is RuleStatus.ACTIVE
    assert get_rule(db, draft.rule_id).revision == 3
    assert [item.status for item in rule_history(db, draft.rule_id)] == [
        RuleStatus.DRAFT,
        RuleStatus.TESTED,
        RuleStatus.ACTIVE,
    ]


def test_rule_grammar_is_bounded(tmp_path: Path) -> None:
    db = _db(tmp_path)
    with pytest.raises(RuleError):
        create_rule_draft(
            db,
            scope_root_id="r",
            target_root_id="t",
            predicates=[_predicate("invoice")],
            filename_template="{date}_{other}",
        )
    with pytest.raises(RuleError):
        create_rule_draft(
            db,
            scope_root_id="r",
            target_root_id="t",
            predicates=[_predicate("invoice")],
            filename_template="no placeholder",
        )
    with pytest.raises(RuleError):
        create_rule_draft(
            db,
            scope_root_id="r",
            target_root_id="t",
            predicates=[_predicate("invoice")],
            filename_template="{subject}",
            target_subdir="/absolute",
        )
    with pytest.raises(RuleError):
        create_rule_draft(
            db,
            scope_root_id="r",
            target_root_id="t",
            predicates=[_predicate(f"value{i}") for i in range(9)],
            filename_template="{subject}",
        )


def test_revise_appends_revision(tmp_path: Path) -> None:
    db = _db(tmp_path)
    inbox_id, archive_id = _root_ids(tmp_path, db)
    draft = create_rule_draft(
        db,
        scope_root_id=inbox_id,
        target_root_id=archive_id,
        predicates=[_predicate("invoice")],
        filename_template="{date}_{subject}",
    )
    revised = revise_rule(db, draft.rule_id, priority=7)
    assert revised.revision == 2
    assert revised.priority == 7
    assert len(rule_history(db, draft.rule_id)) == 2


def test_precedence_and_conflicts() -> None:
    invoice = {"document_type": ["invoice"]}
    general = _rule(rule_id="a", subdir="all")
    specific = _rule(rule_id="b", subtree="invoices", subdir="invoices")
    high_priority = _rule(rule_id="c", priority=5, subdir="priority")

    decision = select_rule(
        [general, specific],
        scope_root_id="root-inbox",
        relative_path="invoices/x.pdf",
        facts=invoice,
    )
    assert decision.state == "routed"
    assert decision.rule_id == "b"

    decision = select_rule(
        [general, high_priority], scope_root_id="root-inbox", relative_path="x.pdf", facts=invoice
    )
    assert decision.rule_id == "c"

    conflicting = _rule(rule_id="d", subdir="other")
    decision = select_rule(
        [general, conflicting], scope_root_id="root-inbox", relative_path="x.pdf", facts=invoice
    )
    assert decision.state == "conflict"


def test_only_active_rules_route() -> None:
    invoice = {"document_type": ["invoice"]}
    draft = _rule(rule_id="a", status=RuleStatus.DRAFT)
    assert (
        select_rule([draft], scope_root_id="root-inbox", relative_path="x.pdf", facts=invoice).state
        == "no_match"
    )
    other_root = _rule(rule_id="b", scope="root-other")
    assert (
        select_rule(
            [other_root], scope_root_id="root-inbox", relative_path="x.pdf", facts=invoice
        ).state
        == "no_match"
    )
    assert (
        select_rule(
            [draft],
            scope_root_id="root-inbox",
            relative_path="x.pdf",
            facts={"document_type": ["receipt"]},
        ).state
        == "no_match"
    )


def test_render_base_name_sanitizes_and_trims() -> None:
    name = render_base_name(
        "{date}_{sender}_{subject}", {"date": "20260314", "sender": "A/B:C", "subject": "Rechnung"}
    )
    assert name == "20260314_ABC_Rechnung"

    long_name = render_base_name("{subject}", {"subject": "Ü" * 400})
    assert len(long_name.encode("utf-8")) <= 255


def test_validate_target_subdir() -> None:
    assert validate_target_subdir("invoices/2026") == "invoices/2026"
    for bad in ("/abs", "..", "a/../b"):
        with pytest.raises(RuleError):
            validate_target_subdir(bad)


def test_rule_requires_known_roots(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _root_ids(tmp_path, db)
    with pytest.raises(RuleError):
        create_rule_draft(
            db,
            scope_root_id="missing",
            target_root_id="missing",
            predicates=[_predicate("invoice")],
            filename_template="{subject}",
        )
