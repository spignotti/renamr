"""Typed database-owned settings, model roles, and legacy rejection guards.

Active behavior lives in the database, not the bootstrap file. CLI overrides are
temporary and never persisted. Cloud/remote model references and enabled PDF
compression are rejected with actionable messages.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from renamr.core.db import Database
from renamr.core.errors import ConfigError, LegacyRejectedError
from renamr.local_transport import OutputMode, normalize_model_name, validate_endpoint

SETTINGS_KEY = "app"
LEGACY_MIGRATION_KEY = "legacy_migration"
DEFAULT_ENDPOINT = "http://127.0.0.1:11434"
ROUTINE_ROLE = "small"
DIALOG_ROLE = "maintenance"


class ModelRoles(BaseModel):
    """Independently configurable local model roles.

    Routine document classification uses ``small``. Rule and structure dialog
    uses ``maintenance``. ``main`` remains a legacy alias for explicit benchmark
    runs and custom callers; it is not used by routine routing.
    """

    model_config = ConfigDict(extra="forbid")

    main: str = "gemma4:e4b"
    small: str = "gemma4:e4b"
    maintenance: str = "gemma4:e4b"

    @field_validator("main", "small", "maintenance")
    @classmethod
    def validate_role(cls, value: str) -> str:
        """Reject cloud/remote model references in any role."""
        return validate_local_model(value)


class ModelKeepAlive(BaseModel):
    """Model retention per role so an unrelated role does not stay resident."""

    model_config = ConfigDict(extra="forbid")

    main: str = "5m"
    small: str = "2m"
    maintenance: str = "1m"

    @field_validator("main", "small", "maintenance")
    @classmethod
    def validate_keep_alive(cls, value: str) -> str:
        """Require a short non-empty duration token."""
        stripped = value.strip()
        if not stripped or len(stripped) > 16:
            raise ValueError("keep_alive must be a short duration such as 5m")
        return stripped


class ServiceBudgets(BaseModel):
    """Bounds for one scheduled reconciliation tick."""

    model_config = ConfigDict(extra="forbid")

    interval_s: int = Field(default=60, ge=30, le=3600)
    max_documents: int = Field(default=4, ge=1, le=100)
    max_model_calls: int = Field(default=4, ge=0, le=100)
    max_pages: int = Field(default=50, ge=1, le=1000)
    max_seconds: float = Field(default=90.0, gt=0, le=3600)


class Settings(BaseModel):
    """Active application behavior owned by the database."""

    model_config = ConfigDict(extra="forbid")

    endpoint: str = DEFAULT_ENDPOINT
    output_mode: OutputMode = "validated_prompt"
    inference_backend: Literal["local"] = "local"
    model_roles: ModelRoles = Field(default_factory=ModelRoles)
    model_keep_alive: ModelKeepAlive = Field(default_factory=ModelKeepAlive)
    service: ServiceBudgets = Field(default_factory=ServiceBudgets)

    request_timeout_s: float = Field(default=120.0, gt=0)
    num_predict: int = Field(default=512, ge=1)
    num_ctx: int = Field(default=4096, ge=1)

    max_pages: int = Field(default=50, ge=1)
    max_ocr_pages: int = Field(default=50, ge=1)
    max_render_pixels: int = Field(default=40_000_000, ge=1)
    max_file_bytes: int = Field(default=200 * 1024 * 1024, ge=1)
    extraction_timeout_s: float = Field(default=120.0, gt=0)
    feature_max_chars: int = Field(default=12_000, ge=1)
    stability_window_s: float = Field(default=2.0, ge=0)

    languages: list[str] = Field(default_factory=lambda: ["en", "de"])
    default_filename_template: str = "{date}_{sender}_{subject}"
    backup_retention: int = Field(default=3, ge=1)

    @field_validator("endpoint")
    @classmethod
    def validate_local_endpoint(cls, value: str) -> str:
        """Reject non-loopback, credentialed, or proxied endpoints early."""
        try:
            validate_endpoint(value)
        except Exception:
            raise ValueError("endpoint must be a loopback http address") from None
        return value

    @field_validator("default_filename_template")
    @classmethod
    def validate_template(cls, value: str) -> str:
        """Restrict the template to the frozen placeholder set."""
        validate_filename_template(value)
        return value

    @field_validator("model_roles")
    @classmethod
    def validate_roles(cls, roles: ModelRoles) -> ModelRoles:
        """Reject cloud/remote model references in any role."""
        for name in (roles.main, roles.small, roles.maintenance):
            validate_local_model(name)
        return roles

    @model_validator(mode="after")
    def validate_languages(self) -> Settings:
        """Require at least one well-formed language code."""
        cleaned = [code.strip().lower() for code in self.languages if code.strip()]
        if not cleaned:
            raise ValueError("at least one language is required")
        object.__setattr__(self, "languages", cleaned)
        return self


DEFAULT_SETTINGS = Settings()

_ALLOWED_TEMPLATE_PLACEHOLDERS = frozenset({"{date}", "{sender}", "{subject}", "{document_type}"})
_TEMPLATE_PLACEHOLDER = re.compile(r"\{[^}]*\}")


def validate_filename_template(value: str) -> None:
    """Raise when a filename template uses separators or unknown placeholders.

    Raises:
        ValueError: If the template contains separators, traversal, no
            placeholder, an unknown placeholder, or an unbalanced brace.
    """
    if "/" in value or "\\" in value or ".." in value:
        raise ValueError("filename template must not contain separators or traversal")
    placeholders = _TEMPLATE_PLACEHOLDER.findall(value)
    if not placeholders:
        raise ValueError("filename template must contain at least one placeholder")
    for placeholder in placeholders:
        if placeholder not in _ALLOWED_TEMPLATE_PLACEHOLDERS:
            raise ValueError(f"filename template placeholder not allowed: {placeholder}")
    remainder = _TEMPLATE_PLACEHOLDER.sub("", value)
    if "{" in remainder or "}" in remainder:
        raise ValueError("filename template has an unbalanced placeholder")


def get_setting(db: Database, key: str) -> str | None:
    """Return a raw settings value, or None when unset."""
    with db.read() as connection:
        row = connection.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return None if row is None else str(row["value"])


def set_setting(db: Database, key: str, value: str) -> None:
    """Upsert a raw settings value."""
    from datetime import UTC, datetime

    now = datetime.now(UTC).isoformat()
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (key, value, now),
        )


def load_settings(db: Database) -> Settings:
    """Load typed settings, falling back to defaults when unset."""
    raw = get_setting(db, SETTINGS_KEY)
    if raw is None:
        return DEFAULT_SETTINGS.model_copy(deep=True)
    try:
        return Settings.model_validate(json.loads(raw))
    except (ValueError, json.JSONDecodeError) as exc:
        raise ConfigError(f"stored settings are invalid: {exc}") from None


def save_settings(db: Database, settings: Settings) -> None:
    """Persist typed settings as validated JSON."""
    set_setting(db, SETTINGS_KEY, settings.model_dump_json())


def resolve_settings(db: Database, overrides: Mapping[str, Any] | None = None) -> Settings:
    """Return stored settings with temporary, non-persisted CLI overrides.

    Args:
        db: Open catalog database.
        overrides: Optional field overrides applied in memory only.

    Returns:
        The effective settings.

    Raises:
        ConfigError: If the override combination is invalid.
    """
    base = load_settings(db)
    if not overrides:
        return base
    merged = base.model_dump()
    for key, value in overrides.items():
        if key not in merged:
            raise ConfigError(f"unknown settings override: {key}")
        merged[key] = value
    try:
        return Settings.model_validate(merged)
    except ValueError as exc:
        raise ConfigError(f"invalid settings override: {exc}") from None


def validate_local_model(model: str) -> str:
    """Return a verified local model name or raise.

    Only bare names and the ``ollama/`` and ``library/`` namespaces are local;
    every other provider prefix is treated as a cloud/remote reference.

    Raises:
        ValueError: If the reference is empty, malformed, or remote.
    """
    name = model.strip()
    lowered = name.lower()
    for prefix in ("ollama/", "library/"):
        if lowered.startswith(prefix):
            name = name[len(prefix) :]
            break
    if "/" in name:
        raise ValueError("cloud/remote model references are not allowed")
    try:
        return normalize_model_name(name)
    except Exception:
        raise ValueError("model name is not a valid local model") from None


def reject_cloud_model(model: str) -> None:
    """Raise when a model reference is remote or cloud-backed.

    Raises:
        LegacyRejectedError: With an actionable migration message.
    """
    try:
        validate_local_model(model)
    except ValueError:
        raise LegacyRejectedError(
            f"cloud or remote model '{model}' is not supported; v2 requires a locally "
            "installed Ollama model such as gemma4:e4b"
        ) from None


def reject_compression(enabled: bool) -> None:
    """Raise when legacy PDF compression is enabled.

    Compression contradicts the byte-preservation invariant and is not migrated.

    Raises:
        LegacyRejectedError: With an actionable migration message.
    """
    if enabled:
        raise LegacyRejectedError(
            "PDF compression is not supported in v2 because it rewrites original "
            "bytes; disable it (compression is a documented no-op)"
        )
