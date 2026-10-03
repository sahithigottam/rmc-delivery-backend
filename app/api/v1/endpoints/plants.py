"""
Plants catalogue + brand analysis endpoints.

Flow the UI follows:
  1. GET  /plants/brands         → populate brand dropdown
  2. POST /plants/analyse        → user enters brand + destination → ranked predictions
  3. User picks a result, clicks "Schedule Delivery"
  4. POST /dispatch              → creates a pending trip (in trips.py)
  5. On the day: POST /trips/{id}/begin → truck loads, trip goes in_progress
  6. Real-time SSE monitoring (existing)
  7. POST /trips/{id}/complete   → outcome recorded in DB
"""
import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import List, Optional

from cachetools import TTLCache
from fastapi import APIRouter, Depends, HTTPException, Query

from app.dependencies import get_prediction_service
from app.schemas import (
    BrandAnalysisRequest,
    BrandAnalysisResponse,
    PlantCreate,
    PlantOut,
    PlantPredictionResult,
    PlantUpdate,
)
from app.services.llm_analyzer import LLMAnalyzer
from app.services.plant_locator import PlantLocator
from app.services.prediction import PredictionService

_PLANTS_JSON = Path(__file__).parent.parent.parent.parent.parent / "config" / "plants.json"


def _load_all_plants() -> List[dict]:
    """Load ALL plants (including inactive) directly from file."""
    with open(_PLANTS_JSON, encoding="utf-8") as f:
        return json.load(f)


def _save_plants(plants: List[dict]) -> None:
    """Write plants list back to file and bust the PlantLocator cache."""
    with open(_PLANTS_JSON, "w", encoding="utf-8") as f:
        json.dump(plants, f, indent=2, ensure_ascii=False)
    # Bust singleton cache so next request reloads
    PlantLocator._plants = None


def _make_id(name: str, brand: str) -> str:
    """Generate a slug ID from brand + name."""
    slug = re.sub(r"[^a-z0-9]+", "_", (brand + " " + name).lower()).strip("_")
    return slug

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/plants", tags=["plants"])

# Cache the full analyse_brand response for 10 minutes.
# Key: (brand_lower, job_site_address_lower, concrete_mix_upper, 10-min time bucket)
# This prevents repeat API calls when a dispatcher re-opens the panel or clicks
# Analyse within the same 10-minute window.
_analysis_cache: TTLCache = TTLCache(maxsize=200, ttl=600)

# ── Helpers ───────────────────────────────────────────────────────────────

def _locator() -> PlantLocator:
    """Return a PlantLocator (singleton via class-level cache)."""
    return PlantLocator()


def _plant_to_schema(p: dict) -> PlantOut:
    return PlantOut(
        id=p["id"],
        name=p["name"],
        brand=p["brand"],
        address=p["address"],
        lat=p["lat"],
        lng=p["lng"],
        region=p["region"],
        active=p.get("active", True),
    )


# ── Catalogue endpoints ───────────────────────────────────────────────────

@router.get("", response_model=List[PlantOut], summary="List all active plants")
def list_plants(
    brand: Optional[str] = Query(None, description="Filter by brand name (case-insensitive)"),
    region: Optional[str] = Query(None, description="Filter by region slug"),
    active_only: bool = Query(True, description="Return only active plants"),
):
    """
    Return the full plant catalogue from `config/plants.json`.

    Use `brand` or `region` query params to narrow results.
    """
    loc = _locator()
    plants = loc._plants or []

    if active_only:
        plants = [p for p in plants if p.get("active", True)]
    if brand:
        plants = [p for p in plants if p["brand"].lower() == brand.lower()]
    if region:
        plants = [p for p in plants if p["region"].lower() == region.lower()]

    return [_plant_to_schema(p) for p in plants]


@router.get("/brands", response_model=List[str], summary="List distinct plant brands")
def list_brands():
    """
    Return alphabetically-sorted distinct brand names.

    Use this to populate the **Brand** dropdown in the dispatch UI.
    """
    loc = _locator()
    brands = sorted({p["brand"] for p in (loc._plants or []) if p.get("active", True)})
    return brands


@router.get("/regions", response_model=List[str], summary="List distinct regions")
def list_regions():
    """Return distinct region slugs (e.g. `central_west`, `north_shore`)."""
    loc = _locator()
    regions = sorted({p["region"] for p in (loc._plants or []) if p.get("active", True)})
    return regions


@router.get("/{plant_id}", response_model=PlantOut, summary="Get a single plant by ID")
def get_plant(plant_id: str):
    """Look up one plant by its `id` field from plants.json."""
    loc = _locator()
    plant = loc.get_by_id(plant_id)
    if not plant:
        raise HTTPException(status_code=404, detail=f"Plant '{plant_id}' not found")
    return _plant_to_schema(plant)


# ── CRUD write endpoints ─────────────────────────────────────────────────────

@router.post("", response_model=PlantOut, status_code=201, summary="Add a new plant")
def create_plant(body: PlantCreate):
    plants = _load_all_plants()
    new_id = _make_id(body.name, body.brand)
    # Ensure uniqueness
    existing_ids = {p["id"] for p in plants}
    base_id, counter = new_id, 2
    while new_id in existing_ids:
        new_id = f"{base_id}_{counter}"
        counter += 1
    new_plant = {
        "id": new_id,
        "name": body.name,
        "brand": body.brand,
        "address": body.address,
        "lat": body.lat,
        "lng": body.lng,
        "region": body.region,
        "active": body.active,
    }
    plants.append(new_plant)
    _save_plants(plants)
    logger.info("Plant created: %s", new_id)
    return _plant_to_schema(new_plant)


@router.put("/{plant_id}", response_model=PlantOut, summary="Replace a plant")
def update_plant(plant_id: str, body: PlantCreate):
    plants = _load_all_plants()
    idx = next((i for i, p in enumerate(plants) if p["id"] == plant_id), None)
    if idx is None:
        raise HTTPException(status_code=404, detail=f"Plant '{plant_id}' not found")
    updated = {
        "id": plant_id,
        "name": body.name,
        "brand": body.brand,
        "address": body.address,
        "lat": body.lat,
        "lng": body.lng,
        "region": body.region,
        "active": body.active,
    }
    plants[idx] = updated
    _save_plants(plants)
    logger.info("Plant updated: %s", plant_id)
    return _plant_to_schema(updated)


@router.patch("/{plant_id}", response_model=PlantOut, summary="Partial update / toggle active")
def patch_plant(plant_id: str, body: PlantUpdate):
    plants = _load_all_plants()
    idx = next((i for i, p in enumerate(plants) if p["id"] == plant_id), None)
    if idx is None:
        raise HTTPException(status_code=404, detail=f"Plant '{plant_id}' not found")
    p = plants[idx]
    if body.name is not None:    p["name"] = body.name
    if body.brand is not None:   p["brand"] = body.brand
    if body.address is not None: p["address"] = body.address
    if body.lat is not None:     p["lat"] = body.lat
    if body.lng is not None:     p["lng"] = body.lng
    if body.region is not None:  p["region"] = body.region
    if body.active is not None:  p["active"] = body.active
    plants[idx] = p
    _save_plants(plants)
    logger.info("Plant patched: %s", plant_id)
    return _plant_to_schema(p)


@router.delete("/{plant_id}", status_code=204, summary="Delete a plant")
def delete_plant(plant_id: str):
    plants = _load_all_plants()
    original_len = len(plants)
    plants = [p for p in plants if p["id"] != plant_id]
    if len(plants) == original_len:
        raise HTTPException(status_code=404, detail=f"Plant '{plant_id}' not found")
    _save_plants(plants)
    logger.info("Plant deleted: %s", plant_id)


# ── Brand analysis ────────────────────────────────────────────────────────

@router.post(
    "/analyse",
    response_model=BrandAnalysisResponse,
    summary="Analyse all plants of a brand for a job site",
)
async def analyse_brand(
    req: BrandAnalysisRequest,
    prediction_svc: PredictionService = Depends(get_prediction_service),
):
    """
    **Core dispatch-planning endpoint.**

    Given a **brand** and **job site address**, runs the full prediction engine
    for every active plant of that brand in parallel and returns a ranked list
    (best remaining concrete life first).

    The dispatcher can inspect each plant's ETA, remaining life, risk level and
    delay constant, then click one to schedule a delivery.

    ### Fields in each result
    | Field | Meaning |
    |---|---|
    | `adjusted_eta_minutes` | Google ETA × NZTA delay constant |
    | `remaining_life_minutes` | `concrete_window - loading_time - adjusted_eta` |
    | `buffer_depletion_rate` | `adjusted_eta / concrete_window` (0–1) |
    | `risk_level` | `low / medium / high / critical` |
    | `success_probability` | 0–1 statistical likelihood of on-time pour |
    """
    # Extract the display_name from the incoming job_site_data object
    job_site_address = req.job_site_data.get("display_name")
    if not job_site_address:
        raise HTTPException(
            status_code=400,
            detail="`job_site_data` must contain a `display_name` field.",
        )

    # ── Cache lookup ──────────────────────────────────────────────────
    # Key uses a 10-min time bucket so cache entries expire naturally within the TTL.
    time_bucket = int(time.time()) // 600
    cache_key = (req.brand.lower(), job_site_address.lower(), req.concrete_mix.upper(), time_bucket)
    cached = _analysis_cache.get(cache_key)
    if cached is not None:
        logger.info(
            "analyse_brand cache HIT: brand=%s, job_site=%s (saved %d Google API calls)",
            req.brand, job_site_address[:40], len(cached.results),
        )
        return cached

    loc = _locator()
    all_plants = loc._plants or []

    # Get all active plants for this brand
    brand_plants = [
        p for p in all_plants
        if p["brand"].lower() == req.brand.lower() and p.get("active", True)
    ]

    if not brand_plants:
        raise HTTPException(
            status_code=404,
            detail=f"No active plants found for brand '{req.brand}'. "
                   f"Available brands: {sorted({p['brand'] for p in all_plants if p.get('active', True)})}",
        )

    # Limit to top_n closest plants to avoid burning too many API calls
    brand_plants = brand_plants[: req.top_n]

    # ── Run predictions in parallel ──────────────────────────────────
    async def _predict_one(plant: dict) -> PlantPredictionResult:
        try:
            result = await prediction_svc.predict_delivery(
                start_address=plant["address"],
                end_address=job_site_address,
                concrete_mix=req.concrete_mix,
                include_llm=False,  # Never call LLM for multi-plant comparison — too slow
            )
            pred = result.get("prediction", {})
            traffic = result.get("traffic", {})
            return PlantPredictionResult(
                plant=_plant_to_schema(plant),
                google_eta_minutes=traffic.get("google_eta_minutes"),
                adjusted_eta_minutes=pred.get("adjusted_eta_minutes"),
                remaining_life_minutes=pred.get("remaining_life_minutes"),
                buffer_depletion_rate=pred.get("buffer_depletion_rate"),
                delay_constant_used=pred.get("delay_constant_used"),
                risk_level=pred.get("risk_level"),
                success_probability=pred.get("success_probability"),
                recommendation=result.get("recommendation"),
                llm_analysis=result.get("llm_analysis") if req.include_llm_analysis else None,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Prediction failed for plant %s: %s", plant["id"], exc)
            return PlantPredictionResult(
                plant=_plant_to_schema(plant),
                error=str(exc),
            )

    results: List[PlantPredictionResult] = await asyncio.gather(
        *[_predict_one(p) for p in brand_plants]
    )

    # ── Rank: highest remaining life first; errors go last ───────────
    def _sort_key(r: PlantPredictionResult) -> float:
        if r.remaining_life_minutes is not None:
            return -r.remaining_life_minutes   # negate → ascending sort = best first
        return 9999.0                          # errors sink to bottom

    results = sorted(results, key=_sort_key)

    # Best plant = first non-error result
    best_id: Optional[str] = None
    for r in results:
        if r.error is None and r.remaining_life_minutes is not None:
            best_id = r.plant.id
            break

    # ── Single LLM call to compare ALL plants and recommend best ────
    llm_comparison = None
    if req.include_llm_analysis:
        valid = [r for r in results if r.error is None and r.remaining_life_minutes is not None]
        if valid:
            plants_data = [
                {
                    "plant_name": r.plant.name,
                    "plant_address": r.plant.address,
                    "google_eta": r.google_eta_minutes or 0,
                    "adjusted_eta": r.adjusted_eta_minutes or 0,
                    "remaining_life": r.remaining_life_minutes or 0,
                    "risk_level": r.risk_level or "unknown",
                    "success_prob": r.success_probability or 0,
                    "buffer_depletion": r.buffer_depletion_rate or 0,
                }
                for r in valid
            ]
            try:
                analyzer = LLMAnalyzer()
                llm_comparison = await analyzer.compare_plants(
                    plants_data=plants_data,
                    job_site=job_site_address,
                    concrete_mix=req.concrete_mix,
                )
            except Exception as exc:
                logger.warning("LLM comparison failed: %s", exc)

    response = BrandAnalysisResponse(
        brand=req.brand,
        job_site_address=job_site_address,
        concrete_mix=req.concrete_mix,
        total_plants_analysed=len(results),
        results=results,
        best_plant_id=best_id,
        llm_comparison=llm_comparison,
    )
    _analysis_cache[cache_key] = response
    return response
