"""
speech.py — Oído (faster-whisper) y voz (Piper), los dos locales y en CPU.

Se cargan una sola vez al arrancar la pasarela. Las clases tienen la misma forma que los dobles de prueba de
tests/test_gateway.py, así el flujo de la llamada se prueba sin modelos.
"""
import logging

import numpy as np

from .audio import float_to_pcm16, pcm16_to_float, resample
from .audiosocket import SAMPLE_RATE

log = logging.getLogger("gateway.speech")

# Receta que funcionó en las llamadas reales (15:05-15:51 del 21/11): contexto corto y beam 1. No tocar sin
# probar de uno en uno: con un contexto largo y beam 5 Whisper empezó a inventar despedidas.
WHISPER_PROMPT = "Conversación telefónica en español para agendar una cita, con horas y días de la semana."
WHISPER_BEAM = 1

# Alucinaciones clásicas de Whisper en español cuando le llega ruido (frases de subtítulos de YouTube). Si la
# frase ENTERA es una de estas, nadie la dijo: se descarta.
_FANTASMAS = (
    "hasta la próxima", "hasta la proxima", "gracias por ver", "gracias por ver el video", "suscríbete",
    "suscribete", "subtítulos", "subtitulos", "amara.org", "gracias por su atención", "nos vemos en el próximo",
    "nos vemos en el proximo", "no olvides suscribirte", "dale like",
)


def _es_fantasma(texto: str) -> bool:
    t = texto.strip().lower().strip(".,¿?¡!… ")
    return any(t == f or (f in t and len(t) <= len(f) + 12) for f in _FANTASMAS)


class WhisperSTT:
    """Audio de la llamada (slin 8 kHz) -> texto en español."""

    def __init__(self, model_size: str, download_dir) -> None:
        from faster_whisper import WhisperModel

        log.info("Cargando faster-whisper '%s' en CPU (la primera vez se descarga)...", model_size)
        self.model = WhisperModel(model_size, device="cpu", compute_type="int8", download_root=str(download_dir))

    def transcribe(self, pcm8k: bytes) -> str:
        audio16k = resample(pcm16_to_float(pcm8k), SAMPLE_RATE, 16000)
        segments, _ = self.model.transcribe(
            audio16k,
            language="es",
            beam_size=WHISPER_BEAM,
            condition_on_previous_text=False,
            initial_prompt=WHISPER_PROMPT,
        )
        texto = " ".join(s.text.strip() for s in segments).strip()
        if _es_fantasma(texto):
            log.info("Descarto una alucinación típica de Whisper (nadie lo dijo): %r", texto)
            return ""
        return texto


class PiperTTS:
    """Texto -> audio para la llamada (slin 8 kHz)."""

    def __init__(self, voice_path) -> None:
        from piper import PiperVoice

        if not voice_path.exists():
            raise FileNotFoundError(
                f"No encuentro la voz de Piper: {voice_path}. Bájala con el comando del README de telephony."
            )
        log.info("Cargando voz Piper %s...", voice_path.name)
        self.voice = PiperVoice.load(str(voice_path))

    def synthesize(self, text: str) -> bytes:
        partes, rate = [], SAMPLE_RATE
        for chunk in self.voice.synthesize(text):
            rate = chunk.sample_rate
            partes.append(chunk.audio_int16_array.astype(np.float32) / 32768.0)
        if not partes:
            return b""
        return float_to_pcm16(resample(np.concatenate(partes), rate, SAMPLE_RATE))
