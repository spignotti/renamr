"""German and ISO date parsing plus the supported deterministic date arithmetic.

These helpers parse explicit document dates only. They never infer a date from a
filename or filesystem timestamp, and the arithmetic is deliberately limited to
calendar month addition and day subtraction so no legal interpretation is made.
"""

from __future__ import annotations

import calendar
import re
from datetime import date, timedelta

_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_GERMAN = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b")
_MONTHS = {
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
    "dezember": 12,
    "december": 12,
    "dec": 12,
    "dez": 12,
}
_NAMED = re.compile(r"\b(\d{1,2})\.?\s+([A-Za-zäöü]+)\s+(\d{4})\b")


def parse_document_date(value: str) -> date | None:
    """Parse one ISO, German numeric, or German/English named date."""
    text = value.strip()
    for pattern, order in ((_ISO, "ymd"), (_GERMAN, "dmy")):
        match = pattern.search(text)
        if match is None:
            continue
        parts = [int(item) for item in match.groups()]
        year, month, day = (
            (parts[0], parts[1], parts[2]) if order == "ymd" else (parts[2], parts[1], parts[0])
        )
        try:
            return date(year, month, day)
        except ValueError:
            return None
    named = _NAMED.search(_ascii_fold(text))
    if named is None:
        return None
    month = _MONTHS.get(named.group(2).lower())
    if month is None:
        return None
    try:
        return date(int(named.group(3)), month, int(named.group(1)))
    except ValueError:
        return None


def find_document_date(text: str) -> tuple[date, str] | None:
    """Return the first parseable date and the line that contains it."""
    for line in text.splitlines():
        parsed = parse_document_date(line)
        if parsed is not None:
            return parsed, line.strip()
    return None


def iso(value: date) -> str:
    """Return an ISO calendar date string."""
    return value.isoformat()


def add_months(start: date, months: int) -> date:
    """Return ``start`` shifted by whole calendar months, clamped to month end."""
    if months < 0:
        raise ValueError("months must not be negative")
    total = start.month - 1 + months
    year = start.year + total // 12
    month = total % 12 + 1
    day = min(start.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def days_before(end: date, days: int) -> date:
    """Return ``end`` minus a non-negative number of days."""
    if days < 0:
        raise ValueError("days must not be negative")
    return end - timedelta(days=days)


def _ascii_fold(value: str) -> str:
    """Normalize German umlauts so month names match ASCII tokens."""
    return value.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
