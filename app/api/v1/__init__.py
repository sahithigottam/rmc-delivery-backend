"""API v1 endpoints"""
from fastapi import APIRouter

from app.api.v1.endpoints import plants, predictions, routes, trips

api_router = APIRouter()
api_router.include_router(plants.router)
api_router.include_router(routes.router)
api_router.include_router(trips.router)
api_router.include_router(predictions.router)

__all__ = ["api_router"]
