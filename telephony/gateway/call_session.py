"""
call_session.py — Una llamada real, de principio a fin.

1. Aviso fijo (no lo dice el modelo): asistente de IA, en nombre de quién y que se transcribe. ¿De acuerdo?
2. Si dice que no, o no queda claro tras repetir la pregunta una vez, o no contesta: despedida fija y colgar.
   El modelo ni se entera.
3. Si dice que sí: turnos con la API del agente (la misma de la UI) hasta que el agente cuelgue.

Conversación por turnos (half-duplex): mientras el agente habla, lo que entra por la línea se descarta.
"""
import asyncio
import json
import logging
import time
from datetime import datetime
from pathlib import Path

from .agent_client import AgentClient, AgentError
from .audiosocket import (
    FRAME_SECONDS, KIND_AUDIO, KIND_ERROR, KIND_HANGUP, KIND_UUID, frames, pack, parse_uuid, read_message,
)
from .consent import clasificar
from .vad import EnergyVAD

log = logging.getLogger("gateway.call")

ESPERA_RESPUESTA_S = 12.0
# Al descolgar habla primero la clínica ("Clínica Dental..., ¿en qué puedo ayudarle?"). El agente espera hasta
# ESPERA_SALUDO_S a que empiece y, si el saludo viene en trozos, junta los que lleguen seguidos.
ESPERA_SALUDO_S = 6.0
CONTINUACION_SALUDO_S = 1.2
DESPEDIDA_SIN_CONSENTIMIENTO = "Entendido, no se preocupe. Gracias por su tiempo, hasta luego."
REPREGUNTA_CONSENTIMIENTO = "Disculpe, ¿está de acuerdo en continuar? Puede decir sí o no."
SIGUE_AHI = "¿Sigue ahí?"
DESPEDIDA_SILENCIO = "Parece que se cortó. Volveremos a llamar más tarde. Gracias."
DESPEDIDA_ERROR = "Disculpe, tuvimos un problema técnico. Volveremos a llamar más tarde. Gracias."
# Si la API rechazó la cita, NO se dice la confirmación del modelo ("queda a las nueve"): se dice esto.
CITA_RECHAZADA = "Perdone, ese horario no lo tengo libre en la agenda. ¿Tendría otro horario disponible?"


class CallSession:
    def __init__(self, reader, writer, *, settings, stt, tts, agent: AgentClient, wait_s: float = ESPERA_RESPUESTA_S,
                 saludo_s: float = ESPERA_SALUDO_S):
        self.saludo_s = saludo_s
        self.saludo: str | None = None          # lo que dijo la clínica al descolgar
        self.reader, self.writer = reader, writer
        self.settings, self.stt, self.tts, self.agent = settings, stt, tts, agent
        self.wait_s = wait_s
        self.vad = EnergyVAD(settings.vad_threshold, settings.vad_silence_ms)
        self.utterances: asyncio.Queue[bytes] = asyncio.Queue()
        self.closed = asyncio.Event()
        self.speaking = False
        self.uuid = "desconocido"
        self.transcript: list[dict] = []
        self.result = "sin_iniciar"
        self.target: dict | None = None          # ficha de la llamada (de la UI vía API, o llamada.json)
        self.model_name: str | None = None       # el que eligió la UI; None = el de la API
        self.ticket_id: str | None = None        # si la llamada salió de la UI
        self._reports: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ línea
    async def _receive_loop(self) -> None:
        try:
            while True:
                kind, payload = await read_message(self.reader)
                if kind == KIND_AUDIO:
                    if self.speaking:
                        continue
                    frase = self.vad.feed(payload)
                    if frase:
                        self.utterances.put_nowait(frase)
                elif kind == KIND_HANGUP:
                    log.info("[%s] La contraparte colgó.", self.uuid)
                    break
                elif kind == KIND_ERROR:
                    log.warning("[%s] Asterisk informó un error: %s", self.uuid, payload.hex())
                    break
        except (asyncio.IncompleteReadError, ConnectionError):
            log.info("[%s] Asterisk cerró la conexión.", self.uuid)
        finally:
            self.closed.set()

    async def _send(self, data: bytes) -> None:
        self.writer.write(data)
        await self.writer.drain()

    async def speak(self, text: str, tools=(), warnings=()) -> None:
        """Sintetiza y reproduce. Las tramas salen a ritmo real (20 ms) para no desbordar el búfer de Asterisk."""
        if self.closed.is_set() or not text.strip():
            return
        self._log("agente", text, tools, warnings)
        pcm = await asyncio.to_thread(self.tts.synthesize, text)
        self.speaking = True
        try:
            inicio = time.monotonic()
            for i, frame in enumerate(frames(pcm)):
                if self.closed.is_set():
                    return
                await self._send(pack(KIND_AUDIO, frame))
                retraso = inicio + (i + 1) * FRAME_SECONDS - time.monotonic()
                if retraso > 0:
                    await asyncio.sleep(retraso)
            await asyncio.sleep(0.2)
        finally:
            self.vad.reset()
            while not self.utterances.empty():      # lo que llegó mientras hablaba el agente no cuenta
                self.utterances.get_nowait()
            self.speaking = False

    async def listen(self, timeout: float | None = None) -> str | None:
        """Espera la siguiente frase de la contraparte y la transcribe. None = silencio o llamada cerrada."""
        while True:
            esperar_frase = asyncio.ensure_future(self.utterances.get())
            esperar_cierre = asyncio.ensure_future(self.closed.wait())
            hechos, _ = await asyncio.wait(
                {esperar_frase, esperar_cierre}, timeout=timeout or self.wait_s, return_when=asyncio.FIRST_COMPLETED
            )
            for tarea in (esperar_frase, esperar_cierre):
                if tarea not in hechos:
                    tarea.cancel()
            if esperar_frase not in hechos:
                return None
            texto = (await asyncio.to_thread(self.stt.transcribe, esperar_frase.result())).strip()
            if texto:
                self._log("contraparte", texto)
                return texto
            # Whisper no entendió nada (ruido): se sigue escuchando.

    async def hangup(self) -> None:
        if not self.closed.is_set():
            try:
                await self._send(pack(KIND_HANGUP))
            except ConnectionError:
                pass
        self.writer.close()

    # ------------------------------------------------------------------ flujo
    async def run(self) -> None:
        try:
            kind, payload = await read_message(self.reader)
        except (asyncio.IncompleteReadError, ConnectionError):
            log.info("Conexión cerrada antes de empezar (sin datos de Asterisk).")
            self.writer.close()
            return
        if kind != KIND_UUID:
            log.warning("Primer mensaje inesperado de Asterisk (%s); cierro.", kind)
            self.writer.close()
            return
        self.uuid = parse_uuid(payload)
        log.info("[%s] Llamada conectada.", self.uuid)
        await self._reclamar_ficha()
        receptor = asyncio.create_task(self._receive_loop())
        try:
            if await self._consentimiento():
                await self._conversacion()
        except AgentError as exc:
            log.error("[%s] %s", self.uuid, exc)
            self.result = "error_api"
            await self.speak(DESPEDIDA_ERROR)
        finally:
            await self.hangup()
            receptor.cancel()
            self._guardar_transcripcion()
            log.info("[%s] Fin de la llamada: %s", self.uuid, self.result)
            # Con la transcripción entera: si la API se reinició a mitad de llamada, la UI la recupera igual.
            await self._report_safe(self.agent.report_end(self.uuid, self.result, {
                "ticket_id": self.ticket_id,
                "contact_name": (self.target or {}).get("contact_name"),
                "purpose": (self.target or {}).get("purpose"),
                "events": self.transcript,
            }))
            if self._reports:
                await asyncio.wait(self._reports, timeout=3)

    async def _escuchar_saludo(self) -> None:
        """La clínica contesta y saluda; el agente no la pisa. Si contesta en silencio, el agente arranca igual."""
        partes = []
        frase = await self.listen(timeout=self.saludo_s)
        while frase is not None:
            partes.append(frase)
            frase = await self.listen(timeout=CONTINUACION_SALUDO_S)
        self.saludo = " ".join(partes) or None
        if self.saludo:
            log.info("[%s] La clínica saludó: %s", self.uuid, self.saludo)

    async def _consentimiento(self) -> bool:
        await self._escuchar_saludo()
        if self.closed.is_set():
            self.result = "colgada"
            return False
        await self.speak(self.settings.disclosure)
        for intento in range(2):
            respuesta = await self.listen()
            if respuesta is None:
                self.result = "colgada" if self.closed.is_set() else "sin_respuesta_al_aviso"
                if not self.closed.is_set():
                    await self.speak(DESPEDIDA_SILENCIO)
                return False
            decision = clasificar(respuesta)
            log.info("[%s] Consentimiento: '%s' -> %s", self.uuid, respuesta, decision)
            if decision == "si":
                self.result = "consentimiento_si"
                return True
            if decision == "no" or intento == 1:
                self.result = "sin_consentimiento"
                await self.speak(DESPEDIDA_SIN_CONSENTIMIENTO)
                return False
            await self.speak(REPREGUNTA_CONSENTIMIENTO)
        return False

    async def _reclamar_ficha(self) -> None:
        """Pide a la API la ficha de esta llamada. Si la lanzó la UI, llega la suya; si no, se usa llamada.json."""
        try:
            local = self.settings.load_target()
        except (FileNotFoundError, ValueError) as exc:
            log.warning("[%s] Sin ficha local (%s).", self.uuid, exc)
            local = None
        try:
            datos = await self.agent.claim(self.uuid, local)
            self.target = datos.get("target") or local
            self.model_name = datos.get("model_name")
            self.ticket_id = datos.get("ticket_id")
            if datos.get("ticket_id"):
                log.info("[%s] Llamada lanzada desde la UI (%s): %s", self.uuid, datos["ticket_id"],
                         (self.target or {}).get("contact_name"))
        except AgentError as exc:
            log.warning("[%s] %s Uso llamada.json.", self.uuid, exc)
            self.target = local

    async def _conversacion(self) -> None:
        if not self.target:
            raise AgentError("No hay ficha de llamada (ni de la UI ni en llamada.json).")
        target = self.target
        turno = await self.agent.step(target, model_name=self.model_name)
        call_id = turno["call_id"]
        silencios = 0
        while True:
            self._avisos(turno)
            await self.speak(self._frase_segura(turno), [t.get("tool") for t in turno.get("tool_calls", [])],
                             turno.get("warnings", []))
            if turno.get("terminated"):
                self.result = turno.get("outcome", "terminada")
                return
            if self.closed.is_set():
                self.result = "colgada_por_contraparte"
                return
            frase = await self.listen()
            while frase is None:
                if self.closed.is_set():
                    self.result = "colgada_por_contraparte"
                    return
                silencios += 1
                if silencios >= 2:
                    self.result = "silencio"
                    await self.speak(DESPEDIDA_SILENCIO)
                    return
                await self.speak(SIGUE_AHI)      # y se vuelve a escuchar, sin repetir la última frase
                frase = await self.listen()
            silencios = 0
            turno = await self.agent.step(target, call_id=call_id, utterance=frase, model_name=self.model_name)

    def _frase_segura(self, turno: dict) -> str:
        """Lo que se dice por teléfono. Nunca se confirma una cita que la API rechazó."""
        avisos = turno.get("warnings", [])
        if any(a.startswith("Respuesta corregida por la API") for a in avisos):
            return turno["assistant_reply"]          # la API ya compuso una respuesta segura con huecos reales
        if any(a.startswith("book_appointment rechazado") for a in avisos):
            log.warning("[%s] La API rechazó la cita: se corrige por voz en lugar de leer la respuesta del modelo.",
                        self.uuid)
            return CITA_RECHAZADA
        return turno["assistant_reply"]

    # ------------------------------------------------------------------ registro
    def _avisos(self, turno: dict) -> None:
        tools = [t.get("tool") for t in turno.get("tool_calls", [])]
        if tools:
            log.info("[%s] Herramientas: %s", self.uuid, tools)
        for aviso in turno.get("warnings", []):
            log.warning("[%s] Aviso de la API: %s", self.uuid, aviso)

    def _log(self, quien: str, texto: str, tools=(), warnings=()) -> None:
        log.info("[%s] %s: %s", self.uuid, quien.upper(), texto)
        self.transcript.append({"t": datetime.now().astimezone().isoformat(timespec="seconds"), "quien": quien,
                                "texto": texto, "tools": [t for t in tools if t], "warnings": list(warnings)})
        # A la UI en vivo, sin esperar: el audio no se frena por la red.
        tarea = asyncio.ensure_future(self._report_safe(
            self.agent.report_event(self.uuid, quien, texto, [t for t in tools if t], list(warnings))))
        self._reports.add(tarea)
        tarea.add_done_callback(self._reports.discard)

    @staticmethod
    async def _report_safe(coro) -> None:
        try:
            await coro
        except Exception as exc:  # un reporte perdido nunca tumba la llamada
            log.debug("Reporte perdido: %s", exc)

    def _guardar_transcripcion(self) -> None:
        carpeta: Path = self.settings.logs_dir
        carpeta.mkdir(parents=True, exist_ok=True)
        archivo = carpeta / f"{datetime.now():%Y%m%d-%H%M%S}_{self.uuid[:8]}.json"
        archivo.write_text(
            json.dumps({"uuid": self.uuid, "resultado": self.result, "ticket_id": self.ticket_id,
                        "contact_name": (self.target or {}).get("contact_name"),
                        "purpose": (self.target or {}).get("purpose"), "turnos": self.transcript},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
