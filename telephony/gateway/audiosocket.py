"""
audiosocket.py — Protocolo AudioSocket de Asterisk (app_audiosocket / res_audiosocket).

Cada mensaje: 1 byte de tipo + 2 bytes de longitud (big-endian) + carga.
  0x00 colgar · 0x01 UUID de la llamada (16 bytes) · 0x03 DTMF · 0x10 audio · 0xff error
El audio va en ambos sentidos como PCM lineal de 16 bits, 8000 Hz, mono (slin): 320 bytes = 20 ms.
"""
import asyncio
import uuid

KIND_HANGUP = 0x00
KIND_UUID = 0x01
KIND_DTMF = 0x03
KIND_AUDIO = 0x10
KIND_ERROR = 0xFF

SAMPLE_RATE = 8000
FRAME_BYTES = 320          # 20 ms de slin a 8 kHz
FRAME_SECONDS = 0.02


def pack(kind: int, payload: bytes = b"") -> bytes:
    if len(payload) > 0xFFFF:
        raise ValueError("Carga de AudioSocket demasiado grande.")
    return bytes([kind]) + len(payload).to_bytes(2, "big") + payload


async def read_message(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    """Lee un mensaje completo. Lanza asyncio.IncompleteReadError si Asterisk cerró la conexión."""
    header = await reader.readexactly(3)
    length = int.from_bytes(header[1:3], "big")
    payload = await reader.readexactly(length) if length else b""
    return header[0], payload


def parse_uuid(payload: bytes) -> str:
    return str(uuid.UUID(bytes=payload)) if len(payload) == 16 else payload.hex()


def frames(pcm: bytes) -> list[bytes]:
    """Parte el audio en tramas de 20 ms; la última se rellena con silencio."""
    out = [pcm[i:i + FRAME_BYTES] for i in range(0, len(pcm), FRAME_BYTES)]
    if out and len(out[-1]) < FRAME_BYTES:
        out[-1] = out[-1] + b"\x00" * (FRAME_BYTES - len(out[-1]))
    return out
