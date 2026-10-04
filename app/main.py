"""Main FastAPI application"""
import logging
import logging.handlers
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.v1 import api_router
from app.config import settings
from app.db import Base, engine
from app.services.google_maps import GoogleMapsService
from app.services.traffic_monitor import start_traffic_monitor, stop_traffic_monitor
from app.dependencies import get_google_maps_client, get_load_manager, get_alert_service

# ── Logging setup ──────────────────────────────────────────────────────────
LOG_FORMAT = "%(asctime)s │ %(levelname)-8s │ %(name)-30s │ %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Root logger
root_logger = logging.getLogger()
root_logger.setLevel(settings.log_level)

# Console handler — clean, concise
console_handler = logging.StreamHandler()
console_handler.setLevel(settings.log_level)
console_handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT))
root_logger.addHandler(console_handler)

# File handlers only in development
if settings.env == "development":
    LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs")
    os.makedirs(LOG_DIR, exist_ok=True)
    
    # File handler — rotating, keeps last 5 × 5MB files
    file_handler = logging.handlers.RotatingFileHandler(
        os.path.join(LOG_DIR, "rmc-delivery.log"),
        maxBytes=5 * 1024 * 1024,  # 5 MB
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT))
    root_logger.addHandler(file_handler)

    # Separate error log
    error_handler = logging.handlers.RotatingFileHandler(
        os.path.join(LOG_DIR, "rmc-errors.log"),
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    error_handler.setLevel(logging.WARNING)
    error_handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT))
    root_logger.addHandler(error_handler)

# Quiet noisy third-party loggers
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


# Lifespan context manager
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan context manager"""
    # Startup
    logger.info("Starting RMC Delivery Route Optimizer API")
    # Create tables
    try:
        Base.metadata.create_all(bind=engine)
        logger.info("Database tables created successfully")
    except Exception as e:
        logger.error(f"Error creating database tables: {e}")

    # Start background traffic monitor for active trips
    try:
        gmaps_service = await get_google_maps_client()
        load_mgr = get_load_manager()
        alert_svc = get_alert_service()
        if gmaps_service and settings.google_maps_api_key:
            await start_traffic_monitor(gmaps_service, load_mgr, alert_svc)
            logger.info("Background traffic monitor started (with RMC load tracking)")
        else:
            logger.warning("Skipping traffic monitor: Google Maps API key not configured")
    except Exception as e:
        logger.warning(f"Traffic monitor startup deferred: {e} (will retry on first request)")

    yield

    # Shutdown
    logger.info("Shutting down RMC Delivery Route Optimizer API")
    await stop_traffic_monitor()
    logger.info("Background traffic monitor stopped")
    try:
        gmaps_service = await get_google_maps_client()
        if gmaps_service:
            await gmaps_service.close()
            logger.info("Google Maps service closed successfully.")
    except Exception as e:
        logger.warning(f"Error closing Google Maps service: {e}")


# Create FastAPI application
app = FastAPI(
    title=settings.api_title,
    version=settings.api_version,
    lifespan=lifespan,
    debug=settings.debug,
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Health check endpoint
@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "service": settings.api_title,
        "version": settings.api_version,
    }


# Include API routes
app.include_router(api_router, prefix=settings.api_v1_prefix)


# Root endpoint
@app.get("/")
async def root():
    """Root endpoint with API information"""
    return {
        "title": settings.api_title,
        "version": settings.api_version,
        "docs": f"{settings.api_v1_prefix}/docs",
        "status": "running",
    }


# Error handlers
@app.exception_handler(Exception)
async def general_exception_handler(request, exc):
    """Handle general exceptions"""
    logger.error(f"Unhandled exception: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=settings.debug,
        log_level=settings.log_level.lower(),
    )
