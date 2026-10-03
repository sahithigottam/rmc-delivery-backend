"""Route optimization and calculation service (Feature 1)"""
import hashlib
import logging
from datetime import datetime, timezone
from typing import Dict, Optional, List

from cachetools import TTLCache
from sqlalchemy.orm import Session

from app.db.models import Route, TrafficSnapshot
from app.schemas import RouteCreate, RouteResponse, RouteStep, Coordinate, LocationRequest
from app.services.google_maps import GoogleMapsService

logger = logging.getLogger(__name__)

# Route cache configuration
ROUTE_CACHE_TTL = 300  # 5 minutes - same as directions cache
ROUTE_CACHE_SIZE = 500  # Max number of cached route responses

# Module-level cache to persist across requests
_route_cache: TTLCache = TTLCache(maxsize=ROUTE_CACHE_SIZE, ttl=ROUTE_CACHE_TTL)
_route_cache_stats = {"hits": 0, "misses": 0}


def get_route_cache_stats() -> Dict:
    """Get route cache statistics"""
    return {
        **_route_cache_stats,
        "route_cache_size": len(_route_cache),
    }


class RouteService:
    """Service for route calculation and optimization"""

    def __init__(self, db: Session, google_maps_client: GoogleMapsService):
        self.db = db
        self.google_maps = google_maps_client

    def _get_route_cache_key(self, route_request: RouteCreate) -> str:
        """Generate a cache key for the route request"""
        # Normalize addresses for better cache hits
        start_addr = route_request.start.address.lower().strip()
        end_addr = route_request.end.address.lower().strip()
        vehicle_type = route_request.vehicle_type or "rmc_truck"
        priority = route_request.priority or "normal"
        
        # Include departure time bucket (15 min windows) if provided
        time_bucket = ""
        if route_request.departure_datetime:
            timestamp = int(route_request.departure_datetime.timestamp())
            time_bucket = str(timestamp // 900 * 900)  # 15-minute buckets
        
        # Include routing preferences that change the API result
        avoid_str = ",".join(sorted(route_request.avoid)) if route_request.avoid else ""
        alt_str = str(route_request.request_alternatives)
        route_idx = str(route_request.route_index)
        
        key_parts = [
            start_addr, end_addr, vehicle_type, priority, time_bucket,
            avoid_str, alt_str, route_idx,
        ]
        key_string = "|".join(key_parts)
        return hashlib.md5(key_string.encode()).hexdigest()

    async def estimate_route(self, route_request: RouteCreate) -> RouteResponse:
        """
        Feature 1: Route Calculation
        Calculate route from street addresses with traffic-aware data from Google Maps API.
        Uses caching to avoid duplicate DB entries for the same route.

        Args:
            route_request: Route calculation request with start/end addresses

        Returns:
            RouteResponse with calculated distance, duration, traffic data
        """
        global _route_cache, _route_cache_stats
        
        # Check route cache first
        cache_key = self._get_route_cache_key(route_request)
        
        if cache_key in _route_cache:
            _route_cache_stats["hits"] += 1
            cached_route_id = _route_cache[cache_key]
            logger.info(f"Route cache HIT - returning existing route ID: {cached_route_id}")
            
            # Fetch the existing route from DB
            existing_route = self.get_route(cached_route_id)
            if existing_route:
                return existing_route
            else:
                # Route was deleted from DB, remove from cache and recalculate
                logger.warning(f"Cached route {cached_route_id} not found in DB, recalculating...")
                del _route_cache[cache_key]
        
        _route_cache_stats["misses"] += 1
        logger.info(f"Route cache MISS - calculating new route")
        
        try:
            # Geocode addresses to coordinates
            logger.info(f"Geocoding start address: {route_request.start.address}")
            start_geo = await self.google_maps.geocode_address(
                route_request.start.address
            )
            
            logger.info(f"Geocoding end address: {route_request.end.address}")
            end_geo = await self.google_maps.geocode_address(
                route_request.end.address
            )

            # Get departure time for traffic calculation.
            # Google Directions rejects past departure_time with ZERO_RESULTS,
            # so clamp to now if the scheduled time has already passed.
            departure_time = None
            if route_request.departure_datetime:
                dt = route_request.departure_datetime
                now = datetime.now(timezone.utc)
                # Ensure tz-aware comparison
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if dt <= now:
                    logger.info(
                        f"departure_datetime {dt.isoformat()} is in the past — using 'now' for Directions API"
                    )
                    departure_time = int(now.timestamp())
                else:
                    departure_time = int(dt.timestamp())

            # Convert waypoints from Coordinate objects to tuples
            waypoints_tuples = None
            if route_request.waypoints:
                waypoints_tuples = [
                    (wp.latitude, wp.longitude) for wp in route_request.waypoints
                ]

            # Map priority to traffic model:
            #   urgent/high → pessimistic (plan for worst-case traffic)
            #   normal      → best_guess  (balanced estimate)
            #   low/economy → optimistic  (assume lighter traffic)
            priority = route_request.priority or "normal"
            traffic_model_map = {
                "urgent": "pessimistic",
                "high": "pessimistic",
                "normal": "best_guess",
                "low": "optimistic",
                "economy": "optimistic",
            }
            traffic_model = traffic_model_map.get(priority, "best_guess")

            # Call Google Directions API with geocoded coordinates
            directions_data = await self.google_maps.get_directions(
                start_lat=start_geo["latitude"],
                start_lng=start_geo["longitude"],
                end_lat=end_geo["latitude"],
                end_lng=end_geo["longitude"],
                departure_time=departure_time,
                traffic_model=traffic_model,
                alternatives=route_request.request_alternatives,
                avoid=route_request.avoid,
                waypoints=waypoints_tuples,
            )

            # Parse response
            if directions_data.get("status") != "OK":
                status_code = directions_data.get("status", "UNKNOWN")
                error_msg = directions_data.get("error_message") or status_code
                logger.error(f"Google Directions API error: {status_code} — {error_msg}")
                raise ValueError(f"Route calculation failed: {status_code} — {error_msg}")

            # Get all available routes
            all_routes = directions_data.get("routes", [])
            total_alternatives = len(all_routes)
            
            # Select the requested route index (default to primary route)
            route_index = min(route_request.route_index, total_alternatives - 1)
            route_data = all_routes[route_index] if all_routes else directions_data["routes"][0]
            
            # Build alternatives summary for other routes
            alternatives_summary = []
            for idx, alt_route in enumerate(all_routes):
                alt_leg = alt_route["legs"][0]
                alternatives_summary.append({
                    "index": idx,
                    "distance_km": alt_leg.get("distance", {}).get("value", 0) / 1000,
                    "duration_mins": alt_leg.get("duration", {}).get("value", 0) / 60,
                    "summary": alt_route.get("summary", f"Route {idx + 1}"),
                    "warnings": alt_route.get("warnings", []),
                })
            
            leg = route_data["legs"][0]

            distance_meters = leg.get("distance", {}).get("value", 0)
            duration_seconds = leg.get("duration", {}).get("value", 0)
            duration_in_traffic = leg.get("duration_in_traffic", {}).get("value")
            traffic_delay = None

            if duration_in_traffic:
                traffic_delay = duration_in_traffic - duration_seconds

            polyline = route_data.get("overview_polyline", {}).get("points")

            # Parse route steps
            route_steps = []
            for step in leg.get("steps", []):
                route_steps.append(
                    RouteStep(
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
                    )
                )

            # Check delivery time constraint
            exceeds_delivery_limit = None
            delivery_limit_exceeded_by = None
            if route_request.max_delivery_minutes:
                duration_minutes = duration_seconds / 60
                if duration_minutes > route_request.max_delivery_minutes:
                    exceeds_delivery_limit = True
                    delivery_limit_exceeded_by = int(duration_minutes - route_request.max_delivery_minutes)
                    logger.warning(
                        f"Route exceeds delivery limit: {duration_minutes:.0f} min > "
                        f"{route_request.max_delivery_minutes} min (exceeded by {delivery_limit_exceeded_by} min)"
                    )
                else:
                    exceeds_delivery_limit = False
                    delivery_limit_exceeded_by = 0

            # Save to database
            db_route = Route(
                start_address=route_request.start.address,
                start_lat=start_geo["latitude"],
                start_lng=start_geo["longitude"],
                resolved_start_address=start_geo["formatted_address"],
                end_address=route_request.end.address,
                end_lat=end_geo["latitude"],
                end_lng=end_geo["longitude"],
                resolved_end_address=end_geo["formatted_address"],
                vehicle_type=route_request.vehicle_type,
                vehicle_id=route_request.vehicle_id,
                load_weight=route_request.load_weight,
                load_volume=route_request.load_volume,
                departure_datetime=route_request.departure_datetime,
                priority=route_request.priority,
                max_delivery_minutes=route_request.max_delivery_minutes,
                distance_meters=distance_meters,
                duration_seconds=duration_seconds,
                traffic_delay_seconds=traffic_delay,
                exceeds_delivery_limit=1 if exceeds_delivery_limit else (0 if exceeds_delivery_limit is False else None),
                delivery_limit_exceeded_by=delivery_limit_exceeded_by,
                polyline=polyline,
                avoid_options=route_request.avoid,
                selected_route_index=route_index,
                total_alternatives=total_alternatives,
                alternatives_summary=alternatives_summary,
                route_steps=[
                    {
                        "start": {
                            "lat": step.start_location.latitude,
                            "lng": step.start_location.longitude,
                        },
                        "end": {
                            "lat": step.end_location.latitude,
                            "lng": step.end_location.longitude,
                        },
                        "instruction": step.instruction,
                        "distance_meters": step.distance_meters,
                        "duration_seconds": step.duration_seconds,
                    }
                    for step in route_steps
                ],
            )

            self.db.add(db_route)
            self.db.commit()
            self.db.refresh(db_route)

            # Save traffic snapshot
            traffic_snapshot = TrafficSnapshot(
                route_id=db_route.id,
                current_duration_seconds=duration_seconds,
                traffic_condition="normal" if traffic_delay is None else "heavy",
                congestion_level=int((traffic_delay or 0) / 60) if traffic_delay else 0,
            )
            self.db.add(traffic_snapshot)
            self.db.commit()

            # Cache the route ID for future requests
            _route_cache[cache_key] = db_route.id
            
            logger.info(
                f"Route calculated and cached: id={db_route.id}, distance={distance_meters}m, "
                f"duration={duration_seconds}s"
            )

            return self._db_model_to_response(db_route, route_steps)

        except Exception as e:
            logger.error(f"Error estimating route: {e}")
            self.db.rollback()
            raise

    def get_route(self, route_id: int) -> Optional[RouteResponse]:
        """Retrieve a previously calculated route"""
        db_route = self.db.query(Route).filter(Route.id == route_id).first()
        if not db_route:
            return None

        # Reconstruct route steps from JSON
        route_steps = []
        if db_route.route_steps:
            for step_data in db_route.route_steps:
                route_steps.append(
                    RouteStep(
                        start_location=Coordinate(
                            latitude=step_data["start"]["lat"],
                            longitude=step_data["start"]["lng"],
                        ),
                        end_location=Coordinate(
                            latitude=step_data["end"]["lat"],
                            longitude=step_data["end"]["lng"],
                        ),
                        instruction=step_data.get("instruction", ""),
                        distance_meters=step_data.get("distance_meters", 0),
                        duration_seconds=step_data.get("duration_seconds", 0),
                    )
                )

        return self._db_model_to_response(db_route, route_steps)

    async def check_reroute(
        self,
        current_lat: float,
        current_lng: float,
        end_address: str,
        vehicle_type: str = "rmc_truck",
        priority: str = "normal",
        avoid: Optional[List[str]] = None,
        original_duration: Optional[float] = None,
    ):
        """
        Check if a reroute is recommended from the truck's current coordinates
        to the destination.  Skips geocoding the start (we already have lat/lng).
        Does NOT save to DB — this is a lightweight traffic probe.
        """
        from app.schemas import RerouteResponse, RouteStep, Coordinate

        # Geocode destination
        end_geo = await self.google_maps.geocode_address(end_address)

        # Map priority → traffic_model
        traffic_model_map = {
            "urgent": "pessimistic", "high": "pessimistic",
            "normal": "best_guess",
            "low": "optimistic", "economy": "optimistic",
        }
        traffic_model = traffic_model_map.get(priority, "best_guess")

        import time
        departure_time = int(time.time())

        directions_data = await self.google_maps.get_directions(
            start_lat=current_lat,
            start_lng=current_lng,
            end_lat=end_geo["latitude"],
            end_lng=end_geo["longitude"],
            departure_time=departure_time,
            traffic_model=traffic_model,
            alternatives=False,
            avoid=avoid,
        )

        if directions_data.get("status") != "OK":
            raise ValueError(f"Reroute check failed: {directions_data.get('error_message', 'Unknown')}")

        route_data = directions_data["routes"][0]
        leg = route_data["legs"][0]

        distance_meters = leg.get("distance", {}).get("value", 0)
        duration_seconds = leg.get("duration", {}).get("value", 0)
        duration_in_traffic = leg.get("duration_in_traffic", {}).get("value")
        traffic_delay = (duration_in_traffic - duration_seconds) if duration_in_traffic else None
        polyline = route_data.get("overview_polyline", {}).get("points")

        # Parse steps
        route_steps = []
        for step in leg.get("steps", []):
            route_steps.append(RouteStep(
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

        # Decide if reroute is recommended
        reroute_recommended = False
        reason = None
        if original_duration and duration_in_traffic:
            remaining_ratio = duration_in_traffic / original_duration if original_duration > 0 else 1
            if remaining_ratio > 1.15:  # >15% slower than expected
                reroute_recommended = True
                reason = f"Traffic adds {int((traffic_delay or 0) / 60)} min — route is {int((remaining_ratio - 1) * 100)}% slower"
        elif traffic_delay and traffic_delay > 300:  # >5 min delay
            reroute_recommended = True
            reason = f"Heavy traffic detected — {int(traffic_delay / 60)} min delay"

        return RerouteResponse(
            distance_meters=distance_meters,
            duration_seconds=duration_seconds,
            traffic_delay_seconds=traffic_delay,
            polyline=polyline,
            route_steps=route_steps,
            reroute_recommended=reroute_recommended,
            reason=reason,
        )

    def get_route_history(self, vehicle_id: Optional[str] = None, limit: int = 10):
        """Retrieve route history"""
        query = self.db.query(Route)
        if vehicle_id:
            query = query.filter(Route.vehicle_id == vehicle_id)

        routes = query.order_by(Route.created_at.desc()).limit(limit).all()
        return [
            self._db_model_to_response(route, []) for route in routes
        ]

    def _db_model_to_response(
        self, db_route: Route, route_steps: List[RouteStep]
    ) -> RouteResponse:
        """Convert database model to response schema"""
        # Convert integer to boolean for exceeds_delivery_limit
        exceeds_limit = None
        if db_route.exceeds_delivery_limit is not None:
            exceeds_limit = db_route.exceeds_delivery_limit == 1
        
        return RouteResponse(
            id=db_route.id,
            start=LocationRequest(address=db_route.start_address),
            end=LocationRequest(address=db_route.end_address),
            resolved_start_address=db_route.resolved_start_address or db_route.start_address,
            resolved_end_address=db_route.resolved_end_address or db_route.end_address,
            start_lat=db_route.start_lat,
            start_lng=db_route.start_lng,
            end_lat=db_route.end_lat,
            end_lng=db_route.end_lng,
            vehicle_type=db_route.vehicle_type,
            vehicle_id=db_route.vehicle_id,
            load_weight=db_route.load_weight,
            load_volume=db_route.load_volume,
            departure_datetime=db_route.departure_datetime,
            priority=db_route.priority,
            max_delivery_minutes=db_route.max_delivery_minutes,
            distance_meters=db_route.distance_meters,
            duration_seconds=db_route.duration_seconds,
            traffic_delay_seconds=db_route.traffic_delay_seconds,
            exceeds_delivery_limit=exceeds_limit,
            delivery_limit_exceeded_by=db_route.delivery_limit_exceeded_by,
            polyline=db_route.polyline,
            route_steps=route_steps,
            total_alternatives=getattr(db_route, 'total_alternatives', 1) or 1,
            selected_route_index=getattr(db_route, 'selected_route_index', 0) or 0,
            alternatives_summary=getattr(db_route, 'alternatives_summary', None),
            created_at=db_route.created_at,
            updated_at=db_route.updated_at,
        )
