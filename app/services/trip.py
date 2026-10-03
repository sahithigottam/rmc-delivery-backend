"""
Trip management service — active delivery tracking + automatic rerouting.

Architecture:
  1. Trip lifecycle:  pending → in_progress → (paused ↔ resumed) → completed/cancelled
  2. Position updates: truck GPS reports update Trip.current_lat/lng
  3. Traffic monitor:  background task probes Google Directions every N minutes
                       for ALL active trips from their current position → destination
  4. Reroute engine:   compares new route vs current remaining duration,
                       if >15% slower or >5 min extra delay → logs reroute_suggested event,
                       auto-applies the new polyline, pushes SSE to connected clients
  5. Load management:  90-minute concrete load timer, status transitions,
                       retarder recommendations, expiry detection (RMC domain)
  6. Event log:        every state change, position update, traffic check, reroute
                       is stored as an immutable TripEvent for audit
"""
import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import AsyncGenerator, Dict, List, Optional, Set

from sqlalchemy.orm import Session

from app.db.models import Route, Trip, TripEvent
from app.domain.enums import ConcreteGrade, LoadStatus
from app.domain.values import ConcreteSpec, LoadTimer
from app.rmc.alerts import AlertService
from app.rmc.load_manager import LoadManager
from app.schemas import (
    Coordinate,
    DispatchRequest,
    DispatchResponse,
    PositionUpdate,
    RouteStep,
    TripBeginRequest,
    TripCompleteRequest,
    TripEventResponse,
    TripRerouteInfo,
    TripResponse,
    TripStart,
)
from app.services.google_maps import GoogleMapsService
from app.services.route import RouteService

logger = logging.getLogger(__name__)

# ── Reroute thresholds ─────────────────────────────────────────────────────
REROUTE_DELAY_THRESHOLD_SECONDS = 300    # 5 min additional delay → reroute
REROUTE_RATIO_THRESHOLD = 1.15           # 15% slower than expected → reroute
TRAFFIC_CHECK_INTERVAL_SECONDS = 120     # check traffic every 2 minutes
MIN_REMAINING_KM_FOR_CHECK = 1.0         # don't bother checking if < 1km left
REROUTE_COOLDOWN_SECONDS = 180           # don't reroute more than once per 3 min
TRAFFIC_CHECK_MIN_MOVEMENT_KM = 0.2      # skip Directions call if truck moved < 200m since last check
TRAFFIC_CHECK_MAX_SKIP_SECONDS = 480     # always call API after 8 min regardless of movement

# ── Per-trip position tracking for API call deduplication ─────────────────
# Maps trip_id → (lat, lng) at the time of the last successful Directions call.
# In-memory only — resets on server restart (acceptable; just causes one extra API call).
_last_checked_positions: Dict[int, tuple] = {}


# ── SSE subscriber registry ───────────────────────────────────────────────
# Maps trip_id → set of asyncio.Queues for connected SSE clients
_sse_subscribers: Dict[int, Set[asyncio.Queue]] = {}


def subscribe_trip(trip_id: int) -> asyncio.Queue:
    """Register an SSE subscriber for a trip. Returns a Queue to read events from."""
    q: asyncio.Queue = asyncio.Queue()
    _sse_subscribers.setdefault(trip_id, set()).add(q)
    logger.info(f"SSE subscriber added for trip {trip_id} (total: {len(_sse_subscribers[trip_id])})")
    return q


def unsubscribe_trip(trip_id: int, q: asyncio.Queue):
    """Remove an SSE subscriber."""
    if trip_id in _sse_subscribers:
        _sse_subscribers[trip_id].discard(q)
        if not _sse_subscribers[trip_id]:
            del _sse_subscribers[trip_id]


async def _push_event(trip_id: int, event_type: str, data: dict):
    """Push an event to all SSE subscribers of a trip."""
    if trip_id not in _sse_subscribers:
        return
    payload = {"event": event_type, "data": data}
    dead: List[asyncio.Queue] = []
    for q in _sse_subscribers[trip_id]:
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            dead.append(q)
    for q in dead:
        _sse_subscribers[trip_id].discard(q)


class TripService:
    """Service for trip lifecycle management and automatic rerouting."""

    def __init__(
        self,
        db: Session,
        google_maps: GoogleMapsService,
        load_manager: Optional[LoadManager] = None,
        alert_service: Optional[AlertService] = None,
    ):
        self.db = db
        self.google_maps = google_maps
        self._load_manager = load_manager or LoadManager()
        self._alerts = alert_service

    # ── Trip lifecycle ─────────────────────────────────────────────────

    async def start_trip(self, req: TripStart) -> TripResponse:
        """Start a new trip from a previously calculated route."""
        route = self.db.query(Route).filter(Route.id == req.route_id).first()
        if not route:
            raise ValueError(f"Route {req.route_id} not found")

        # ── RMC: resolve batch time + concrete spec ──
        batch_time = req.batch_time or datetime.now(timezone.utc)
        grade_str = req.concrete_grade or "25MPa"
        try:
            grade = ConcreteGrade(grade_str)
        except ValueError:
            grade = ConcreteGrade.MPA_25

        spec = ConcreteSpec(
            mix_code=req.mix_code or f"{grade.value}/20/100",
            grade=grade,
            volume_m3=req.volume_m3 or 6.0,
            requires_retarder=req.requires_retarder,
        )

        # Create load timer
        load_timer = self._load_manager.start_timer(batch_time, spec)

        trip = Trip(
            route_id=route.id,
            status="in_progress",
            start_address=route.resolved_start_address or route.start_address,
            end_address=route.resolved_end_address or route.end_address,
            start_lat=route.start_lat,
            start_lng=route.start_lng,
            end_lat=route.end_lat,
            end_lng=route.end_lng,
            current_lat=route.start_lat,
            current_lng=route.start_lng,
            vehicle_type=route.vehicle_type,
            vehicle_id=req.vehicle_id or route.vehicle_id,
            priority=route.priority,
            avoid_options=route.avoid_options,
            current_polyline=route.polyline,
            original_distance_meters=route.distance_meters,
            original_duration_seconds=route.duration_seconds,
            remaining_distance_meters=route.distance_meters,
            remaining_duration_seconds=route.duration_seconds,
            current_traffic_delay=route.traffic_delay_seconds,
            started_at=datetime.now(timezone.utc),
            # RMC fields
            batch_time=batch_time,
            load_expiry_time=load_timer.expiry_time,
            load_status=LoadStatus.FRESH.value,
            mix_code=spec.mix_code,
            concrete_grade=spec.grade.value,
            volume_m3=spec.volume_m3,
            retarder_added=spec.requires_retarder,
            retarder_extension_minutes=load_timer.retarder_extension_minutes,
            load_max_life_minutes=load_timer.effective_max_minutes,
            plant_id=req.plant_id,
            pour_duration_minutes=req.pour_duration_minutes,
        )

        # Calculate ETA
        if route.duration_seconds:
            from datetime import timedelta
            trip.estimated_arrival = datetime.now(timezone.utc) + timedelta(
                seconds=route.duration_seconds + (route.traffic_delay_seconds or 0)
            )

        self.db.add(trip)
        self.db.flush()

        # Log event
        self._log_event(trip.id, "trip_started", {
            "route_id": route.id,
            "distance_meters": route.distance_meters,
            "duration_seconds": route.duration_seconds,
            "vehicle_id": trip.vehicle_id,
            "batch_time": batch_time.isoformat(),
            "mix_code": spec.mix_code,
            "volume_m3": spec.volume_m3,
            "load_expiry_time": load_timer.expiry_time.isoformat(),
            "load_max_life_minutes": load_timer.effective_max_minutes,
        }, lat=route.start_lat, lng=route.start_lng)

        # Publish load_batched event via alert service
        if self._alerts:
            batch_event = self._load_manager.create_batch_event(
                trip_id=trip.id, timer=load_timer, spec=spec,
            )
            await self._alerts.publish(batch_event)

        self.db.commit()
        self.db.refresh(trip)

        logger.info(f"Trip {trip.id} started: {trip.start_address} → {trip.end_address}")
        return self._to_response(trip)

    async def update_position(self, trip_id: int, pos: PositionUpdate) -> TripResponse:
        """Update truck's GPS position and estimate remaining distance."""
        trip = self._get_trip(trip_id)
        if trip.status not in ("in_progress", "paused"):
            raise ValueError(f"Cannot update position for trip in '{trip.status}' state")

        trip.current_lat = pos.lat
        trip.current_lng = pos.lng
        if pos.heading is not None:
            trip.heading = pos.heading

        # Rough remaining distance estimate (haversine to destination)
        remaining_km = self._haversine(pos.lat, pos.lng, trip.end_lat, trip.end_lng)
        trip.remaining_distance_meters = remaining_km * 1000

        # Log position (throttled — only if moved >100m from last logged position)
        self._log_event(trip.id, "position_update", {
            "lat": pos.lat,
            "lng": pos.lng,
            "heading": pos.heading,
            "speed_kmh": pos.speed_kmh,
            "remaining_km": round(remaining_km, 2),
        }, lat=pos.lat, lng=pos.lng)

        # Auto-complete if very close to destination (<200m)
        if remaining_km < 0.2:
            return await self._complete_trip(trip, auto=True)

        self.db.commit()
        self.db.refresh(trip)

        # Push position to SSE subscribers
        await _push_event(trip_id, "position", {
            "lat": pos.lat, "lng": pos.lng,
            "remaining_km": round(remaining_km, 2),
        })

        return self._to_response(trip)

    async def pause_trip(self, trip_id: int) -> TripResponse:
        trip = self._get_trip(trip_id)
        if trip.status != "in_progress":
            raise ValueError("Can only pause an in-progress trip")
        trip.status = "paused"
        self._log_event(trip_id, "trip_paused", {},
                        lat=trip.current_lat, lng=trip.current_lng)
        self.db.commit()
        self.db.refresh(trip)
        await _push_event(trip_id, "status", {"status": "paused"})
        return self._to_response(trip)

    async def resume_trip(self, trip_id: int) -> TripResponse:
        trip = self._get_trip(trip_id)
        if trip.status != "paused":
            raise ValueError("Can only resume a paused trip")
        trip.status = "in_progress"
        self._log_event(trip_id, "trip_resumed", {},
                        lat=trip.current_lat, lng=trip.current_lng)
        self.db.commit()
        self.db.refresh(trip)
        await _push_event(trip_id, "status", {"status": "in_progress"})
        return self._to_response(trip)

    async def cancel_trip(self, trip_id: int) -> TripResponse:
        trip = self._get_trip(trip_id)
        if trip.status in ("completed", "cancelled"):
            raise ValueError(f"Trip already {trip.status}")
        trip.status = "cancelled"
        self._log_event(trip_id, "trip_cancelled", {},
                        lat=trip.current_lat, lng=trip.current_lng)
        self.db.commit()
        self.db.refresh(trip)
        await _push_event(trip_id, "status", {"status": "cancelled"})
        return self._to_response(trip)

    async def complete_trip(
        self,
        trip_id: int,
        req: Optional["TripCompleteRequest"] = None,
    ) -> TripResponse:
        """
        Mark a trip completed and record the outcome.

        If `req.success` is explicitly set, use that value.
        Otherwise auto-derive: load still within time window → succeeded.
        """
        trip = self._get_trip(trip_id)

        # Determine outcome
        if req is not None and req.success is not None:
            outcome = "succeeded" if req.success else "failed"
        else:
            # Auto-derive from load status
            outcome = "failed" if trip.load_status == LoadStatus.EXPIRED.value else "succeeded"

        trip.outcome = outcome
        notes = req.notes if req is not None else None
        return await self._complete_trip(trip, auto=False, notes=notes)

    # ── Dispatch lifecycle ──────────────────────────────────────────────

    async def dispatch_trip(
        self,
        req: "DispatchRequest",
        route_service: "RouteService",
    ) -> "DispatchResponse":
        """
        Create a **pending** trip from a plant + job site.

        1. Resolves the plant from plants.json
        2. Calculates the route via Google Maps + stores it in DB
        3. Creates Trip(status=pending, scheduled_at=req.scheduled_at)
        4. Returns a lightweight DispatchResponse

        The trip stays pending until the driver calls POST /trips/{id}/begin.
        """
        import json as _json

        from app.schemas import DispatchResponse, RouteCreate, LocationRequest
        from app.services.plant_locator import PlantLocator

        loc = PlantLocator()
        plant = loc.get_by_id(req.plant_id)
        if not plant:
            raise ValueError(f"Plant '{req.plant_id}' not found in plants catalogue")

        # Build and calculate route
        route_req = RouteCreate(
            start=LocationRequest(address=plant["address"]),
            end=LocationRequest(address=req.job_site_address),
            vehicle_type="rmc_truck",
            departure_datetime=req.scheduled_at,
        )
        route_resp = await route_service.estimate_route(route_req)
        route = self.db.query(Route).filter(Route.id == route_resp.id).first()
        if not route:
            raise ValueError("Route could not be saved to database")

        # Serialize prediction snapshot if provided
        pred_json: Optional[str] = None
        if req.prediction_snapshot:
            pred_json = _json.dumps(req.prediction_snapshot)

        trip = Trip(
            route_id=route.id,
            status="pending",
            start_address=route.resolved_start_address or route.start_address,
            end_address=route.resolved_end_address or route.end_address,
            start_lat=route.start_lat,
            start_lng=route.start_lng,
            end_lat=route.end_lat,
            end_lng=route.end_lng,
            current_lat=route.start_lat,
            current_lng=route.start_lng,
            current_polyline=route.polyline,
            vehicle_type="rmc_truck",
            vehicle_id=req.vehicle_id,
            original_distance_meters=route.distance_meters,
            original_duration_seconds=route.duration_seconds,
            remaining_distance_meters=route.distance_meters,
            remaining_duration_seconds=route.duration_seconds,
            # RMC fields (load timer set on begin)
            concrete_mix=req.concrete_mix,
            plant_id=req.plant_id,
            volume_m3=req.volume_m3,
            pour_duration_minutes=req.pour_duration_minutes,
            # Scheduling
            scheduled_at=req.scheduled_at,
            prediction_json=pred_json,
        )
        self.db.add(trip)
        self.db.flush()

        self._log_event(trip.id, "trip_dispatched", {
            "plant_id": req.plant_id,
            "plant_name": plant["name"],
            "job_site": req.job_site_address,
            "concrete_mix": req.concrete_mix,
            "scheduled_at": req.scheduled_at.isoformat(),
            "route_id": route.id,
        })
        self.db.commit()
        self.db.refresh(trip)

        logger.info(
            "Trip %s dispatched (pending): %s → %s, scheduled %s",
            trip.id, plant["name"], req.job_site_address, req.scheduled_at,
        )

        duration_min = None
        if route.duration_seconds:
            duration_min = round(route.duration_seconds / 60, 1)

        return DispatchResponse(
            trip_id=trip.id,
            route_id=route.id,
            status="pending",
            plant_id=req.plant_id,
            plant_name=plant["name"],
            plant_address=plant["address"],
            job_site_address=req.job_site_address,
            concrete_mix=req.concrete_mix,
            scheduled_at=req.scheduled_at,
            distance_meters=route.distance_meters,
            estimated_duration_minutes=duration_min,
            created_at=trip.created_at,
        )

    async def begin_trip(self, trip_id: int, req: "TripBeginRequest") -> TripResponse:
        """
        Activate a **pending** trip — concrete has been batched, truck is leaving.

        Starts the load timer and transitions status → in_progress.
        """
        trip = self._get_trip(trip_id)
        if trip.status != "pending":
            raise ValueError(
                f"Only pending trips can be begun (trip {trip_id} is '{trip.status}')"
            )

        batch_time = req.batch_time or datetime.now(timezone.utc)
        concrete_grade_str = req.concrete_grade or "25MPa"
        try:
            grade = ConcreteGrade(concrete_grade_str)
        except ValueError:
            grade = ConcreteGrade.MPA_25

        # Derive mix family from trip.concrete_mix if not overridden
        mix_family = trip.concrete_mix or "GP"
        spec = ConcreteSpec(
            mix_code=req.mix_code or f"{grade.value}/20/100",
            grade=grade,
            volume_m3=req.volume_m3 or trip.volume_m3 or 6.0,
            requires_retarder=req.requires_retarder,
        )
        load_timer = self._load_manager.start_timer(batch_time, spec)

        trip.status = "in_progress"
        trip.started_at = datetime.now(timezone.utc)
        trip.batch_time = batch_time
        trip.load_expiry_time = load_timer.expiry_time
        trip.load_status = LoadStatus.FRESH.value
        trip.mix_code = req.mix_code or spec.mix_code
        trip.concrete_grade = grade.value
        trip.volume_m3 = spec.volume_m3
        trip.retarder_added = spec.requires_retarder
        trip.retarder_extension_minutes = load_timer.retarder_extension_minutes
        trip.load_max_life_minutes = load_timer.effective_max_minutes

        if trip.original_duration_seconds:
            trip.estimated_arrival = datetime.now(timezone.utc) + timedelta(
                seconds=trip.original_duration_seconds
            )

        self._log_event(trip.id, "trip_started", {
            "batch_time": batch_time.isoformat(),
            "mix_code": spec.mix_code,
            "volume_m3": spec.volume_m3,
            "load_expiry_time": load_timer.expiry_time.isoformat(),
            "load_max_life_minutes": load_timer.effective_max_minutes,
        }, lat=trip.current_lat, lng=trip.current_lng)

        if self._alerts:
            batch_event = self._load_manager.create_batch_event(
                trip_id=trip.id, timer=load_timer, spec=spec,
            )
            await self._alerts.publish(batch_event)

        self.db.commit()
        self.db.refresh(trip)

        await _push_event(trip_id, "status", {"status": "in_progress"})
        logger.info("Trip %s begun (in_progress), batch_time=%s", trip_id, batch_time)
        return self._to_response(trip)

    def get_trip(self, trip_id: int) -> TripResponse:
        """Return full trip state (used by GET /trips/{id})."""
        trip = self._get_trip(trip_id)
        return self._to_response(trip)

    def get_active_trips(self) -> List[TripResponse]:
        trips = (
            self.db.query(Trip)
            .filter(Trip.status.in_(["in_progress", "paused", "pending"]))
            .order_by(Trip.created_at.desc())
            .all()
        )
        return [self._to_response(t) for t in trips]

    def get_all_trips(self, limit: int = 100) -> List[TripResponse]:
        trips = (
            self.db.query(Trip)
            .order_by(Trip.created_at.desc())
            .limit(limit)
            .all()
        )
        return [self._to_response(t) for t in trips]

    def delete_trip(self, trip_id: int) -> None:
        """Hard-delete a trip. Only allowed for cancelled or completed trips."""
        trip = self._get_trip(trip_id)
        if trip.status not in ("cancelled", "completed"):
            raise ValueError(
                f"Cannot delete trip in '{trip.status}' state. "
                "Only cancelled or completed trips can be deleted."
            )
        # Delete associated events first (cascade safety)
        self.db.query(TripEvent).filter(TripEvent.trip_id == trip_id).delete()
        self.db.delete(trip)
        self.db.commit()
        logger.info(f"Trip {trip_id} deleted.")

    def get_trip_events(self, trip_id: int, limit: int = 50) -> List[TripEventResponse]:
        events = (
            self.db.query(TripEvent)
            .filter(TripEvent.trip_id == trip_id)
            .order_by(TripEvent.created_at.desc())
            .limit(limit)
            .all()
        )
        return [TripEventResponse.model_validate(e) for e in events]

    # ── Reroute Engine ─────────────────────────────────────────────────

    async def check_and_reroute(self, trip_id: int) -> Optional[TripRerouteInfo]:
        """
        Core reroute logic — called by the background monitor.
        1. Get trip's current position
        2. Call Google Directions from current pos → destination
        3. Compare with current remaining duration
        4. If significantly worse → apply reroute, log event, push SSE
        """
        trip = self._get_trip(trip_id)

        if trip.status != "in_progress":
            return None

        # Don't check if too close to destination
        remaining_km = self._haversine(
            trip.current_lat or trip.start_lat,
            trip.current_lng or trip.start_lng,
            trip.end_lat, trip.end_lng,
        )
        if remaining_km < MIN_REMAINING_KM_FOR_CHECK:
            return None

        # Cooldown check
        if trip.last_reroute_at:
            elapsed = (datetime.now(timezone.utc) - trip.last_reroute_at.replace(tzinfo=timezone.utc)).total_seconds()
            if elapsed < REROUTE_COOLDOWN_SECONDS:
                return None

        current_lat = trip.current_lat or trip.start_lat
        current_lng = trip.current_lng or trip.start_lng

        # ── Skip Directions API call if truck hasn't moved meaningfully ──
        # This is the primary cost-saving guard: a stationary or slow-moving
        # truck generates the same ETA, so there's no point calling Google again.
        prev = _last_checked_positions.get(trip_id)
        if prev and trip.last_traffic_check_at:
            moved_km = self._haversine(prev[0], prev[1], current_lat, current_lng)
            time_since_s = (
                datetime.now(timezone.utc)
                - trip.last_traffic_check_at.replace(tzinfo=timezone.utc)
            ).total_seconds()
            if moved_km < TRAFFIC_CHECK_MIN_MOVEMENT_KM and time_since_s < TRAFFIC_CHECK_MAX_SKIP_SECONDS:
                logger.debug(
                    f"Trip {trip_id}: skipping Directions API call "
                    f"(moved {moved_km * 1000:.0f}m, last check {time_since_s:.0f}s ago)"
                )
                return None


        traffic_model_map = {
            "urgent": "pessimistic", "high": "pessimistic",
            "normal": "best_guess",
            "low": "optimistic", "economy": "optimistic",
        }
        traffic_model = traffic_model_map.get(trip.priority or "normal", "best_guess")

        try:
            # Call Google Directions from current position.
            # coarse_start=True snaps the start coords to a ~1.1km grid so minor
            # truck movements reuse cached responses instead of triggering new API calls.
            directions = await self.google_maps.get_directions(
                start_lat=current_lat,
                start_lng=current_lng,
                end_lat=trip.end_lat,
                end_lng=trip.end_lng,
                departure_time=int(time.time()),
                traffic_model=traffic_model,
                alternatives=False,
                avoid=trip.avoid_options,
                coarse_start=True,
            )

            if directions.get("status") != "OK":
                logger.warning(f"Traffic check failed for trip {trip_id}: {directions.get('status')}")
                self._log_event(trip_id, "traffic_check", {
                    "status": "api_error",
                    "error": directions.get("error_message", "Unknown"),
                }, lat=current_lat, lng=current_lng)
                return None

            # Record position at time of this successful API call
            _last_checked_positions[trip_id] = (current_lat, current_lng)

            route_data = directions["routes"][0]
            leg = route_data["legs"][0]

            new_distance = leg.get("distance", {}).get("value", 0)
            new_duration = leg.get("duration", {}).get("value", 0)
            new_duration_traffic = leg.get("duration_in_traffic", {}).get("value")
            new_delay = (new_duration_traffic - new_duration) if new_duration_traffic else 0
            new_polyline = route_data.get("overview_polyline", {}).get("points")

            # Update trip with latest traffic info
            trip.last_traffic_check_at = datetime.now(timezone.utc)
            trip.remaining_distance_meters = new_distance
            trip.remaining_duration_seconds = new_duration_traffic or new_duration
            trip.current_traffic_delay = new_delay

            # Update ETA
            from datetime import timedelta
            trip.estimated_arrival = datetime.now(timezone.utc) + timedelta(
                seconds=new_duration_traffic or new_duration
            )

            # Log traffic check
            self._log_event(trip_id, "traffic_check", {
                "distance_meters": new_distance,
                "duration_seconds": new_duration,
                "duration_in_traffic": new_duration_traffic,
                "delay_seconds": new_delay,
                "condition": "heavy" if new_delay > 300 else "moderate" if new_delay > 60 else "clear",
            }, lat=current_lat, lng=current_lng)

            # ── RMC: Evaluate load timer ──
            eta_minutes = (new_duration_traffic or new_duration) / 60.0
            await self._evaluate_load_status(trip, eta_minutes)

            # ── Reroute decision ──
            old_remaining = trip.original_duration_seconds or new_duration
            effective_new = new_duration_traffic or new_duration
            reroute_recommended = False
            reason = ""

            # Check ratio
            if old_remaining > 0:
                ratio = effective_new / old_remaining
                if ratio > REROUTE_RATIO_THRESHOLD:
                    reroute_recommended = True
                    pct = int((ratio - 1) * 100)
                    reason = f"Route is {pct}% slower than expected"

            # Check absolute delay
            if new_delay > REROUTE_DELAY_THRESHOLD_SECONDS:
                reroute_recommended = True
                delay_min = int(new_delay / 60)
                reason = f"Heavy traffic — {delay_min} min delay detected"

            if reroute_recommended and new_polyline:
                trip.reroute_count += 1
                trip.last_reroute_at = datetime.now(timezone.utc)
                old_polyline = trip.current_polyline
                trip.current_polyline = new_polyline

                # Parse new steps
                new_steps = []
                for step in leg.get("steps", []):
                    new_steps.append(RouteStep(
                        start_location=Coordinate(
                            latitude=step["start_location"]["lat"],
                            longitude=step["start_location"]["lng"],
                        ),
                        end_location=Coordinate(
                            latitude=step["end_location"]["lat"],
                            longitude=step["end_location"]["lng"],
                        ),
                        instruction=step.get("html_instructions", ""),
                        distance_meters=step.get("distance", {}).get("value", 0),
                        duration_seconds=step.get("duration", {}).get("value", 0),
                    ))

                reroute_info = TripRerouteInfo(
                    trip_id=trip_id,
                    reroute_number=trip.reroute_count,
                    reason=reason,
                    old_duration_seconds=old_remaining,
                    new_duration_seconds=effective_new,
                    new_distance_meters=new_distance,
                    traffic_delay_seconds=new_delay,
                    new_polyline=new_polyline,
                    new_steps=new_steps,
                    applied=True,
                )

                # Log reroute events
                self._log_event(trip_id, "reroute_suggested", {
                    "reason": reason,
                    "reroute_number": trip.reroute_count,
                    "new_distance": new_distance,
                    "new_duration": effective_new,
                    "delay_seconds": new_delay,
                }, lat=current_lat, lng=current_lng)

                self._log_event(trip_id, "reroute_applied", {
                    "reroute_number": trip.reroute_count,
                    "old_duration": old_remaining,
                    "new_duration": effective_new,
                    "distance_saved_meters": (trip.remaining_distance_meters or 0) - new_distance,
                }, lat=current_lat, lng=current_lng)

                self.db.commit()

                # Push to SSE
                await _push_event(trip_id, "reroute", reroute_info.model_dump())

                logger.info(
                    f"🔄 Trip {trip_id} rerouted (#{trip.reroute_count}): {reason}"
                )
                return reroute_info
            else:
                # No reroute needed, just save updated metrics
                self.db.commit()

                await _push_event(trip_id, "traffic_ok", {
                    "remaining_km": round(new_distance / 1000, 1),
                    "delay_seconds": new_delay,
                    "eta": trip.estimated_arrival.isoformat() if trip.estimated_arrival else None,
                })

                return None

        except Exception as e:
            logger.error(f"Error checking traffic for trip {trip_id}: {e}")
            self._log_event(trip_id, "traffic_check", {
                "status": "error", "error": str(e),
            }, lat=current_lat, lng=current_lng)
            self.db.commit()
            return None

    # ── Internal helpers ───────────────────────────────────────────────

    def _get_trip(self, trip_id: int) -> Trip:
        trip = self.db.query(Trip).filter(Trip.id == trip_id).first()
        if not trip:
            raise ValueError(f"Trip {trip_id} not found")
        return trip

    async def _complete_trip(
        self, trip: Trip, auto: bool = False, notes: Optional[str] = None
    ) -> TripResponse:
        trip.status = "completed"
        trip.completed_at = datetime.now(timezone.utc)
        trip.remaining_distance_meters = 0
        trip.remaining_duration_seconds = 0
        event_data: dict = {
            "auto_completed": auto,
            "total_reroutes": trip.reroute_count,
            "outcome": trip.outcome,
            "final_lat": trip.current_lat,
            "final_lng": trip.current_lng,
        }
        if notes:
            event_data["notes"] = notes
        self._log_event(trip.id, "trip_completed", event_data,
                        lat=trip.current_lat, lng=trip.current_lng)
        self.db.commit()
        self.db.refresh(trip)
        await _push_event(trip.id, "status", {"status": "completed"})
        logger.info(f"🏁 Trip {trip.id} completed (reroutes: {trip.reroute_count})")
        return self._to_response(trip)

    def _log_event(self, trip_id: int, event_type: str, data: dict,
                   lat: Optional[float] = None, lng: Optional[float] = None):
        event = TripEvent(
            trip_id=trip_id,
            event_type=event_type,
            data=data,
            lat=lat,
            lng=lng,
        )
        self.db.add(event)

    async def _evaluate_load_status(
        self, trip: Trip, eta_minutes: float
    ) -> None:
        """
        Evaluate load timer and publish domain events.

        Reconstructs the LoadTimer from persisted trip fields,
        runs the LoadManager evaluation, updates trip.load_status,
        and publishes any resulting events through the alert service.
        """
        if not trip.batch_time:
            return  # no load timer — legacy trip without RMC data

        # Reconstruct LoadTimer from persisted state
        timer = LoadTimer(
            batch_time=trip.batch_time,
            max_life_minutes=trip.load_max_life_minutes or 90,
            retarder_added=bool(trip.retarder_added),
            retarder_extension_minutes=trip.retarder_extension_minutes or 0,
        )

        previous_status_str = trip.load_status or "fresh"
        try:
            previous_status = LoadStatus(previous_status_str)
        except ValueError:
            previous_status = LoadStatus.FRESH

        # Run evaluation
        events = self._load_manager.evaluate(
            timer=timer,
            eta_minutes=eta_minutes,
            previous_status=previous_status,
            trip_id=trip.id,
        )

        # Update persisted load status
        new_status = self._load_manager.check_status(timer, eta_minutes)
        trip.load_status = new_status.value

        # Log load timer snapshot as event
        self._log_event(trip.id, "load_check", {
            **timer.snapshot(),
            "eta_minutes": round(eta_minutes, 1),
            "projected_status": new_status.value,
        }, lat=trip.current_lat, lng=trip.current_lng)

        # Publish domain events via alert service
        if self._alerts and events:
            await self._alerts.publish_all(events)

    def _to_response(self, trip: Trip) -> TripResponse:
        # Get last 20 events
        recent = (
            self.db.query(TripEvent)
            .filter(TripEvent.trip_id == trip.id)
            .order_by(TripEvent.created_at.desc())
            .limit(20)
            .all()
        )
        return TripResponse(
            id=trip.id,
            route_id=trip.route_id,
            status=trip.status,
            current_lat=trip.current_lat,
            current_lng=trip.current_lng,
            heading=trip.heading,
            start_address=trip.start_address,
            end_address=trip.end_address,
            start_lat=trip.start_lat,
            start_lng=trip.start_lng,
            end_lat=trip.end_lat,
            end_lng=trip.end_lng,
            vehicle_type=trip.vehicle_type,
            vehicle_id=trip.vehicle_id,
            priority=trip.priority,
            avoid_options=trip.avoid_options,
            current_polyline=trip.current_polyline,
            original_distance_meters=trip.original_distance_meters,
            original_duration_seconds=trip.original_duration_seconds,
            remaining_distance_meters=trip.remaining_distance_meters,
            remaining_duration_seconds=trip.remaining_duration_seconds,
            current_traffic_delay=trip.current_traffic_delay,
            reroute_count=trip.reroute_count or 0,
            last_reroute_at=trip.last_reroute_at,
            last_traffic_check_at=trip.last_traffic_check_at,
            estimated_arrival=trip.estimated_arrival,
            # RMC fields
            batch_time=trip.batch_time,
            load_expiry_time=trip.load_expiry_time,
            load_status=trip.load_status,
            mix_code=trip.mix_code,
            concrete_grade=trip.concrete_grade,
            volume_m3=trip.volume_m3,
            retarder_added=bool(trip.retarder_added) if trip.retarder_added else False,
            retarder_extension_minutes=trip.retarder_extension_minutes or 0,
            load_max_life_minutes=trip.load_max_life_minutes or 90,
            load_minutes_remaining=self._calc_load_remaining(trip),
            plant_id=trip.plant_id,
            pour_duration_minutes=trip.pour_duration_minutes,
            # Dispatch / scheduling
            scheduled_at=trip.scheduled_at,
            concrete_mix=trip.concrete_mix,
            outcome=trip.outcome,
            # Timestamps
            started_at=trip.started_at,
            completed_at=trip.completed_at,
            created_at=trip.created_at,
            updated_at=trip.updated_at,
            recent_events=[TripEventResponse.model_validate(e) for e in recent],
        )

    @staticmethod
    def _calc_load_remaining(trip: Trip) -> Optional[float]:
        """Calculate minutes remaining on the load timer (live, not persisted)."""
        if not trip.batch_time:
            return None
        batch_utc = trip.batch_time
        if batch_utc.tzinfo is None:
            batch_utc = batch_utc.replace(tzinfo=timezone.utc)
        max_life = (trip.load_max_life_minutes or 90)
        if trip.retarder_added:
            max_life += (trip.retarder_extension_minutes or 0)
        elapsed = (datetime.now(timezone.utc) - batch_utc).total_seconds() / 60.0
        return round(max_life - elapsed, 1)

    @staticmethod
    def _haversine(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
        """Haversine distance in km."""
        import math
        R = 6371
        dLat = math.radians(lat2 - lat1)
        dLng = math.radians(lng2 - lng1)
        a = (math.sin(dLat / 2) ** 2 +
             math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
             math.sin(dLng / 2) ** 2)
        return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
