from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "OmniVoice Agent"
    environment: str = "development"
    database_url: str = "postgresql+asyncpg://omni:omni@postgres:5432/omnivoice"
    redis_url: str = "redis://redis:6379/0"

    jwt_secret: str = "change-me"
    jwt_algorithm: str = "HS256"
    jwt_ttl_seconds: int = 3600

    # Las claves del proveedor viven SOLO en el backend (nunca en el navegador).
    openai_api_key: str = ""
    openai_realtime_url: str = "wss://api.openai.com/v1/realtime"
    openai_realtime_model: str = "gpt-realtime"

    cors_origins: list[str] = ["http://localhost:3000"]
    max_sessions_per_org: int = 50
    tool_timeout_seconds: float = 8.0
    vad_energy_threshold: float = 0.015
    vad_min_speech_ms: int = 120
    sample_rate: int = 24000


@lru_cache
def get_settings() -> Settings:
    return Settings()
