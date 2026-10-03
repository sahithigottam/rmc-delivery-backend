"""
API v1 trip endpoints — active trip management + real-time SSE stream.

Endpoints:
  POST   /trips/dispatch         → Schedule a pending trip (plant→job site, future date)
  POST   /trips/{id}/begin       → Activate a pending trip (truck is loading, starts load timer)
  POST   /trips/start            → Start a trip immediately from a route (legacy flow)
  GET    /trips/active           → List all active trips
  GET    /trips/{id}             → Get trip state
  GET    /trips/{id}/events      → Get trip event log
  GET    /trips/{id}/stream      → SSE stream for real-time updates
  POST   /trips/{id}/position    → Report GPS position
  POST   /trips/{id}/check       → Force an immediate traffic check
  POST   /trips/{id}/pause       → Pause a trip
  POST   /trips/{id}/resume      → Resume a paused trip
  POST   /trips/{id}/complete    → Mark trip as completed (optionally record outcome)
  POST   /trips/{id}/cancel      → Cancel a trip
"""
import asyncio
import json
import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from app.dependencies import get_trip_service, get_route_service
from app.schemas import (
    DispatchRequest,
    DispatchResponse,
    ErrorResponse,
    PositionUpdate,
    TripBeginRequest,
    TripCompleteRequest,
    TripEventResponse,
    TripResponse,
    TripRerouteInfo,
    TripStart,
)
from app.services.route import RouteService
from app.services.trip import TripService, subscribe_trip, unsubscribe_trip

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/trips", tags=["trips"])


# ── Trip lifecycle ──────────────────────────────────────────────────────────


@router.post(
    "/start",
    response_model=TripResponse,
    responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)
async def start_trip(
    req: TripStart,
    trip_service: TripService = Depends(get_trip_service),
) -> TripResponse:
    """
    Start a new trip from a previously calculated route.
    The backend will begin background traffic monitoring for this trip.
    """
    try:
        return await trip_service.start_trip(req)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to start trip: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to start trip")


@router.get("/active", response_model=List[TripResponse])
async def list_active_trips(
    trip_service: TripService = Depends(get_trip_service),
) -> List[TripResponse]:
    """List all active (in_progress, paused, pending) trips."""
    return trip_service.get_active_trips()


@router.get("/all", response_model=List[TripResponse])
async def list_all_trips(
    limit: int = 100,
    trip_service: TripService = Depends(get_trip_service),
) -> List[TripResponse]:
    """List all trips (any status), most recent first."""
    return trip_service.get_all_trips(limit=limit)


@router.get("/{trip_id}", response_model=TripResponse)
async def get_trip(
    trip_id: int,
    trip_service: TripService = Depends(get_trip_service),
) -> TripResponse:
    """Get full trip state including recent events."""
    try:
        return trip_service.get_trip(trip_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/{trip_id}/events", response_model=List[TripEventResponse])
async def get_trip_events(
    trip_id: int,
    limit: int = 50,
    trip_service: TripService = Depends(get_trip_service),
) -> List[TripEventResponse]:
    """Get trip event log (most recent first)."""
    return trip_service.get_trip_events(trip_id, limit=limit)


# ── Position + reroute ──────────────────────────────────────────────────────


@router.post("/{trip_id}/position", response_model=TripResponse)
async def update_position(
    trip_id: int,
    pos: PositionUpdate,
    trip_service: TripService = Depends(get_trip_service),
) -> TripResponse:
    """
    Report truck's GPS position. The backend will:
    1. Update the trip's current position
    2. Estimate remaining distance (haversine)
    3. Auto-complete if within 200m of destination
    """
    try:
        return await trip_service.update_position(trip_id, pos)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post(
    "/{trip_id}/check",
    response_model=TripRerouteInfo,
    responses={200: {"description": "Reroute applied or traffic OK (null)"}, 404: {"model": ErrorResponse}},
)
async def force_traffic_check(
    trip_id: int,
    trip_service: TripService = Depends(get_trip_service),
):
    """
    Force an immediate traffic check and reroute analysis.
    Returns reroute info if a reroute was applied, otherwise null.
    """
    try:
        result = await trip_service.check_and_reroute(trip_id)
        return result or {"message": "Traffic is clear, no reroute needed"}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ── State transitions ──────────────────────────────────────────────────────


@router.post("/{trip_id}/pause", response_model=TripResponse)
async def pause_trip(
    trip_id: int,
    trip_service: TripService = Depends(get_trip_service),
) -> TripResponse:
    """Pause an active trip. Background traffic checks will skip paused trips."""
    try:
        return await trip_service.pause_trip(trip_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{trip_id}/resume", response_model=TripResponse)
async def resume_trip(
    trip_id: int,
    trip_service: TripService = Depends(get_trip_service),
) -> TripResponse:
    """Resume a paused trip."""
    try:
        return await trip_service.resume_trip(trip_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{trip_id}/complete", response_model=TripResponse)
async def complete_trip(
    trip_id: int,
    req: Optional[TripCompleteRequest] = None,
    trip_service: TripService = Depends(get_trip_service),
) -> TripResponse:
    """
    Mark a trip as completed and record the delivery outcome.

    Optionally pass a JSON body:
    ```json
    { "success": true, "notes": "Poured on time, no issues" }
    ```
    If omitted, outcome is auto-derived: `succeeded` unless the concrete load
    had already expired (`load_status == 'expired'`).
    """
    try:
        return await trip_service.complete_trip(trip_id, req)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{trip_id}/cancel", response_model=TripResponse)
async def cancel_trip(
    trip_id: int,
    trip_service: TripService = Depends(get_trip_service),
) -> TripResponse:
    """Cancel a trip."""
    try:
        return await trip_service.cancel_trip(trip_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete(
    "/{trip_id}",
    status_code=204,
    responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)
async def delete_trip(
    trip_id: int,
    trip_service: TripService = Depends(get_trip_service),
) -> None:
    """Hard-delete a trip. Only cancelled or completed trips can be deleted."""
    try:
        trip_service.delete_trip(trip_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── Dispatch flow ─────────────────────────────────────────────────────────


@router.post(
    "/dispatch",
    response_model=DispatchResponse,
    responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
    summary="Schedule a pending trip from a plant to a job site",
)
async def dispatch_trip(
    req: DispatchRequest,
    trip_service: TripService = Depends(get_trip_service),
    route_service: RouteService = Depends(get_route_service),
) -> DispatchResponse:
    """
    **Step 2 of the dispatch UI flow.**

    After the dispatcher has used `POST /plants/analyse` to compare plants and
    chosen one, call this endpoint to create a **pending** scheduled trip.

    - Resolves the plant address from `config/plants.json`
    - Calculates the route via Google Maps and stores it in the database
    - Creates `Trip(status=pending, scheduled_at=...)`
    - Stores the prediction snapshot alongside the trip (for accuracy tracking)

    The trip stays **pending** until the driver calls `POST /trips/{id}/begin`
    when the truck is actually loaded and ready to leave.
    """
    try:
        return await trip_service.dispatch_trip(req, route_service)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post(
    "/{trip_id}/begin",
    response_model=TripResponse,
    responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
    summary="Activate a pending trip — truck is loading now",
)
async def begin_trip(
    trip_id: int,
    req: TripBeginRequest,
    trip_service: TripService = Depends(get_trip_service),
) -> TripResponse:
    """
    **Step 3 of the dispatch UI flow.**

    Called when the truck is physically at the plant and concrete has been
    batched. This:

    - Starts the **90-minute load timer** from the batch time (default: now)
    - Transitions the trip from `pending` → `in_progress`
    - Begins background traffic monitoring and reroute engine

    Pass `batch_time` in the body if the concrete was batched before the driver
    clicked "Start", e.g. when the batch slip shows an earlier time.
    """
    try:
        return await trip_service.begin_trip(trip_id, req)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── SSE Stream ─────────────────────────────────────────────────────────────


@router.get("/{trip_id}/stream")
async def stream_trip_events(trip_id: int):
    """
    Server-Sent Events stream for real-time trip updates.

    Event types pushed:
      - reroute:    new route applied (includes polyline, steps, reason)
      - position:   truck position updated
      - traffic_ok: traffic check passed, no reroute needed
      - status:     trip status changed (paused, completed, cancelled)
      - heartbeat:  keepalive every 30s

    Usage:
      const es = new EventSource('/api/v1/trips/42/stream');
      es.addEventListener('reroute', (e) => {
        const data = JSON.parse(e.data);
        // data.new_polyline, data.reason, data.new_steps, ...
      });
    """
    queue = subscribe_trip(trip_id)

    async def event_generator():
        try:
            while True:
                try:
                    # Wait for events with a 30s timeout for heartbeat
                    payload = await asyncio.wait_for(queue.get(), timeout=30.0)
                    event_type = payload.get("event", "message")
                    data = json.dumps(payload.get("data", {}))
                    yield f"event: {event_type}\ndata: {data}\n\n"
                except asyncio.TimeoutError:
                    # Send heartbeat to keep connection alive
                    yield f"event: heartbeat\ndata: {{}}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            unsubscribe_trip(trip_id, queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # nginx: disable proxy buffering
        },
    )
