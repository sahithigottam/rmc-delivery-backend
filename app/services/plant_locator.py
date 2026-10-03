"""
PlantLocator — Find the nearest or best concrete batching plant.

Two modes — choose based on what you need:

  1. nearest_by_distance()  — FREE, instant, no API call.
     Uses the Haversine formula on hardcoded plant coordinates.
     Good for: showing a rough "closest plant" suggestion,
               pre-filtering candidates before calling Google.

  2. nearest_by_distance() is called FIRST to get the top N candidates,
     then PredictionService.best_plant() calls Google Directions only
     for those N plants — not all 20.

Why this matters:
  - All 20 plants → Google = 20 API calls ($0.10 per request)
  - Top 3 by distance → Google = 3 API calls ($0.015 per request)
  - Savings: 85% fewer Google calls, same accuracy

Data source:
  config/plants.json — real verified coordinates from Google Places API.
  Static file, no API dependency, loads once at startup.
"""
from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

# Path to plant data — always relative to repo root
_PLANTS_JSON = Path(__file__).parent.parent.parent / "config" / "plants.json"


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """
    Straight-line distance between two coordinates in kilometres.
    Uses Haversine formula — accurate to within ~0.5% for Auckland distances.
    No external library needed.
    """
    R = 6371.0  # Earth radius km
    d_lat = math.radians(lat2 - lat1)
    d_lng = math.radians(lng2 - lng1)
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(d_lng / 2) ** 2
    )
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


class PlantLocator:
    """
    Finds the nearest concrete batching plants for a given job site.

    Loads config/plants.json once on first use (lazy singleton pattern).
    All distance calculations run locally — zero API calls, zero cost.

    Usage:
        locator = PlantLocator()

        # Get all plants sorted by distance
        ranked = locator.nearest_by_distance(lat=-36.8485, lng=174.7633)

        # Get top 3 only (for passing to Google Directions)
        top3 = locator.nearest_by_distance(lat=-36.8485, lng=174.7633, top_n=3)

        # Get a single plant by ID
        plant = locator.get_by_id("holcim_avondale")

        # Get all addresses for a brand
        allied = locator.get_by_brand("Allied")
    """

    _plants: Optional[List[dict]] = None   # class-level cache — load once

    def __init__(self):
        if PlantLocator._plants is None:
            PlantLocator._plants = self._load()

    @staticmethod
    def _load() -> List[dict]:
        try:
            with open(_PLANTS_JSON, encoding="utf-8") as f:
                plants = json.load(f)
            active = [p for p in plants if p.get("active", True)]
            logger.info(f"PlantLocator: loaded {len(active)} active plants from {_PLANTS_JSON.name}")
            return active
        except FileNotFoundError:
            logger.error(f"PlantLocator: {_PLANTS_JSON} not found")
            return []
        except json.JSONDecodeError as e:
            logger.error(f"PlantLocator: invalid JSON in plants.json — {e}")
            return []

    @property
    def plants(self) -> List[dict]:
        return PlantLocator._plants or []

    def nearest_by_distance(
        self,
        lat: float,
        lng: float,
        top_n: Optional[int] = None,
        brand: Optional[str] = None,
        region: Optional[str] = None,
    ) -> List[dict]:
        """
        Return plants sorted by straight-line distance to the job site.
        FREE — no API calls, runs in microseconds.

        Args:
            lat, lng  : Job site coordinates
            top_n     : Return only top N closest (None = all)
            brand     : Filter by brand  e.g. "Holcim", "Allied", "Firth"
            region    : Filter by region e.g. "south", "north_shore", "west"

        Returns:
            List of plant dicts, each with an added "distance_km" field,
            sorted nearest first.

        Example:
            [
              {"id": "firth_manukau", "name": "Firth Manukau",
               "distance_km": 1.2, "lat": ..., "lng": ..., ...},
              ...
            ]
        """
        candidates = self.plants

        if brand:
            candidates = [p for p in candidates if p.get("brand", "").lower() == brand.lower()]
        if region:
            candidates = [p for p in candidates if p.get("region", "") == region]

        scored = []
        for plant in candidates:
            dist = _haversine_km(lat, lng, plant["lat"], plant["lng"])
            scored.append({**plant, "distance_km": round(dist, 2)})

        scored.sort(key=lambda p: p["distance_km"])

        return scored[:top_n] if top_n else scored

    def get_by_id(self, plant_id: str) -> Optional[dict]:
        """Get a plant by its unique ID."""
        return next((p for p in self.plants if p["id"] == plant_id), None)

    def get_by_brand(self, brand: str) -> List[dict]:
        """Get all active plants for a specific brand."""
        return [p for p in self.plants if p.get("brand", "").lower() == brand.lower()]

    def all_addresses(self, top_n: Optional[int] = None) -> List[str]:
        """Return all plant addresses as strings (for passing to Google Directions)."""
        plants = self.plants[:top_n] if top_n else self.plants
        return [p["address"] for p in plants]

    def candidates_for_job(
        self,
        job_lat: float,
        job_lng: float,
        concrete_limit_minutes: int = 90,
        truck_speed_factor: float = 1.08,
        top_n: int = 5,
    ) -> List[dict]:
        """
        Return the most viable plant candidates for a job site.

        Filters out plants that are physically too far away to deliver
        within the concrete limit — no point calling Google for them.

        Logic:
          straight-line distance / avg road speed (40 km/h) × truck factor
          → rough time estimate. Discard if already > 80% of the limit.

        Args:
            job_lat, job_lng      : Job site coordinates
            concrete_limit_minutes: NZS 3109 window (90/60/120 min)
            truck_speed_factor    : 1.08× for RMC trucks (NZTA)
            top_n                 : Max candidates to return

        Returns:
            Top N plants most likely to make the delivery window,
            sorted by straight-line distance.
        """
        AVG_ROAD_SPEED_KMH = 40.0   # Conservative average for Auckland roads
        MAX_FRACTION = 0.80         # Discard if rough ETA > 80% of limit

        max_rough_minutes = concrete_limit_minutes * MAX_FRACTION
        max_km = (max_rough_minutes / 60.0) * AVG_ROAD_SPEED_KMH / truck_speed_factor

        ranked = self.nearest_by_distance(job_lat, job_lng)
        viable = [p for p in ranked if p["distance_km"] <= max_km]

        if not viable:
            # Fallback: return top 3 even if distance is marginal
            viable = ranked[:3]
            logger.warning(
                f"No plants within {max_km:.0f} km of ({job_lat:.4f}, {job_lng:.4f}). "
                f"Returning {len(viable)} closest regardless."
            )

        result = viable[:top_n]
        logger.info(
            f"PlantLocator: {len(result)} candidates for job at "
            f"({job_lat:.4f}, {job_lng:.4f}) within {concrete_limit_minutes} min window"
        )
        return result
