"""Google Maps API integration service"""
import hashlib
import logging
from typing import Dict, List, Optional, Tuple

import httpx
from cachetools import TTLCache

from app.config import settings

logger = logging.getLogger(__name__)

# Cache configuration
GEOCODE_CACHE_TTL = 86400  # 24 hours - addresses don't change often
GEOCODE_CACHE_SIZE = 1000  # Max number of cached addresses

DIRECTIONS_CACHE_TTL = 600  # 10 minutes — traffic doesn't change meaningfully in < 10 min
DIRECTIONS_CACHE_SIZE = 500  # Max number of cached routes


class GoogleMapsService:
    """Service for Google Maps API operations"""

    BASE_URL = "https://maps.googleapis.com/maps/api"

    def __init__(self, api_key: str = settings.google_maps_api_key):
        self.api_key = api_key
        self.client = httpx.AsyncClient(timeout=30.0)
        
        # Initialize caches
        self._geocode_cache: TTLCache = TTLCache(maxsize=GEOCODE_CACHE_SIZE, ttl=GEOCODE_CACHE_TTL)
        self._directions_cache: TTLCache = TTLCache(maxsize=DIRECTIONS_CACHE_SIZE, ttl=DIRECTIONS_CACHE_TTL)
        
        # Cache statistics
        self._cache_stats = {
            "geocode_hits": 0,
            "geocode_misses": 0,
            "directions_hits": 0,
            "directions_misses": 0,
        }

    def _get_directions_cache_key(
        self,
        start_lat: float,
        start_lng: float,
        end_lat: float,
        end_lng: float,
        departure_time: Optional[int] = None,
        alternatives: bool = False,
        avoid: Optional[List[str]] = None,
        waypoints: Optional[List[Tuple[float, float]]] = None,
        coarse_start: bool = False,
    ) -> str:
        """Generate a cache key for directions request.
        
        coarse_start=True snaps the start coordinates to a ~1.1km grid so that
        small truck movements (e.g. during background traffic checks) reuse cached
        results instead of triggering a new API call.
        """
        if coarse_start:
            # Round to 2 decimal places ≈ 111m per unit → ~1.1km grid cell
            s_lat = round(start_lat, 2)
            s_lng = round(start_lng, 2)
        else:
            s_lat = start_lat
            s_lng = start_lng

        key_parts = [
            f"{s_lat:.5f}" if not coarse_start else f"{s_lat:.2f}",
            f"{s_lng:.5f}" if not coarse_start else f"{s_lng:.2f}",
            f"{end_lat:.5f}",
            f"{end_lng:.5f}",
        ]
        # Don't include exact departure_time in key, use time windows (15 min buckets)
        if departure_time:
            time_bucket = departure_time // 900 * 900  # 15-minute buckets
            key_parts.append(str(time_bucket))
        
        # Include alternatives flag
        key_parts.append(f"alt:{alternatives}")
        
        # Include avoid options
        if avoid:
            key_parts.append(f"avoid:{','.join(sorted(avoid))}")
        
        # Include waypoints
        if waypoints:
            wp_str = "|".join([f"{lat:.5f},{lng:.5f}" for lat, lng in waypoints])
            key_parts.append(f"wp:{wp_str}")
        
        key_string = "|".join(key_parts)
        return hashlib.md5(key_string.encode()).hexdigest()

    def _get_geocode_cache_key(self, address: str) -> str:
        """Generate a cache key for geocode request"""
        # Normalize address for better cache hits
        normalized = address.lower().strip()
        return hashlib.md5(normalized.encode()).hexdigest()

    def get_cache_stats(self) -> Dict:
        """Return cache statistics"""
        return {
            **self._cache_stats,
            "geocode_cache_size": len(self._geocode_cache),
            "directions_cache_size": len(self._directions_cache),
        }

    async def get_directions(
        self,
        start_lat: float,
        start_lng: float,
        end_lat: float,
        end_lng: float,
        departure_time: Optional[int] = None,
        traffic_model: str = "best_guess",
        alternatives: bool = False,
        avoid: Optional[List[str]] = None,
        waypoints: Optional[List[Tuple[float, float]]] = None,
        coarse_start: bool = False,
    ) -> Dict:
        """
        Get directions between two points using Google Directions API.
        Results are cached for 5 minutes.

        Args:
            start_lat, start_lng: Starting point coordinates
            end_lat, end_lng: Ending point coordinates
            departure_time: Unix timestamp for traffic calculation
            traffic_model: "best_guess", "pessimistic", or "optimistic"
            alternatives: If True, request alternative routes
            avoid: List of features to avoid: "tolls", "highways", "ferries"
            waypoints: List of (lat, lng) tuples to route through (for rerouting around obstacles)

        Returns:
            Dictionary with route data including distance, duration, and steps
        """
        # Check cache first
        cache_key = self._get_directions_cache_key(
            start_lat, start_lng, end_lat, end_lng, departure_time,
            alternatives, avoid, waypoints, coarse_start=coarse_start
        )
        
        if cache_key in self._directions_cache:
            self._cache_stats["directions_hits"] += 1
            logger.info(f"Directions cache HIT for route {start_lat},{start_lng} -> {end_lat},{end_lng}")
            return self._directions_cache[cache_key]
        
        self._cache_stats["directions_misses"] += 1
        logger.info(f"Directions cache MISS for route {start_lat},{start_lng} -> {end_lat},{end_lng}")
        
        try:
            url = f"{self.BASE_URL}/directions/json"

            params = {
                "origin": f"{start_lat},{start_lng}",
                "destination": f"{end_lat},{end_lng}",
                "key": self.api_key,
                "mode": "driving",
            }

            if departure_time:
                params["departure_time"] = str(departure_time)
                params["traffic_model"] = traffic_model
            
            # Request alternative routes
            if alternatives:
                params["alternatives"] = "true"
            
            # Add avoidance options (tolls, highways, ferries)
            if avoid:
                valid_avoid = [a for a in avoid if a in ["tolls", "highways", "ferries"]]
                if valid_avoid:
                    params["avoid"] = "|".join(valid_avoid)
            
            # Add waypoints for rerouting around obstacles
            if waypoints:
                wp_str = "|".join([f"{lat},{lng}" for lat, lng in waypoints])
                params["waypoints"] = wp_str

            response = await self.client.get(url, params=params)
            response.raise_for_status()
            result = response.json()
            
            # Cache the result
            self._directions_cache[cache_key] = result
            
            return result

        except httpx.HTTPError as e:
            logger.error(f"HTTP error calling Google Directions API: {e}")
            raise
        except Exception as e:
            logger.error(f"Error calling Google Directions API: {e}")
            raise

    async def geocode_address(self, address: str) -> dict:
        """
        Geocode an address to coordinates using Google Geocoding API.
        Results are cached for 24 hours.

        Args:
            address: Street address or location name

        Returns:
            Dictionary with latitude, longitude, and formatted address
        """
        # Check cache first
        cache_key = self._get_geocode_cache_key(address)
        
        if cache_key in self._geocode_cache:
            self._cache_stats["geocode_hits"] += 1
            logger.info(f"Geocode cache HIT for address: {address}")
            return self._geocode_cache[cache_key]
        
        self._cache_stats["geocode_misses"] += 1
        logger.info(f"Geocode cache MISS for address: {address}")
        
        try:
            url = f"{self.BASE_URL}/geocode/json"

            params = {
                "address": address,
                "key": self.api_key,
                "components": "country:NZ",  # Restrict to New Zealand
            }

            response = await self.client.get(url, params=params)
            response.raise_for_status()
            data = response.json()

            if data.get("status") != "OK" or not data.get("results"):
                logger.error(f"Geocoding failed for address: {address}. Response: {data}")
                raise ValueError(f"Could not geocode address: {address}")

            result = data["results"][0]
            location = result["geometry"]["location"]
            formatted_address = result.get("formatted_address", address)

            geocode_result = {
                "latitude": location["lat"],
                "longitude": location["lng"],
                "formatted_address": formatted_address,
            }
            
            # Cache the result
            self._geocode_cache[cache_key] = geocode_result
            
            return geocode_result

        except Exception as e:
            logger.error(f"Error geocoding address '{address}': {e}")
            raise

    async def reverse_geocode(
        self, latitude: float, longitude: float
    ) -> str:
        """
        Reverse geocode coordinates to address.

        Args:
            latitude: Latitude coordinate
            longitude: Longitude coordinate

        Returns:
            Formatted address string
        """
        try:
            url = f"{self.BASE_URL}/geocode/json"

            params = {
                "latlng": f"{latitude},{longitude}",
                "key": self.api_key,
            }

            response = await self.client.get(url, params=params)
            response.raise_for_status()
            data = response.json()

            if data.get("status") != "OK" or not data.get("results"):
                return f"{latitude}, {longitude}"

            return data["results"][0].get("formatted_address", f"{latitude}, {longitude}")

        except Exception as e:
            logger.error(f"Error reverse geocoding coordinates: {e}")
            return f"{latitude}, {longitude}"
        """
        Get distance matrix between multiple origins and destinations.

        Args:
            origins: List of (lat, lng) tuples
            destinations: List of (lat, lng) tuples
            departure_time: Unix timestamp for traffic calculation

        Returns:
            Dictionary with distance and duration data
        """
        try:
            url = f"{self.BASE_URL}/distancematrix/json"

            origins_str = "|".join([f"{lat},{lng}" for lat, lng in origins])
            destinations_str = "|".join(
                [f"{lat},{lng}" for lat, lng in destinations]
            )

            params = {
                "origins": origins_str,
                "destinations": destinations_str,
                "key": self.api_key,
                "mode": "driving",
            }

            if departure_time:
                params["departure_time"] = departure_time

            response = await self.client.get(url, params=params)
            response.raise_for_status()
            return response.json()

        except httpx.HTTPError as e:
            logger.error(f"HTTP error calling Distance Matrix API: {e}")
            raise
        except Exception as e:
            logger.error(f"Error calling Distance Matrix API: {e}")
            raise

    async def close(self):
        """Close the httpx client."""
        await self.client.aclose()

    def __del__(self):
        """Cleanup on object deletion"""
        try:
            import asyncio

            asyncio.run(self.close())
        except:
            pass
