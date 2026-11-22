from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    # Entorno y servidor FastAPI
    app_env: Literal["development", "staging", "production"] = "development"
    api_host: str = Field(default="127.0.0.1", min_length=1, max_length=128)
    api_port: int = Field(default=8015, ge=1024, le=65535)

    # Runtime del modelo local / fine-tuneado (LM Studio / OpenAI-compatible)
    llm_base_url: str = Field(default="http://127.0.0.1:1234/v1", min_length=1, max_length=256)
    llm_default_model: str = Field(default="vertex-voice-agenda", min_length=1, max_length=128)
    llm_timeout_sec: float = Field(default=45.0, ge=5.0, le=300.0)
    max_concurrent_calls: int = Field(default=2, ge=1, le=32)

    # Muestreo: los mismos valores del chat de LM Studio con los que el modelo responde bien.
    # Sin repeat_penalty el modelo puede entrar en bucle (p. ej. "<tool_call>" repetido).
    llm_top_p: float = Field(default=0.95, gt=0.0, le=1.0)
    llm_top_k: int = Field(default=40, ge=0, le=500)
    llm_repeat_penalty: float = Field(default=1.1, ge=1.0, le=2.0)
    # Salida estructurada (gramática de llama.cpp en LM Studio): el modelo solo puede escribir el JSON acordado.
    llm_structured_output: bool = True

    # Agenda: en nombre de quién llama el agente y ventana por defecto (siguiente día hábil, hora local)
    principal_name: str = Field(default="Denis Sánchez", min_length=1, max_length=128)
    default_window_start_hour: int = Field(default=9, ge=0, le=23)
    default_window_end_hour: int = Field(default=12, ge=1, le=24)

    # CORS restringido al frontend local por defecto (sin json.loads forzado desde .env)
    allowed_origins: Annotated[list[str], NoDecode] = Field(
        default=[
            "http://localhost:4200",
            "http://127.0.0.1:4200",
        ]
    )

    # Llamadas de voz reales (telephony/): la UI las lanza a través de la API con docker exec.
    telephony_docker_bin: str = Field(default="docker", min_length=1, max_length=260)
    telephony_container: str = Field(default="vva-asterisk", pattern=r"^[A-Za-z0-9_.-]{1,64}$")
    telephony_sip_user: str = Field(default="denis", pattern=r"^[A-Za-z0-9_.-]{1,32}$")
    telephony_ticket_ttl_sec: float = Field(default=90.0, ge=15.0, le=600.0)
    # Historial permanente de llamadas reales (SQLite) y transcripciones de la pasarela que se importan al arrancar.
    # Rutas relativas = desde la carpeta api.
    call_history_db: str = Field(default="data/llamadas.sqlite3", min_length=1, max_length=260)
    telephony_logs_dir: str = Field(default="../telephony/logs", min_length=1, max_length=260)

    # Puente SIP / RTP (señalización y medio desacoplados del modelo).
    # Mientras SIP_BRIDGE_ENABLED=false TODO es dry-run, diga lo que diga la petición: no se marca ni se escribe
    # en la agenda real. Con el puente activado, manda el dry_run de cada petición (por defecto true).
    sip_bridge_enabled: bool = False
    sip_registrar_host: str = Field(default="127.0.0.1", min_length=1, max_length=255)
    sip_registrar_port: int = Field(default=5061, ge=1024, le=65535)
    sip_transport: Literal["SIPS/TLS", "SIP/UDP"] = "SIPS/TLS"
    sip_domain: str = Field(default="vertex.local", min_length=1, max_length=255)
    sip_username: str = Field(default="", max_length=128)
    sip_auth_secret: str = Field(default="", max_length=256)

    rtp_bind_ip: str = Field(default="127.0.0.1", min_length=1, max_length=64)
    rtp_port_min: int = Field(default=20000, ge=1024, le=65535)
    rtp_port_max: int = Field(default=20100, ge=1024, le=65535)
    rtp_codec: Literal["PCMU/8000", "OPUS/48000"] = "PCMU/8000"
    rtp_use_srtp: bool = True

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @field_validator("allowed_origins", mode="before")
    @classmethod
    def parse_allowed_origins(cls, value: object) -> list[str]:
        default_origins = [
            "http://localhost:4200",
            "http://127.0.0.1:4200",
        ]
        if value is None:
            return default_origins
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return default_origins
            return [item.strip() for item in stripped.split(",") if item.strip()]
        if isinstance(value, list):
            cleaned = [str(item).strip() for item in value if str(item).strip()]
            return cleaned if cleaned else default_origins
        raise ValueError("ALLOWED_ORIGINS debe ser una lista o cadena separada por comas.")

    @field_validator("allowed_origins")
    @classmethod
    def validate_origins_no_wildcard(cls, origins: list[str]) -> list[str]:
        if not origins or "*" in origins:
            raise ValueError("ALLOWED_ORIGINS no puede estar vacío ni usar comodín '*'.")
        return origins

    @field_validator("default_window_end_hour")
    @classmethod
    def validate_window_hours(cls, end_hour: int, info) -> int:
        if end_hour <= info.data.get("default_window_start_hour", 9):
            raise ValueError("DEFAULT_WINDOW_END_HOUR debe ser mayor que DEFAULT_WINDOW_START_HOUR.")
        return end_hour

    @field_validator("rtp_port_max")
    @classmethod
    def validate_rtp_range(cls, port_max: int, info) -> int:
        port_min = info.data.get("rtp_port_min", 20000)
        if port_max <= port_min:
            raise ValueError("RTP_PORT_MAX debe ser mayor que RTP_PORT_MIN.")
        return port_max


settings = Settings()


def effective_dry_run(requested: bool) -> bool:
    """Única regla de dry-run para toda la API: sin puente SIP activado, siempre es simulación."""
    return requested or not settings.sip_bridge_enabled