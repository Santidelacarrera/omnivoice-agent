from functools import lru_cache
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "OmniVoice Agent"
    environment: Literal["development", "test", "production"] = "development"

    # Backends: "memory" solo para desarrollo/tests; producción exige postgres + redis.
    persistence_backend: Literal["memory", "postgres"] = "memory"
    state_backend: Literal["memory", "redis"] = "memory"
    database_url: str = "postgresql+asyncpg://omni_app:omni_app@postgres:5432/omnivoice"
    redis_url: str = "redis://redis:6379/0"

    jwt_secret: str = "change-me"
    jwt_algorithm: str = "HS256"
    jwt_ttl_seconds: int = 3600

    # Las claves del proveedor viven SOLO en el backend (nunca en el navegador).
    openai_api_key: str = ""
    openai_realtime_url: str = "wss://api.openai.com/v1/realtime"
    openai_realtime_model: str = "gpt-realtime"

    cors_origins: list[str] = ["http://localhost:3000"]
    ws_ticket_ttl_seconds: int = 30
    max_sessions_per_org: int = 50
    rate_limit_sessions_per_min: int = 20  # creaciones de sesión por usuario
    rate_limit_api_per_min: int = 240  # peticiones REST por usuario
    max_ws_frame_bytes: int = 16384  # un frame PCM de 20 ms ocupa ~960 B; este tope frena abuso
    max_session_seconds: int = 1800
    tool_timeout_seconds: float = 8.0
    vad_energy_threshold: float = 0.015
    vad_min_speech_ms: int = 120
    sample_rate: int = 24000

    @model_validator(mode="after")
    def _production_guards(self) -> "Settings":
        if self.environment == "production":
            problems = []
            if self.jwt_secret == "change-me" or len(self.jwt_secret) < 32:
                problems.append("JWT_SECRET debe tener al menos 32 caracteres y no ser el valor por defecto")
            if self.persistence_backend != "postgres":
                problems.append("PERSISTENCE_BACKEND debe ser 'postgres'")
            if self.state_backend != "redis":
                problems.append("STATE_BACKEND debe ser 'redis'")
            if "*" in self.cors_origins:
                problems.append("CORS_ORIGINS no puede ser '*'")
            if problems:
                raise ValueError("Configuración insegura para producción: " + "; ".join(problems))
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
