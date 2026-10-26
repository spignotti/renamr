"""Filename component sanitization shared by routing rules."""

from __future__ import annotations

import re

UNSAFE_CHARS_PATTERN = re.compile(r'[\\/:*?"<>|]')
WHITESPACE_PATTERN = re.compile(r"\s+")


def sanitize_component(value: str) -> str:
    """Sanitize one filename component for safe local filesystem use."""
    cleaned = UNSAFE_CHARS_PATTERN.sub("", value).replace("_", " ")
    cleaned = WHITESPACE_PATTERN.sub(" ", cleaned).strip(" .")
    return cleaned or "Unknown"
