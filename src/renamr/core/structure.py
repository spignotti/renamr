"""Revisioned folder/structure proposals with journaled, recoverable application.

Proposals are persisted and never change files. Applying one snapshots file and
rule preconditions, moves only explicitly mapped folders, leaves unmapped and
hidden files untouched, and activates a rule update only after every file
operation succeeded. Old folders are never deleted.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from renamr.core import catalog, contracts, folders, mutation, operations, roots, rules
from renamr.core.db import Database
from renamr.core.errors import CoreError, RuleError
from renamr.core.journal import FaultInjector
from renamr.core.roots import Root
from renamr.core.workspace import Workspace


class FolderMapping(BaseModel):
    """One explicitly approved source-to-target folder mapping."""

    model_config = ConfigDict(extra="forbid")

    source: str
    target: str


class RuleUpdate(BaseModel):
    """An optional rule template change activated after files moved."""

    model_config = ConfigDict(extra="forbid")

    rule_id: str
    rule_revision: int
    filename_template: str


class StructureDraft(BaseModel):
    """The user-visible content of a structure proposal."""

    model_config = ConfigDict(extra="forbid")

    root_id: str
    mappings: list[FolderMapping] = []
    rename_template: str | None = None
    rule_update: RuleUpdate | None = None


class StructureItem(BaseModel):
    """One planned file move with its precondition."""

    model_config = ConfigDict(extra="forbid")

    item_id: str
    source_path: Path
    target_path: Path
    content_hash: str
    state: str = "planned"


class StructurePlan(BaseModel):
    """A persisted structure proposal and its file snapshot."""

    model_config = ConfigDict(extra="forbid")

    plan_id: str
    root_id: str
    revision: int
    state: str
    draft: StructureDraft
    run_id: str | None = None
    rule_fingerprint: str = ""
    items: list[StructureItem] = []


class StructureApplyResult(BaseModel):
    """Outcome of applying one structure plan."""

    model_config = ConfigDict(extra="forbid")

    plan_id: str
    state: str
    committed: int = 0
    blocked: int = 0
    failed: int = 0
    rule_updated: bool = False


def propose_structure(db: Database, draft: StructureDraft) -> StructurePlan:
    """Persist a draft plan and snapshot every explicit file precondition.

    Raises:
        CoreError: If the root/mapping/template is invalid or a mapped target
            escapes its root.
    """
    root = roots.get_root(db, draft.root_id)
    _validate_draft(db, root, draft)
    fingerprint = _rule_fingerprint(db)
    plan_id = _new_plan_id()
    items = _snapshot_items(db, root, draft)
    now = datetime.now(UTC).isoformat()
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO structure_plans (plan_id, root_id, revision, state, payload, "
            "rule_fingerprint, created_at) VALUES (?, ?, 1, 'draft', ?, ?, ?)",
            (plan_id, root.root_id, draft.model_dump_json(), fingerprint, now),
        )
        for item in items:
            connection.execute(
                "INSERT INTO structure_items (item_id, plan_id, source_path, target_path, "
                "content_hash, state, created_at) VALUES (?, ?, ?, ?, ?, 'planned', ?)",
                (
                    item.item_id,
                    plan_id,
                    str(item.source_path),
                    str(item.target_path),
                    item.content_hash,
                    now,
                ),
            )
    return StructurePlan(
        plan_id=plan_id,
        root_id=root.root_id,
        revision=1,
        state="draft",
        draft=draft,
        rule_fingerprint=fingerprint,
        items=items,
    )


def get_plan(db: Database, plan_id: str) -> StructurePlan:
    """Return one plan with its items or raise."""
    with db.read() as connection:
        row = connection.execute(
            "SELECT * FROM structure_plans WHERE plan_id = ?", (plan_id,)
        ).fetchone()
        if row is None:
            raise CoreError("unknown structure plan")
        item_rows = connection.execute(
            "SELECT * FROM structure_items WHERE plan_id = ? ORDER BY source_path", (plan_id,)
        ).fetchall()
    return StructurePlan(
        plan_id=str(row["plan_id"]),
        root_id=str(row["root_id"]),
        revision=int(row["revision"]),
        state=str(row["state"]),
        draft=StructureDraft.model_validate_json(str(row["payload"])),
        run_id=None if row["run_id"] is None else str(row["run_id"]),
        rule_fingerprint=str(row["rule_fingerprint"]),
        items=[
            StructureItem(
                item_id=str(item["item_id"]),
                source_path=Path(str(item["source_path"])),
                target_path=Path(str(item["target_path"])),
                content_hash=str(item["content_hash"]),
                state=str(item["state"]),
            )
            for item in item_rows
        ],
    )


def list_plans(db: Database) -> list[StructurePlan]:
    """Return every plan newest first."""
    with db.read() as connection:
        ids = [
            str(row["plan_id"])
            for row in connection.execute(
                "SELECT plan_id FROM structure_plans ORDER BY created_at DESC"
            ).fetchall()
        ]
    return [get_plan(db, plan_id) for plan_id in ids]


def apply_structure(
    ws: Workspace, plan_id: str, *, fault: FaultInjector | None = None
) -> StructureApplyResult:
    """Apply one plan under the writer lock after revalidating every precondition."""
    with ws.lock() as lock:
        plan = get_plan(ws.db, plan_id)
        if plan.state not in ("draft", "failed", "partial"):
            raise CoreError("structure plan is not applicable")
        if plan.rule_fingerprint != _rule_fingerprint(ws.db):
            _set_plan_state(ws.db, plan_id, "stale")
            raise RuleError("active rules changed since the plan was proposed")
        run_id = _plan_run(ws.db, plan)
        operations.recover_run(ws.db, run_id, lock=lock, guard=mutation.boundary_guard(ws))
        result = operations.apply_run(
            ws.db,
            run_id,
            lock=lock,
            guard=mutation.boundary_guard(ws),
            fault=fault,
        )
        _copy_item_states(ws.db, plan_id, run_id)
        if result.blocked or result.failed:
            operations.recover_run(ws.db, run_id, lock=lock, guard=None)
            state = "failed" if result.failed and not result.committed else "partial"
            _set_plan_state(ws.db, plan_id, state)
            return StructureApplyResult(
                plan_id=plan_id,
                state=state,
                committed=result.committed,
                blocked=result.blocked,
                failed=result.failed,
            )
        contracts.remap_members(
            ws.db,
            plan.root_id,
            [(item.source, item.target) for item in plan.draft.mappings],
        )
        contracts.reconcile_members(ws.db)
        updated = _apply_rule_update(ws.db, plan.draft.rule_update)
        _set_plan_state(ws.db, plan_id, "applied")
        return StructureApplyResult(
            plan_id=plan_id,
            state="applied",
            committed=result.committed,
            blocked=result.blocked,
            failed=result.failed,
            rule_updated=updated,
        )


def _plan_run(db: Database, plan: StructurePlan) -> str:
    """Return the plan's journal run, creating and recording it once."""
    if plan.run_id is not None:
        return plan.run_id
    run_id = operations.create_run(db, kind="structure", root_id=plan.root_id)
    for item in plan.items:
        size = item.source_path.stat().st_size if item.source_path.exists() else 0
        operations.add_plan_item(
            db,
            run_id,
            source_path=item.source_path,
            target_path=item.target_path,
            content_hash=item.content_hash,
            size_bytes=size,
            annotation="structure",
        )
    with db.transaction() as connection:
        connection.execute(
            "UPDATE structure_plans SET run_id = ? WHERE plan_id = ?", (run_id, plan.plan_id)
        )
    return run_id


def _snapshot_items(db: Database, root: Root, draft: StructureDraft) -> list[StructureItem]:
    """Plan mapped files and record review questions for missing name fields."""
    items: list[StructureItem] = []
    for location in catalog.locations_for_root(db, root.root_id):
        if location.presence is not catalog.Presence.PRESENT:
            continue
        relative = _relative(location.canonical_path, root.canonical_path)
        mapped = _mapped_target(relative, draft)
        if mapped is None:
            continue
        target = roots.resolve_subpath(root, mapped)
        if draft.rename_template is not None:
            fields = _fields(db, location.revision_id)
            missing = _missing_fields(draft.rename_template, fields)
            if missing:
                catalog.record_review(
                    db,
                    kind="naming_field_missing",
                    detail=f"structure|{missing[0]}",
                    revision_id=location.revision_id,
                    location_id=location.location_id,
                )
                continue
            base = rules.render_base_name(draft.rename_template, fields)
            target = target / f"{base}{location.canonical_path.suffix.lower()}"
        if target == location.canonical_path:
            continue
        revision = catalog.get_revision(db, location.revision_id)
        items.append(
            StructureItem(
                item_id=_new_item_id(),
                source_path=location.canonical_path,
                target_path=target,
                content_hash=revision.content_hash,
            )
        )
    return items


def _mapped_target(relative: str, draft: StructureDraft) -> str | None:
    """Return the mapped relative target for one file, or None when untouched."""
    for mapping in sorted(draft.mappings, key=lambda item: len(item.source), reverse=True):
        source = mapping.source.strip("/")
        target = mapping.target.strip("/")
        if not source:
            remainder = relative
        elif relative == source:
            remainder = Path(relative).name
        elif relative.startswith(f"{source}/"):
            remainder = relative[len(source) + 1 :]
        else:
            continue
        return f"{target}/{remainder}".strip("/") if target else remainder
    if draft.rename_template is not None:
        return relative.rsplit("/", 1)[0] if "/" in relative else ""
    return None


def _validate_draft(db: Database, root: Root, draft: StructureDraft) -> None:
    """Validate mappings, template, and rule update."""
    if not draft.mappings and draft.rename_template is None:
        raise CoreError("a structure draft needs an explicit mapping or rename template")
    for mapping in draft.mappings:
        source = folders.validate_relative_path(mapping.source)
        target = folders.validate_relative_path(mapping.target)
        roots.resolve_subpath(root, source)
        roots.resolve_subpath(root, target)
        if source in ("", ".") and target in ("", "."):
            raise CoreError("mapping does not change a folder")
    if draft.rename_template is not None:
        _validate_template(draft.rename_template)
    if draft.rule_update is not None:
        current = rules.get_rule(db, draft.rule_update.rule_id)
        if current.status is not rules.RuleStatus.ACTIVE:
            raise RuleError("rule update must reference an active rule")
        if current.revision != draft.rule_update.rule_revision:
            raise RuleError("rule update references a stale revision")
        _validate_template(draft.rule_update.filename_template)


def _apply_rule_update(db: Database, update: RuleUpdate | None) -> bool:
    """Activate a rule template update only after every file operation succeeded."""
    if update is None:
        return False
    current = rules.get_rule(db, update.rule_id)
    if current.revision != update.rule_revision:
        raise RuleError("rule changed while the plan was applying")
    rules.revise_rule(
        db,
        update.rule_id,
        filename_template=update.filename_template,
        status=str(rules.RuleStatus.ACTIVE),
    )
    return True


def _copy_item_states(db: Database, plan_id: str, run_id: str) -> None:
    """Mirror operation item states into the plan snapshot."""
    items = {str(item.source_path): item.state for item in operations.run_items(db, run_id)}
    with db.transaction() as connection:
        for row in connection.execute(
            "SELECT item_id, source_path FROM structure_items WHERE plan_id = ?", (plan_id,)
        ).fetchall():
            state = items.get(str(row["source_path"]))
            if state is not None:
                connection.execute(
                    "UPDATE structure_items SET state = ? WHERE item_id = ?",
                    (str(state), str(row["item_id"])),
                )


def _set_plan_state(db: Database, plan_id: str, state: str) -> None:
    """Record a plan's lifecycle state."""
    now = datetime.now(UTC).isoformat()
    with db.transaction() as connection:
        connection.execute(
            "UPDATE structure_plans SET state = ?, applied_at = ? WHERE plan_id = ?",
            (state, now, plan_id),
        )


def _fields(db: Database, revision_id: str) -> dict[str, str]:
    """Return confirmed filename fields for one revision."""
    values = {fact.field: fact.value or "" for fact in catalog.get_facts(db, revision_id)}
    return {
        "date": values.get("date", ""),
        "sender": values.get("issuer", ""),
        "subject": values.get("subject", ""),
        "document_type": values.get("document_type", ""),
    }


def _missing_fields(template: str, fields: dict[str, str]) -> list[str]:
    """Return template placeholders without a confirmed value."""
    return [
        name
        for name in ("date", "sender", "subject", "document_type")
        if "{" + name + "}" in template and not fields.get(name, "").strip()
    ]


def _validate_template(template: str) -> None:
    """Validate a filename template, mapping failures to a core error."""
    from renamr.core.settings import validate_filename_template

    try:
        validate_filename_template(template)
    except ValueError as exc:
        raise CoreError(str(exc)) from None


def _rule_fingerprint(db: Database) -> str:
    """Return a stable fingerprint of currently active rule revisions."""
    parts = [
        f"{rule.rule_id}:{rule.revision}"
        for rule in sorted(rules.active_rules(db), key=lambda item: item.rule_id)
    ]
    return "|".join(parts)


def _relative(path: Path, root: Path) -> str:
    """Return a POSIX relative path."""
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.name


def _new_plan_id() -> str:
    """Return an opaque structure plan id."""
    from renamr.core.ids import new_id

    return new_id("plan")


def _new_item_id() -> str:
    """Return an opaque structure item id."""
    from renamr.core.ids import new_id

    return new_id("item")
