"""Render validated dialog proposal payloads for terminal confirmation."""

from __future__ import annotations

from renamr.core import catalog, learning, roots
from renamr.core.db import Database
from renamr.core.dialog import DialogProposal, DialogResponse, StructureProposal, validate_response
from renamr.core.sanitize import clean_text


def describe_proposal(db: Database, proposal: DialogProposal) -> list[str]:
    """Render validated fields and affected counts outside model explanation."""
    response = DialogResponse.model_validate(proposal.payload["response"])
    validate_response(db, response)
    lines = [f"intent={response.intent}"]
    if response.intent == "propose_rule" and response.rule is not None:
        lines.extend(_describe_rule(db, response))
    elif response.intent == "label_documents":
        lines.extend(_describe_labels(response))
    elif response.intent == "propose_structure" and response.structure is not None:
        lines.extend(_describe_structure(db, response.structure))
    elif response.intent == "propose_setting" and response.setting is not None:
        lines.extend(
            [
                f"setting={response.setting.field}",
                f"new_value={clean_text(response.setting.value, limit=200)}",
            ]
        )
    else:
        lines.append("read-only explanation; no payload")
    return lines


def _describe_rule(db: Database, response: DialogResponse) -> list[str]:
    """Render one rule draft and its affected count."""
    draft = response.rule
    if draft is None:
        return []
    predicates = "; ".join(
        f"{item.field} {item.operator} {','.join(item.values)}" for item in draft.predicates
    )
    return [
        f"scope_root={draft.scope_root_id} subtree={draft.scope_subtree or '.'}",
        f"target_root={draft.target_root_id} target_folder={draft.target_subdir or '.'}",
        f"filename_template={draft.filename_template} priority={draft.priority}",
        f"predicates={clean_text(predicates, limit=300)}",
        f"affected_documents={_rule_affected_count(db, response)}",
        "state=draft (test and activation are separate)",
    ]


def _describe_labels(response: DialogResponse) -> list[str]:
    """Render one-off labels and their locations."""
    lines = [f"labels={len(response.labels)}"]
    for label in response.labels:
        values = ", ".join(
            f"{name}={value}"
            for name, value in {
                "document_type": label.document_type,
                "issuer": label.issuer,
                "date": label.date,
                "subject": label.subject,
                "language": label.language,
            }.items()
            if value
        )
        lines.append(f"location={label.location_id} {clean_text(values, limit=240)}")
    return lines


def _describe_structure(db: Database, proposal: StructureProposal) -> list[str]:
    """Render structure mappings and their affected file count."""
    lines = [f"root={proposal.root_id}"]
    for mapping in proposal.mappings:
        lines.append(
            f"mapping {clean_text(mapping.source or '.', limit=120)} -> "
            f"{clean_text(mapping.target or '.', limit=120)}"
        )
    lines.append(f"affected_documents={_structure_affected(db, proposal)}")
    lines.append("state=structure plan (file moves require separate apply)")
    return lines


def _rule_affected_count(db: Database, response: DialogResponse) -> int:
    """Return the number of catalogued documents a proposed rule would match."""
    if response.rule is None:
        return 0
    candidate = learning.RuleCandidate(
        scope_root_id=response.rule.scope_root_id,
        target_root_id=response.rule.target_root_id,
        target_subdir=response.rule.target_subdir,
        predicates=response.rule.predicates,
        filename_template=response.rule.filename_template,
        impact=learning.RuleImpact(),
    )
    return learning.evaluate(db, candidate).matching_documents


def _structure_affected(db: Database, proposal: StructureProposal) -> int:
    """Count catalogued files under a structure proposal's mapped sources."""
    count = 0
    root = roots.get_root(db, proposal.root_id)
    for location in catalog.locations_for_root(db, proposal.root_id):
        try:
            relative = location.canonical_path.relative_to(root.canonical_path).as_posix()
        except ValueError:
            continue
        if _under_any(relative, [mapping.source for mapping in proposal.mappings]):
            count += 1
    return count


def _under_any(relative: str, sources: list[str]) -> bool:
    """Return whether a relative path lies under a non-empty source folder."""
    for source in sources:
        stripped = source.strip("/")
        if not stripped or relative == stripped or relative.startswith(f"{stripped}/"):
            return True
    return False
