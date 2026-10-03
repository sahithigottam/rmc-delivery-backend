"""FastAPI dependencies"""
from typing import Optional
from fastapi import Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.rmc.alerts import AlertService, LoggingAlertHandler, SSEAlertHandler
from app.rmc.load_manager import LoadManager
from app.services import GoogleMapsService, RouteService, TripService
from app.services.prediction import PredictionService

_gmaps_service: Optional[GoogleMapsService] = None
_load_manager: Optional[LoadManager] = None
_alert_service: Optional[AlertService] = None


async def get_google_maps_client() -> GoogleMapsService:
    """Dependency for Google Maps client"""
    global _gmaps_service
    if _gmaps_service is None:
        _gmaps_service = GoogleMapsService()
    return _gmaps_service


def get_load_manager() -> LoadManager:
    """Dependency for RMC Load Manager (singleton, stateless)"""
    global _load_manager
    if _load_manager is None:
        _load_manager = LoadManager()
    return _load_manager


def get_alert_service() -> AlertService:
    """Dependency for Alert Service (singleton, Observer pattern)"""
    global _alert_service
    if _alert_service is None:
        _alert_service = AlertService()
        _alert_service.register(LoggingAlertHandler())
        _alert_service.register(SSEAlertHandler())
    return _alert_service


def get_route_service(
    db: Session = Depends(get_db),
    google_maps: GoogleMapsService = Depends(get_google_maps_client),
) -> RouteService:
    """Dependency for Route service"""
    return RouteService(db, google_maps)


def get_trip_service(
    db: Session = Depends(get_db),
    google_maps: GoogleMapsService = Depends(get_google_maps_client),
    load_manager: LoadManager = Depends(get_load_manager),
    alert_service: AlertService = Depends(get_alert_service),
) -> TripService:
    """Dependency for Trip service (with RMC load management)"""
    return TripService(
        db=db,
        google_maps=google_maps,
        load_manager=load_manager,
        alert_service=alert_service,
    )


def get_prediction_service(
    google_maps: GoogleMapsService = Depends(get_google_maps_client),
) -> PredictionService:
    """Dependency for Prediction service (pre-trip dispatcher decision support)"""
    return PredictionService(google_maps)
