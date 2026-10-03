"""
Alert Service — Observer pattern for domain event distribution.

Central event bus that routes domain events to registered handlers.
Handlers are decoupled from event producers — new handlers can be
added without modifying the code that generates events.

Open/Closed Principle:
  - AlertService never changes when new event types are added
  - New handlers are registered, not hardcoded
  - Each handler decides which events it cares about via can_handle()

Fault tolerance:
  - Handler failures are caught and logged — one broken handler
    doesn't crash the pipeline or block other handlers
  - publish_all() processes events sequentially to preserve ordering

Usage:
    alert_service = AlertService()
    alert_service.register(SSEAlertHandler())
    alert_service.register(LoggingAlertHandler())

    # When events occur (e.g. from LoadManager.evaluate()):
    events = load_manager.evaluate(timer, eta_minutes=25.0)
    await alert_service.publish_all(events)
"""
import logging
from typing import List

from app.domain.events import DomainEvent

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Alert Service (event bus)
# ─────────────────────────────────────────────────────────────────────────────

class AlertService:
    """
    Central event bus — publishes domain events to all registered handlers.

    Implements the Observer pattern:
      - Subject:    AlertService (holds the subscriber list)
      - Observers:  IAlertHandler implementations (LoggingAlertHandler, SSEAlertHandler, etc.)
      - Events:     DomainEvent subclasses (LoadStatusChanged, LoadExpired, etc.)

    Thread-safe for async usage (single event loop, no shared mutable state
    beyond the handler list which is only modified during setup).
    """

    def __init__(self) -> None:
        self._handlers: List = []

    def register(self, handler: object) -> None:
        """
        Register an alert handler.

        Handlers must implement:
          - can_handle(event: DomainEvent) -> bool
          - handle(event: DomainEvent) -> None  (async)
        """
        self._handlers.append(handler)
        logger.info(f"Alert handler registered: {type(handler).__name__}")

    def unregister(self, handler: object) -> None:
        """Remove an alert handler."""
        self._handlers = [h for h in self._handlers if h is not handler]

    async def publish(self, event: DomainEvent) -> None:
        """
        Distribute an event to all interested handlers.

        Each handler's can_handle() is checked first. If a handler
        raises during handle(), the error is logged but does not
        propagate — other handlers still receive the event.
        """
        for handler in self._handlers:
            if handler.can_handle(event):
                try:
                    await handler.handle(event)
                except Exception as e:
                    logger.error(
                        f"Alert handler {type(handler).__name__} failed on "
                        f"{event.event_type}: {e}",
                        exc_info=True,
                    )

    async def publish_all(self, events: List[DomainEvent]) -> None:
        """Publish multiple events in sequence (preserves ordering)."""
        for event in events:
            await self.publish(event)

    @property
    def handler_count(self) -> int:
        return len(self._handlers)


# ─────────────────────────────────────────────────────────────────────────────
# Concrete Alert Handlers
# ─────────────────────────────────────────────────────────────────────────────

class LoggingAlertHandler:
    """
    Writes domain events to structured logging.

    Maps event severity to Python log levels:
      info     → INFO
      warning  → WARNING
      critical → CRITICAL
      emergency → CRITICAL (Python has no EMERGENCY level)

    Events without a severity attribute default to INFO.
    """

    LEVEL_MAP = {
        "info": logging.INFO,
        "warning": logging.WARNING,
        "critical": logging.CRITICAL,
        "emergency": logging.CRITICAL,
    }

    def can_handle(self, event: DomainEvent) -> bool:
        """Logs everything — no filtering."""
        return True

    async def handle(self, event: DomainEvent) -> None:
        severity = getattr(event, "severity", None)
        severity_value = getattr(severity, "value", "info")
        level = self.LEVEL_MAP.get(severity_value, logging.INFO)
        logger.log(level, f"[ALERT:{event.event_type}] {event.to_dict()}")


class SSEAlertHandler:
    """
    Pushes domain events to SSE subscribers via the trip pub/sub system.

    Only handles events that have a trip_id — events without a trip
    context (e.g. system-level alerts) are ignored.

    Integration: imports _push_event from trip service lazily to avoid
    circular imports.
    """

    def can_handle(self, event: DomainEvent) -> bool:
        """Only handle events with a valid trip_id."""
        trip_id = getattr(event, "trip_id", 0)
        return isinstance(trip_id, int) and trip_id > 0

    async def handle(self, event: DomainEvent) -> None:
        # Lazy import to avoid circular dependency
        from app.services.trip import _push_event

        trip_id = getattr(event, "trip_id", 0)
        if trip_id:
            await _push_event(trip_id, event.event_type, event.to_dict())
