"""
Background traffic monitor — proactively checks all active trips for delays
and automatically reroutes when conditions deteriorate.

Lifecycle:
  - Started via FastAPI lifespan (app startup)
  - Runs an infinite loop: sleep → query active trips → check_and_reroute each
  - Stopped gracefully on app shutdown via cancellation

Design:
  - Each check cycle creates its own DB session (thread-safe)
  - Google Directions calls are rate-limited per cycle to avoid quota spikes
  - Trips are checked in priority order (urgent first, economy last)
  - Dead-letter: if a trip fails traffic checks 5 times in a row, it's marked
    for manual review (logged as event, not auto-cancelled)
  - RMC load timer evaluation runs alongside traffic checks
"""
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.db.database import SessionLocal
from app.db.models import Trip
from app.rmc.alerts import AlertService
from app.rmc.load_manager import LoadManager
from app.services.google_maps import GoogleMapsService
from app.services.trip import TripService

logger = logging.getLogger(__name__)

# ── Configuration ──────────────────────────────────────────────────────────
CHECK_INTERVAL_SECONDS = 120            # how often to run a full cycle
INTER_TRIP_DELAY_SECONDS = 2            # delay between individual trip checks (rate limit)
MAX_CONSECUTIVE_ERRORS = 5              # mark trip for review after N failures
PRIORITY_ORDER = ["urgent", "high", "normal", "low", "economy"]

_monitor_task: Optional[asyncio.Task] = None
_load_manager: Optional[LoadManager] = None
_alert_service: Optional[AlertService] = None


async def start_traffic_monitor(
    google_maps: GoogleMapsService,
    load_manager: Optional[LoadManager] = None,
    alert_service: Optional[AlertService] = None,
):
    """Start the background traffic monitor. Called from FastAPI lifespan."""
    global _monitor_task, _load_manager, _alert_service
    if _monitor_task and not _monitor_task.done():
        logger.warning("Traffic monitor already running")
        return
    _load_manager = load_manager
    _alert_service = alert_service
    _monitor_task = asyncio.create_task(_monitor_loop(google_maps))
    logger.info("🟢 Background traffic monitor started")


async def stop_traffic_monitor():
    """Gracefully stop the background monitor. Called from FastAPI lifespan shutdown."""
    global _monitor_task
    if _monitor_task and not _monitor_task.done():
        _monitor_task.cancel()
        try:
            await _monitor_task
        except asyncio.CancelledError:
            pass
        logger.info("🔴 Background traffic monitor stopped")
    _monitor_task = None


async def _monitor_loop(google_maps: GoogleMapsService):
    """Main monitoring loop — runs until cancelled."""
    logger.info("Traffic monitor loop entering first cycle")
    consecutive_cycle_errors = 0

    while True:
        try:
            await _run_check_cycle(google_maps)
            consecutive_cycle_errors = 0
        except asyncio.CancelledError:
            raise  # let cancellation propagate
        except Exception as e:
            consecutive_cycle_errors += 1
            logger.error(
                f"Traffic monitor cycle error ({consecutive_cycle_errors}): {e}",
                exc_info=True,
            )
            # Exponential backoff if repeated failures
            if consecutive_cycle_errors > 3:
                backoff = min(CHECK_INTERVAL_SECONDS * 2, 600)
                logger.warning(f"Backing off for {backoff}s after repeated failures")
                await asyncio.sleep(backoff)
                continue

        await asyncio.sleep(CHECK_INTERVAL_SECONDS)


async def _run_check_cycle(google_maps: GoogleMapsService):
    """Run one cycle: query active trips, check each for traffic issues."""
    db: Session = SessionLocal()
    try:
        # Get all in-progress trips, sorted by priority
        active_trips = (
            db.query(Trip)
            .filter(Trip.status == "in_progress")
            .order_by(
                # Urgent trips checked first
                Trip.priority.desc()
            )
            .all()
        )

        if not active_trips:
            return

        logger.info(f"📡 Traffic check cycle: {len(active_trips)} active trip(s)")
        reroutes_this_cycle = 0
        checks_this_cycle = 0

        for trip in active_trips:
            try:
                # Each trip gets a fresh service instance with the shared session
                trip_service = TripService(
                    db=db,
                    google_maps=google_maps,
                    load_manager=_load_manager,
                    alert_service=_alert_service,
                )
                result = await trip_service.check_and_reroute(trip.id)

                checks_this_cycle += 1
                if result:
                    reroutes_this_cycle += 1
                    logger.info(
                        f"  ↳ Trip {trip.id} rerouted: {result.reason}"
                    )

            except Exception as e:
                logger.error(f"  ↳ Trip {trip.id} check failed: {e}")
                # Track consecutive failures for this trip
                _record_trip_check_failure(db, trip)

            # Rate-limit between trips
            if len(active_trips) > 1:
                await asyncio.sleep(INTER_TRIP_DELAY_SECONDS)

        logger.info(
            f"📡 Cycle done: {checks_this_cycle} checked, "
            f"{reroutes_this_cycle} rerouted"
        )

    finally:
        db.close()


def _record_trip_check_failure(db: Session, trip: Trip):
    """Track consecutive check failures. If too many, mark for manual review."""
    from app.db.models import TripEvent

    # Count recent consecutive errors
    recent_errors = (
        db.query(TripEvent)
        .filter(
            TripEvent.trip_id == trip.id,
            TripEvent.event_type == "traffic_check",
        )
        .order_by(TripEvent.created_at.desc())
        .limit(MAX_CONSECUTIVE_ERRORS)
        .all()
    )

    consecutive_errors = 0
    for evt in recent_errors:
        if isinstance(evt.data, dict) and evt.data.get("status") in ("error", "api_error"):
            consecutive_errors += 1
        else:
            break

    if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
        logger.warning(
            f"⚠️ Trip {trip.id}: {consecutive_errors} consecutive traffic check failures — "
            f"flagging for manual review"
        )
        flag_event = TripEvent(
            trip_id=trip.id,
            event_type="delay_detected",
            data={
                "reason": "consecutive_check_failures",
                "count": consecutive_errors,
                "action": "manual_review_required",
            },
            lat=trip.current_lat,
            lng=trip.current_lng,
        )
        db.add(flag_event)
        try:
            db.commit()
        except Exception:
            db.rollback()
