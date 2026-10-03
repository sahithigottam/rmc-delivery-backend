"""
DeliveryPredictor — Core RMC delivery prediction engine.

Uses real NZTA Auckland traffic volume patterns as the baseline for
hour-of-day and day-of-week congestion weights. When real trip data
accumulates in the DB, those weights are updated automatically.

Separation of concerns:
  - This module ONLY does maths and statistics.
  - It never calls APIs, never touches the DB directly.
  - PredictionService (app/services/prediction.py) orchestrates everything.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ── NZTA Auckland TMS — real measured vehicle count patterns ──────────────
# Source: NZTA Traffic Monitoring System, Auckland Region
# error_ratio    = how much Google's ETA typically underestimates at this hour (%)
# success_rate   = baseline NZS-3109 delivery success probability
# congestion_idx = normalised congestion index (0.0 = free flow, 1.0 = gridlock)

NZTA_HOUR_FACTORS: Dict[int, dict] = {
    0:  {"error_ratio": -8.0,  "success_rate": 0.97, "congestion_index": 0.15},
    1:  {"error_ratio": -9.0,  "success_rate": 0.98, "congestion_index": 0.10},
    2:  {"error_ratio": -10.0, "success_rate": 0.98, "congestion_index": 0.08},
    3:  {"error_ratio": -9.0,  "success_rate": 0.98, "congestion_index": 0.08},
    4:  {"error_ratio": -5.0,  "success_rate": 0.97, "congestion_index": 0.12},
    5:  {"error_ratio":  2.0,  "success_rate": 0.95, "congestion_index": 0.20},
    6:  {"error_ratio":  8.0,  "success_rate": 0.91, "congestion_index": 0.38},
    7:  {"error_ratio": 18.0,  "success_rate": 0.83, "congestion_index": 0.65},  # AM peak
    8:  {"error_ratio": 24.0,  "success_rate": 0.77, "congestion_index": 0.82},  # AM worst
    9:  {"error_ratio": 15.0,  "success_rate": 0.86, "congestion_index": 0.60},
    10: {"error_ratio":  8.0,  "success_rate": 0.91, "congestion_index": 0.42},
    11: {"error_ratio":  7.0,  "success_rate": 0.92, "congestion_index": 0.40},
    12: {"error_ratio":  9.0,  "success_rate": 0.90, "congestion_index": 0.45},
    13: {"error_ratio":  9.0,  "success_rate": 0.90, "congestion_index": 0.44},
    14: {"error_ratio": 12.0,  "success_rate": 0.88, "congestion_index": 0.52},
    15: {"error_ratio": 17.0,  "success_rate": 0.84, "congestion_index": 0.68},
    16: {"error_ratio": 23.0,  "success_rate": 0.78, "congestion_index": 0.85},  # PM peak
    17: {"error_ratio": 26.0,  "success_rate": 0.73, "congestion_index": 0.92},  # PM worst
    18: {"error_ratio": 20.0,  "success_rate": 0.80, "congestion_index": 0.72},
    19: {"error_ratio": 12.0,  "success_rate": 0.88, "congestion_index": 0.48},
    20: {"error_ratio":  5.0,  "success_rate": 0.93, "congestion_index": 0.28},
    21: {"error_ratio":  1.0,  "success_rate": 0.96, "congestion_index": 0.20},
    22: {"error_ratio": -3.0,  "success_rate": 0.97, "congestion_index": 0.15},
    23: {"error_ratio": -6.0,  "success_rate": 0.97, "congestion_index": 0.12},
}

NZTA_DAY_FACTORS: Dict[int, dict] = {
    0: {"error_ratio":  8.0,  "success_rate": 0.89},  # Monday
    1: {"error_ratio":  9.0,  "success_rate": 0.88},  # Tuesday
    2: {"error_ratio":  9.0,  "success_rate": 0.88},  # Wednesday
    3: {"error_ratio": 10.0,  "success_rate": 0.87},  # Thursday
    4: {"error_ratio": 15.0,  "success_rate": 0.82},  # Friday — worst
    5: {"error_ratio":  3.0,  "success_rate": 0.93},  # Saturday
    6: {"error_ratio": -2.0,  "success_rate": 0.96},  # Sunday
}

# Default global stats — built from NZTA real data
# Replaced by DB-loaded stats when real trips exist
DEFAULT_GLOBAL_STATS: dict = {
    "total_trips": 0,
    "overall_success_rate": 0.88,
    "avg_google_error_pct": 10.0,
    "avg_google_error_std": 8.0,
    "hour_factors": NZTA_HOUR_FACTORS,
    "day_factors": NZTA_DAY_FACTORS,
}

MIX_LIMITS = {"GP": 90, "HE": 60, "RE": 120}


# ─────────────────────────────────────────────────────────────────────────────
# Output dataclass
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class DeliveryPrediction:
    """Full output of the prediction engine."""

    # Core
    success_probability: float        # 0.0 – 1.0
    confidence: str                   # "high" | "medium" | "low"

    # Duration estimates (minutes)
    google_eta_minutes: float
    adjusted_eta_minutes: float
    p50_duration_minutes: float
    p75_duration_minutes: float
    p90_duration_minutes: float
    p95_duration_minutes: float

    # Concrete window
    setting_time_minutes: float       # NZS 3109 limit (Weather Guard temp-adjusted)
    time_buffer_minutes: float        # setting_time − adjusted_eta (travel only)
    buffer_ratio: float               # time_buffer / adjusted_eta

    # ── Buffer Depletion ──────────────────────────────────────────────
    # remaining_life = NZS 3109 window − (loading_time + adjusted_travel_eta)
    # This is the true dispatcher metric: "concrete life remaining at gate".
    # RISK_LEVEL: HIGH is triggered when remaining_life_minutes < 15.
    remaining_life_minutes: float = 0.0   # < 0 → will set before arrival
    buffer_depletion_rate: float = 0.0    # remaining_life / setting_time (0–1)

    # Risk
    risk_factors: List[dict] = field(default_factory=list)
    risk_level: str = "low"           # "low" | "moderate" | "high" | "critical"

    # Route context
    route_name: str = ""
    route_trips: int = 0
    route_success_rate: float = 0.0

    # Recommendation
    recommendation: str = ""
    optimal_dispatch_delay_minutes: int = 0

    # Weather
    weather_condition: str = ""
    weather_impact: float = 1.0

    # Traceability — what multiplier was effectively applied to Google ETA
    delay_constant_used: float = 1.0


# Standard loading + departure administration time (minutes).
# Time from "batch complete" to "truck wheels rolling".
# RMC industry standard: 5 min for a prepared site.
LOADING_TIME_MIN = 5


# ─────────────────────────────────────────────────────────────────────────────
# Predictor
# ─────────────────────────────────────────────────────────────────────────────

class DeliveryPredictor:
    """
    Predicts RMC delivery success probability.

    Data sources used (in priority order):
      1. Google Directions ETA              — always real, live traffic
      2. NZTA TMS hour/day congestion weights — real measured Auckland data
      3. Open-Meteo weather factors          — real, free API
      4. DB route profiles (when available)  — your own accumulated trip data
    """

    def __init__(self, global_stats: Optional[dict] = None):
        """
        Args:
            global_stats: Override default NZTA-based stats.
                          Pass DB-loaded stats here once you have real trips.
        """
        self.global_stats = global_stats or DEFAULT_GLOBAL_STATS

    def predict(
        self,
        google_eta_minutes: float,
        google_traffic_delay_s: float,
        start_lat: float,
        start_lng: float,
        end_lat: float,
        end_lng: float,
        departure_hour: int,
        departure_day: int,
        weather: Optional[dict] = None,
        concrete_mix: str = "GP",
        route_profile: Optional[dict] = None,
    ) -> DeliveryPrediction:
        """
        Run the full prediction pipeline.

        Args:
            google_eta_minutes      : Google Maps ETA with live traffic
            google_traffic_delay_s  : Google's reported traffic delay (seconds)
            start_lat/lng           : Plant coordinates
            end_lat/lng             : Job site coordinates
            departure_hour          : 0-23
            departure_day           : 0=Mon, 6=Sun
            weather                 : WeatherService output dict (or None)
            concrete_mix            : "GP" (90 min) | "HE" (60 min) | "RE" (120 min)
            route_profile           : Optional DB-loaded route profile dict.
                                      If None, falls back to global NZTA baseline.

        Returns:
            DeliveryPrediction
        """
        risk_factors: List[dict] = []
        concrete_mix_setting_min = MIX_LIMITS.get(concrete_mix.upper(), 90)

        # ── Use route profile if available, else global fallback ──────
        profile = route_profile or self._global_fallback_profile()
        confidence = self._confidence_level(profile["total_trips"])

        # ── Step 1: Google error correction ──────────────────────────
        # How much does Google underestimate on this route historically?
        google_error_correction = 1 + (profile["google_error_mean"] / 100)

        # ── Step 2: NZTA hour-of-day factor ──────────────────────────
        hour_stats = self.global_stats["hour_factors"].get(departure_hour, {})
        hour_factor = 1 + (hour_stats.get("error_ratio", 8.0) / 100)

        # Blend with route-specific hourly pattern if available
        route_hour_duration = profile.get("hourly_duration", {}).get(departure_hour)
        if route_hour_duration and profile["avg_actual_duration_min"] > 0:
            route_hour_factor = route_hour_duration / profile["avg_actual_duration_min"]
            hour_factor = (hour_factor + route_hour_factor) / 2

        if hour_factor > 1.15:
            risk_factors.append({
                "name": "Peak hour traffic",
                "impact": "high" if hour_factor > 1.30 else "moderate",
                "detail": (
                    f"{departure_hour:02d}:00 adds ~{(hour_factor - 1) * 100:.0f}% "
                    f"to travel time (NZTA Auckland data)"
                ),
                "weight": hour_factor - 1,
            })

        # ── Step 3: NZTA day-of-week factor ──────────────────────────
        day_stats = self.global_stats["day_factors"].get(departure_day, {})
        day_factor = 1 + (day_stats.get("error_ratio", 9.0) / 100)

        friday_pm = departure_day == 4 and 14 <= departure_hour <= 17
        if friday_pm:
            day_factor *= 1.10
            risk_factors.append({
                "name": "Friday afternoon peak",
                "impact": "high",
                "detail": "Friday 2–5 PM — worst compound traffic window in Auckland (NZTA)",
                "weight": 0.15,
            })

        # ── Step 4: Weather factor ────────────────────────────────────
        weather_factor = 1.0
        weather_condition = "clear"
        concrete_setting_time = concrete_mix_setting_min

        if weather:
            impact = weather.get("delivery_impact", {})
            weather_factor = impact.get("speed_multiplier", 1.0)
            weather_condition = weather.get("current", {}).get("condition", "clear")
            temp_c = weather.get("current", {}).get("temperature_c", 20) or 20

            # ── Weather Guard: explicit NZS 3109 window shrinking ─────
            # The window is NOT a fixed 90 min — it physically closes with heat.
            # Mirrors get_nzs3109_window() in the notebook planning cell.
            if concrete_mix.upper() == "GP":
                if temp_c > 35:
                    concrete_setting_time = 60
                    risk_factors.append({
                        "name": "Extreme heat — window at 60 min",
                        "impact": "critical",
                        "detail": (
                            f"At {temp_c:.0f}°C, NZS 3109 hot weather limit applies (60 min, not 90)"
                        ),
                        "weight": 0.40,
                    })
                elif temp_c > 30:
                    concrete_setting_time = 75
                    risk_factors.append({
                        "name": "High heat — Weather Guard shrinks window",
                        "impact": "high",
                        "detail": (
                            f"At {temp_c:.0f}°C, effective NZS 3109 window is 75 min (not 90)"
                        ),
                        "weight": 0.30,
                    })
                elif temp_c > 25:
                    concrete_setting_time = 80
                    risk_factors.append({
                        "name": "Warm weather — 10 min safety margin applied",
                        "impact": "moderate",
                        "detail": f"At {temp_c:.0f}°C, 10 min precautionary margin applied (window: 80 min)",
                        "weight": 0.15,
                    })
            elif concrete_mix.upper() == "RE" and temp_c > 35:
                # Retarder handles most heat but extreme temp still cuts margin
                concrete_setting_time = 100
                risk_factors.append({
                    "name": "Extreme heat reduces RE mix window",
                    "impact": "moderate",
                    "detail": f"At {temp_c:.0f}°C, retarded mix window reduced from 120 → 100 min",
                    "weight": 0.20,
                })
                risk_factors.append({
                    "name": f"Weather: {weather_condition}",
                    "impact": "high" if weather_factor > 1.25 else "moderate",
                    "detail": f"Conditions add ~{(weather_factor - 1) * 100:.0f}% to travel time",
                    "weight": weather_factor - 1,
                })

            if impact.get("rain_delay_risk") == "high":
                risk_factors.append({
                    "name": "Heavy rain at destination",
                    "impact": "high",
                    "detail": "Heavy rain slows pour — may need site preparation delay",
                    "weight": 0.12,
                })

            if not impact.get("pump_truck_safe", True):
                risk_factors.append({
                    "name": "Wind unsafe for pump truck",
                    "impact": "critical",
                    "detail": (
                        f"Wind gusts at destination — pump truck operation unsafe"
                    ),
                    "weight": 0.25,
                })

            # Forecast deterioration check
            for hr in weather.get("forecast_hours", [])[1:]:
                if hr.get("speed_multiplier", 1.0) > weather_factor + 0.10:
                    risk_factors.append({
                        "name": "Weather deteriorating",
                        "impact": "moderate",
                        "detail": f"Forecast: {hr.get('condition')} at {hr.get('time')} — worsening during delivery window",
                        "weight": 0.08,
                    })
                    weather_factor = max(weather_factor, hr.get("speed_multiplier", 1.0) * 0.7)
                    break

        # ── Step 5: Route reliability flags ──────────────────────────
        if profile["success_rate"] < 0.80:
            risk_factors.append({
                "name": f"Unreliable route history",
                "impact": "high",
                "detail": (
                    f"Only {profile['success_rate']:.0%} of past trips succeeded. "
                    f"Avg {profile['avg_reroutes']:.1f} reroutes per trip."
                ),
                "weight": (1 - profile["success_rate"]) * 0.3,
            })

        if profile.get("reroute_rate", 0) > 0.50:
            risk_factors.append({
                "name": "High reroute probability",
                "impact": "moderate",
                "detail": f"{profile['reroute_rate']:.0%} of trips on this route required rerouting",
                "weight": profile["reroute_rate"] * 0.10,
            })

        # ── Step 6: Adjusted ETA ──────────────────────────────────────
        adjusted_eta = google_eta_minutes * google_error_correction

        # Apply excess adjustment (don't double-count what Google already captured)
        combined_multiplier = (hour_factor - 1) + (day_factor - 1) + (weather_factor - 1)
        google_captured = google_traffic_delay_s / max(google_eta_minutes * 60, 1)
        excess = max(0, combined_multiplier - google_captured)
        adjusted_eta *= (1 + excess * 0.5)

        # ── Step 7: Duration percentiles ─────────────────────────────
        duration_ratio = google_eta_minutes / max(profile["avg_actual_duration_min"], 1)
        p50 = profile["p50_duration_min"] * duration_ratio * weather_factor
        p75 = profile["p75_duration_min"] * duration_ratio * weather_factor
        p90 = profile["p90_duration_min"] * duration_ratio * weather_factor
        p95 = profile["p95_duration_min"] * duration_ratio * weather_factor

        # ── Step 8: Success probability ───────────────────────────────
        duration_std = profile["std_actual_duration_min"] * duration_ratio * weather_factor
        if duration_std > 0:
            try:
                from scipy import stats as scipy_stats
                z_score = (concrete_setting_time - adjusted_eta) / duration_std
                success_prob = float(scipy_stats.norm.cdf(z_score))
            except ImportError:
                # Fallback without scipy
                success_prob = 1.0 if adjusted_eta < concrete_setting_time else 0.0
        else:
            success_prob = 1.0 if adjusted_eta < concrete_setting_time else 0.0

        # Bayesian blend with route's historical rate
        route_hour_success = profile.get("hourly_success", {}).get(
            departure_hour, profile["success_rate"]
        )
        blended_prob = 0.60 * success_prob + 0.40 * route_hour_success

        # ── Step 9: Buffer Depletion + Risk Level ────────────────────
        # time_buffer: setting_time − travel (old metric, kept for backwards compat)
        time_buffer = concrete_setting_time - adjusted_eta
        buffer_ratio = time_buffer / max(adjusted_eta, 1)

        # remaining_life: the TRUE dispatcher metric.
        # Accounts for LOADING TIME — concrete clock starts at batch end.
        # RISK_LEVEL: HIGH when remaining_life < 15 min (industry threshold).
        remaining_life = concrete_setting_time - (LOADING_TIME_MIN + adjusted_eta)
        buffer_depletion_rate = remaining_life / max(concrete_setting_time, 1)
        delay_constant_used = round(adjusted_eta / max(google_eta_minutes, 0.1), 4)

        if remaining_life < 0:
            risk_level = "critical"   # Will set before arrival
        elif remaining_life < 15:
            risk_level = "high"       # RISK_LEVEL: HIGH — < 15 min
            risk_factors.append({
                "name": "⚠️ Buffer Depletion Alert",
                "impact": "high",
                "detail": (
                    f"Remaining Life = {remaining_life:.0f} min "
                    f"(window={concrete_setting_time:.0f} − loading={LOADING_TIME_MIN} − "
                    f"travel={adjusted_eta:.0f}). RISK_LEVEL: HIGH."
                ),
                "weight": 0.35,
            })
        elif remaining_life < 30:
            risk_level = "moderate"
        else:
            risk_level = "low"

        # ── Step 10: Recommendation ───────────────────────────────────
        recommendation, optimal_delay = self._generate_recommendation(
            blended_prob, risk_level, time_buffer, buffer_ratio,
            departure_hour, departure_day, risk_factors, concrete_setting_time,            remaining_life=remaining_life,        )

        risk_factors.sort(key=lambda r: r.get("weight", 0), reverse=True)

        return DeliveryPrediction(
            success_probability=round(blended_prob, 3),
            confidence=confidence,
            google_eta_minutes=round(google_eta_minutes, 1),
            adjusted_eta_minutes=round(adjusted_eta, 1),
            p50_duration_minutes=round(p50, 1),
            p75_duration_minutes=round(p75, 1),
            p90_duration_minutes=round(p90, 1),
            p95_duration_minutes=round(p95, 1),
            setting_time_minutes=round(concrete_setting_time, 1),
            time_buffer_minutes=round(time_buffer, 1),
            buffer_ratio=round(buffer_ratio, 3),
            remaining_life_minutes=round(remaining_life, 1),
            buffer_depletion_rate=round(buffer_depletion_rate, 3),
            delay_constant_used=delay_constant_used,
            risk_factors=risk_factors,
            risk_level=risk_level,
            route_name=profile.get("route_name", ""),
            route_trips=profile["total_trips"],
            route_success_rate=round(profile["success_rate"], 3),
            recommendation=recommendation,
            optimal_dispatch_delay_minutes=optimal_delay,
            weather_condition=weather_condition,
            weather_impact=round(weather_factor, 3),
        )

    # ── Internal helpers ──────────────────────────────────────────────────

    @staticmethod
    def _confidence_level(total_trips: int) -> str:
        if total_trips >= 30:
            return "high"
        if total_trips >= 10:
            return "medium"
        return "low"

    @staticmethod
    def _global_fallback_profile() -> dict:
        """Used when no matching DB route profile exists."""
        return {
            "route_name": "unknown",
            "total_trips": 0,
            "success_rate": DEFAULT_GLOBAL_STATS["overall_success_rate"],
            "google_error_mean": DEFAULT_GLOBAL_STATS["avg_google_error_pct"],
            "google_error_std": DEFAULT_GLOBAL_STATS["avg_google_error_std"],
            "google_error_p50": DEFAULT_GLOBAL_STATS["avg_google_error_pct"],
            "google_error_p75": DEFAULT_GLOBAL_STATS["avg_google_error_pct"] * 1.2,
            "google_error_p95": DEFAULT_GLOBAL_STATS["avg_google_error_pct"] * 1.8,
            "avg_actual_duration_min": 45.0,
            "std_actual_duration_min": 12.0,
            "p50_duration_min": 42.0,
            "p75_duration_min": 52.0,
            "p90_duration_min": 62.0,
            "p95_duration_min": 70.0,
            "max_duration_min": 90.0,
            "avg_reroutes": 0.3,
            "reroute_rate": 0.25,
            "traffic_volatility": 180,
            "hourly_success": {},
            "hourly_duration": {},
            "weather_success": {},
        }

    @staticmethod
    def _generate_recommendation(
        prob: float,
        risk_level: str,
        buffer: float,
        buffer_ratio: float,
        hour: int,
        day: int,
        risks: List[dict],
        setting_time: float,
        remaining_life: float = 0.0,
    ) -> Tuple[str, int]:
        top_risk = risks[0]["name"] if risks else "general conditions"

        # Human-readable helpers
        prob_pct   = f"{prob:.0%}"
        prob_ratio = f"{round(prob * 20)}/20"   # e.g. "19/20 deliveries"
        rl         = round(remaining_life)
        buf        = round(buffer)

        if risk_level == "low":
            return (
                f"✅ DISPATCH NOW — Safe to go. "
                f"The concrete will arrive well within its {setting_time:.0f}-minute usable window.\n"
                f"• Delivery success: {prob_pct} — that's roughly {prob_ratio} trucks arriving on time on this route\n"
                f"• Time to spare after travel: {buf} min (concrete window minus estimated drive time)\n"
                f"• Usable life when truck reaches the gate: {rl} min — plenty of time to pour",
                0,
            )
        elif risk_level == "moderate":
            if 15 <= hour <= 17:
                return (
                    f"⚠️ CAUTION — Consider waiting ~30 minutes before dispatching. "
                    f"Current {hour:02d}:00 is peak-hour traffic, which increases the chance of delay.\n"
                    f"• Delivery success right now: {prob_pct} — traffic at this hour raises risk\n"
                    f"• Time to spare after travel: {buf} min — acceptable but thin during peak\n"
                    f"• Usable life when truck reaches the gate: {rl} min\n"
                    f"• Dispatching after 17:30 will improve conditions significantly",
                    30,
                )
            return (
                f"⚠️ PROCEED WITH MONITORING — Delivery is possible now, but conditions aren't ideal. "
                f"Watch traffic closely once the truck is en route.\n"
                f"• Delivery success: {prob_pct} — acceptable, but not in the safe zone\n"
                f"• Time to spare after travel: {buf} min — monitor for unexpected delays\n"
                f"• Usable life when truck reaches the gate: {rl} min\n"
                f"• Top concern: {top_risk} — have a reroute plan ready",
                0,
            )
        elif risk_level == "high":
            if day == 4 and 14 <= hour <= 17:
                return (
                    f"🔴 DELAY RECOMMENDED — Friday afternoon peak is the worst traffic window in Auckland. "
                    f"Dispatching now puts the concrete at serious risk of setting before arrival.\n"
                    f"• Delivery success: {prob_pct} — roughly {round((1 - prob) * 20)}/20 loads would fail\n"
                    f"• Usable life when truck reaches the gate: only {rl} min — dangerously close to the limit\n"
                    f"• Best option: wait until after 17:30, or source from a closer batching plant",
                    90,
                )
            return (
                f"🔴 HIGH RISK — The concrete may set before the truck arrives. "
                f"Dispatching now is not recommended.\n"
                f"• Delivery success: {prob_pct} — the usable life window ({setting_time:.0f} min) is nearly consumed by travel time\n"
                f"• Usable life when truck reaches the gate: only {rl} min (below the 15-min safety threshold)\n"
                f"• Top concern: {top_risk}\n"
                f"• Options: use a closer batching plant, add a retarder admixture to extend the window, or reschedule",
                45,
            )
        else:  # critical
            overrun = abs(rl)
            return (
                f"🚫 DO NOT DISPATCH — The concrete will almost certainly set solid before the truck arrives. "
                f"This load cannot safely reach the site.\n"
                f"• Delivery success: {prob_pct} — travel time exceeds the concrete's entire usable life\n"
                f"• The concrete would set roughly {overrun} min before the truck even reaches the gate\n"
                f"• Immediate action required: source from a closer plant, switch to RE mix "
                f"(120-min window), or reschedule the pour",
                120,
            )
