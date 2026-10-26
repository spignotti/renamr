"""Immutable opaque identifiers.

Identifiers carry no meaning beyond their prefix and a random suffix, so they
never leak paths, hashes, or content. Content identity (a hash) is stored
separately from location identity (an opaque location id).
"""

from __future__ import annotations

import secrets

_RANDOM_BYTES = 12

DOCUMENT_PREFIX = "doc"
REVISION_PREFIX = "rev"
LOCATION_PREFIX = "loc"
FACT_PREFIX = "fact"
RULE_PREFIX = "rule"
RUN_PREFIX = "run"
ITEM_PREFIX = "item"
REVIEW_PREFIX = "review"
EXAMPLE_PREFIX = "ex"
CONTRACT_PREFIX = "ctr"


def new_id(prefix: str) -> str:
    """Return a new opaque identifier with the given prefix.

    Args:
        prefix: Short lowercase tag such as ``doc`` or ``loc``.

    Returns:
        An identifier of the form ``<prefix>_<24 hex characters>``.
    """
    return f"{prefix}_{secrets.token_hex(_RANDOM_BYTES)}"
