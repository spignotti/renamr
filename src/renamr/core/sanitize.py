"""Sanitizers for untrusted model/document text shown in a terminal or export.

Document and model strings are data. They are stripped of terminal control
sequences and rich-markup tags before display, and aliases are restricted to a
small safe alphabet so no path, control character, or markup can leak through a
name.
"""

from __future__ import annotations

import re
import unicodedata

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_MARKUP = re.compile(
    r"\[/?(?:bold|italic|underline|link|red|green|yellow|blue|magenta|cyan|white)[^\]]*\]"
)
_ALIAS = re.compile(r"[^A-Za-z0-9._-]+")
_SAFE_LABEL = re.compile(r"^\w[\w .,&'()/+_-]{0,79}$", re.UNICODE)
_SAFE_ALIAS = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")
MAX_DISPLAY_CHARS = 600


def clean_text(value: str, *, limit: int = MAX_DISPLAY_CHARS) -> str:
    """Return untrusted text without control sequences or markup."""
    stripped = _ANSI.sub("", value)
    stripped = _CONTROL.sub("", stripped)
    stripped = _MARKUP.sub("", stripped)
    return stripped[:limit].strip()


def safe_alias(value: str, *, fallback: str = "contract") -> str:
    """Return a bounded alias made only of letters, digits, dot, dash, underscore."""
    cleaned = _ALIAS.sub("-", value.strip()).strip("-._")
    return (cleaned or fallback)[:40]


def is_safe_label(value: str) -> bool:
    """Return whether a locally confirmed label is safe to export."""
    return bool(_SAFE_LABEL.match(value.strip())) and not any(
        unicodedata.category(char).startswith("C") for char in value
    )


def is_safe_alias(value: str) -> bool:
    """Return whether a locally chosen alias stays in the ASCII alias alphabet."""
    return bool(_SAFE_ALIAS.match(value.strip()))
