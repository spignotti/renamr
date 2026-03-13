"""LLM-backed metadata extraction for file renaming."""

from __future__ import annotations

import json
import re
import time
from datetime import date, datetime
from typing import Any, Literal, cast

import structlog
from litellm import completion
from pydantic import BaseModel

from renamr.models import (
    DEFAULT_RENAME_PROMPT as MODELS_DEFAULT_RENAME_PROMPT,
)
from renamr.models import (
    AppConfig,
)

logger = structlog.get_logger(__name__)
DEFAULT_RENAME_PROMPT = MODELS_DEFAULT_RENAME_PROMPT


class FileMetadata(BaseModel):
    """Metadata used to build the renamed filename."""

    sender: str
    subject: str
    document_date: date | None
    filename_format: Literal["date_sender_subject", "date_subject"]


def extract_metadata(
    filename: str,
    created_at: datetime,
    preview_text: str,
    image_base64: str | None,
    config: AppConfig,
) -> FileMetadata:
    """Extract rename metadata from preview text and optional document image."""
    if not preview_text.strip() and image_base64 is None:
        return FileMetadata(
            sender="Unknown",
            subject="Document",
            document_date=None,
            filename_format="date_subject",
        )
    prompt = _build_user_prompt(filename, created_at, preview_text)
    system_content = (
        f"Language for all extracted metadata values: {config.language}\n\n"
        f"{config.rename_prompt}"
    )
    messages = [
        {"role": "system", "content": system_content},
        {"role": "user", "content": _build_user_content(prompt, image_base64)},
    ]
    for attempt in range(config.llm.max_retries + 1):
        try:
            response = completion(
                model=config.llm.model,
                messages=messages,
                response_format={"type": "json_object"},
                temperature=config.llm.temperature,
                api_base=config.llm.api_base,
                timeout=config.llm.timeout,
            )
            content = cast(Any, response).choices[0].message.content
            if not content:
                raise ValueError("Empty response content from LiteLLM.")
            return _parse_metadata(content)
        except Exception as exc:
            if attempt >= config.llm.max_retries:
                logger.exception("metadata_extraction_failed", error=str(exc), filename=filename)
                raise
            backoff = min(2**attempt, 30)
            logger.warning(
                "metadata_extraction_retry",
                attempt=attempt + 1,
                backoff=backoff,
                error=str(exc),
                filename=filename,
            )
            time.sleep(backoff)
    raise AssertionError("unreachable")


def _build_user_prompt(filename: str, created_at: datetime, preview_text: str) -> str:
    """Build the text prompt sent alongside preview content."""
    return (
        f"Filename: {filename}\n"
        f"Created: {created_at.isoformat()}\n"
        "Content preview:\n"
        f"{preview_text or '[none]'}\n\n"
        "Extract sender, subject, date, and filename format for renaming this file."
    )


def _build_user_content(prompt: str, image_base64: str | None) -> str | list[dict[str, object]]:
    """Return either plain text content or multimodal content."""
    if image_base64 is None:
        return prompt
    return [
        {"type": "text", "text": prompt},
        {
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"},
        },
    ]


def _parse_metadata(raw_json: str) -> FileMetadata:
    """Parse raw JSON into FileMetadata with defensive fallbacks."""
    data = json.loads(raw_json)
    sender = str(data.get("sender", "")).strip() or "Unknown"
    subject = str(data.get("subject", "")).strip() or "Document"
    document_date = _parse_date_string(str(data.get("date", "")).strip())
    raw_filename_format = str(data.get("filename_format", "")).strip().lower()
    filename_format: Literal["date_sender_subject", "date_subject"]
    if raw_filename_format == "date_subject":
        filename_format = "date_subject"
    else:
        filename_format = "date_sender_subject"
    if sender == "Unknown" and filename_format == "date_sender_subject":
        filename_format = "date_subject"
    return FileMetadata(
        sender=sender,
        subject=subject,
        document_date=document_date,
        filename_format=filename_format,
    )


def _parse_date_string(value: str) -> date | None:
    """Parse common date formats returned by the model."""
    if not value:
        return None
    normalized = value.strip().lower()
    if normalized in {"none", "null", "n/a", "na"}:
        return None
    for pattern, fmt in _DATE_MATCHERS:
        match = re.search(pattern, value)
        if not match:
            continue
        if fmt is not None:
            try:
                return datetime.strptime(match.group(0), fmt).date()
            except ValueError:
                continue
        parsed = _parse_ambiguous_date(match.group(1), match.group(2), match.group(3))
        if parsed is not None:
            return parsed
    ascii_value = _normalize_umlauts(normalized)
    month_match = re.search(r"(\d{1,2})\.\s*([a-z]+)\s*(\d{4})", ascii_value)
    if month_match is None:
        return None
    day = int(month_match.group(1))
    month = _MONTH_MAP.get(month_match.group(2))
    if month is None:
        return None
    try:
        return date(int(month_match.group(3)), month, day)
    except ValueError:
        return None


def _parse_ambiguous_date(first: str, second: str, year_value: str) -> date | None:
    """Parse day/month dates where the separator format does not disambiguate order."""
    year = int(year_value)
    if year < 100:
        year += 2000 if year <= 69 else 1900
    day_or_month = int(first)
    month_or_day = int(second)
    if day_or_month > 12:
        day, month = day_or_month, month_or_day
    elif month_or_day > 12:
        month, day = day_or_month, month_or_day
    else:
        day, month = day_or_month, month_or_day
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _normalize_umlauts(value: str) -> str:
    """Normalize German umlauts so ASCII month matching works."""
    return (
        value.replace("ä", "ae")
        .replace("ö", "oe")
        .replace("ü", "ue")
        .replace("ß", "ss")
    )


_DATE_MATCHERS = [
    (r"(\d{4})-(\d{2})-(\d{2})", "%Y-%m-%d"),
    (r"(\d{4})/(\d{2})/(\d{2})", "%Y/%m/%d"),
    (r"(\d{4})\.(\d{2})\.(\d{2})", "%Y.%m.%d"),
    (r"(\d{2})\.(\d{2})\.(\d{4})", "%d.%m.%Y"),
    (r"(\d{1,2})\.(\d{1,2})\.(\d{2,4})", None),
    (r"(\d{2})/(\d{2})/(\d{4})", None),
    (r"(\d{2})-(\d{2})-(\d{4})", None),
    (r"(\d{8})", "%Y%m%d"),
]

_MONTH_MAP = {
    "januar": 1,
    "january": 1,
    "jan": 1,
    "februar": 2,
    "february": 2,
    "feb": 2,
    "maerz": 3,
    "marz": 3,
    "march": 3,
    "mrz": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "mai": 5,
    "may": 5,
    "juni": 6,
    "june": 6,
    "jun": 6,
    "juli": 7,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sep": 9,
    "sept": 9,
    "oktober": 10,
    "october": 10,
    "okt": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "december": 12,
    "dezember": 12,
    "dec": 12,
    "dez": 12,
}
