"""
WeatherService — Real-time weather data from Open-Meteo API.

Free, no API key, no rate limits for moderate use.
Returns structured weather data with RMC-specific delivery impact factors.

RMC-relevant outputs:
  - speed_multiplier    : travel time multiplier (rain/snow slow traffic)
  - concrete_setting_factor : NZS 3109 temperature adjustment for setting time
  - rain_delay_risk     : "high" if heavy rain expected at job site
  - pump_truck_safe     : False if wind gusts > 60 km/h
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# WMO Weather Code → human-readable condition
WMO_CONDITIONS = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Icy fog",
    51: "Light drizzle", 53: "Moderate drizzle", 55: "Heavy drizzle",
    61: "Light rain", 63: "Moderate rain", 65: "Heavy rain",
    71: "Light snow", 73: "Moderate snow", 75: "Heavy snow",
    77: "Snow grains",
    80: "Light showers", 81: "Moderate showers", 82: "Violent showers",
    85: "Snow showers", 86: "Heavy snow showers",
    95: "Thunderstorm", 96: "Thunderstorm with hail", 99: "Thunderstorm heavy hail",
}

PUMP_TRUCK_MAX_WIND_KMPH = 60.0  # km/h gust limit for safe pump operation


class WeatherService:
    """
    Fetches current and near-term forecast weather from Open-Meteo.
    Translates raw weather data into RMC delivery impact factors.
    """

    BASE_URL = "https://api.open-meteo.com/v1/forecast"

    def __init__(self):
        self._client = httpx.AsyncClient(timeout=15.0)

    async def get_weather(
        self,
        lat: float,
        lng: float,
        hours_ahead: int = 3,
    ) -> dict:
        """
        Get weather at a location and compute RMC delivery impact.

        Args:
            lat, lng      : Coordinates of the job site (destination)
            hours_ahead   : How many hours ahead to include in forecast

        Returns:
            {
              "current": {condition, temperature_c, rain_mm, wind_kmh, wind_gusts_kmh},
              "delivery_impact": {
                  speed_multiplier, concrete_setting_factor,
                  rain_delay_risk, pump_truck_safe
              },
              "forecast_hours": [ {time, condition, speed_multiplier}, ... ]
            }
        """
        try:
            params = {
                "latitude":            lat,
                "longitude":           lng,
                "hourly":              "temperature_2m,precipitation,windspeed_10m,windgusts_10m,weathercode",
                "forecast_days":       1,
                "timezone":            "Pacific/Auckland",
                "windspeed_unit":      "kmh",
            }
            resp = await self._client.get(self.BASE_URL, params=params, timeout=10.0)
            resp.raise_for_status()
            data = resp.json()

            return self._parse_response(data, hours_ahead)

        except httpx.TimeoutException:
            logger.warning("WeatherService: request timed out — using safe defaults")
            return self._safe_default()
        except Exception as e:
            logger.warning(f"WeatherService: fetch failed ({e}) — using safe defaults")
            return self._safe_default()

    def _parse_response(self, data: dict, hours_ahead: int) -> dict:
        """Parse Open-Meteo response into RMC delivery impact dict."""
        hourly = data.get("hourly", {})
        times        = hourly.get("time", [])
        temps        = hourly.get("temperature_2m", [])
        precip       = hourly.get("precipitation", [])
        wind         = hourly.get("windspeed_10m", [])
        gusts        = hourly.get("windgusts_10m", [])
        codes        = hourly.get("weathercode", [])

        # Find the current hour index
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:00")
        try:
            current_idx = next(
                i for i, t in enumerate(times) if t.startswith(now_str[:13])
            )
        except StopIteration:
            current_idx = 0

        # Current conditions
        temp_c       = temps[current_idx]  if temps  else 20.0
        rain_mm      = precip[current_idx] if precip else 0.0
        wind_kmh     = wind[current_idx]   if wind   else 0.0
        gust_kmh     = gusts[current_idx]  if gusts  else 0.0
        wmo_code     = codes[current_idx]  if codes  else 0
        condition    = WMO_CONDITIONS.get(wmo_code, "Unknown")

        # ── RMC delivery impact calculations ──────────────────────────

        # Speed multiplier — how much rain slows traffic
        if rain_mm > 10.0:
            speed_multiplier = 1.35    # Heavy rain — major slowdown
            rain_risk = "high"
        elif rain_mm > 5.0:
            speed_multiplier = 1.20    # Moderate rain
            rain_risk = "moderate"
        elif rain_mm > 1.0:
            speed_multiplier = 1.10    # Light rain
            rain_risk = "low"
        elif wmo_code in (71, 73, 75, 85, 86):
            speed_multiplier = 1.50    # Snow — rare in Auckland but affects Hamilton/Tauranga
            rain_risk = "high"
        elif wmo_code in (45, 48):
            speed_multiplier = 1.20    # Fog
            rain_risk = "low"
        else:
            speed_multiplier = 1.00    # Clear
            rain_risk = "none"

        # NZS 3109 concrete setting factor (temperature adjustment)
        # < 10°C: concrete sets slower → more time allowed
        # 25-30°C: concrete sets normally
        # > 30°C: hot weather limit — NZS 3109 reduces window to 60 min
        if temp_c >= 30.0:
            concrete_setting_factor = 0.667   # 90 → 60 min (NZS 3109 hot weather)
        elif temp_c >= 25.0:
            concrete_setting_factor = 0.833   # 90 → 75 min
        elif temp_c <= 10.0:
            concrete_setting_factor = 1.10    # 90 → 99 min (slower setting in cold)
        else:
            concrete_setting_factor = 1.00    # Standard

        pump_truck_safe = gust_kmh < PUMP_TRUCK_MAX_WIND_KMPH

        # ── Forecast for delivery window ──────────────────────────────
        forecast_hours = []
        for i in range(current_idx + 1, min(current_idx + 1 + hours_ahead, len(times))):
            hr_rain = precip[i] if precip else 0.0
            hr_code = codes[i]  if codes  else 0
            hr_mult = (
                1.35 if hr_rain > 10.0
                else 1.20 if hr_rain > 5.0
                else 1.10 if hr_rain > 1.0
                else 1.00
            )
            forecast_hours.append({
                "time":             times[i],
                "condition":        WMO_CONDITIONS.get(hr_code, "Unknown"),
                "rain_mm":          hr_rain,
                "speed_multiplier": hr_mult,
            })

        return {
            "current": {
                "condition":      condition,
                "temperature_c":  temp_c,
                "rain_mm":        rain_mm,
                "wind_kmh":       wind_kmh,
                "wind_gusts_kmh": gust_kmh,
                "wmo_code":       wmo_code,
            },
            "delivery_impact": {
                "speed_multiplier":       speed_multiplier,
                "concrete_setting_factor": concrete_setting_factor,
                "rain_delay_risk":        rain_risk,
                "pump_truck_safe":        pump_truck_safe,
            },
            "forecast_hours": forecast_hours,
        }

    @staticmethod
    def _safe_default() -> dict:
        """Conservative defaults used when the API is unreachable."""
        return {
            "current": {
                "condition": "Unknown (API unavailable)",
                "temperature_c": 20.0,
                "rain_mm": 0.0,
                "wind_kmh": 0.0,
                "wind_gusts_kmh": 0.0,
                "wmo_code": 0,
            },
            "delivery_impact": {
                "speed_multiplier": 1.0,
                "concrete_setting_factor": 1.0,
                "rain_delay_risk": "unknown",
                "pump_truck_safe": True,
            },
            "forecast_hours": [],
        }

    async def close(self):
        """Close the HTTP client. Called on app shutdown."""
        await self._client.aclose()
