"""Bounded local context and ephemeral history for the dialog."""

from __future__ import annotations

from collections.abc import Sequence

from renamr.core import catalog, examples, folders, roots, rules
from renamr.core.db import Database
from renamr.core.sanitize import clean_text

MAX_CONTEXT_CHARS = 8000
_MAX_HISTORY_CHARS = 1800
_MAX_TURNS = 4
_MAX_FOLDER_LINES = 40
_MAX_LOCATION_SAMPLES = 8


def build_context(db: Database, *, root_ids: list[str], max_chars: int = MAX_CONTEXT_CHARS) -> str:
    """Build a bounded local context from real folders, counts, and rules.

    The context contains root-relative folder names, document counts, classes,
    and a few opaque location/revision ids. It never contains page text.

    Raises:
        RootError: If a selected root id is unknown.
    """
    lines: list[str] = ["Roots:"]
    for root_id in root_ids:
        root = roots.get_root(db, root_id)
        label = root.alias or root.description or root.role
        lines.append(f"- id={root.root_id} role={root.role} label={clean_text(label, limit=80)}")
        for override in folders.list_folder_overrides(db, root.root_id)[:10]:
            lines.append(
                f"  folder={clean_text(override.relative_path, limit=80)} "
                f"label={clean_text(override.alias or override.description, limit=80)}"
            )
        lines.extend(_folder_context(db, root.root_id))
    lines.extend(_rule_context(db))
    lines.extend(_example_context(db))
    text = "\n".join(lines)
    return text[: max(1000, max_chars)]


def _folder_context(db: Database, root_id: str) -> list[str]:
    """Return bounded folder counts, class summaries, and location samples."""
    summary: dict[str, dict[str, int]] = {}
    samples: list[str] = []
    root = roots.get_root(db, root_id)
    for location in catalog.locations_for_root(db, root_id):
        if location.presence is not catalog.Presence.PRESENT:
            continue
        folder = folders.folder_for_path(root, location.canonical_path)
        document_type = next(
            (
                fact.value
                for fact in catalog.get_facts(db, location.revision_id)
                if fact.field == "document_type" and fact.value
            ),
            "unclassified",
        )
        bucket = summary.setdefault(folder, {})
        bucket[document_type] = bucket.get(document_type, 0) + 1
        if len(samples) < _MAX_LOCATION_SAMPLES:
            samples.append(
                f"  location={location.location_id} revision={location.revision_id} "
                f"folder={clean_text(folder or '.', limit=80)} type={document_type}"
            )
    lines: list[str] = []
    for folder in sorted(summary)[:_MAX_FOLDER_LINES]:
        classes = ",".join(f"{name}:{count}" for name, count in sorted(summary[folder].items()))
        documents = sum(summary[folder].values())
        lines.append(f"  folder={clean_text(folder or '.', limit=80)} docs={documents} {classes}")
    if samples:
        lines.append("Location samples:")
        lines.extend(samples)
    return lines


def _rule_context(db: Database) -> list[str]:
    """Return bounded active-rule lines."""
    lines = ["Active rules:"]
    for rule in rules.active_rules(db)[:20]:
        predicates = ",".join(f"{item.field}={','.join(item.values)}" for item in rule.predicates)
        lines.append(
            f"- id={rule.rule_id} revision={rule.revision} scope={rule.scope_root_id} "
            f"subtree={clean_text(rule.scope_subtree, limit=60)} target={rule.target_root_id}/"
            f"{clean_text(rule.target_subdir, limit=60)} template={rule.filename_template} "
            f"priority={rule.priority} predicates={clean_text(predicates, limit=200)}"
        )
    return lines


def _example_context(db: Database) -> list[str]:
    """Return bounded confirmed-example lines."""
    lines = ["Confirmed examples:"]
    for example in examples.list_examples(db)[-20:]:
        values = " ".join(
            f"{key}={clean_text(str(value), limit=60)}"
            for key, value in example.payload.items()
            if key in ("document_type", "issuer", "language", "entity_id", "new_path")
        )
        lines.append(f"- source={example.source} {values}")
    return lines


def bounded_history(history: Sequence[str] | None) -> str:
    """Return a bounded ephemeral summary of the most recent turns."""
    if not history:
        return ""
    recent = list(history)[-_MAX_TURNS:]
    text = "\n".join(f"- {clean_text(item, limit=300)}" for item in recent)
    return text[:_MAX_HISTORY_CHARS]
