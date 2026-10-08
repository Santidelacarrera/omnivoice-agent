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

    cors_origins: list[str] = ["http://localhost:3000", "http://localhost:4310"]
    ws_ticket_ttl_seconds: int = 30
    max_sessions_per_org: int = 50
    rate_limit_sessions_per_min: int = 20  # creaciones de sesión por usuario
    rate_limit_api_per_min: int = 240  # peticiones REST por usuario
    max_ws_frame_bytes: int = 16384  # un frame PCM de 20 ms ocupa ~960 B; este tope frena abuso
    max_session_seconds: int = 1800
    tool_timeout_seconds: float = 8.0
    # VAD del servidor: "webrtc" (modelo, por defecto) o "energy". Si el módulo nativo falta, cae a energía.
    vad_backend: Literal["webrtc", "energy"] = "webrtc"
    vad_aggressiveness: int = 2  # 0 (permisivo) .. 3 (muy estricto con el ruido)
    vad_energy_threshold: float = 0.015
    vad_min_speech_ms: int = 120
    vad_hangover_ms: int = 600
    # El navegador solo pausa el audio al detectar voz; si el servidor no la confirma en este plazo, se reanuda.
    barge_in_confirm_ms: int = 400
    sample_rate: int = 24000

    # Voces e idiomas ofrecidos en la UI. Los valores se validan en el servidor: el cliente no puede inyectar otros.
    allowed_voices: list[str] = ["alloy", "ash", "ballad", "coral", "echo", "sage", "shimmer", "verse", "marin", "cedar"]
    allowed_languages: list[str] = ["es", "en", "pt", "fr", "de", "it"]

    # Grabación (opt-in): ninguna sesión se graba salvo que haya almacenamiento configurado, la organización lo
    # tenga habilitado (organizations.recording_enabled) y la persona haya dado su consentimiento explícito.
    recording_storage: Literal["none", "local", "s3"] = "none"
    recording_local_dir: str = "/data/recordings"
    recording_s3_bucket: str = ""
    recording_s3_region: str = ""
    recording_s3_endpoint_url: str = ""  # MinIO, R2, etc.
    recording_s3_sse: Literal["AES256", "aws:kms"] = "AES256"
    recording_s3_kms_key_id: str = ""
    recording_url_ttl_seconds: int = 300

    # Retención: purga periódica según organizations.retention_days.
    retention_job_enabled: bool = True
    retention_interval_seconds: int = 3600
    retention_batch_size: int = 500
    memory_retention_days: int = 30  # solo modo memoria (en Postgres manda organizations.retention_days)

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
            if self.recording_storage == "local":
                problems.append("RECORDING_STORAGE=local no es válido en producción (usa 's3')")
            if problems:
                raise ValueError("Configuración insegura para producción: " + "; ".join(problems))
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
