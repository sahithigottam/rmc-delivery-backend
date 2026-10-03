"""
Immutable value objects — core domain concepts that have no identity, only values.

All value objects are frozen (immutable) dataclasses:
  - Equality is structural (two LoadTimers with the same fields are equal)
  - No side effects, no I/O, no framework dependencies
  - Thread-safe by design (immutable)
  - State changes produce new instances (e.g. LoadTimer.with_retarder())

Value objects:
  LoadTimer       — 90-minute countdown for a concrete load
  ConcreteSpec    — mix code, grade, volume, aggregate size
  TravelEstimate  — distance/duration result from route calculation
  DeliveryWindow  — time window for a delivery (batch → departure → arrival)
  Plant           — RMC batching plant (fixed location, capacity, grades)
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

from app.domain.enums import ConcreteGrade, LoadStatus


# ─────────────────────────────────────────────────────────────────────────────
# LoadTimer — the heartbeat of every RMC delivery
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class LoadTimer:
    """
    Tracks the usable life of a concrete load from batch time.

    The 90-minute rule: concrete must be poured within 90 minutes of
    batching (NZS 3109 / NZ Ready-Mixed Concrete practice). Chemical
    retarders can extend this by 30-60 minutes depending on the mix
    design and ambient temperature.

    Thresholds (configurable per grade):
      FRESH:    0 → warning_at_minutes       (default 60)
      WARNING:  warning_at → critical_at      (default 75)
      CRITICAL: critical_at → max_life        (default 90)
      EXPIRED:  beyond max_life

    All times are in UTC. The timer never mutates — state changes
    produce new LoadTimer instances via with_retarder().
    """

    batch_time: datetime
    max_life_minutes: int = 90
    warning_at_minutes: int = 60
    critical_at_minutes: int = 75
    retarder_added: bool = False
    retarder_extension_minutes: int = 0

    # ── Effective limits (accounting for retarder) ───────────────────

    @property
    def effective_max_minutes(self) -> int:
        """Max life including any retarder extension."""
        ext = self.retarder_extension_minutes if self.retarder_added else 0
        return self.max_life_minutes + ext

    @property
    def effective_warning_minutes(self) -> int:
        ext = self.retarder_extension_minutes if self.retarder_added else 0
        return self.warning_at_minutes + ext

    @property
    def effective_critical_minutes(self) -> int:
        ext = self.retarder_extension_minutes if self.retarder_added else 0
        return self.critical_at_minutes + ext

    # ── Time calculations ────────────────────────────────────────────

    @property
    def expiry_time(self) -> datetime:
        """Absolute UTC time when the load expires."""
        return self.batch_time + timedelta(minutes=self.effective_max_minutes)

    @property
    def elapsed_minutes(self) -> float:
        """Minutes elapsed since batch."""
        batch_utc = self.batch_time.replace(tzinfo=timezone.utc) if self.batch_time.tzinfo is None else self.batch_time
        delta = datetime.now(timezone.utc) - batch_utc
        return max(0.0, delta.total_seconds() / 60.0)

    @property
    def minutes_remaining(self) -> float:
        """Minutes until expiry. Negative means already expired."""
        return self.effective_max_minutes - self.elapsed_minutes

    # ── Status evaluation ────────────────────────────────────────────

    @property
    def status(self) -> LoadStatus:
        """Current load status based on elapsed time."""
        elapsed = self.elapsed_minutes
        if elapsed >= self.effective_max_minutes:
            return LoadStatus.EXPIRED
        if elapsed >= self.effective_critical_minutes:
            return LoadStatus.CRITICAL
        if elapsed >= self.effective_warning_minutes:
            return LoadStatus.WARNING
        return LoadStatus.FRESH

    @property
    def is_expired(self) -> bool:
        return self.status == LoadStatus.EXPIRED

    @property
    def is_at_risk(self) -> bool:
        """True if load is in WARNING or CRITICAL status."""
        return self.status in (LoadStatus.WARNING, LoadStatus.CRITICAL)

    # ── Retarder logic ───────────────────────────────────────────────

    def can_retarder_help(self, additional_minutes_needed: float) -> bool:
        """Check if adding retarder would extend life enough."""
        if self.retarder_added:
            return False  # already used — can't double-dose
        # Assume standard 45-minute extension
        potential_remaining = (self.max_life_minutes + 45) - self.elapsed_minutes
        return potential_remaining >= additional_minutes_needed

    def with_retarder(self, extension_minutes: int = 45) -> LoadTimer:
        """Return a new LoadTimer with retarder applied (immutable)."""
        return LoadTimer(
            batch_time=self.batch_time,
            max_life_minutes=self.max_life_minutes,
            warning_at_minutes=self.warning_at_minutes,
            critical_at_minutes=self.critical_at_minutes,
            retarder_added=True,
            retarder_extension_minutes=extension_minutes,
        )

    # ── Serialization ────────────────────────────────────────────────

    def snapshot(self) -> dict:
        """Serialize current state for event logging / SSE payloads."""
        return {
            "batch_time": self.batch_time.isoformat(),
            "elapsed_minutes": round(self.elapsed_minutes, 1),
            "minutes_remaining": round(self.minutes_remaining, 1),
            "effective_max_minutes": self.effective_max_minutes,
            "status": self.status.value,
            "retarder_added": self.retarder_added,
            "retarder_extension_minutes": self.retarder_extension_minutes,
            "expiry_time": self.expiry_time.isoformat(),
        }


# ─────────────────────────────────────────────────────────────────────────────
# ConcreteSpec — what's in the truck
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ConcreteSpec:
    """
    Specification for a concrete mix — what's being delivered.

    NZ convention: mix code format is "{strength}MPa/{aggregate}/{slump}"
    e.g. "30MPa/20/120" = 30 MPa strength, 20mm aggregate, 120mm slump.

    High-grade concrete (≥40 MPa) has tighter delivery windows because
    it hydrates faster and is less forgiving of delays.
    """

    mix_code: str = "25MPa/20/100"
    grade: ConcreteGrade = ConcreteGrade.MPA_25
    aggregate_size_mm: int = 20
    slump_mm: int = 100
    volume_m3: float = 6.0
    requires_retarder: bool = False
    special_requirements: Optional[str] = None

    HIGH_STRENGTH_GRADES: Tuple[ConcreteGrade, ...] = field(
        default=(ConcreteGrade.MPA_40, ConcreteGrade.MPA_45, ConcreteGrade.MPA_50),
        repr=False,
        compare=False,
    )

    @property
    def is_high_grade(self) -> bool:
        """High-grade mixes have tighter time limits."""
        return self.grade in self.HIGH_STRENGTH_GRADES

    @property
    def recommended_max_life_minutes(self) -> int:
        """
        Grade-adjusted maximum load life.

        Standard: 90 minutes (NZS 3109 guideline)
        High-grade (≥40 MPa): 75 minutes (faster hydration)
        """
        if self.is_high_grade:
            return 75
        return 90


# ─────────────────────────────────────────────────────────────────────────────
# TravelEstimate — route calculation result
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TravelEstimate:
    """
    Result of a route time/distance calculation.

    Separates base duration from traffic delay so we can reason about
    how much of the travel time is "expected" vs "congestion penalty".
    """

    distance_meters: float
    duration_seconds: float
    traffic_delay_seconds: float = 0.0
    calculated_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    @property
    def total_seconds(self) -> float:
        """Duration including traffic delay."""
        return self.duration_seconds + self.traffic_delay_seconds

    @property
    def total_minutes(self) -> float:
        return self.total_seconds / 60.0

    @property
    def distance_km(self) -> float:
        return self.distance_meters / 1000.0


# ─────────────────────────────────────────────────────────────────────────────
# DeliveryWindow — when must concrete arrive?
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class DeliveryWindow:
    """
    Time window for a delivery — works backwards from pour time.

    Calculation:
      latest_batch_time = pour_time - travel_time - buffer - loading_time
      earliest_departure = latest_batch_time + loading_time

    This tells the batching plant WHEN to start mixing so the concrete
    arrives on site with enough remaining life.
    """

    requested_pour_time: datetime
    travel_estimate: TravelEstimate
    buffer_minutes: int = 15
    loading_minutes: int = 5

    @property
    def latest_batch_time(self) -> datetime:
        """Latest time the plant can start batching and still deliver on time."""
        total_lead = self.travel_estimate.total_minutes + self.buffer_minutes + self.loading_minutes
        return self.requested_pour_time - timedelta(minutes=total_lead)

    @property
    def earliest_departure(self) -> datetime:
        """Earliest the truck should leave the plant after loading."""
        return self.latest_batch_time + timedelta(minutes=self.loading_minutes)

    @property
    def on_time_arrival(self) -> datetime:
        """Target arrival time (before pour, with buffer)."""
        return self.requested_pour_time - timedelta(minutes=self.buffer_minutes)

    def is_achievable(self, from_time: Optional[datetime] = None) -> bool:
        """Can we still make this delivery window?"""
        now = from_time or datetime.now(timezone.utc)
        return now < self.latest_batch_time

    def slack_minutes(self, from_time: Optional[datetime] = None) -> float:
        """How many minutes of slack remain before we miss the window."""
        now = from_time or datetime.now(timezone.utc)
        delta = self.latest_batch_time - now
        return delta.total_seconds() / 60.0


# ─────────────────────────────────────────────────────────────────────────────
# Plant — RMC batching plant
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Plant:
    """
    RMC batching plant — a fixed location that produces concrete.

    Plants have:
      - Geographic position (for distance/route calculations)
      - Production capacity (m³/hour)
      - Operational status (can go offline for maintenance)
      - Available grades (some plants can't produce high-strength)
    """

    plant_id: str
    name: str
    lat: float
    lng: float
    capacity_m3_per_hour: float = 60.0
    is_operational: bool = True
    available_grades: Tuple[ConcreteGrade, ...] = ()

    def can_produce(self, spec: ConcreteSpec) -> bool:
        """Check if this plant can produce the requested concrete."""
        if not self.is_operational:
            return False
        if self.available_grades and spec.grade not in self.available_grades:
            return False
        return True

    def batching_time_minutes(self, volume_m3: float) -> float:
        """Estimate how long it takes to batch a given volume."""
        if self.capacity_m3_per_hour <= 0:
            return float("inf")
        return (volume_m3 / self.capacity_m3_per_hour) * 60.0

    def distance_to(self, lat: float, lng: float) -> float:
        """Haversine distance in km to a target location."""
        R = 6371.0
        d_lat = math.radians(lat - self.lat)
        d_lng = math.radians(lng - self.lng)
        a = (
            math.sin(d_lat / 2) ** 2
            + math.cos(math.radians(self.lat))
            * math.cos(math.radians(lat))
            * math.sin(d_lng / 2) ** 2
        )
        return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
