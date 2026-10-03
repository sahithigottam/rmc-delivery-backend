"""
Domain enumerations — single source of truth for all domain constants.

These enums are used across the entire domain layer and beyond.
They carry no behaviour, only names and values.
"""
from enum import Enum


class LoadStatus(str, Enum):
    """
    Concrete load lifecycle status.

    Timeline (standard 90-minute load, configurable per grade):
      FRESH:    0  → 60 min   — load is fine, no action needed
      WARNING:  60 → 75 min   — approaching limit, consider retarder
      CRITICAL: 75 → 90 min   — must pour NOW or waste load
      EXPIRED:  90+ min       — load is unusable, reject at site

    Thresholds shift for high-grade concrete (tighter) or when
    retarder is applied (extended).
    """

    FRESH = "fresh"
    WARNING = "warning"
    CRITICAL = "critical"
    EXPIRED = "expired"


class AlertSeverity(str, Enum):
    """Alert severity levels for the Observer pipeline."""

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"
    EMERGENCY = "emergency"


class FeasibilityResult(str, Enum):
    """
    Result of a pre-dispatch feasibility check.

    Each check in the Chain of Responsibility pipeline returns one of these.
    REQUIRES_RETARDER means the route is feasible only if retarder is added.
    """

    FEASIBLE = "feasible"
    MARGINAL = "marginal"
    INFEASIBLE = "infeasible"
    REQUIRES_RETARDER = "requires_retarder"


class DispatchMode(str, Enum):
    """Dispatch strategy selection — how to pick the plant + truck."""

    NEAREST_PLANT = "nearest_plant"
    FASTEST_ROUTE = "fastest_route"
    LEAST_COST = "least_cost"


class ConcreteGrade(str, Enum):
    """
    Common NZ concrete grades (MPa compressive strength).

    NZS 3104 / NZS 3109 standard grades used in the NZ market.
    Higher grades have tighter delivery time limits because they
    hydrate faster and are less tolerant of delays.
    """

    MPA_17_5 = "17.5MPa"  # Residential foundations, non-structural
    MPA_20 = "20MPa"      # General purpose
    MPA_25 = "25MPa"      # Driveways, footpaths, light structural
    MPA_30 = "30MPa"      # Standard structural
    MPA_35 = "35MPa"      # High-strength structural
    MPA_40 = "40MPa"      # Heavy commercial, precast
    MPA_45 = "45MPa"      # High-performance structural
    MPA_50 = "50MPa"      # Specialist / post-tensioned


class TripPhase(str, Enum):
    """
    Distinct phases of an RMC delivery trip.

    A trip moves through these phases sequentially. Each phase has
    different monitoring and alerting requirements.
    """

    BATCHING = "batching"      # At plant, being loaded
    EN_ROUTE = "en_route"      # Travelling to pour site
    ON_SITE = "on_site"        # At site, waiting to pour
    POURING = "pouring"        # Actively discharging concrete
    WASHOUT = "washout"        # Post-pour drum washout
    RETURNING = "returning"    # Heading back to plant
