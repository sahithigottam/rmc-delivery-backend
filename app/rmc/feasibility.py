"""
Feasibility pipeline — Chain of Responsibility pattern.

Pre-flight checks before dispatching a truck. Each check evaluates
one aspect of delivery feasibility and either returns a definitive
result or delegates to the next check in the chain.

Chain order: cheapest / most-common-failure checks run first.
  TimeCheck → GradeCheck → CapacityCheck → (extensible)

Open/Closed Principle: add new checks by creating a new subclass
of BaseFeasibilityCheck and inserting it into the chain — no
existing code needs to change.

Usage:
    pipeline = build_feasibility_pipeline()
    result = await pipeline.check(plant, spec, travel_estimate)

    # Or build a custom chain:
    pipeline = TimeCheck()
    pipeline.set_next(GradeCheck()).set_next(CapacityCheck())
    result = await pipeline.check(plant, spec, travel_estimate)
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from app.domain.enums import FeasibilityResult
from app.domain.values import ConcreteSpec, Plant, TravelEstimate

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Abstract base handler
# ─────────────────────────────────────────────────────────────────────────────

class BaseFeasibilityCheck:
    """
    Abstract base for feasibility checks (Chain of Responsibility).

    Subclasses override check() to implement their specific logic.
    Call _pass_to_next() at the end of a passing check to continue
    the chain. If a check fails, return the result directly — the
    chain short-circuits.

    Implements IFeasibilityCheck Protocol without importing it
    (structural typing — duck typing with type checking).
    """

    def __init__(self) -> None:
        self._next: Optional[BaseFeasibilityCheck] = None

    def set_next(self, check: BaseFeasibilityCheck) -> BaseFeasibilityCheck:
        """
        Set the next handler in the chain.

        Returns the NEXT handler (not self) to enable fluent chaining:
            a.set_next(b).set_next(c)  # chain: a → b → c
        """
        self._next = check
        return check

    async def check(
        self,
        plant: Plant,
        spec: ConcreteSpec,
        travel_estimate: TravelEstimate,
        context: Optional[dict[str, Any]] = None,
    ) -> FeasibilityResult:
        """Override in subclass. Call _pass_to_next() to continue chain."""
        return await self._pass_to_next(plant, spec, travel_estimate, context)

    async def _pass_to_next(
        self,
        plant: Plant,
        spec: ConcreteSpec,
        travel_estimate: TravelEstimate,
        context: Optional[dict[str, Any]] = None,
    ) -> FeasibilityResult:
        """Delegate to the next check, or return FEASIBLE if end of chain."""
        if self._next:
            return await self._next.check(plant, spec, travel_estimate, context)
        return FeasibilityResult.FEASIBLE


# ─────────────────────────────────────────────────────────────────────────────
# Concrete checks
# ─────────────────────────────────────────────────────────────────────────────

class TimeCheck(BaseFeasibilityCheck):
    """
    Can the delivery be completed within the concrete's usable life?

    Accounts for:
      - Travel time (from route calculation, includes traffic)
      - On-site buffer (15 min default — unloading, positioning, etc.)
      - Concrete grade-specific time limits (90 min standard, 75 min high-grade)
      - Retarder availability (can extend by ~45 min)

    This is the most common failure mode and the cheapest to evaluate,
    so it runs first in the chain.
    """

    ON_SITE_BUFFER_MINUTES: float = 15.0
    RETARDER_EXTENSION_MINUTES: float = 45.0
    MARGINAL_SLACK_MINUTES: float = 10.0

    async def check(
        self,
        plant: Plant,
        spec: ConcreteSpec,
        travel_estimate: TravelEstimate,
        context: Optional[dict[str, Any]] = None,
    ) -> FeasibilityResult:
        max_life = spec.recommended_max_life_minutes
        required = travel_estimate.total_minutes + self.ON_SITE_BUFFER_MINUTES

        logger.debug(
            f"TimeCheck [{plant.name}]: "
            f"{travel_estimate.total_minutes:.0f}min travel + "
            f"{self.ON_SITE_BUFFER_MINUTES}min buffer = {required:.0f}min "
            f"(limit: {max_life}min, grade: {spec.grade.value})"
        )

        if required > max_life:
            # Check if retarder would save it
            retarder_max = max_life + self.RETARDER_EXTENSION_MINUTES
            if required <= retarder_max:
                logger.info(
                    f"TimeCheck [{plant.name}]: REQUIRES_RETARDER — "
                    f"{required:.0f}min needed, {max_life}min limit, "
                    f"retarder extends to {retarder_max:.0f}min"
                )
                return FeasibilityResult.REQUIRES_RETARDER
            else:
                logger.warning(
                    f"TimeCheck [{plant.name}]: INFEASIBLE — "
                    f"{required:.0f}min exceeds even retarder-extended "
                    f"limit of {retarder_max:.0f}min"
                )
                return FeasibilityResult.INFEASIBLE

        # Marginal: within MARGINAL_SLACK_MINUTES of the limit
        slack = max_life - required
        if slack < self.MARGINAL_SLACK_MINUTES:
            logger.info(
                f"TimeCheck [{plant.name}]: MARGINAL — "
                f"only {slack:.0f}min slack"
            )
            return FeasibilityResult.MARGINAL

        # Passes — continue chain
        return await self._pass_to_next(plant, spec, travel_estimate, context)


class GradeCheck(BaseFeasibilityCheck):
    """
    Can this plant produce the requested concrete grade?

    Some plants lack the equipment or raw materials for high-strength
    mixes (≥40 MPa). If the plant's available_grades is empty, it's
    assumed to produce all grades.
    """

    async def check(
        self,
        plant: Plant,
        spec: ConcreteSpec,
        travel_estimate: TravelEstimate,
        context: Optional[dict[str, Any]] = None,
    ) -> FeasibilityResult:
        if not plant.can_produce(spec):
            logger.warning(
                f"GradeCheck [{plant.name}]: INFEASIBLE — "
                f"cannot produce {spec.grade.value}"
            )
            return FeasibilityResult.INFEASIBLE

        return await self._pass_to_next(plant, spec, travel_estimate, context)


class CapacityCheck(BaseFeasibilityCheck):
    """
    Does the plant have capacity to batch this order in reasonable time?

    Checks if the plant's production rate can batch the requested volume
    without causing unacceptable delay. A 6m³ load at 60m³/hour takes
    6 minutes — fine. But a 12m³ load at 20m³/hour takes 36 minutes,
    which eats into the delivery window.
    """

    MAX_BATCHING_MINUTES: float = 30.0

    async def check(
        self,
        plant: Plant,
        spec: ConcreteSpec,
        travel_estimate: TravelEstimate,
        context: Optional[dict[str, Any]] = None,
    ) -> FeasibilityResult:
        if not plant.is_operational:
            logger.warning(
                f"CapacityCheck [{plant.name}]: INFEASIBLE — plant is offline"
            )
            return FeasibilityResult.INFEASIBLE

        batching_minutes = plant.batching_time_minutes(spec.volume_m3)
        if batching_minutes > self.MAX_BATCHING_MINUTES:
            logger.info(
                f"CapacityCheck [{plant.name}]: MARGINAL — "
                f"{spec.volume_m3}m³ takes {batching_minutes:.0f}min to batch"
            )
            return FeasibilityResult.MARGINAL

        return await self._pass_to_next(plant, spec, travel_estimate, context)


# ─────────────────────────────────────────────────────────────────────────────
# Pipeline factory
# ─────────────────────────────────────────────────────────────────────────────

def build_feasibility_pipeline() -> BaseFeasibilityCheck:
    """
    Factory: construct the default feasibility pipeline.

    Chain: TimeCheck → GradeCheck → CapacityCheck

    Time is checked first because:
      1. It's the most common failure mode (>70% of infeasible dispatches)
      2. It's the cheapest to evaluate (no external calls)
      3. Early rejection saves unnecessary grade/capacity checks
    """
    time_check = TimeCheck()
    grade_check = GradeCheck()
    capacity_check = CapacityCheck()

    time_check.set_next(grade_check).set_next(capacity_check)

    return time_check
