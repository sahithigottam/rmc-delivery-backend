"""
Domain exceptions — structured error hierarchy for RMC business logic.

All domain exceptions inherit from DomainError, which carries:
  - message: human-readable description
  - code:    machine-readable error code (for API responses)
  - context: dict of structured data for logging / debugging

Exception hierarchy:
  DomainError
  ├── LoadExpiredError       — concrete exceeded usable life
  ├── InfeasibleRouteError   — route can't deliver within time limit
  ├── DispatchError          — no viable plant / truck found
  └── RetarderLimitError     — retarder can't extend life enough
"""
from __future__ import annotations

from typing import Optional


class DomainError(Exception):
    """Base exception for all domain errors."""

    def __init__(
        self,
        message: str,
        code: str = "DOMAIN_ERROR",
        context: Optional[dict] = None,
    ):
        self.message = message
        self.code = code
        self.context = context or {}
        super().__init__(message)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r}, message={self.message!r})"

    def to_dict(self) -> dict:
        """Serialize for API error responses."""
        return {
            "error": self.code,
            "message": self.message,
            "context": self.context,
        }


class LoadExpiredError(DomainError):
    """Concrete load has exceeded its usable life — cannot be poured."""

    def __init__(self, trip_id: int, minutes_over: float, batch_time: str):
        super().__init__(
            message=(
                f"Load expired for trip {trip_id}: "
                f"{minutes_over:.0f} min past limit (batched at {batch_time})"
            ),
            code="LOAD_EXPIRED",
            context={
                "trip_id": trip_id,
                "minutes_over": round(minutes_over, 1),
                "batch_time": batch_time,
            },
        )


class InfeasibleRouteError(DomainError):
    """Route cannot be completed within concrete's usable life."""

    def __init__(self, travel_minutes: float, max_minutes: float, reason: str):
        super().__init__(
            message=(
                f"Route infeasible: {travel_minutes:.0f}min travel exceeds "
                f"{max_minutes:.0f}min limit — {reason}"
            ),
            code="INFEASIBLE_ROUTE",
            context={
                "travel_minutes": round(travel_minutes, 1),
                "max_minutes": round(max_minutes, 1),
                "reason": reason,
            },
        )


class DispatchError(DomainError):
    """No viable dispatch option found."""

    def __init__(self, reason: str, alternatives_checked: int = 0):
        super().__init__(
            message=(
                f"Dispatch failed: {reason} "
                f"({alternatives_checked} alternative(s) checked)"
            ),
            code="DISPATCH_FAILED",
            context={
                "reason": reason,
                "alternatives_checked": alternatives_checked,
            },
        )


class RetarderLimitError(DomainError):
    """Retarder cannot extend load life enough to cover remaining travel."""

    def __init__(self, remaining_minutes: float, required_minutes: float):
        super().__init__(
            message=(
                f"Retarder insufficient: {remaining_minutes:.0f}min remaining, "
                f"{required_minutes:.0f}min needed"
            ),
            code="RETARDER_LIMIT",
            context={
                "remaining_minutes": round(remaining_minutes, 1),
                "required_minutes": round(required_minutes, 1),
            },
        )
