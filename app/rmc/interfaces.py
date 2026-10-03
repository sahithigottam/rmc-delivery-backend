"""
RMC interfaces — Protocol classes for Dependency Inversion Principle (DIP).

All concrete implementations depend on these abstractions, never on
each other directly. This enables:
  - Easy mocking in tests (just implement the Protocol)
  - Swapping implementations without touching consumers
  - Clear contractual boundaries between modules
  - Static type checking via runtime_checkable

Interface Segregation (ISP):
  Each Protocol defines the minimal surface area its consumers need.
  ILoadTracker doesn't know about dispatching; IDispatchStrategy
  doesn't know about load timers.

Protocols defined:
  ILoadTracker       — load lifecycle (timer, status, retarder)
  IFeasibilityCheck  — single check in the feasibility pipeline
  IAlertHandler      — observer that receives domain events
  IDispatchStrategy  — plant/truck selection algorithm
  IRouteCalculator   — route time/distance calculation

Result types:
  DispatchPlan       — output of the dispatch strategy
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, List, Optional, Protocol, runtime_checkable

from app.domain.enums import FeasibilityResult, LoadStatus
from app.domain.events import DomainEvent
from app.domain.values import ConcreteSpec, LoadTimer, Plant, TravelEstimate


# ─────────────────────────────────────────────────────────────────────────────
# ILoadTracker — load lifecycle management
# ─────────────────────────────────────────────────────────────────────────────

@runtime_checkable
class ILoadTracker(Protocol):
    """
    Tracks concrete load lifecycle — timer, status, expiry.

    Consumers:
      - TripService (start_trip, check_and_reroute)
      - Traffic monitor (each check cycle)
    """

    def start_timer(
        self,
        batch_time: datetime,
        spec: ConcreteSpec,
    ) -> LoadTimer:
        """Create a load timer for a newly batched load."""
        ...

    def check_status(
        self,
        timer: LoadTimer,
        current_eta_minutes: Optional[float] = None,
    ) -> LoadStatus:
        """
        Evaluate current load status.

        If current_eta_minutes is provided, projects forward to check
        whether the load will survive until arrival.
        """
        ...

    def should_add_retarder(
        self,
        timer: LoadTimer,
        eta_minutes: float,
    ) -> bool:
        """Determine if retarder should be recommended."""
        ...


# ─────────────────────────────────────────────────────────────────────────────
# IFeasibilityCheck — Chain of Responsibility
# ─────────────────────────────────────────────────────────────────────────────

@runtime_checkable
class IFeasibilityCheck(Protocol):
    """
    Single check in the feasibility pipeline (Chain of Responsibility).

    Each check evaluates one aspect of delivery feasibility and either:
      - Returns a definitive result (FEASIBLE / INFEASIBLE / etc.)
      - Delegates to the next check in the chain

    Chain order matters: cheapest / most-common-failure checks go first.
    """

    def set_next(self, check: IFeasibilityCheck) -> IFeasibilityCheck:
        """Set the next check in the chain. Returns next for fluent chaining."""
        ...

    async def check(
        self,
        plant: Plant,
        spec: ConcreteSpec,
        travel_estimate: TravelEstimate,
        context: Optional[dict[str, Any]] = None,
    ) -> FeasibilityResult:
        """Evaluate feasibility. Return result or delegate to next."""
        ...


# ─────────────────────────────────────────────────────────────────────────────
# IAlertHandler — Observer pattern
# ─────────────────────────────────────────────────────────────────────────────

@runtime_checkable
class IAlertHandler(Protocol):
    """
    Observer that receives domain events.

    Implementations might:
      - Push SSE events to connected clients
      - Write to structured logs
      - Send SMS / email notifications (future)
      - Update real-time dashboards (future)
      - Trigger Slack / Teams webhooks (future)

    Handlers MUST NOT raise exceptions — the alert service catches
    and logs any handler failures to prevent cascading failures.
    """

    async def handle(self, event: DomainEvent) -> None:
        """Process a domain event. Must not raise."""
        ...

    def can_handle(self, event: DomainEvent) -> bool:
        """Check if this handler is interested in this event type."""
        ...


# ─────────────────────────────────────────────────────────────────────────────
# IDispatchStrategy — Strategy pattern
# ─────────────────────────────────────────────────────────────────────────────

@runtime_checkable
class IDispatchStrategy(Protocol):
    """
    Strategy for selecting the optimal plant + truck + batch time.

    Implementations:
      - NearestPlant:  minimize straight-line distance
      - FastestRoute:  minimize travel time (considers live traffic)
      - LeastCost:     minimize cost (fuel, overtime, toll — future)

    Each strategy receives only viable plants (pre-filtered by the
    DispatchEngine through the feasibility pipeline).
    """

    async def select(
        self,
        plants: List[Plant],
        spec: ConcreteSpec,
        destination_lat: float,
        destination_lng: float,
        requested_time: Optional[datetime] = None,
    ) -> DispatchPlan:
        """Select the best dispatch option. Raises DispatchError if none viable."""
        ...


# ─────────────────────────────────────────────────────────────────────────────
# IRouteCalculator — abstraction over Google Maps / etc.
# ─────────────────────────────────────────────────────────────────────────────

@runtime_checkable
class IRouteCalculator(Protocol):
    """
    Abstraction over the route calculation engine.

    Decouples dispatch logic from Google Maps specifics, enabling:
      - Testing with mock calculators
      - Swapping to alternative providers (OSRM, Mapbox, etc.)
    """

    async def estimate_travel(
        self,
        origin_lat: float,
        origin_lng: float,
        dest_lat: float,
        dest_lng: float,
        priority: str = "normal",
    ) -> TravelEstimate:
        """Get travel time/distance estimate between two points."""
        ...


# ─────────────────────────────────────────────────────────────────────────────
# Result Types
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class DispatchPlan:
    """
    Result of the dispatch strategy — the selected option.

    Contains everything needed to execute the dispatch:
      - Which plant to batch at
      - Travel estimate to the pour site
      - When to start batching
      - Whether retarder is needed
      - Feasibility assessment
      - Human-readable notes
    """

    plant: Plant
    estimated_travel: TravelEstimate
    recommended_batch_time: datetime
    requires_retarder: bool = False
    feasibility: FeasibilityResult = FeasibilityResult.FEASIBLE
    notes: List[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        """Human-readable dispatch summary."""
        return (
            f"Plant: {self.plant.name} | "
            f"Travel: {self.estimated_travel.total_minutes:.0f}min | "
            f"Batch at: {self.recommended_batch_time.strftime('%H:%M')} | "
            f"Feasibility: {self.feasibility.value}"
        )
