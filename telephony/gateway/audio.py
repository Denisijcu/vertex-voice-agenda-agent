"""audio.py — Conversiones de audio con numpy (sin scipy): cambio de frecuencia de muestreo y volumen."""
import numpy as np


def pcm16_to_float(pcm: bytes) -> np.ndarray:
    return np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0


def float_to_pcm16(samples: np.ndarray) -> bytes:
    return (np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


def rms(pcm: bytes) -> float:
    if not pcm:
        return 0.0
    x = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
    return float(np.sqrt(np.mean(x * x)))


def _lowpass(samples: np.ndarray, cutoff_ratio: float, taps: int = 63) -> np.ndarray:
    """FIR paso bajo (sinc con ventana de Hamming). cutoff_ratio = corte / frecuencia de muestreo."""
    n = np.arange(taps) - (taps - 1) / 2
    h = np.sinc(2 * cutoff_ratio * n) * np.hamming(taps)
    h /= h.sum()
    return np.convolve(samples, h, mode="same").astype(np.float32)


def resample(samples: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """Cambia la frecuencia de muestreo. Al bajar, filtra antes para no meter ruido (aliasing)."""
    if src_rate == dst_rate or samples.size == 0:
        return samples.astype(np.float32)
    if dst_rate < src_rate:
        samples = _lowpass(samples, 0.45 * dst_rate / src_rate)
    duration = samples.size / src_rate
    n_out = max(1, int(round(duration * dst_rate)))
    t_src = np.arange(samples.size) / src_rate
    t_dst = np.arange(n_out) / dst_rate
    return np.interp(t_dst, t_src, samples).astype(np.float32)
