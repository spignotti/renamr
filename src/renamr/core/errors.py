"""Error hierarchy for the local-first core.

Every message is written to be safe for diagnostics: no document text, real
paths, prompts, or credentials. Categories are stable, machine-readable tokens
used by callers and tests.
"""

from __future__ import annotations


class CoreError(Exception):
    """Base class for all core failures."""

    category = "core_error"


class StateError(CoreError):
    """Invalid or unsafe application state directory."""

    category = "state_error"


class ConfigError(CoreError):
    """Invalid bootstrap or settings configuration."""

    category = "config_error"


class LegacyRejectedError(ConfigError):
    """A legacy cloud/compression setting cannot be migrated."""

    category = "legacy_rejected"


class DatabaseError(CoreError):
    """Base class for database failures."""

    category = "database_error"


class DatabaseUnavailableError(DatabaseError):
    """The database file cannot be opened."""

    category = "database_unavailable"


class DatabaseCorruptError(DatabaseError):
    """The database failed integrity verification."""

    category = "database_corrupt"


class DatabaseNewerVersionError(DatabaseError):
    """The database schema is newer than this build understands."""

    category = "database_newer_version"


class MigrationError(DatabaseError):
    """A schema migration failed."""

    category = "migration_error"


class LockError(CoreError):
    """The writer lock could not be acquired."""

    category = "lock_error"


class RootError(CoreError):
    """Invalid or unknown registered root."""

    category = "root_error"


class PathBoundaryError(RootError):
    """A path escapes its registered root or crosses a symlink boundary."""

    category = "path_boundary"


class CatalogError(CoreError):
    """Inconsistent catalog state."""

    category = "catalog_error"


class ExtractionError(CoreError):
    """Extraction failed or exceeded configured limits."""

    category = "extraction_error"


class InferenceError(CoreError):
    """Local inference is unavailable or misconfigured."""

    category = "inference_error"

    def __init__(self, category: str, message: str | None = None) -> None:
        super().__init__(message or f"local inference failed: {category}")
        self.category = category


class RuleError(CoreError):
    """A rule is invalid, out of bounds, or in a bad state."""

    category = "rule_error"


class RuleConflictError(RuleError):
    """Equally applicable active rules contradict each other."""

    category = "rule_conflict"


class OperationError(CoreError):
    """A file operation could not be planned or applied safely."""

    category = "operation_error"


class DestinationOccupiedError(OperationError):
    """The destination already exists; publication never overwrites."""

    category = "destination_occupied"


class PreconditionError(OperationError):
    """A source hash, size, or presence precondition changed."""

    category = "precondition_failed"


class MetadataUnsupportedError(OperationError):
    """A destination filesystem cannot preserve required metadata."""

    category = "metadata_unsupported"


class JournalError(OperationError):
    """The operation journal is inconsistent or unrecoverable."""

    category = "journal_error"


class UndoError(OperationError):
    """An undo request cannot be satisfied safely."""

    category = "undo_error"
