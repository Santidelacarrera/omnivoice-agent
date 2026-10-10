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
    # Proveedor de voz en la nube. "auto": Gemini si hay GEMINI_API_KEY, si no OpenAI si hay clave, si no simulado.
    voice_provider: Literal["auto", "gemini", "openai", "cascade"] = "auto"
    gemini_api_key: str = ""
    gemini_live_model: str = "gemini-3.8-live"
    gemini_voices: list[str] = ["Kore", "Puck", "Charon", "Fenrir", "Aoede", "Leda", "Orus", "Zephyr"]

    # Pipeline en cascada (STT -> LLM -> TTS): permite medir cada etapa por separado.
    # Se activa con VOICE_PROVIDER=cascade (o en "auto" si no hay clave de Gemini/OpenAI pero sí estas dos).
    deepgram_api_key: str = ""
    deepgram_stt_url: str = "wss://api.deepgram.com/v1/listen"
    deepgram_tts_url: str = "https://api.deepgram.com/v1/speak"
    deepgram_stt_model: str = "nova-3"
    deepgram_stt_endpointing_ms: int = 300  # silencio que Deepgram necesita para dar por terminado el turno
    deepgram_tts_voices: dict[str, str] = {"es": "aura-2-celeste-es", "en": "aura-2-thalia-en"}
    anthropic_api_key: str = ""
    anthropic_url: str = "https://api.anthropic.com/v1/messages"
    cascade_llm_model: str = "claude-haiku-5-5"
    cascade_max_tokens: int = 300
    cascade_history_messages: int = 20  # tope de mensajes de contexto enviados al LLM (coste y latencia acotados)
    cascade_max_tool_rounds: int = 4
    cascade_turn_timeout_s: float = 30.0

    # Fallos de proveedor: conexión con plazo, reconexión acotada por sesión y tope global de sesiones.
    provider_connect_timeout_s: float = 10.0
    provider_max_reconnects: int = 3  # por sesión
    provider_reconnect_backoff_s: float = 0.5  # exponencial: 0.5, 1, 2...
    max_total_sessions: int = 200  # por proceso, todas las organizaciones (protege el consumo de proveedores)
    org_daily_budget_minutes: float = 0  # minutos de conversación por organización y día UTC; 0 = sin tope

    # Precios de REFERENCIA para estimar el coste (USD). Son configuración, no hechos: verifica las tarifas vigentes de
    # cada proveedor antes de usarlas para facturar o decidir. El consumo (minutos, tokens, caracteres) sí se mide.
    price_s2s_in_per_min: float = 0.06  # audio de entrada del proveedor voz-a-voz
    price_s2s_out_per_min: float = 0.24  # audio de salida del proveedor voz-a-voz
    price_stt_per_min: float = 0.0077
    price_tts_per_1k_chars: float = 0.030
    price_llm_in_per_mtok: float = 1.0
    price_llm_out_per_mtok: float = 5.0

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

    # Telefonía (PSTN/SIP) mediante Twilio Programmable Voice + Media Streams. Vacío = telefonía desactivada.
    telephony_provider: Literal["none", "twilio"] = "none"
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_from_number: str = ""  # número E.164 desde el que se emiten llamadas
    telephony_public_url: str = ""  # URL pública https del backend (la usa Twilio para el webhook y el stream)
    telephony_org_id: str = "00000000-0000-0000-0000-000000000001"  # organización que atiende las llamadas entrantes
    telephony_agent_id: str = ""  # agente que contesta (vacío = agente por defecto)
    telephony_language: str = "es"
    human_transfer_number: str = ""  # número E.164 del operador/cola humana
    transfer_announce_ms: int = 3500  # espera para que el agente termine de avisar antes de desviar la llamada
    twilio_validate_signature: bool = True

    @property
    def active_provider(self) -> str:
        if self.voice_provider != "auto":
            return self.voice_provider
        if self.gemini_api_key:
            return "gemini"
        if self.openai_api_key:
            return "openai"
        return "cascade" if self.deepgram_api_key and self.anthropic_api_key else "simulated"

    @property
    def available_voices(self) -> list[str]:
        if self.active_provider == "cascade":
            return sorted(set(self.deepgram_tts_voices.values()))
        return self.gemini_voices if self.active_provider == "gemini" else self.allowed_voices

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
            if self.telephony_provider == "twilio":
                if not (self.twilio_account_sid and self.twilio_auth_token and self.telephony_public_url.startswith("https://")):
                    problems.append("TELEPHONY_PROVIDER=twilio exige TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN y TELEPHONY_PUBLIC_URL https")
                if not self.twilio_validate_signature:
                    problems.append("TWILIO_VALIDATE_SIGNATURE no puede desactivarse en producción")
            if problems:
                raise ValueError("Configuración insegura para producción: " + "; ".join(problems))
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
