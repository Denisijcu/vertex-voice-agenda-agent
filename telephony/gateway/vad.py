"""
vad.py — Detector de voz por energía, trama a trama (20 ms).

Decide cuándo la contraparte empezó a hablar y cuándo terminó la frase (tras VAD_SILENCIO_MS de silencio).
Sencillo a propósito: sin dependencias nativas, y el umbral se ajusta solo al ruido de fondo de la línea.
"""
from collections import deque

from .audio import rms
from .audiosocket import FRAME_SECONDS

PREROLL_FRAMES = 10        # 200 ms de audio previo, para no cortar la primera sílaba
START_FRAMES = 3           # 60 ms seguidos con voz para considerar que empezó a hablar
MIN_SPEECH_S = 0.3         # frases más cortas se descartan (golpes, clics)
MAX_SPEECH_S = 20.0        # una frase nunca pasa de aquí


class EnergyVAD:
    def __init__(self, threshold: float = 600.0, silence_ms: int = 800):
        self.min_threshold = threshold
        self.silence_frames = max(1, int(silence_ms / 1000 / FRAME_SECONDS))
        self.noise = threshold / 3
        self.reset()

    def reset(self) -> None:
        self.preroll: deque[bytes] = deque(maxlen=PREROLL_FRAMES)
        self.speech: list[bytes] = []
        self.voiced_run = 0
        self.silent_run = 0
        self.in_speech = False

    @property
    def threshold(self) -> float:
        return max(self.min_threshold, self.noise * 3.0)

    def feed(self, frame: bytes) -> bytes | None:
        """Mete una trama. Devuelve el audio de una frase completa cuando la contraparte termina de hablar."""
        level = rms(frame)
        voiced = level >= self.threshold
        if not self.in_speech:
            if not voiced:
                self.noise = 0.95 * self.noise + 0.05 * level   # el ruido de fondo se aprende solo en silencio
            self.preroll.append(frame)
            self.voiced_run = self.voiced_run + 1 if voiced else 0
            if self.voiced_run >= START_FRAMES:
                self.in_speech = True
                self.speech = list(self.preroll)
                self.silent_run = 0
            return None

        self.speech.append(frame)
        self.silent_run = 0 if voiced else self.silent_run + 1
        duration = len(self.speech) * FRAME_SECONDS
        if self.silent_run >= self.silence_frames or duration >= MAX_SPEECH_S:
            audio = b"".join(self.speech)
            spoken = duration - self.silent_run * FRAME_SECONDS
            self.reset()
            return audio if spoken >= MIN_SPEECH_S else None
        return None
