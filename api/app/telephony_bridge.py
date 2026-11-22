"""
telephony_bridge.py — Une la UI con las llamadas de voz reales (telephony/: Asterisk + pasarela).

Flujo de una llamada lanzada desde la UI:
  1. UI  -> POST /api/v1/telephony/calls  {target, model_name}
     La API comprueba la línea (Asterisk, celular disponible, pasarela viva), guarda un "ticket" con la ficha de
     la llamada y le pide a Asterisk que marque al celular (docker exec ... channel originate).
  2. Asterisk conecta la llamada con la pasarela, que hace POST /api/v1/telephony/claim: recibe la ficha del
     ticket (o, si la llamada no salió de la UI, usa la suya de llamada.json).
  3. La pasarela reporta cada frase (POST .../events) y el final (POST .../end). La UI lo lee en vivo con
     GET /api/v1/telephony/tickets/{ticket_id}.

Nada de esto pasa por el modelo: los turnos siguen yendo por /api/v1/calls/step, como siempre.
"""
import asyncio
import logging
import os
import re
import shutil
import subprocess
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable
from uuid import uuid4

from pydantic import BaseModel, Field

from .call_history import CallHistory
from .schemas import VoiceTargetInput

log = logging.getLogger("vva.telephony")

SIP_USER_RE = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")
_MAX_EVENTS = 300
_MAX_CALLS = 50
_HEARTBEAT_TTL_S = 12.0
_STATUS_CACHE_S = 2.0

Runner = Callable[[list[str]], Awaitable[tuple[int, str]]]


class TelephonyError(Exception):
    """Fallo hablando con Docker/Asterisk."""


class LineNotReady(Exception):
    """La línea no está lista para llamar (el texto explica por qué)."""


# ---------------------------------------------------------------------------------------------- esquemas
class RealCallRequest(BaseModel):
    target: VoiceTargetInput
    model_name: str | None = Field(default=None, max_length=128)


class ClaimRequest(BaseModel):
    voice_id: str = Field(..., min_length=1, max_length=64)
    fallback_target: VoiceTargetInput | None = None


class VoiceEvent(BaseModel):
    quien: str = Field(..., pattern=r"^(agente|contraparte|sistema)$")
    texto: str = Field(..., max_length=2000)
    tools: list[str] = Field(default_factory=list, max_length=10)
    warnings: list[str] = Field(default_factory=list, max_length=10)


class VoiceEndEvent(VoiceEvent):
    t: str | None = Field(default=None, max_length=40)


class VoiceEnd(BaseModel):
    result: str = Field(..., max_length=64)
    # Resumen de la pasarela: con él se recupera la llamada si la API se reinició mientras sonaba.
    ticket_id: str | None = Field(default=None, max_length=40)
    contact_name: str | None = Field(default=None, max_length=200)
    purpose: str | None = Field(default=None, max_length=64)
    events: list[VoiceEndEvent] = Field(default_factory=list, max_length=_MAX_EVENTS)


# ---------------------------------------------------------------------------------------------- Asterisk
_DOCKER_WINDOWS = r"C:\Program Files\Docker\Docker\resources\bin\docker.exe"


async def _run_subprocess(args: list[str]) -> tuple[int, str]:
    """docker exec ... sin shell. En un hilo: funciona con cualquier bucle de eventos (también en Windows)."""

    def _run() -> tuple[int, str]:
        cmd = list(args)
        if cmd[0] == "docker" and os.name == "nt" and shutil.which("docker") is None and os.path.exists(
            _DOCKER_WINDOWS
        ):
            cmd[0] = _DOCKER_WINDOWS     # la terminal arrancó antes que Docker Desktop y no tiene docker en el PATH
        try:
            done = subprocess.run(cmd, capture_output=True, text=True, timeout=8, encoding="utf-8",
                                  errors="replace")
        except FileNotFoundError:
            return 127, f"No encuentro el ejecutable '{cmd[0]}'."
        except subprocess.TimeoutExpired:
            return 124, "Docker tardó demasiado en responder."
        return done.returncode, (done.stdout or "") + (done.stderr or "")

    return await asyncio.to_thread(_run)


class AsteriskControl:
    def __init__(self, docker_bin: str, container: str, runner: Runner | None = None) -> None:
        self.docker_bin = docker_bin
        self.container = container
        self._run = runner or _run_subprocess

    async def is_running(self) -> bool:
        code, out = await self._run([self.docker_bin, "inspect", "-f", "{{.State.Running}}", self.container])
        return code == 0 and out.strip() == "true"

    async def rx(self, command: str) -> str:
        code, out = await self._run([self.docker_bin, "exec", self.container, "asterisk", "-rx", command])
        if code != 0:
            raise TelephonyError(out.strip()[:300] or f"docker exec devolvió {code}")
        return out

    async def contact_status(self, sip_user: str) -> str | None:
        """Estado del teléfono registrado: Avail, Unavail, NonQual... o None si no está registrado."""
        out = await self.rx("pjsip show contacts")
        for line in out.splitlines():
            if re.match(rf"\s*Contact:\s+{re.escape(sip_user)}/", line):
                partes = line.split()
                for estado in ("Avail", "Unavail", "NonQual", "Unknown", "Created", "Removed"):
                    if estado in partes:
                        return estado
                return "Desconocido"
        return None

    async def originate(self, sip_user: str) -> None:
        if not SIP_USER_RE.match(sip_user):
            raise TelephonyError("Usuario SIP no válido.")
        await self.rx(f"channel originate PJSIP/{sip_user} extension agente@agente")


# ---------------------------------------------------------------------------------------------- estado
@dataclass
class Ticket:
    ticket_id: str
    target: dict[str, Any]
    model_name: str | None
    created: float = field(default_factory=time.monotonic)
    voice_id: str | None = None


@dataclass
class VoiceCall:
    voice_id: str
    target: dict[str, Any]
    ticket_id: str | None
    started: str = field(default_factory=lambda: datetime.now().astimezone().isoformat(timespec="seconds"))
    status: str = "en_curso"            # en_curso | terminada
    result: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "voice_id": self.voice_id, "ticket_id": self.ticket_id, "started": self.started, "status": self.status,
            "result": self.result, "contact_name": self.target.get("contact_name"),
            "purpose": self.target.get("purpose"), "events": list(self.events),
        }


class TelephonyStore:
    def __init__(self, ticket_ttl_s: float = 90.0, history: "CallHistory | None" = None) -> None:
        self.history = history          # historial permanente (SQLite); None = solo memoria (pruebas)
        self.ticket_ttl_s = ticket_ttl_s
        self.tickets: "OrderedDict[str, Ticket]" = OrderedDict()
        self.calls: "OrderedDict[str, VoiceCall]" = OrderedDict()
        self.last_heartbeat: float | None = None

    # -- tickets (llamadas pedidas desde la UI)
    def create_ticket(self, target: dict[str, Any], model_name: str | None) -> Ticket:
        ticket = Ticket(ticket_id=f"tkt_{uuid4().hex[:10]}", target=target, model_name=model_name)
        self.tickets[ticket.ticket_id] = ticket
        while len(self.tickets) > _MAX_CALLS:
            self.tickets.popitem(last=False)
        return ticket

    def drop_ticket(self, ticket_id: str) -> None:
        self.tickets.pop(ticket_id, None)

    def _expired(self, ticket: Ticket) -> bool:
        return ticket.voice_id is None and time.monotonic() - ticket.created > self.ticket_ttl_s

    def claim(self, voice_id: str, fallback_target: dict[str, Any] | None) -> tuple[Ticket | None, VoiceCall]:
        """La pasarela reclama la ficha de la llamada que acaba de conectar: el ticket pendiente más antiguo."""
        if voice_id in self.calls:
            call = self.calls[voice_id]
            return (self.tickets.get(call.ticket_id) if call.ticket_id else None), call
        ticket = next((t for t in self.tickets.values() if t.voice_id is None and not self._expired(t)), None)
        if ticket:
            ticket.voice_id = voice_id
            target = ticket.target
        elif fallback_target is not None:
            target = fallback_target
        else:
            target = {}
        call = VoiceCall(voice_id=voice_id, target=target, ticket_id=ticket.ticket_id if ticket else None)
        self.calls[voice_id] = call
        while len(self.calls) > _MAX_CALLS:
            self.calls.popitem(last=False)
        self._persist(call)
        return ticket, call

    def ticket_status(self, ticket_id: str) -> dict[str, Any] | None:
        ticket = self.tickets.get(ticket_id)
        if ticket is None:
            return None
        call = self.calls.get(ticket.voice_id) if ticket.voice_id else None
        if call:
            estado = call.status
        elif self._expired(ticket):
            estado = "sin_contestar"
        else:
            estado = "llamando"
        return {"ticket_id": ticket_id, "estado": estado, "call": call.to_dict() if call else None}

    # -- eventos que reporta la pasarela
    def add_event(self, voice_id: str, event: VoiceEvent) -> bool:
        call = self.calls.get(voice_id)
        if call is None:
            return False
        if len(call.events) < _MAX_EVENTS:
            call.events.append({"t": datetime.now().astimezone().isoformat(timespec="seconds"),
                                **event.model_dump()})
        self._persist(call)
        return True

    def finish(self, end: "VoiceEnd", voice_id: str) -> bool:
        call = self.calls.get(voice_id)
        if call is None:
            if not end.events:
                return False
            # La API se reinició durante la llamada: se reconstruye con el resumen de la pasarela.
            call = VoiceCall(voice_id=voice_id, ticket_id=end.ticket_id,
                             target={"contact_name": end.contact_name, "purpose": end.purpose})
            self.calls[voice_id] = call
            while len(self.calls) > _MAX_CALLS:
                self.calls.popitem(last=False)
        if end.ticket_id and end.ticket_id not in self.tickets:
            self.tickets[end.ticket_id] = Ticket(ticket_id=end.ticket_id, target=call.target, model_name=None,
                                                 voice_id=voice_id)
        if len(end.events) > len(call.events):
            ahora = datetime.now().astimezone().isoformat(timespec="seconds")
            call.events = [{"t": e.t or ahora, **e.model_dump(exclude={"t"})} for e in end.events]
        call.status, call.result = "terminada", end.result
        self._persist(call)
        return True

    def _persist(self, call: "VoiceCall") -> None:
        if self.history is not None:
            try:
                self.history.save(call.to_dict(), origen="ui" if call.ticket_id else "pasarela")
            except Exception as exc:  # el historial nunca tumba una llamada
                log.warning("No pude guardar la llamada %s en el historial: %s", call.voice_id, exc)

    def list_calls(self, limit: int = 10, offset: int = 0, q: str = "") -> dict[str, Any]:
        if self.history is not None:
            return self.history.list(limit, offset, q)
        calls = [c for c in reversed(self.calls.values())
                 if not q or q.lower() in (c.target.get("contact_name") or "").lower()]
        items = [{k: v for k, v in c.to_dict().items() if k != "events"} | {"turnos": len(c.events)}
                 for c in calls[offset:offset + limit]]
        return {"total": len(calls), "items": items}

    def active_ids(self) -> set[str]:
        return {c.voice_id for c in self.calls.values() if c.status == "en_curso"}

    def delete_call(self, voice_id: str) -> bool:
        """Borra una llamada terminada del historial (y de la memoria)."""
        if voice_id in self.active_ids():
            raise ValueError("Esa llamada está en curso; se puede borrar cuando termine.")
        en_memoria = self.calls.pop(voice_id, None) is not None
        en_disco = self.history.delete(voice_id) if self.history is not None else False
        return en_memoria or en_disco

    def delete_all_calls(self) -> int:
        activas = self.active_ids()
        borradas_mem = [v for v in self.calls if v not in activas]
        for v in borradas_mem:
            self.calls.pop(v, None)
        if self.history is not None:
            return self.history.delete_all(keep=activas)
        return len(borradas_mem)

    def get_call(self, voice_id: str) -> dict[str, Any] | None:
        if voice_id in self.calls:
            return self.calls[voice_id].to_dict()
        return self.history.get(voice_id) if self.history is not None else None

    # -- pasarela viva
    def heartbeat(self) -> None:
        self.last_heartbeat = time.monotonic()

    @property
    def gateway_alive(self) -> bool:
        return self.last_heartbeat is not None and time.monotonic() - self.last_heartbeat < _HEARTBEAT_TTL_S


class TelephonyService:
    def __init__(self, asterisk: AsteriskControl, store: TelephonyStore, sip_user: str) -> None:
        self.asterisk, self.store, self.sip_user = asterisk, store, sip_user
        self._status_cache: tuple[float, dict[str, Any]] | None = None

    async def status(self, use_cache: bool = True) -> dict[str, Any]:
        if use_cache and self._status_cache and time.monotonic() - self._status_cache[0] < _STATUS_CACHE_S:
            return self._status_cache[1]
        estado: dict[str, Any] = {"asterisk": False, "phone": None, "gateway": self.store.gateway_alive,
                                  "sip_user": self.sip_user, "ready": False, "detail": ""}
        try:
            estado["asterisk"] = await self.asterisk.is_running()
            if estado["asterisk"]:
                estado["phone"] = await self.asterisk.contact_status(self.sip_user)
        except TelephonyError as exc:
            estado["detail"] = str(exc)
        if not estado["asterisk"]:
            estado["detail"] = estado["detail"] or "Asterisk no está corriendo (docker compose up en telephony)."
        elif estado["phone"] != "Avail":
            estado["detail"] = (f"El teléfono de la clínica (Linphone, usuario {self.sip_user}) no está disponible: "
                                "abre Linphone y deja la pantalla encendida.")
        elif not estado["gateway"]:
            estado["detail"] = "La pasarela de voz no está corriendo (python -m gateway en telephony)."
        else:
            estado["ready"] = True
            estado["detail"] = "Línea lista."
        self._status_cache = (time.monotonic(), estado)
        return estado

    async def start_call(self, request: RealCallRequest) -> Ticket:
        estado = await self.status(use_cache=False)
        if not estado["ready"]:
            raise LineNotReady(estado["detail"])
        ticket = self.store.create_ticket(request.target.model_dump(mode="json"), request.model_name)
        try:
            await self.asterisk.originate(self.sip_user)
        except TelephonyError:
            self.store.drop_ticket(ticket.ticket_id)
            raise
        return ticket
