"""Operation journal states and recovery fault-injection types.

File and database changes cannot share one atomic transaction, so each item
moves through explicit, persisted states. Recovery reconciles the journal before
any new writer runs. Tests inject a fault at each transition to prove bytes stay
recoverable.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from typing import Literal

STAGE_PREFIX = ".renamr-stage-"


class ItemState(StrEnum):
    """Journal state of one planned file operation."""

    PLANNED = "planned"
    STAGED = "staged"
    PUBLISHED = "published"
    SOURCE_RETIRED = "source_retired"
    COMMITTED = "committed"
    NOOP = "noop"
    BLOCKED = "blocked"
    FAILED = "failed"


TERMINAL_STATES = frozenset(
    {ItemState.COMMITTED, ItemState.NOOP, ItemState.BLOCKED, ItemState.FAILED}
)

FaultPoint = Literal["after_stage", "after_publish", "after_retire"]
FaultInjector = Callable[[str], None]
