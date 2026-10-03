"""
RMC module — Ready-Mixed Concrete business logic.

Architecture:
  - interfaces.py    Protocol classes (DIP — Dependency Inversion Principle)
  - load_manager.py  Load lifecycle: timer, expiry, retarder (SRP)
  - feasibility.py   Chain of Responsibility: pre-dispatch checks (OCP)
  - alerts.py        Observer pattern: event distribution (OCP)
  - dispatch.py      Strategy pattern: plant/truck selection (OCP + SRP)

All implementations depend on interfaces, never on each other (DIP).
"""
from app.rmc.load_manager import LoadManager
from app.rmc.feasibility import (
    BaseFeasibilityCheck,
    CapacityCheck,
    GradeCheck,
    TimeCheck,
    build_feasibility_pipeline,
)
from app.rmc.alerts import AlertService, LoggingAlertHandler, SSEAlertHandler
from app.rmc.dispatch import (
    DispatchEngine,
    FastestRouteStrategy,
    NearestPlantStrategy,
)

__all__ = [
    # Core services
    "LoadManager",
    "AlertService",
    "DispatchEngine",
    # Feasibility pipeline
    "BaseFeasibilityCheck",
    "TimeCheck",
    "GradeCheck",
    "CapacityCheck",
    "build_feasibility_pipeline",
    # Alert handlers
    "LoggingAlertHandler",
    "SSEAlertHandler",
    # Dispatch strategies
    "NearestPlantStrategy",
    "FastestRouteStrategy",
]
