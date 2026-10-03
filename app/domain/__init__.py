"""
Domain layer — pure business logic with zero framework dependencies.

This package contains:
  - enums:      LoadStatus, AlertSeverity, FeasibilityResult, DispatchMode, ConcreteGrade
  - exceptions: Structured domain error hierarchy
  - values:     Immutable value objects (LoadTimer, ConcreteSpec, DeliveryWindow, Plant)
  - events:     Domain events for the Observer pipeline
"""
from app.domain.enums import (
    AlertSeverity,
    ConcreteGrade,
    DispatchMode,
    FeasibilityResult,
    LoadStatus,
    TripPhase,
)
from app.domain.exceptions import (
    DispatchError,
    DomainError,
    InfeasibleRouteError,
    LoadExpiredError,
    RetarderLimitError,
)
from app.domain.values import (
    ConcreteSpec,
    DeliveryWindow,
    LoadTimer,
    Plant,
    TravelEstimate,
)
from app.domain.events import (
    DeliveryInfeasible,
    DomainEvent,
    FeasibilityChecked,
    LoadBatched,
    LoadExpired,
    LoadStatusChanged,
    RerouteCritical,
    RetarderRecommended,
)

__all__ = [
    # Enums
    "AlertSeverity",
    "ConcreteGrade",
    "DispatchMode",
    "FeasibilityResult",
    "LoadStatus",
    "TripPhase",
    # Exceptions
    "DispatchError",
    "DomainError",
    "InfeasibleRouteError",
    "LoadExpiredError",
    "RetarderLimitError",
    # Value objects
    "ConcreteSpec",
    "DeliveryWindow",
    "LoadTimer",
    "Plant",
    "TravelEstimate",
    # Events
    "DeliveryInfeasible",
    "DomainEvent",
    "FeasibilityChecked",
    "LoadBatched",
    "LoadExpired",
    "LoadStatusChanged",
    "RerouteCritical",
    "RetarderRecommended",
]
