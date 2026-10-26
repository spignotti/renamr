"""Versioned SQLite schema for the local catalog.

SQL migrations are applied in order and recorded in ``PRAGMA user_version``.
Tables use foreign keys and short explicit transactions; page text is indexed
with an FTS5 ``unicode61`` table. Reserved tables give later modules a place to
add concrete records without duplicating page text.
"""

from __future__ import annotations

from dataclasses import dataclass

SCHEMA_VERSION = 3
DATABASE_APPLICATION_ID = 0x524E4D52


@dataclass(frozen=True)
class Migration:
    """One ordered schema migration."""

    version: int
    statements: tuple[str, ...]


_MIGRATION_1 = (
    f"PRAGMA application_id = {DATABASE_APPLICATION_ID}",
    """
    CREATE TABLE settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE roots (
        root_id TEXT PRIMARY KEY,
        canonical_path TEXT NOT NULL UNIQUE,
        role TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        trusted_examples INTEGER NOT NULL DEFAULT 0,
        automation_enabled INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE root_targets (
        source_root_id TEXT NOT NULL,
        target_root_id TEXT NOT NULL,
        PRIMARY KEY (source_root_id, target_root_id),
        FOREIGN KEY (source_root_id) REFERENCES roots (root_id) ON DELETE CASCADE,
        FOREIGN KEY (target_root_id) REFERENCES roots (root_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE documents (
        document_id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE revisions (
        revision_id TEXT PRIMARY KEY,
        document_id TEXT NOT NULL,
        content_hash TEXT NOT NULL UNIQUE,
        size_bytes INTEGER NOT NULL,
        media_type TEXT NOT NULL,
        extract_key TEXT NOT NULL DEFAULT '',
        feature_key TEXT NOT NULL DEFAULT '',
        quality_state TEXT NOT NULL DEFAULT 'pending',
        failed_stage TEXT NOT NULL DEFAULT '',
        attempts INTEGER NOT NULL DEFAULT 0,
        next_retry_at TEXT,
        error_category TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        FOREIGN KEY (document_id) REFERENCES documents (document_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE locations (
        location_id TEXT PRIMARY KEY,
        revision_id TEXT NOT NULL,
        root_id TEXT NOT NULL,
        canonical_path TEXT NOT NULL UNIQUE,
        dev INTEGER,
        ino INTEGER,
        size_bytes INTEGER NOT NULL,
        mtime_ns INTEGER,
        presence TEXT NOT NULL DEFAULT 'present',
        observed_at TEXT NOT NULL,
        FOREIGN KEY (revision_id) REFERENCES revisions (revision_id) ON DELETE CASCADE,
        FOREIGN KEY (root_id) REFERENCES roots (root_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE pages (
        revision_id TEXT NOT NULL,
        page_number INTEGER NOT NULL,
        text TEXT NOT NULL,
        method TEXT NOT NULL,
        confidence REAL,
        rotation INTEGER NOT NULL DEFAULT 0,
        quality_state TEXT NOT NULL,
        incomplete INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (revision_id, page_number),
        FOREIGN KEY (revision_id) REFERENCES revisions (revision_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE VIRTUAL TABLE pages_fts USING fts5 (
        text,
        revision_id UNINDEXED,
        page_number UNINDEXED,
        tokenize = 'unicode61'
    )
    """,
    """
    CREATE TABLE facts (
        fact_id TEXT PRIMARY KEY,
        revision_id TEXT NOT NULL,
        field TEXT NOT NULL,
        value TEXT,
        evidence_page INTEGER,
        evidence_quote TEXT,
        extractor_version TEXT NOT NULL DEFAULT '',
        validation_state TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY (revision_id) REFERENCES revisions (revision_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE rules (
        rule_id TEXT PRIMARY KEY,
        revision INTEGER NOT NULL,
        scope_root_id TEXT NOT NULL,
        scope_subtree TEXT NOT NULL DEFAULT '',
        priority INTEGER NOT NULL DEFAULT 0,
        predicates TEXT NOT NULL,
        target_root_id TEXT NOT NULL,
        target_subdir TEXT NOT NULL DEFAULT '',
        filename_template TEXT NOT NULL,
        status TEXT NOT NULL,
        provenance TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (scope_root_id) REFERENCES roots (root_id),
        FOREIGN KEY (target_root_id) REFERENCES roots (root_id)
    )
    """,
    """
    CREATE TABLE rule_revisions (
        rule_id TEXT NOT NULL,
        revision INTEGER NOT NULL,
        scope_root_id TEXT NOT NULL,
        scope_subtree TEXT NOT NULL DEFAULT '',
        priority INTEGER NOT NULL DEFAULT 0,
        predicates TEXT NOT NULL,
        target_root_id TEXT NOT NULL,
        target_subdir TEXT NOT NULL DEFAULT '',
        filename_template TEXT NOT NULL,
        status TEXT NOT NULL,
        provenance TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        PRIMARY KEY (rule_id, revision)
    )
    """,
    """
    CREATE TABLE runs (
        run_id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        status TEXT NOT NULL,
        root_id TEXT,
        rule_policy_version TEXT,
        created_at TEXT NOT NULL,
        completed_at TEXT
    )
    """,
    """
    CREATE TABLE run_items (
        item_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL,
        revision_id TEXT,
        location_id TEXT,
        annotation TEXT NOT NULL DEFAULT '',
        source_path TEXT NOT NULL,
        target_path TEXT,
        state TEXT NOT NULL,
        content_hash TEXT,
        size_bytes INTEGER,
        source_dev INTEGER,
        source_ino INTEGER,
        source_size INTEGER,
        source_mtime_ns INTEGER,
        target_dev INTEGER,
        target_ino INTEGER,
        rule_id TEXT,
        rule_revision INTEGER,
        error TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (run_id) REFERENCES runs (run_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE review_items (
        review_id TEXT PRIMARY KEY,
        revision_id TEXT,
        location_id TEXT,
        kind TEXT NOT NULL,
        detail TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'open',
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX idx_locations_revision ON locations (revision_id)
    """,
    """
    CREATE INDEX idx_run_items_run ON run_items (run_id)
    """,
    """
    CREATE INDEX idx_review_items_status ON review_items (status)
    """,
    """
    CREATE UNIQUE INDEX idx_review_open_unique ON review_items (revision_id, location_id, kind)
        WHERE status = 'open'
    """,
    """
    CREATE TABLE review_examples (
        example_id TEXT PRIMARY KEY,
        revision_id TEXT,
        location_id TEXT,
        label TEXT,
        payload TEXT NOT NULL DEFAULT '{}',
        source TEXT NOT NULL DEFAULT 'user',
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE contracts (
        contract_id TEXT PRIMARY KEY,
        alias TEXT,
        description TEXT NOT NULL DEFAULT '',
        state TEXT NOT NULL DEFAULT 'unconfigured',
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE contract_facts (
        fact_id TEXT PRIMARY KEY,
        contract_id TEXT NOT NULL,
        field TEXT NOT NULL,
        value TEXT,
        fact_state TEXT NOT NULL,
        source_revision_id TEXT,
        source_page INTEGER,
        created_at TEXT NOT NULL,
        FOREIGN KEY (contract_id) REFERENCES contracts (contract_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE mcp_permissions (
        permission_id TEXT PRIMARY KEY,
        contract_id TEXT,
        field TEXT NOT NULL,
        granted INTEGER NOT NULL DEFAULT 0,
        granted_at TEXT NOT NULL
    )
    """,
)

_MIGRATION_2 = (
    "ALTER TABLE roots ADD COLUMN alias TEXT NOT NULL DEFAULT ''",
    """
    CREATE TABLE folder_overrides (
        root_id TEXT NOT NULL,
        relative_path TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        alias TEXT NOT NULL DEFAULT '',
        filename_template TEXT,
        target_root_id TEXT,
        target_subdir TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (root_id, relative_path),
        FOREIGN KEY (root_id) REFERENCES roots (root_id) ON DELETE CASCADE,
        FOREIGN KEY (target_root_id) REFERENCES roots (root_id)
    )
    """,
    """
    CREATE TABLE manual_move_observations (
        observation_id TEXT PRIMARY KEY,
        revision_id TEXT NOT NULL,
        old_location_id TEXT NOT NULL,
        new_location_id TEXT NOT NULL,
        old_path TEXT NOT NULL,
        new_path TEXT NOT NULL,
        match_kind TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'candidate',
        example_id TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY (revision_id) REFERENCES revisions (revision_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE structure_plans (
        plan_id TEXT PRIMARY KEY,
        root_id TEXT NOT NULL,
        revision INTEGER NOT NULL,
        state TEXT NOT NULL DEFAULT 'draft',
        payload TEXT NOT NULL,
        run_id TEXT,
        rule_fingerprint TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        applied_at TEXT,
        FOREIGN KEY (root_id) REFERENCES roots (root_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE structure_items (
        item_id TEXT PRIMARY KEY,
        plan_id TEXT NOT NULL,
        source_path TEXT NOT NULL,
        target_path TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        rule_id TEXT,
        rule_revision INTEGER,
        state TEXT NOT NULL DEFAULT 'planned',
        error TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY (plan_id) REFERENCES structure_plans (plan_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE proposals (
        proposal_id TEXT PRIMARY KEY,
        intent TEXT NOT NULL,
        payload TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'proposed',
        decision TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        decided_at TEXT
    )
    """,
    """
    CREATE TABLE contract_members (
        contract_id TEXT NOT NULL,
        root_id TEXT NOT NULL,
        relative_path TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (contract_id, root_id, relative_path),
        FOREIGN KEY (contract_id) REFERENCES contracts (contract_id) ON DELETE CASCADE,
        FOREIGN KEY (root_id) REFERENCES roots (root_id) ON DELETE CASCADE
    )
    """,
    "ALTER TABLE contract_facts ADD COLUMN evidence_quote TEXT",
    "ALTER TABLE contract_facts ADD COLUMN source_document_type TEXT",
    "ALTER TABLE contract_facts ADD COLUMN effective_date TEXT",
    "ALTER TABLE contract_facts ADD COLUMN relationship TEXT",
    "ALTER TABLE contract_facts ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''",
    """
    CREATE UNIQUE INDEX idx_mcp_permission_unique
        ON mcp_permissions (contract_id, field)
    """,
    """
    CREATE INDEX idx_manual_moves_status
        ON manual_move_observations (status)
    """,
    """
    CREATE INDEX idx_contract_members_root
        ON contract_members (root_id)
    """,
    """
    CREATE INDEX idx_structure_items_plan
        ON structure_items (plan_id)
    """,
)

_MIGRATION_3 = (
    """
    CREATE TABLE contract_scopes (
        root_id TEXT PRIMARY KEY,
        base_path TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        FOREIGN KEY (root_id) REFERENCES roots (root_id) ON DELETE CASCADE
    )
    """,
    "ALTER TABLE contract_members ADD COLUMN member_id TEXT",
    "ALTER TABLE contract_members ADD COLUMN dev INTEGER",
    "ALTER TABLE contract_members ADD COLUMN ino INTEGER",
    """
    CREATE TABLE contract_references (
        reference_id TEXT PRIMARY KEY,
        contract_id TEXT NOT NULL,
        value TEXT NOT NULL,
        value_key TEXT NOT NULL,
        source_revision_id TEXT NOT NULL,
        source_page INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE (contract_id, value_key),
        FOREIGN KEY (contract_id) REFERENCES contracts (contract_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE INDEX idx_contract_references_key ON contract_references (value_key)
    """,
    """
    CREATE INDEX idx_contract_members_identity ON contract_members (dev, ino)
    """,
)

MIGRATIONS: tuple[Migration, ...] = (
    Migration(version=1, statements=_MIGRATION_1),
    Migration(version=2, statements=_MIGRATION_2),
    Migration(version=3, statements=_MIGRATION_3),
)
