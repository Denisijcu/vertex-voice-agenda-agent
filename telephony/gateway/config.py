"""config.py — Lee telephony/.env (KEY=VALUE) sin dependencias. Las variables de entorno tienen prioridad."""
import json
import os
from dataclasses import dataclass
from pathlib import Path

TELEPHONY_DIR = Path(__file__).resolve().parent.parent


def _load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _path(value: str) -> Path:
    p = Path(value)
    return p if p.is_absolute() else TELEPHONY_DIR / p


@dataclass(frozen=True)
class Settings:
    gateway_host: str
    gateway_port: int
    api_url: str
    model_name: str
    principal_name: str
    whisper_model: str
    whisper_dir: Path
    piper_voice: Path
    vad_threshold: float
    vad_silence_ms: int
    llamada_file: Path
    logs_dir: Path

    @property
    def disclosure(self) -> str:
        return (
            f"Hola, buenas. Le habla un asistente de inteligencia artificial que llama en nombre de {self.principal_name}. "
            "Esta llamada se transcribe para tomar nota. ¿Está de acuerdo en continuar?"
        )

    def load_target(self) -> dict:
        """Ficha de la llamada (llamada.json): a quién, para qué y notas. Se relee en cada llamada."""
        if not self.llamada_file.exists():
            raise FileNotFoundError(f"No encuentro la ficha de la llamada: {self.llamada_file}")
        data = json.loads(self.llamada_file.read_text(encoding="utf-8-sig"))
        data.setdefault("dry_run", True)
        return data


def load_settings(env_file: Path | None = None) -> Settings:
    values = _load_env_file(env_file or TELEPHONY_DIR / ".env")
    values.update({k: v for k, v in os.environ.items() if k in _KEYS})

    def get(key: str, default: str) -> str:
        return values.get(key, "") or default

    return Settings(
        gateway_host=get("GATEWAY_HOST", "127.0.0.1"),
        gateway_port=int(get("GATEWAY_PORT", "9092")),
        api_url=get("API_URL", "http://127.0.0.1:8015").rstrip("/"),
        model_name=values.get("MODEL_NAME", ""),
        principal_name=get("PRINCIPAL_NAME", "Denis Sánchez"),
        whisper_model=get("WHISPER_MODEL", "small"),
        whisper_dir=_path(get("WHISPER_DIR", "models/whisper")),
        piper_voice=_path(get("PIPER_VOICE", "models/piper/es_MX-ald-medium.onnx")),
        vad_threshold=float(get("VAD_UMBRAL", "600")),
        vad_silence_ms=int(get("VAD_SILENCIO_MS", "800")),
        llamada_file=_path(get("LLAMADA_FILE", "llamada.json")),
        logs_dir=TELEPHONY_DIR / "logs",
    )


_KEYS = {
    "GATEWAY_HOST", "GATEWAY_PORT", "API_URL", "MODEL_NAME", "PRINCIPAL_NAME", "WHISPER_MODEL", "WHISPER_DIR",
    "PIPER_VOICE", "VAD_UMBRAL", "VAD_SILENCIO_MS", "LLAMADA_FILE",
}
