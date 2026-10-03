"""Application configuration settings"""
import logging
from pathlib import Path
from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    """Application settings from environment variables or credentials.json"""

    # Environment
    env: str = "development"
    debug: bool = False

    # Database
    database_url: str = "sqlite:///./rmc_delivery.db"
    db_echo: bool = False

    # Google Maps API
    google_maps_api_key: str = ""

    # Cloud Routing Service
    routing_service_url: str = "https://rmc-routing-service-production.up.railway.app"

    # FastAPI
    log_level: str = "INFO"
    api_v1_prefix: str = "/api/v1"
    api_title: str = "RMC Delivery Route Optimizer"
    api_version: str = "1.0.0"

    # Rate Limiting
    rate_limit_requests: int = 100
    rate_limit_window: int = 60

    class Config:
        env_file = ".env"
        case_sensitive = False


def load_settings() -> Settings:
    """Load settings with credentials.json priority"""
    settings = Settings()

    # Try to load from credentials.json if it exists
    credentials_file = Path(__file__).parent.parent / "config" / "credentials.json"

    if credentials_file.exists():
        try:
            import json

            with open(credentials_file, "r") as f:
                creds = json.load(f)

            # Load debug setting
            if "debug" in creds:
                settings.debug = creds.get("debug", False)
                logger.info(f"Debug mode: {settings.debug}")

            # Load Google Maps API key from credentials.json
            google_key = (
                creds.get("google_maps", {}).get("api_key") or settings.google_maps_api_key
            )
            if google_key and google_key != "YOUR_GOOGLE_MAPS_API_KEY_HERE":
                settings.google_maps_api_key = google_key
                logger.info("Google Maps API key loaded from config/credentials.json")

            # Load database URL from credentials.json
            db_config = creds.get("database", {})
            if db_config:
                db_type = db_config.get("type", "sqlite")
                
                if db_type == "sqlite":
                    # SQLite database
                    db_path = db_config.get("path", "./rmc_delivery.db")
                    db_url = f"sqlite:///{db_path}"
                else:
                    # PostgreSQL database
                    username = db_config.get("username", "user")
                    password = db_config.get("password", "password")
                    host = db_config.get("host", "localhost")
                    port = db_config.get("port", 5432)
                    database = db_config.get("database", "rmc_delivery")
                    db_url = f"postgresql://{username}:{password}@{host}:{port}/{database}"
                
                settings.database_url = db_url
                logger.info(f"Database configured: {db_type} - {settings.database_url}")

        except Exception as e:
            logger.warning(f"Could not load credentials from {credentials_file}: {e}")
            logger.info("Falling back to environment variables")

    return settings


# Global settings instance
settings = load_settings()
