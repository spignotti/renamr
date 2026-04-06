"""Shared application models and config loading."""

from __future__ import annotations

import tomllib
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog
from pydantic import BaseModel, Field, field_validator, model_validator

logger = structlog.get_logger(__name__)

DEFAULT_RENAME_PROMPT = """
---
language: en
output_format: json_only
---

# Purpose
Extract sender, subject, document date, and filename format from a document for file renaming.

# Role
File-organization assistant for concise metadata extraction from text previews or document images.

# Context
- Domain: document metadata extraction for automated rename workflows.
- Inputs: filename, created timestamp, and either:
  - content preview text (extracted from PDF/TXT), OR
  - an image of the document (scanned PDF, photo of letter, etc.)
- The content (text or visible text in image) is the primary and only valid source for all
  extracted fields.
- Filesystem "Created" timestamp is NOT a document date unless the same date also appears
  in the content.

# Pre-Task Validation
1. Verify content preview or image exists and is non-empty.
2. If both are missing, empty, or unreadable, return fallback JSON immediately:
   {"sender":"Unknown","subject":"Document","date":"none","filename_format":"date_subject"}

# Task Workflow
1. **Date:** Identify the most meaningful document date from the content.
   Prefer explicitly labeled dates: "Datum", "Versanddatum", "Bestelldatum",
   "Rechnungsdatum", "Ausstellungsdatum", "Date", "Invoice Date", "Order Date", "Sent:".
   If multiple dates exist, choose the clearly labeled primary document date.
   If unclear, set "none". Normalize valid dates to YYYY-MM-DD.
   If partial, invalid, or ambiguous, set "none".
2. **Subject:** Identify a concise topic in 1-5 words.
   For confirmations, prefer explicit types: "Versandbestaetigung", "Bestellbestaetigung",
   "Rechnung", "Mahnung", "Lieferung". Fallback: "Document" or "Note".
3. **Sender:** Identify sender only if it adds clear value (company name, person name,
   organization).
   Sources: letterhead, signature, "From:" fields, logo text. Otherwise "Unknown".
4. **Filename format:** Choose based on sender value:
   - "date_subject" for confirmations, or when sender is Unknown or adds little value.
   - "date_sender_subject" for invoices, emails, or official letters where sender matters.
   - If sender is "Unknown", always use "date_subject".
5. **Language:** Keep extracted text in the document's original language (German stays German,
   English stays English).

# Constraints
- Date: extracted only from document content (text or visible text in image).
  Must be YYYY-MM-DD or "none".
- Do not use filename or filesystem-created timestamp to fabricate a date.
- Do not output any explanatory text outside the JSON object.
- Empty/unreadable content: strict fallback JSON, no exceptions.
- Multiple non-primary dates: "none".
- Partial or invalid date: "none".
- For image inputs: read all visible text before extracting fields.

# Output Contract
- Format: single valid JSON object, nothing else.
- Structure: {"sender":"...","subject":"...","date":"YYYY-MM-DD or none",
  "filename_format":"date_sender_subject or date_subject"}
- Keys: exactly sender, subject, date, filename_format - no additional keys.
- Acceptance checks:
  - All four keys present.
  - date is either a valid ISO date (YYYY-MM-DD) or "none".
  - filename_format is either "date_sender_subject" or "date_subject".
  - No surrounding text, markdown, or code fences.

# Examples
Example A - Versandbestaetigung (no clear sender):
{"sender":"Unknown","subject":"Versandbestaetigung","date":"2025-12-31","filename_format":"date_subject"}

Example B - Invoice from identifiable sender:
{"sender":"ACME GmbH","subject":"Rechnung 2024","date":"2024-11-02",
 "filename_format":"date_sender_subject"}

Example C - Scanned image, no readable date:
{"sender":"Deutsche Post","subject":"Zustellbenachrichtigung","date":"none",
 "filename_format":"date_sender_subject"}
""".strip()


@dataclass(frozen=True)
class EffectiveInboxConfig:
    """Resolved configuration for a single inbox (global defaults + overrides)."""

    path: Path
    filename_template: str
    language: str
    rename_prompt: str


class InboxConfig(BaseModel):
    """Per-inbox configuration with optional override fields."""

    path: str
    filename_template: str | None = None
    language: str | None = None
    rename_prompt: str | None = None

    @field_validator("filename_template")
    @classmethod
    def validate_filename_template(cls, value: str | None) -> str | None:
        """Reject templates that reference unsupported placeholders."""
        if value is None:
            return None
        try:
            value.format(date="x", sender="x", subject="x")
        except KeyError as exc:
            placeholder = exc.args[0]
            raise ValueError(
                f"filename_template contains unknown placeholder: {placeholder}"
            ) from exc
        return value


class LoggingConfig(BaseModel):
    """Logging-related configuration."""

    level: str = Field(default="WARNING")
    json_logs: bool = Field(default=False)


class LLMConfig(BaseModel):
    """LLM provider configuration."""

    model: str = Field(default="gpt-4o-mini")
    api_base: str | None = None
    temperature: float = Field(default=0.2)
    max_retries: int = Field(default=2, ge=0)
    timeout: int = Field(default=60, ge=1)


class CompressConfig(BaseModel):
    """PDF compression settings."""

    enabled: bool = Field(default=False)
    dpi: int = Field(default=150, ge=72)
    jpeg_quality: int = Field(default=80, ge=1, le=100)


class AppConfig(BaseModel):
    """Top-level application configuration."""

    model_config = {"populate_by_name": True}

    # Global defaults for all inboxes
    language: str = Field(default="en")
    filename_template: str = Field(default="{date}_{sender}_{subject}")
    rename_prompt: str = Field(default=DEFAULT_RENAME_PROMPT)

    # Per-inbox configuration (replaces inbox_paths)
    # TOML array-of-tables [[inbox]] creates key "inbox", so alias maps it
    inboxes: list[InboxConfig] = Field(default_factory=list, alias="inbox")

    # Legacy field for backwards compatibility (deprecated)
    inbox_paths: list[str] = Field(default_factory=list)

    # Other global settings
    file_extensions: list[str] = Field(
        default_factory=lambda: [".pdf", ".jpg", ".jpeg", ".png", ".txt"]
    )
    recursive: bool = Field(default=False)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    compress: CompressConfig = Field(default_factory=CompressConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)

    @field_validator("filename_template")
    @classmethod
    def validate_filename_template(cls, value: str) -> str:
        """Reject templates that reference unsupported placeholders."""
        try:
            value.format(date="x", sender="x", subject="x")
        except KeyError as exc:
            placeholder = exc.args[0]
            raise ValueError(
                f"filename_template contains unknown placeholder: {placeholder}"
            ) from exc
        return value

    def get_effective_config(self, inbox: InboxConfig) -> EffectiveInboxConfig:
        """Merge global defaults with inbox-specific overrides."""
        return EffectiveInboxConfig(
            path=Path(inbox.path).expanduser().resolve(),
            filename_template=inbox.filename_template or self.filename_template,
            language=inbox.language or self.language,
            rename_prompt=inbox.rename_prompt or self.rename_prompt,
        )

    @model_validator(mode="after")
    def validate_inboxes(self) -> AppConfig:
        """Ensure at least one inbox is configured (legacy or new format)."""
        if not self.inboxes and not self.inbox_paths:
            raise ValueError("At least one inbox must be configured")
        return self


def load_config(path: Path) -> AppConfig:
    """Load TOML config from disk and merge with model defaults.

    Handles backwards compatibility for legacy inbox_paths format.
    """
    raw_config: dict[str, Any] = {}
    if path.exists():
        with path.open("rb") as file_handle:
            raw_config = tomllib.load(file_handle)

    # Handle backwards compatibility for legacy inbox_paths
    # Note: TOML [[inbox]] creates key "inbox" (singular), not "inboxes"
    if raw_config.get("inbox_paths") and not raw_config.get("inbox"):
        warnings.warn(
            "inbox_paths is deprecated and will be removed in a future version. "
            "Use [[inbox]] sections instead. See README for migration guide.",
            DeprecationWarning,
            stacklevel=2,
        )
        # Convert legacy inbox_paths to new inbox format (TOML [[inbox]] key)
        raw_config["inbox"] = [
            {"path": p} for p in raw_config["inbox_paths"]
        ]

    return AppConfig.model_validate(raw_config)
