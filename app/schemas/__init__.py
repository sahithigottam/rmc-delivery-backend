"""Pydantic schemas for request/response validation"""
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class Coordinate(BaseModel):
    """Geographic coordinate"""

    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)


class LocationRequest(BaseModel):
    """Location as street address (will be geocoded to coordinates)"""

    address: str = Field(..., min_length=3, description="Street address or location name")


class RouteRequestBase(BaseModel):
    """Base Route request schema"""

    start: LocationRequest
    end: LocationRequest
    vehicle_type: str = Field(default="rmc_truck", description="Default: rmc_truck")
    vehicle_id: Optional[str] = None
    load_weight: Optional[float] = Field(None, ge=0)
    load_volume: Optional[float] = Field(None, ge=0)
    departure_datetime: Optional[datetime] = None
    priority: str = Field("normal", pattern="^(normal|urgent|economy|low|high)$")
    max_delivery_minutes: Optional[int] = Field(None, ge=1, description="Maximum acceptable delivery time in minutes")
    
    # Dynamic routing options
    request_alternatives: bool = Field(False, description="Request alternative routes")
    avoid: Optional[List[str]] = Field(None, description="Features to avoid: tolls, highways, ferries")
    waypoints: Optional[List[Coordinate]] = Field(None, description="Intermediate waypoints for rerouting")
    route_index: int = Field(0, ge=0, description="Which alternative route to use (0 = primary)")


class RouteCreate(RouteRequestBase):
    """Schema for creating a new route"""

    pass


class RouteStep(BaseModel):
    """Single step in a route"""

    start_location: Coordinate
    end_location: Coordinate
    instruction: str
    distance_meters: float
    duration_seconds: float


class RouteResponse(RouteRequestBase):
    """Route response with calculated results"""

    id: int
    resolved_start_address: str  # Geocoded address from coordinates
    resolved_end_address: str    # Geocoded address from coordinates
    start_lat: float  # Start latitude
    start_lng: float  # Start longitude
    end_lat: float    # End latitude
    end_lng: float    # End longitude
    distance_meters: float
    duration_seconds: float
    traffic_delay_seconds: Optional[float] = None
    exceeds_delivery_limit: Optional[bool] = None  # True if route exceeds max_delivery_minutes
    delivery_limit_exceeded_by: Optional[int] = None  # Minutes exceeded by
    polyline: Optional[str] = None
    route_steps: Optional[List[RouteStep]] = None
    
    # Alternative routes info
    total_alternatives: int = Field(1, description="Total number of route alternatives available")
    selected_route_index: int = Field(0, description="Index of the selected route (0 = primary)")
    alternatives_summary: Optional[List[dict]] = Field(None, description="Summary of alternative routes")
    
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class VehicleBase(BaseModel):
    """Base Vehicle schema"""

    vehicle_code: str
    vehicle_type: str
    max_height_cm: Optional[int] = None
    max_width_cm: Optional[int] = None
    max_weight_kg: Optional[float] = None
    max_volume_m3: Optional[float] = None
    registration_number: Optional[str] = None


class VehicleCreate(VehicleBase):
    """Schema for creating a vehicle"""

    pass


class VehicleResponse(VehicleBase):
    """Vehicle response schema"""

    id: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class RerouteRequest(BaseModel):
    """Request for mid-trip reroute using current lat/lng (skips geocoding start)"""

    current_lat: float = Field(..., ge=-90, le=90, description="Truck's current latitude")
    current_lng: float = Field(..., ge=-180, le=180, description="Truck's current longitude")
    end: LocationRequest
    vehicle_type: str = Field(default="rmc_truck")
    priority: str = Field("normal", pattern="^(normal|urgent|economy|low|high)$")
    avoid: Optional[List[str]] = Field(None, description="Features to avoid: tolls, highways, ferries")


class RerouteResponse(BaseModel):
    """Lightweight response for reroute checks (no DB save)"""

    distance_meters: float
    duration_seconds: float
    traffic_delay_seconds: Optional[float] = None
    polyline: Optional[str] = None
    route_steps: Optional[List[RouteStep]] = None
    reroute_recommended: bool = Field(False, description="True if new route differs significantly")
    reason: Optional[str] = None


class ErrorResponse(BaseModel):
    """Standard error response"""

    detail: str
    error_code: Optional[str] = None


# ============================================================================
# Trip Management Schemas
# ============================================================================

class TripStart(BaseModel):
    """Start a trip from a previously calculated route"""

    route_id: int = Field(..., description="ID of the calculated route to start a trip for")
    vehicle_id: Optional[str] = Field(None, description="Override vehicle ID for this trip")

    # RMC-specific fields
    batch_time: Optional[datetime] = Field(None, description="When concrete was batched (defaults to now)")
    mix_code: Optional[str] = Field(None, description="Concrete mix code e.g. '30MPa/20/120'")
    concrete_grade: Optional[str] = Field(None, description="Concrete grade e.g. '30MPa'")
    volume_m3: Optional[float] = Field(None, ge=0.1, le=12.0, description="Load volume in m³ (typical NZ truck: 6m³)")
    requires_retarder: bool = Field(False, description="Whether retarder should be pre-added")
    plant_id: Optional[str] = Field(None, description="Dispatching plant ID")
    pour_duration_minutes: Optional[int] = Field(None, ge=1, description="Expected pour time on site")


class PositionUpdate(BaseModel):
    """GPS position report from the truck"""

    lat: float = Field(..., ge=-90, le=90)
    lng: float = Field(..., ge=-180, le=180)
    heading: Optional[float] = Field(None, ge=0, le=360, description="Heading in degrees")
    speed_kmh: Optional[float] = Field(None, ge=0, description="Current speed in km/h")


class TripEventResponse(BaseModel):
    """A single trip event"""

    id: int
    trip_id: int
    event_type: str
    data: Optional[dict] = None
    lat: Optional[float] = None
    lng: Optional[float] = None
    created_at: datetime

    class Config:
        from_attributes = True


class TripResponse(BaseModel):
    """Full trip state"""

    id: int
    route_id: int
    status: str
    current_lat: Optional[float] = None
    current_lng: Optional[float] = None
    heading: Optional[float] = None

    start_address: str
    end_address: str
    start_lat: float
    start_lng: float
    end_lat: float
    end_lng: float
    vehicle_type: str
    vehicle_id: Optional[str] = None
    priority: str
    avoid_options: Optional[List[str]] = None

    current_polyline: Optional[str] = None
    original_distance_meters: Optional[float] = None
    original_duration_seconds: Optional[float] = None
    remaining_distance_meters: Optional[float] = None
    remaining_duration_seconds: Optional[float] = None
    current_traffic_delay: Optional[float] = None

    reroute_count: int = 0
    last_reroute_at: Optional[datetime] = None
    last_traffic_check_at: Optional[datetime] = None
    estimated_arrival: Optional[datetime] = None

    # RMC-specific fields
    batch_time: Optional[datetime] = None
    load_expiry_time: Optional[datetime] = None
    load_status: Optional[str] = None
    mix_code: Optional[str] = None
    concrete_grade: Optional[str] = None
    volume_m3: Optional[float] = None
    retarder_added: bool = False
    retarder_extension_minutes: int = 0
    load_max_life_minutes: int = 90
    load_minutes_remaining: Optional[float] = None  # calculated, not persisted
    plant_id: Optional[str] = None
    pour_duration_minutes: Optional[int] = None

    # Dispatch / scheduling
    scheduled_at: Optional[datetime] = None
    concrete_mix: Optional[str] = None          # GP | HE | RE
    outcome: Optional[str] = None               # succeeded | failed

    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    # Recent events (last 20)
    recent_events: Optional[List[TripEventResponse]] = None

    class Config:
        from_attributes = True


class TripRerouteInfo(BaseModel):
    """Info about a reroute suggestion pushed via SSE"""

    trip_id: int
    reroute_number: int
    reason: str
    old_duration_seconds: float
    new_duration_seconds: float
    new_distance_meters: float
    traffic_delay_seconds: Optional[float] = None
    new_polyline: Optional[str] = None
    new_steps: Optional[List[RouteStep]] = None
    applied: bool = False


# ── Plant schemas ─────────────────────────────────────────────────────────

class PlantOut(BaseModel):
    """Single concrete plant from plants.json catalogue."""
    id: str
    name: str
    brand: str
    address: str
    lat: float
    lng: float
    region: str
    active: bool = True


class PlantCreate(BaseModel):
    """Payload for creating or fully updating a plant."""
    name: str
    brand: str
    address: str
    lat: float
    lng: float
    region: str
    active: bool = True


class PlantUpdate(BaseModel):
    """Partial update — all fields optional."""
    name: Optional[str] = None
    brand: Optional[str] = None
    address: Optional[str] = None
    lat: Optional[float] = None
    lng: Optional[float] = None
    region: Optional[str] = None
    active: Optional[bool] = None


class BrandAnalysisRequest(BaseModel):
    """Request brand analysis: pick a brand + destination, get ranked predictions."""
    brand: str = Field(..., description="Plant brand, e.g. 'Holcim'")
    job_site_data: Dict[str, Any] = Field(
        ...,
        description="Full address data object for the job site from OpenStreetMap (must include 'display_name').",
    )
    concrete_mix: str = Field(default="GP", description="Mix family: GP | HE | RE")
    top_n: int = Field(default=5, ge=1, le=20, description="Max plants to analyse")
    include_llm_analysis: bool = Field(default=True, description="Include LLM comparison of all routes")


class PlantPredictionResult(BaseModel):
    """Prediction result for a single plant within a brand analysis."""
    plant: PlantOut
    google_eta_minutes: Optional[float] = None
    adjusted_eta_minutes: Optional[float] = None
    remaining_life_minutes: Optional[float] = None
    buffer_depletion_rate: Optional[float] = None
    delay_constant_used: Optional[float] = None
    risk_level: Optional[str] = None          # low | medium | high | critical
    success_probability: Optional[float] = None
    recommendation: Optional[str] = None
    llm_analysis: Optional[dict] = None
    error: Optional[str] = None              # if this plant failed to resolve


class BrandAnalysisResponse(BaseModel):
    """Response for brand analysis — list of plant predictions, ranked by remaining life."""
    brand: str
    job_site_address: str
    concrete_mix: str
    total_plants_analysed: int
    results: List[PlantPredictionResult]
    best_plant_id: Optional[str] = None      # plant with highest remaining life
    llm_comparison: Optional[dict] = None    # single LLM comparison of all plants


# ── Dispatch schemas ──────────────────────────────────────────────────────

class DispatchRequest(BaseModel):
    """Create a scheduled (pending) trip from a chosen plant + job site."""
    plant_id: str = Field(..., description="Plant ID from plants.json")
    job_site_address: str = Field(..., description="Delivery destination address")
    concrete_mix: str = Field(default="GP", description="Mix family: GP | HE | RE")
    scheduled_at: datetime = Field(..., description="Planned departure time (ISO 8601)")
    vehicle_id: Optional[str] = None
    volume_m3: Optional[float] = Field(None, ge=0.5, le=20.0)
    pour_duration_minutes: Optional[int] = Field(None, ge=1, le=240)
    # Snapshot from /plants/analyse (saved for accuracy tracking)
    prediction_snapshot: Optional[dict] = Field(
        None, description="Prediction result dict from /plants/analyse to store with trip"
    )


class DispatchResponse(BaseModel):
    """Response when a trip is scheduled (status=pending)."""
    trip_id: int
    route_id: int
    status: str                          # "pending"
    plant_id: str
    plant_name: str
    plant_address: str
    job_site_address: str
    concrete_mix: str
    scheduled_at: datetime
    distance_meters: Optional[float] = None
    estimated_duration_minutes: Optional[float] = None
    created_at: datetime


class TripBeginRequest(BaseModel):
    """Activate a pending trip (truck is loading now)."""
    batch_time: Optional[datetime] = Field(
        None, description="When concrete was batched. Defaults to now."
    )
    mix_code: Optional[str] = Field(None, description="Full mix code e.g. '30MPa/20/100'")
    concrete_grade: Optional[str] = Field(None, description="e.g. '30MPa'. Defaults to mix family default.")
    volume_m3: Optional[float] = Field(None, ge=0.5, le=20.0)
    requires_retarder: bool = False
    pour_duration_minutes: Optional[int] = None


class TripCompleteRequest(BaseModel):
    """Optional body for POST /trips/{id}/complete to record outcome."""
    success: Optional[bool] = Field(
        None,
        description="True = concrete was poured successfully. "
                    "Defaults to True if load is still within time window."
    )
    notes: Optional[str] = Field(None, max_length=500)


__all__ = [
    "Coordinate",
    "LocationRequest",
    "RouteRequestBase",
    "RouteCreate",
    "RouteStep",
    "RouteResponse",
    "RerouteRequest",
    "RerouteResponse",
    "VehicleBase",
    "VehicleCreate",
    "VehicleResponse",
    "ErrorResponse",
    "TripStart",
    "PositionUpdate",
    "TripEventResponse",
    "TripResponse",
    "TripRerouteInfo",
    # Plants / dispatch
    "PlantOut",
    "PlantCreate",
    "PlantUpdate",
    "BrandAnalysisRequest",
    "PlantPredictionResult",
    "BrandAnalysisResponse",
    "DispatchRequest",
    "DispatchResponse",
    "TripBeginRequest",
    "TripCompleteRequest",
]
