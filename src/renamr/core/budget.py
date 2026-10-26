"""Bounded scan budgets shared by scheduled and manual scans."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from renamr.core.reconcile import ScanOutcome


class ScanBudget(BaseModel):
    """Bounds for one scheduled reconciliation tick."""

    model_config = ConfigDict(extra="forbid")

    max_documents: int | None = Field(default=4, ge=1)
    max_model_calls: int | None = Field(default=4, ge=0)
    max_pages: int | None = Field(default=50, ge=1)
    max_seconds: float | None = Field(default=90.0, gt=0)


class BudgetTracker:
    """Mutable counters for one bounded scan."""

    def __init__(self, budget: ScanBudget, *, allow_models: bool = True) -> None:
        self.budget = budget
        self.allow_models = allow_models
        self.documents = 0
        self.model_calls = 0
        self.pages = 0
        self.started = time.monotonic()

    def exhausted(self) -> bool:
        """Return whether another document must not start."""
        budget = self.budget
        if budget.max_documents is not None and self.documents >= budget.max_documents:
            return True
        if (
            self.allow_models
            and budget.max_model_calls is not None
            and self.model_calls >= budget.max_model_calls
        ):
            return True
        if budget.max_pages is not None and self.pages >= budget.max_pages:
            return True
        return (
            budget.max_seconds is not None and time.monotonic() - self.started >= budget.max_seconds
        )

    def page_room(self) -> int | None:
        """Return remaining page budget, or None when unlimited."""
        if self.budget.max_pages is None:
            return None
        return max(0, self.budget.max_pages - self.pages)

    def model_room(self) -> int | None:
        """Return remaining model-call budget, or None when unlimited."""
        if self.budget.max_model_calls is None:
            return None
        return max(0, self.budget.max_model_calls - self.model_calls)

    def deadline(self) -> float | None:
        """Return the monotonic deadline for this scan, or None when unlimited."""
        if self.budget.max_seconds is None:
            return None
        return self.started + self.budget.max_seconds

    def record(self, outcome: ScanOutcome, before: tuple[int, int]) -> None:
        """Record one processed document's page/model cost."""
        self.documents += 1
        self.pages += outcome.pages_extracted - before[0]
        self.model_calls += outcome.model_calls - before[1]
