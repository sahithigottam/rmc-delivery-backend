"""
Load Manager — concrete load lifecycle management.

Single Responsibility: manages the 90-minute load timer, status
transitions, retarder recommendations, and expiry detection.

This is the heart of the RMC domain — every delivery is a race
against the clock once concrete is batched.

Usage:
    manager = LoadManager()
    timer = manager.start_timer(batch_time, spec)

    # On each traffic check / position update:
    status = manager.check_status(timer, eta_minutes=25.0)
    events = manager.evaluate(timer, eta_minutes=25.0, previous_status=LoadStatus.FRESH)

    # Events might include LoadStatusChanged, RetarderRecommended, LoadExpired

Design:
    - Implements ILoadTracker (Protocol) for DIP
    - Pure logic — no DB access, no HTTP calls, no side effects
    - All state is in the LoadTimer value object (immutable)
    - Events are returned, not published — caller decides what to do with them
"""
import logging
from datetime import datetime
from typing import List, Optional

from app.domain.enums import LoadStatus
from app.domain.events import (
    DeliveryInfeasible,
    DomainEvent,
    LoadBatched,
    LoadExpired,
    LoadStatusChanged,
    RetarderRecommended,
)
from app.domain.values import ConcreteSpec, LoadTimer

logger = logging.getLogger(__name__)


class LoadManager:
    """
    Manages the concrete load lifecycle.

    Implements ILoadTracker — can be injected wherever load tracking
    is needed without coupling to this specific implementation.

    Thread-safe: all state lives in LoadTimer (immutable value object).
    The manager itself is stateless — safe to share across requests.
    """

    # If ETA > remaining - buffer → recommend retarder
    RETARDER_BUFFER_MINUTES: float = 10.0

    # Don't recommend retarder if less than this remaining
    RETARDER_MIN_REMAINING: float = 15.0

    # Default retarder extension (can be overridden per call)
    DEFAULT_RETARDER_EXTENSION: int = 45

    # ── ILoadTracker implementation ──────────────────────────────────

    def start_timer(
        self,
        batch_time: datetime,
        spec: ConcreteSpec,
    ) -> LoadTimer:
        """
        Create a load timer, adjusting limits for the concrete grade.

        High-grade concrete (≥40 MPa) gets tighter thresholds:
          - Warning at 45 min (vs 60 for standard)
          - Critical at 60 min (vs 75 for standard)
          - Max life 75 min (vs 90 for standard)
        """
        max_life = spec.recommended_max_life_minutes

        if spec.is_high_grade:
            warning_at = 45
            critical_at = 60
        else:
            warning_at = 60
            critical_at = 75

        timer = LoadTimer(
            batch_time=batch_time,
            max_life_minutes=max_life,
            warning_at_minutes=warning_at,
            critical_at_minutes=critical_at,
            retarder_added=spec.requires_retarder,
            retarder_extension_minutes=(
                self.DEFAULT_RETARDER_EXTENSION if spec.requires_retarder else 0
            ),
        )

        logger.info(
            f"Load timer started: max_life={timer.effective_max_minutes}min, "
            f"grade={spec.grade.value}, volume={spec.volume_m3}m³, "
            f"retarder={'yes' if spec.requires_retarder else 'no'}"
        )
        return timer

    def check_status(
        self,
        timer: LoadTimer,
        current_eta_minutes: Optional[float] = None,
    ) -> LoadStatus:
        """
        Evaluate current load status.

        If ETA is provided and the load will expire before arrival,
        returns the *projected* status (CRITICAL/EXPIRED) even if the
        current elapsed time hasn't physically reached that threshold.

        This forward-projection is critical — by the time concrete
        actually expires, it's too late to act.
        """
        current = timer.status

        if current_eta_minutes is not None and current != LoadStatus.EXPIRED:
            # Project forward: will the load survive until arrival?
            projected_elapsed = timer.elapsed_minutes + current_eta_minutes

            if projected_elapsed >= timer.effective_max_minutes:
                return LoadStatus.EXPIRED
            if projected_elapsed >= timer.effective_critical_minutes:
                return LoadStatus.CRITICAL
            if projected_elapsed >= timer.effective_warning_minutes:
                return LoadStatus.WARNING

        return current

    def should_add_retarder(
        self,
        timer: LoadTimer,
        eta_minutes: float,
    ) -> bool:
        """
        Determine if retarder should be recommended.

        Recommends retarder when:
          1. Retarder hasn't already been added
          2. ETA would push load past the warning threshold
          3. There's still enough remaining life for retarder to be effective
             (adding retarder to a nearly-expired load is pointless)
        """
        if timer.retarder_added:
            return False

        remaining = timer.minutes_remaining
        if remaining < self.RETARDER_MIN_REMAINING:
            return False  # too late

        # Would arrival push past the safe window?
        return eta_minutes > (remaining - self.RETARDER_BUFFER_MINUTES)

    # ── Full evaluation (generates domain events) ────────────────────

    def evaluate(
        self,
        timer: LoadTimer,
        eta_minutes: Optional[float] = None,
        previous_status: LoadStatus = LoadStatus.FRESH,
        trip_id: int = 0,
    ) -> List[DomainEvent]:
        """
        Full evaluation — checks status, generates domain events.

        Called on every traffic check cycle. Returns a list of domain
        events that should be published through the AlertService.

        This method is pure: same inputs always produce the same outputs.
        No side effects, no DB writes, no HTTP calls.
        """
        events: List[DomainEvent] = []
        current = self.check_status(timer, eta_minutes)

        # ── Status transition event ──
        if current != previous_status:
            action = self._action_for_status(current, timer, eta_minutes)
            events.append(LoadStatusChanged(
                trip_id=trip_id,
                previous_status=previous_status,
                new_status=current,
                minutes_remaining=timer.minutes_remaining,
                eta_minutes=eta_minutes,
                action_required=action,
            ))
            logger.info(
                f"Trip {trip_id}: load status {previous_status.value} → {current.value} "
                f"({timer.minutes_remaining:.0f}min remaining)"
            )

        # ── Retarder recommendation ──
        if eta_minutes and self.should_add_retarder(timer, eta_minutes):
            events.append(RetarderRecommended(
                trip_id=trip_id,
                current_minutes_remaining=timer.minutes_remaining,
                eta_minutes=eta_minutes,
                recommended_extension_minutes=self.DEFAULT_RETARDER_EXTENSION,
            ))
            logger.warning(
                f"Trip {trip_id}: retarder recommended — "
                f"{timer.minutes_remaining:.0f}min remaining, ETA {eta_minutes:.0f}min"
            )

        # ── Expiry event ──
        if current == LoadStatus.EXPIRED:
            events.append(LoadExpired(
                trip_id=trip_id,
                minutes_over=abs(timer.minutes_remaining),
                was_retarded=timer.retarder_added,
            ))
            logger.error(
                f"Trip {trip_id}: LOAD EXPIRED — "
                f"{abs(timer.minutes_remaining):.0f}min over limit"
            )

        # ── Delivery infeasible (projected expiry before arrival) ──
        if (
            eta_minutes
            and current != LoadStatus.EXPIRED
            and timer.minutes_remaining < eta_minutes
        ):
            shortfall = eta_minutes - timer.minutes_remaining
            # Only fire if retarder can't save it either
            if not timer.can_retarder_help(eta_minutes):
                events.append(DeliveryInfeasible(
                    trip_id=trip_id,
                    minutes_remaining=timer.minutes_remaining,
                    eta_minutes=eta_minutes,
                    shortfall_minutes=shortfall,
                    recommended_action=self._infeasibility_action(shortfall),
                ))
                logger.error(
                    f"Trip {trip_id}: DELIVERY INFEASIBLE — "
                    f"ETA {eta_minutes:.0f}min, remaining {timer.minutes_remaining:.0f}min, "
                    f"shortfall {shortfall:.0f}min"
                )

        return events

    # ── Batch event factory ──────────────────────────────────────────

    def create_batch_event(
        self,
        trip_id: int,
        timer: LoadTimer,
        spec: ConcreteSpec,
    ) -> LoadBatched:
        """Create a LoadBatched event for a newly started trip."""
        return LoadBatched(
            trip_id=trip_id,
            mix_code=spec.mix_code,
            volume_m3=spec.volume_m3,
            max_life_minutes=timer.effective_max_minutes,
            expiry_time=timer.expiry_time.isoformat(),
        )

    # ── Private helpers ──────────────────────────────────────────────

    @staticmethod
    def _action_for_status(
        status: LoadStatus,
        timer: LoadTimer,
        eta_minutes: Optional[float],
    ) -> str:
        """Human-readable action for each status transition."""
        if status == LoadStatus.WARNING:
            remaining = timer.minutes_remaining
            return (
                f"Load approaching limit ({remaining:.0f}min remaining). "
                f"Consider retarder if ETA exceeds {remaining - 10:.0f}min."
            )
        if status == LoadStatus.CRITICAL:
            remaining = timer.minutes_remaining
            return (
                f"URGENT: only {remaining:.0f}min remaining. "
                f"Expedite delivery or prepare to reject load."
            )
        if status == LoadStatus.EXPIRED:
            return (
                "LOAD EXPIRED. Do NOT pour. "
                "Return to plant for disposal. Notify dispatch."
            )
        return "No action required."

    @staticmethod
    def _infeasibility_action(shortfall_minutes: float) -> str:
        """Recommended action based on how badly we'll miss the window."""
        if shortfall_minutes < 5:
            return "Marginal — expedite delivery, consider faster route"
        if shortfall_minutes < 15:
            return "Divert to nearest viable pour site or cancel trip"
        return "Cancel trip immediately — load will be wasted"
