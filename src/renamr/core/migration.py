"""One-time, explicit migration of legacy v1 TOML into the database.

The legacy TOML and ``undo.json`` are only read. Cloud model settings, remote
endpoints, and enabled PDF compression are rejected with specific messages.
Import is explicit and applied once; results are recorded in settings.
"""

from __future__ import annotations

import json
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from renamr.core.db import Database
from renamr.core.errors import ConfigError, LegacyRejectedError, RootError
from renamr.core.paths import ensure_private_dir
from renamr.core.roots import RootRole, register_root
from renamr.core.settings import (
    LEGACY_MIGRATION_KEY,
    ModelRoles,
    Settings,
    get_setting,
    load_settings,
    save_settings,
    set_setting,
    validate_filename_template,
)
from renamr.local_transport import EndpointValidationError, normalize_model_name, validate_endpoint

MIGRATION_STATUS_VERSION = 1


class MigrationIssue(BaseModel):
    """A migration problem with a stable code and actionable message."""

    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    blocking: bool = True


class LegacyRootPreview(BaseModel):
    """One legacy inbox path to be registered."""

    model_config = ConfigDict(extra="forbid")

    path: Path
    role: RootRole = RootRole.INBOX
    exists: bool
    description: str = ""


class MigrationPreview(BaseModel):
    """Preview of what a legacy migration would import."""

    model_config = ConfigDict(extra="forbid")

    config_path: Path
    state_dir: Path
    roots: list[LegacyRootPreview] = []
    languages: list[str] = []
    default_filename_template: str | None = None
    model_roles: ModelRoles | None = None
    endpoint: str | None = None
    issues: list[MigrationIssue] = []
    notes: list[str] = []

    @property
    def blocked(self) -> bool:
        """Return whether any blocking issue prevents application."""
        return any(issue.blocking for issue in self.issues)


class MigrationResult(BaseModel):
    """Outcome of applying a migration preview."""

    model_config = ConfigDict(extra="forbid")

    applied: bool
    roots_imported: int = 0
    skipped_roots: list[Path] = []
    backup_path: Path | None = None


def preview_legacy_migration(config_path: Path, *, state_dir: Path) -> MigrationPreview:
    """Build a migration preview from a legacy TOML file without writing state.

    Raises:
        ConfigError: If the legacy file is missing or unreadable.
    """
    config_path = Path(config_path)
    raw = _read_legacy(config_path)
    issues: list[MigrationIssue] = []
    roots = _preview_roots(raw)
    model_roles, endpoint = _preview_models(raw, issues)
    _reject_compression(raw, issues)
    template = _preview_template(raw)
    if template is not None:
        try:
            validate_filename_template(template)
        except ValueError:
            issues.append(
                MigrationIssue(
                    code="invalid_filename_template",
                    message=(
                        "legacy filename_template is not supported in v2; "
                        "use only {date}, {sender}, {subject}, {document_type}"
                    ),
                )
            )
            template = None
    if not roots:
        issues.append(
            MigrationIssue(
                code="no_roots", message="legacy config contains no inbox paths to import"
            )
        )
    return MigrationPreview(
        config_path=config_path,
        state_dir=Path(state_dir),
        roots=roots,
        languages=_preview_languages(raw),
        default_filename_template=template,
        model_roles=model_roles,
        endpoint=endpoint,
        issues=issues,
        notes=["legacy config.toml and undo.json are left untouched"],
    )


def apply_legacy_migration(
    db: Database,
    preview: MigrationPreview,
    *,
    state_dir: Path,
    backup_path: Path | None = None,
) -> MigrationResult:
    """Apply a previewed migration exactly once.

    Raises:
        LegacyRejectedError: If the preview contains blocking issues.
        ConfigError: If a migration was already applied.
    """
    if preview.blocked:
        blocked = "; ".join(issue.message for issue in preview.issues if issue.blocking)
        raise LegacyRejectedError(blocked)
    if get_setting(db, LEGACY_MIGRATION_KEY) is not None:
        raise ConfigError("legacy migration was already applied")
    merged = _validated_settings(db, preview)
    imported, skipped = _import_roots(db, preview, Path(state_dir))
    save_settings(db, merged)
    _record_migration(db, preview, imported)
    return MigrationResult(
        applied=True, roots_imported=imported, skipped_roots=skipped, backup_path=backup_path
    )


def migration_applied(db: Database) -> bool:
    """Return whether a legacy migration has already been applied."""
    return get_setting(db, LEGACY_MIGRATION_KEY) is not None


def ensure_migration_target(state_dir: Path) -> None:
    """Create the private state directory before a migration."""
    ensure_private_dir(Path(state_dir))


def _read_legacy(config_path: Path) -> dict[str, Any]:
    """Read a legacy TOML file, raising a sanitized error on failure."""
    try:
        return tomllib.loads(config_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"legacy config is unreadable: {exc.strerror}") from None
    except tomllib.TOMLDecodeError:
        raise ConfigError("legacy config is not valid TOML") from None


def _import_roots(
    db: Database, preview: MigrationPreview, state_dir: Path
) -> tuple[int, list[Path]]:
    """Register existing legacy roots, returning the count and skipped paths."""
    skipped: list[Path] = []
    imported = 0
    for legacy_root in preview.roots:
        if not legacy_root.exists:
            skipped.append(legacy_root.path)
            continue
        try:
            register_root(
                db,
                path=legacy_root.path,
                role=legacy_root.role,
                state_dir=state_dir,
                description=legacy_root.description,
            )
        except (RootError, ConfigError):
            skipped.append(legacy_root.path)
            continue
        imported += 1
    return imported, skipped


def _record_migration(db: Database, preview: MigrationPreview, imported: int) -> None:
    """Persist the migration status marker."""
    record = {
        "version": MIGRATION_STATUS_VERSION,
        "applied_at": datetime.now(UTC).isoformat(),
        "config_path": str(preview.config_path),
        "roots_imported": imported,
    }
    set_setting(db, LEGACY_MIGRATION_KEY, json.dumps(record))


def _preview_languages(raw: dict[str, Any]) -> list[str]:
    """Return the migrated language list."""
    language = raw.get("language")
    if isinstance(language, str) and language:
        return [language.lower()]
    return ["en", "de"]


def _preview_template(raw: dict[str, Any]) -> str | None:
    """Return the migrated filename template, if any."""
    template = raw.get("filename_template")
    return template if isinstance(template, str) and template else None


def _preview_roots(raw: dict[str, Any]) -> list[LegacyRootPreview]:
    """Collect inbox roots from both new and legacy TOML shapes."""
    entries: list[Any] = []
    if isinstance(raw.get("inbox"), list):
        entries = list(raw["inbox"])
    elif isinstance(raw.get("inbox_paths"), list):
        entries = [{"path": item} for item in raw["inbox_paths"]]
    previews: list[LegacyRootPreview] = []
    for entry in entries:
        preview = _preview_root_entry(entry)
        if preview is not None:
            previews.append(preview)
    return previews


def _preview_root_entry(entry: object) -> LegacyRootPreview | None:
    """Build one root preview from a TOML entry, or None when unusable."""
    if not isinstance(entry, dict):
        return None
    raw_path = entry.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None
    candidate = Path(raw_path).expanduser()
    try:
        resolved = candidate.resolve()
    except OSError:
        resolved = candidate
    description = entry.get("description")
    return LegacyRootPreview(
        path=resolved,
        exists=resolved.is_dir(),
        description=description if isinstance(description, str) else "",
    )


def _preview_models(
    raw: dict[str, Any], issues: list[MigrationIssue]
) -> tuple[ModelRoles | None, str | None]:
    """Inspect legacy model settings, rejecting cloud/remote configuration."""
    llm = raw.get("llm")
    llm_map = llm if isinstance(llm, dict) else {}
    endpoint = _preview_endpoint(llm_map)
    local_endpoint = _loopback_endpoint(endpoint, issues)
    names = [name for name in (llm_map.get("model"), llm_map.get("vision_model")) if name]
    if not names:
        issues.append(
            MigrationIssue(
                code="no_local_model",
                message="legacy config has no local Ollama model to migrate",
            )
        )
        return None, endpoint
    local_names = _local_names(names, local_endpoint, issues)
    if not local_names:
        return None, endpoint
    main = local_names[0]
    if len(local_names) > 1 and local_names[1] != main:
        issues.append(
            MigrationIssue(
                code="vision_role_folded",
                message="separate vision model is not migrated; all roles use the main model",
                blocking=False,
            )
        )
    return ModelRoles(main=main, small=main, maintenance=main), endpoint


def _preview_endpoint(llm_map: dict[str, Any]) -> str | None:
    """Return the legacy api_base endpoint, if configured."""
    api_base = llm_map.get("api_base")
    return str(api_base) if isinstance(api_base, str) and api_base else None


def _local_names(names: list[Any], loopback: bool, issues: list[MigrationIssue]) -> list[str]:
    """Return normalized local model names, recording cloud rejections."""
    local_names: list[str] = []
    for name in names:
        if _is_local_model(str(name), loopback):
            local_names.append(normalize_model_name(str(name)))
        else:
            issues.append(
                MigrationIssue(
                    code="cloud_model_rejected",
                    message=(
                        f"cloud/remote model '{name}' cannot be migrated; configure a local "
                        "Ollama model such as gemma4:e4b"
                    ),
                )
            )
    return local_names


def _loopback_endpoint(endpoint: str | None, issues: list[MigrationIssue]) -> bool:
    """Return whether an endpoint is loopback, recording a blocking issue if not."""
    if endpoint is None:
        return True
    try:
        validate_endpoint(endpoint)
    except EndpointValidationError:
        issues.append(
            MigrationIssue(
                code="remote_endpoint_rejected",
                message="remote api_base endpoints cannot be migrated; only loopback is allowed",
            )
        )
        return False
    return True


def _is_local_model(model: str, loopback_endpoint: bool) -> bool:
    """Return whether a legacy model reference resolves to a local model."""
    if model.strip().lower().startswith("ollama/"):
        return _normalizes(model)
    if not loopback_endpoint or "/" in model:
        return False
    return _normalizes(model)


def _normalizes(model: str) -> bool:
    """Return whether a model name passes local validation."""
    try:
        normalize_model_name(model)
    except Exception:
        return False
    return True


def _reject_compression(raw: dict[str, Any], issues: list[MigrationIssue]) -> None:
    """Record a blocking issue when legacy compression was enabled."""
    compress = raw.get("compress")
    if isinstance(compress, dict) and bool(compress.get("enabled")):
        issues.append(
            MigrationIssue(
                code="compression_rejected",
                message=(
                    "enabled PDF compression cannot be migrated because it rewrites original "
                    "bytes; disable it before migrating"
                ),
            )
        )


def _validated_settings(db: Database, preview: MigrationPreview) -> Settings:
    """Validate the complete merged settings payload before any write.

    Raises:
        ConfigError: If the imported values would make the stored settings invalid.
    """
    current = load_settings(db)
    updates: dict[str, Any] = {"languages": preview.languages}
    if preview.model_roles is not None:
        updates["model_roles"] = preview.model_roles
    if preview.endpoint is not None:
        updates["endpoint"] = preview.endpoint
    if preview.default_filename_template:
        updates["default_filename_template"] = preview.default_filename_template
    try:
        return Settings.model_validate({**current.model_dump(), **updates})
    except ValueError as exc:
        raise ConfigError(f"legacy settings cannot be migrated: {exc}") from None
