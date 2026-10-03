"""SQLAlchemy ORM models"""
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Enum, Float, ForeignKey, Integer, String, Text, JSON
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.db.database import Base


class Route(Base):
    """Route request and calculation storage"""

    __tablename__ = "routes"

    id = Column(Integer, primary_key=True, index=True)
    start_address = Column(String(500), nullable=False)  # Original input address
    start_lat = Column(Float, nullable=False)  # Geocoded latitude
    start_lng = Column(Float, nullable=False)  # Geocoded longitude
    resolved_start_address = Column(String(500), nullable=True)  # Geocoded formatted address
    
    end_address = Column(String(500), nullable=False)  # Original input address
    end_lat = Column(Float, nullable=False)  # Geocoded latitude
    end_lng = Column(Float, nullable=False)  # Geocoded longitude
    resolved_end_address = Column(String(500), nullable=True)  # Geocoded formatted address
    vehicle_type = Column(String(50), nullable=False)
    vehicle_id = Column(String(100), nullable=True)
    load_weight = Column(Float, nullable=True)
    load_volume = Column(Float, nullable=True)
    departure_datetime = Column(DateTime, nullable=True)
    priority = Column(String(20), default="normal")
    max_delivery_minutes = Column(Integer, nullable=True)  # Maximum delivery time constraint

    # Route results
    distance_meters = Column(Float, nullable=True)
    duration_seconds = Column(Float, nullable=True)
    traffic_delay_seconds = Column(Float, nullable=True)
    exceeds_delivery_limit = Column(Integer, nullable=True)  # 1 if exceeds, 0 if not, NULL if no limit
    delivery_limit_exceeded_by = Column(Integer, nullable=True)  # Minutes exceeded by
    polyline = Column(Text, nullable=True)
    route_steps = Column(JSON, nullable=True)
    
    # Dynamic routing fields
    avoid_options = Column(JSON, nullable=True)  # List of avoided features: tolls, highways, ferries
    selected_route_index = Column(Integer, default=0)  # Which alternative route was selected
    total_alternatives = Column(Integer, default=1)  # Number of route alternatives available
    alternatives_summary = Column(JSON, nullable=True)  # Summary info of all alternatives

    # Metadata
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self):
        return f"<Route(id={self.id}, vehicle_type={self.vehicle_type})>"


class Vehicle(Base):
    """Vehicle specifications and constraints"""

    __tablename__ = "vehicles"

    id = Column(Integer, primary_key=True, index=True)
    vehicle_code = Column(String(100), unique=True, index=True)
    vehicle_type = Column(String(50), nullable=False)
    max_height_cm = Column(Integer, nullable=True)
    max_width_cm = Column(Integer, nullable=True)
    max_weight_kg = Column(Float, nullable=True)
    max_volume_m3 = Column(Float, nullable=True)
    registration_number = Column(String(50), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self):
        return f"<Vehicle(vehicle_code={self.vehicle_code}, type={self.vehicle_type})>"


class TrafficSnapshot(Base):
    """Store traffic conditions at time of route calculation"""

    __tablename__ = "traffic_snapshots"

    id = Column(Integer, primary_key=True, index=True)
    route_id = Column(Integer, nullable=False, index=True)
    timestamp = Column(DateTime(timezone=True), server_default=func.now())
    current_duration_seconds = Column(Float, nullable=False)
    traffic_condition = Column(String(50), default="normal")
    congestion_level = Column(Integer, nullable=True)

    def __repr__(self):
        return f"<TrafficSnapshot(route_id={self.route_id}, condition={self.traffic_condition})>"


# ============================================================================
# Trip Management — active delivery tracking + rerouting
# ============================================================================

class Trip(Base):
    """Active delivery trip — tracks a truck from start to destination"""

    __tablename__ = "trips"

    id = Column(Integer, primary_key=True, index=True)
    route_id = Column(Integer, ForeignKey("routes.id"), nullable=False, index=True)

    # Trip state
    status = Column(
        String(20), nullable=False, default="pending",
        index=True,
        # pending | in_progress | paused | completed | cancelled
    )

    # Truck current position (updated by GPS / position reports)
    current_lat = Column(Float, nullable=True)
    current_lng = Column(Float, nullable=True)
    heading = Column(Float, nullable=True)  # degrees 0-360

    # Trip metadata from the original route
    start_address = Column(String(500), nullable=False)
    end_address = Column(String(500), nullable=False)
    start_lat = Column(Float, nullable=False)
    start_lng = Column(Float, nullable=False)
    end_lat = Column(Float, nullable=False)
    end_lng = Column(Float, nullable=False)
    vehicle_type = Column(String(50), default="rmc_truck")
    vehicle_id = Column(String(100), nullable=True)
    priority = Column(String(20), default="normal")
    avoid_options = Column(JSON, nullable=True)

    # Current route state
    current_polyline = Column(Text, nullable=True)  # may differ from original after reroute
    original_distance_meters = Column(Float, nullable=True)
    original_duration_seconds = Column(Float, nullable=True)
    remaining_distance_meters = Column(Float, nullable=True)
    remaining_duration_seconds = Column(Float, nullable=True)
    current_traffic_delay = Column(Float, nullable=True)  # latest traffic delay in seconds

    # Reroute tracking
    reroute_count = Column(Integer, default=0)
    last_reroute_at = Column(DateTime(timezone=True), nullable=True)
    last_traffic_check_at = Column(DateTime(timezone=True), nullable=True)

    # ETA
    estimated_arrival = Column(DateTime(timezone=True), nullable=True)

    # ── RMC-specific fields ──────────────────────────────────────────
    # Concrete load lifecycle — the 90-minute countdown
    batch_time = Column(DateTime(timezone=True), nullable=True)       # when concrete was batched
    load_expiry_time = Column(DateTime(timezone=True), nullable=True) # calculated expiry
    load_status = Column(String(20), default="fresh")                 # fresh|warning|critical|expired
    mix_code = Column(String(50), nullable=True)                      # e.g. "30MPa/20/120"
    concrete_grade = Column(String(20), nullable=True)                # e.g. "30MPa"
    volume_m3 = Column(Float, nullable=True)                          # load volume
    retarder_added = Column(Boolean, default=False)                   # chemical retarder applied?
    retarder_extension_minutes = Column(Integer, default=0)           # extension granted
    load_max_life_minutes = Column(Integer, default=90)               # grade-adjusted max life
    plant_id = Column(String(50), nullable=True)                      # dispatching plant
    pour_duration_minutes = Column(Integer, nullable=True)            # expected pour time on site

    # ── Dispatch / scheduling fields ─────────────────────────────────
    scheduled_at = Column(DateTime(timezone=True), nullable=True)     # planned departure time
    concrete_mix = Column(String(10), nullable=True)                  # mix family: GP | HE | RE
    outcome = Column(String(20), nullable=True)                       # succeeded | failed (set on complete)
    prediction_json = Column(Text, nullable=True)                     # JSON snapshot from prediction engine

    # Timestamps
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    events = relationship("TripEvent", back_populates="trip", order_by="TripEvent.created_at")

    def __repr__(self):
        return f"<Trip(id={self.id}, status={self.status}, vehicle={self.vehicle_id})>"


class TripEvent(Base):
    """
    Immutable event log for a trip — full audit trail.
    Every state change, traffic check, reroute, position update is logged.
    """

    __tablename__ = "trip_events"

    id = Column(Integer, primary_key=True, index=True)
    trip_id = Column(Integer, ForeignKey("trips.id"), nullable=False, index=True)

    # Event type
    event_type = Column(
        String(30), nullable=False, index=True,
        # trip_started | trip_paused | trip_resumed | trip_completed | trip_cancelled
        # position_update | traffic_check | reroute_suggested | reroute_applied
        # eta_updated | delay_detected | delay_cleared
    )

    # Event data (flexible JSON payload)
    data = Column(JSON, nullable=True)
    # Examples:
    #   position_update:    {"lat": -36.8, "lng": 174.7, "heading": 180}
    #   traffic_check:      {"delay_seconds": 300, "condition": "heavy"}
    #   reroute_suggested:  {"reason": "...", "new_distance": 12000, "new_duration": 900, "polyline": "..."}
    #   reroute_applied:    {"reroute_number": 2, "old_duration": 1200, "new_duration": 900}
    #   delay_detected:     {"delay_minutes": 8, "severity": "moderate"}

    # Position at time of event
    lat = Column(Float, nullable=True)
    lng = Column(Float, nullable=True)

    # Timestamp
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationship
    trip = relationship("Trip", back_populates="events")

    def __repr__(self):
        return f"<TripEvent(trip_id={self.trip_id}, type={self.event_type})>"
