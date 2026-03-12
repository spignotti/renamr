"""Shared application models and config loading."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

DEFAULT_RENAME_PROMPT = """
<!--
id: file_rename_metadata
prompt_language: en
output_language: en
complexity: standard
output_format_style: json
template_type: task
-->

# Purpose
Extract sender, subject, document date, and filename format for file renaming.

# Role
You extract concise metadata from document text or an image of the document.

# Input
Filename, created timestamp, and either:
- content preview text, or
- an image of the document (read visible text).

# Task
1) Identify the most meaningful document date from the content.
   Prefer explicitly labeled dates: "Datum", "Versanddatum", "Bestelldatum",
   "Rechnungsdatum", "Ausstellungsdatum", "Date", "Invoice Date", "Order Date".
2) Identify a subject in 1-5 words, short and specific.
   For confirmations, prefer explicit types like:
   "Versandbestatigung", "Bestellbestatigung", "Rechnung", "Mahnung", "Lieferung".
3) Identify a sender only if it adds clear value (company/person name).
   Otherwise use "Unknown".
4) Choose filename format:
   - Use "date_subject" for confirmations or when sender adds little value.
   - Use "date_sender_subject" for invoices, emails, or official letters where sender matters.
5) Keep language as in the document (German stays German).

# Constraints
- Date must be from document content only (not file metadata).
- If no clear date is present, set "date":"none".
- If sender is Unknown, prefer "date_subject".
- Output JSON only, exactly:
  {"sender":"...","subject":"...","date":"YYYY-MM-DD or none",
   "filename_format":"date_sender_subject or date_subject"}

# Fallback
If content is empty or unreadable:
{"sender":"Unknown","subject":"Document","date":"none",
 "filename_format":"date_subject"}

# Examples
Example A (Versandbestatigung):
Output: {"sender":"Unknown","subject":"Versandbestatigung",
 "date":"2025-12-31","filename_format":"date_subject"}

Example B (Invoice from sender):
Output: {"sender":"ACME GmbH","subject":"Rechnung 2024",
 "date":"2024-11-02","filename_format":"date_sender_subject"}
""".strip()


class LoggingConfig(BaseModel):
    """Logging-related configuration."""

    level: str = Field(default="INFO")
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

    inbox_path: str = Field(default=".")
    file_extensions: list[str] = Field(
        default_factory=lambda: [".pdf", ".jpg", ".jpeg", ".png", ".txt"]
    )
    recursive: bool = Field(default=False)
    filename_template: str = Field(default="{date}_{sender}_{subject}")
    rename_prompt: str = Field(default=DEFAULT_RENAME_PROMPT)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    compress: CompressConfig = Field(default_factory=CompressConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)


def load_config(path: Path) -> AppConfig:
    """Load TOML config from disk and merge with model defaults."""
    raw_config: dict[str, Any] = {}
    if path.exists():
        with path.open("rb") as file_handle:
            raw_config = tomllib.load(file_handle)
    return AppConfig.model_validate(raw_config)
