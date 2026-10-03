"""
API v1 prediction endpoints — Pre-trip dispatcher decision support.

Endpoints:
  POST  /predict/delivery       → Full prediction: YES/NO + LLM narrative
  POST  /predict/best-plant     → Compare all plants, return fastest + safest
"""
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.dependencies import get_prediction_service
from app.schemas import ErrorResponse
from app.services.prediction import PredictionService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/predict", tags=["prediction"])


# ── Request / Response schemas ─────────────────────────────────────────────

class DeliveryPredictionRequest(BaseModel):
    """Dispatcher provides only start and end address. Everything else is automatic."""

    start_address: str = Field(
        ...,
        min_length=5,
        description="Batching plant address  e.g. 'Holcim Avondale, 54 Patiki Rd, Auckland'",
        examples=["Holcim Avondale, 54 Patiki Rd, Auckland"],
    )
    end_address: Dict[str, Any] = Field(
        ...,
        description="Full address data object for the job site from OpenStreetMap or a similar provider.",
        examples=[{
            "place_id": 22035753,
            "display_name": "ANZ, Queen Street, Aotea Arts Quarter, Wynyard Quarter, City Centre, Auckland, Waitematā, Auckland, 1010, New Zealand",
            "address": { "road": "Queen Street", "city": "Auckland" }
        }],
    )
    concrete_mix: str = Field(
        default="GP",
        pattern="^(GP|HE|RE)$",
        description="GP = General Purpose 90 min | HE = High Early 60 min | RE = Retarded 120 min",
    )
    include_llm_analysis: bool = Field(
        default=True,
        description="Include plain-English LLM risk narrative (requires Ollama). Set False for faster response.",
    )


class BestPlantRequest(BaseModel):
    """
    Find the best dispatching plant for a given job site.

    No need to list plants — the system reads config/plants.json automatically,
    filters by driving distance (free Haversine), then calls Google only for
    the viable shortlist. Typical saving: 15 → 4 Google API calls per request.
    """

    job_site_data: Dict[str, Any] = Field(
        ...,
        description="Full address data object for the job site from OpenStreetMap or a similar provider.",
        examples=[{
            "place_id": 22035753,
            "display_name": "ANZ, Queen Street, Aotea Arts Quarter, Wynyard Quarter, City Centre, Auckland, Waitematā, Auckland, 1010, New Zealand",
            "address": { "road": "Queen Street", "city": "Auckland" }
        }],
    )
    concrete_mix: str = Field(
        default="GP",
        pattern="^(GP|HE|RE)$",
        description="GP = General Purpose 90 min | HE = High Early 60 min | RE = Retarded 120 min",
    )
    top_n: int = Field(
        default=5,
        ge=1,
        le=10,
        description="Max candidate plants to query via Google Directions (default 5)",
    )


class NearestPlantRequest(BaseModel):
    """
    Return nearest plants by straight-line Haversine distance — no API calls, instant.

    Use this when the dispatcher just wants a quick sanity check on which plants
    are geographically close to a job site. For the true best pick (accounting for
    Auckland road network + live traffic), use /predict/best-plant instead.
    """

    job_site_address: str = Field(
        ...,
        min_length=5,
        description="The job site (destination) address  e.g. '45 Queen Street, Auckland CBD'",
        examples=["45 Queen Street, Auckland CBD"],
    )
    top_n: int = Field(
        default=5,
        ge=1,
        le=20,
        description="How many nearest plants to return (default 5, max 20)",
    )
    brand: Optional[str] = Field(
        default=None,
        description="Filter to a specific brand  e.g. 'Holcim' | 'Allied' | 'Firth'",
    )
    region: Optional[str] = Field(
        default=None,
        description="Filter to a region  e.g. 'South Auckland' | 'North Shore'",
    )


# ── Endpoints ──────────────────────────────────────────────────────────────

@router.post(
    "/delivery",
    responses={
        400: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
    summary="Pre-trip delivery feasibility prediction",
    description="""
**Dispatcher decision support — the core feature.**

Provide a start address and end address. The system automatically:
1. Calls Google Directions for live-traffic ETA
2. Fetches current weather at the job site (Open-Meteo)
3. Applies NZTA Auckland congestion calibration
4. Computes NZS 3109 concrete window feasibility
5. Asks the LLM (qwen2.5:3b) to explain the risk in plain English

**Concrete mix limits (NZS 3109:1997):**
- `GP` — General Purpose: 90 min normal, 60 min if temp > 30°C
- `HE` — High Early: 60 min
- `RE` — Retarded: 120 min

**Example response:** `HIGH RISK — Friday 4PM traffic on SH1 will likely push 
this to 68 min. Buffer is only 12 min. Recommend dispatching from Penrose instead.`
""",
)
async def predict_delivery(
    req: DeliveryPredictionRequest,
    service: PredictionService = Depends(get_prediction_service),
) -> dict:
    """Full pre-trip prediction. Dispatcher YES/NO decision support."""
    try:
        # Extract the display_name from the incoming end_address object
        end_address = req.end_address.get("display_name")
        if not end_address:
            raise HTTPException(
                status_code=400,
                detail="`end_address` must be an object containing a `display_name` field.",
            )

        result = await service.predict_delivery(
            start_address=req.start_address,
            end_address=end_address,
            concrete_mix=req.concrete_mix,
            include_llm=req.include_llm_analysis,
        )
        if "error" in result:
            raise HTTPException(status_code=400, detail=result["error"])
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"predict_delivery failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Prediction failed. Please try again.")


@router.post(
    "/best-plant",
    responses={
        400: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
    summary="Find best dispatching plant for a job site",
    description="""
Automatically selects viable candidate plants from `config/plants.json` using
free Haversine distance filtering, then scores them in parallel via Google
Directions. Returns the plant with the highest delivery success probability.

**Two-stage approach:**
1. **Haversine filter (free)** — eliminates plants physically too far away.
2. **Google Directions (paid)** — only called for the shortlist (default top 5).

This typically reduces Google API calls from 20 → 4–5 per request.

**Concrete mix window limits (NZS 3109:1997):**
- `GP` — 90 min | `HE` — 60 min | `RE` — 120 min
""",
)
async def best_plant(
    req: BestPlantRequest,
    service: PredictionService = Depends(get_prediction_service),
) -> dict:
    """Compare shortlisted plants, return ranked by success probability."""
    try:
        # Extract the display_name from the incoming job_site_data object
        job_site_address = req.job_site_data.get("display_name")
        if not job_site_address:
            raise HTTPException(
                status_code=400,
                detail="`job_site_data` must contain a `display_name` field.",
            )

        result = await service.best_plant(
            job_site_address=job_site_address,
            concrete_mix=req.concrete_mix,
            top_n=req.top_n,
        )
        if "error" in result:
            raise HTTPException(status_code=400, detail=result["error"])
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"best_plant failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Best plant check failed. Please try again.")


@router.post(
    "/nearest-plant",
    summary="Nearest plants by straight-line distance (free, instant)",
    description="""
Returns nearest plants using Haversine straight-line distance — **no Google API
calls, no cost, instant response**.

Use for:
- Quick dispatcher sanity check ("what plants are nearby?")
- Pre-filtering before calling `/predict/best-plant`
- Offline / fallback when Google is unavailable

Note: straight-line distance ≠ road distance. For traffic-aware routing
and success probability, use `/predict/best-plant`.
""",
    responses={400: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
)
async def nearest_plant(
    req: NearestPlantRequest,
    service: PredictionService = Depends(get_prediction_service),
) -> dict:
    """Return nearest plants by Haversine distance. Zero API cost."""
    from app.services.plant_locator import PlantLocator

    try:
        # Geocode job site to get lat/lng for Haversine
        job_geo = await service._gmaps.geocode_address(req.job_site_address)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not geocode address: {e}")

    try:
        locator = PlantLocator()
        plants = locator.nearest_by_distance(
            lat=job_geo["latitude"],
            lng=job_geo["longitude"],
            top_n=req.top_n,
            brand=req.brand,
            region=req.region,
        )
        return {
            "job_site":          req.job_site_address,
            "job_site_coords":   {"lat": job_geo["latitude"], "lng": job_geo["longitude"]},
            "nearest_plants":    plants,
            "note": (
                "Distances are straight-line (Haversine). "
                "For traffic-adjusted ETAs use POST /predict/best-plant."
            ),
        }
    except Exception as e:
        logger.error(f"nearest_plant failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Nearest plant lookup failed.")
