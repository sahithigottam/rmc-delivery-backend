"""
Dispatch Engine — Strategy pattern for plant/truck selection.

The engine coordinates the dispatch workflow:
  1. Get travel estimates for each candidate plant
  2. Run the feasibility pipeline (Chain of Responsibility)
  3. Filter to viable plants
  4. Apply the selected strategy to pick the best option

Open/Closed Principle:
  - New strategies are added by implementing IDispatchStrategy
  - The engine itself never changes when strategies are added
  - Feasibility pipeline is injected, not hardcoded

Dependency Inversion:
  - Engine depends on IRouteCalculator (Protocol), not GoogleMapsService
  - Strategies depend on abstract Plant/ConcreteSpec, not DB models

Usage:
    engine = DispatchEngine(
        route_calculator=GoogleRouteCalculator(google_maps),
        feasibility_pipeline=build_feasibility_pipeline(),
        alert_service=alert_service,
    )

    plan = await engine.dispatch(
        plants=[plant_a, plant_b],
        spec=ConcreteSpec(mix_code="30MPa/20/120", volume_m3=6.0),
        destination_lat=-36.85,
        destination_lng=174.76,
        strategy=NearestPlantStrategy(),
    )
"""
import logging
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from app.domain.enums import FeasibilityResult
from app.domain.events import FeasibilityChecked
from app.domain.exceptions import DispatchError
from app.domain.values import ConcreteSpec, Plant, TravelEstimate
from app.rmc.alerts import AlertService
from app.rmc.feasibility import BaseFeasibilityCheck, build_feasibility_pipeline
from app.rmc.interfaces import DispatchPlan

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Dispatch Engine
# ─────────────────────────────────────────────────────────────────────────────

class DispatchEngine:
    """
    Coordinates dispatch decisions using pluggable strategies.

    Single Responsibility: orchestrates the dispatch pipeline.
    Does NOT own the strategy logic — that's delegated to the
    injected IDispatchStrategy implementation.

    Does NOT own feasibility logic — that's delegated to the
    injected feasibility pipeline.
    """

    def __init__(
        self,
        route_calculator: object,
        feasibility_pipeline: Optional[BaseFeasibilityCheck] = None,
        alert_service: Optional[AlertService] = None,
    ) -> None:
        self._route_calc = route_calculator
        self._feasibility = feasibility_pipeline or build_feasibility_pipeline()
        self._alerts = alert_service

    async def dispatch(
        self,
        plants: List[Plant],
        spec: ConcreteSpec,
        destination_lat: float,
        destination_lng: float,
        strategy: object,
        requested_time: Optional[datetime] = None,
        trip_id: int = 0,
    ) -> DispatchPlan:
        """
        Run the full dispatch pipeline.

        Steps:
          1. Filter out offline plants
          2. Get travel estimates for each plant → destination
          3. Run feasibility pipeline for each (plant, estimate) pair
          4. Collect viable options
          5. Apply strategy to select the best viable option
          6. Enrich the plan with feasibility details

        Raises DispatchError if no viable plant is found.
        """
        if not plants:
            raise DispatchError("No plants provided", alternatives_checked=0)

        viable: List[Tuple[Plant, TravelEstimate, FeasibilityResult]] = []

        for plant in plants:
            if not plant.is_operational:
                logger.debug(f"Dispatch: skipping offline plant {plant.name}")
                continue

            # ── Step 1: Get travel estimate ──
            try:
                estimate = await self._route_calc.estimate_travel(
                    origin_lat=plant.lat,
                    origin_lng=plant.lng,
                    dest_lat=destination_lat,
                    dest_lng=destination_lng,
                )
            except Exception as e:
                logger.warning(
                    f"Dispatch: could not estimate route from {plant.name}: {e}"
                )
                continue

            # ── Step 2: Run feasibility pipeline ──
            result = await self._feasibility.check(plant, spec, estimate)

            # ── Step 3: Publish feasibility event ──
            if self._alerts:
                await self._alerts.publish(FeasibilityChecked(
                    trip_id=trip_id,
                    result=result,
                    travel_minutes=estimate.total_minutes,
                    max_allowed_minutes=spec.recommended_max_life_minutes,
                    plant_id=plant.plant_id,
                    reason=f"Plant {plant.name}: {result.value}",
                ))

            # ── Step 4: Collect viable plants ──
            if result != FeasibilityResult.INFEASIBLE:
                viable.append((plant, estimate, result))

        if not viable:
            raise DispatchError(
                reason="No plants can deliver within concrete life limit",
                alternatives_checked=len(plants),
            )

        # ── Step 5: Apply strategy ──
        plan = await strategy.select(
            plants=[v[0] for v in viable],
            spec=spec,
            destination_lat=destination_lat,
            destination_lng=destination_lng,
            requested_time=requested_time,
        )

        # ── Step 6: Enrich plan with feasibility data ──
        for plant, estimate, result in viable:
            if plant.plant_id == plan.plant.plant_id:
                plan.feasibility = result
                plan.estimated_travel = estimate
                if result == FeasibilityResult.REQUIRES_RETARDER:
                    plan.requires_retarder = True
                    plan.notes.append(
                        "Retarder required — travel time exceeds standard limit"
                    )
                elif result == FeasibilityResult.MARGINAL:
                    plan.notes.append(
                        "Marginal time buffer — monitor closely during delivery"
                    )
                break

        logger.info(
            f"Dispatch: selected {plan.plant.name}, "
            f"travel={plan.estimated_travel.total_minutes:.0f}min, "
            f"feasibility={plan.feasibility.value}"
        )
        return plan


# ─────────────────────────────────────────────────────────────────────────────
# Concrete Strategies
# ─────────────────────────────────────────────────────────────────────────────

class NearestPlantStrategy:
    """
    Select the plant closest to the destination (straight-line distance).

    Fastest to evaluate — no external API calls. Good default when
    traffic data is unavailable or for initial estimates.
    """

    async def select(
        self,
        plants: List[Plant],
        spec: ConcreteSpec,
        destination_lat: float,
        destination_lng: float,
        requested_time: Optional[datetime] = None,
    ) -> DispatchPlan:
        if not plants:
            raise DispatchError("No viable plants", alternatives_checked=0)

        # Sort by straight-line distance to destination
        ranked = sorted(
            plants,
            key=lambda p: p.distance_to(destination_lat, destination_lng),
        )
        selected = ranked[0]
        batch_time = requested_time or datetime.now(timezone.utc)

        return DispatchPlan(
            plant=selected,
            # Placeholder — engine will overwrite with actual estimate
            estimated_travel=TravelEstimate(
                distance_meters=0, duration_seconds=0
            ),
            recommended_batch_time=batch_time,
            notes=[
                f"Nearest plant: {selected.name} "
                f"({selected.distance_to(destination_lat, destination_lng):.1f}km away)"
            ],
        )


class FastestRouteStrategy:
    """
    Select the plant with the shortest travel time (accounts for traffic).

    Requires a route calculator to get actual travel times. More
    accurate than NearestPlant but makes N external API calls
    (one per viable plant).
    """

    def __init__(self, route_calculator: object) -> None:
        self._route_calc = route_calculator

    async def select(
        self,
        plants: List[Plant],
        spec: ConcreteSpec,
        destination_lat: float,
        destination_lng: float,
        requested_time: Optional[datetime] = None,
    ) -> DispatchPlan:
        if not plants:
            raise DispatchError("No viable plants", alternatives_checked=0)

        # Get travel estimates for all plants
        estimates: List[Tuple[Plant, TravelEstimate]] = []
        for plant in plants:
            try:
                est = await self._route_calc.estimate_travel(
                    origin_lat=plant.lat,
                    origin_lng=plant.lng,
                    dest_lat=destination_lat,
                    dest_lng=destination_lng,
                )
                estimates.append((plant, est))
            except Exception as e:
                logger.warning(f"FastestRoute: skip {plant.name} — {e}")

        if not estimates:
            raise DispatchError(
                "Could not get travel estimates for any plant",
                alternatives_checked=len(plants),
            )

        # Sort by total travel time (including traffic delay)
        estimates.sort(key=lambda x: x[1].total_seconds)
        selected_plant, selected_estimate = estimates[0]
        batch_time = requested_time or datetime.now(timezone.utc)

        return DispatchPlan(
            plant=selected_plant,
            estimated_travel=selected_estimate,
            recommended_batch_time=batch_time,
            notes=[
                f"Fastest route: {selected_plant.name} "
                f"({selected_estimate.total_minutes:.0f}min travel)"
            ],
        )
