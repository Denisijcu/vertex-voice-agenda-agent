import asyncio
import logging
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .call_history import CallHistory
from .call_orchestrator import CallFinishedError, CallInputError, CallOrchestrator
from .config import settings
from .schemas import StartCallRequest, TurnExecutionResponse
from .telephony_bridge import (
    AsteriskControl,
    ClaimRequest,
    LineNotReady,
    RealCallRequest,
    TelephonyError,
    TelephonyService,
    TelephonyStore,
    VoiceEnd,
    VoiceEvent,
)

app = FastAPI(
    title="Vertex Voice Agenda Agent API",
    version="0.2.0",
    docs_url="/docs",
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Content-Type"],
)

_call_semaphore = asyncio.Semaphore(settings.max_concurrent_calls)
_orchestrator = CallOrchestrator()
_API_DIR = Path(__file__).resolve().parent.parent


def _ruta(valor: str) -> Path:
    p = Path(valor)
    return p if p.is_absolute() else (_API_DIR / p).resolve()


_history = CallHistory(_ruta(settings.call_history_db))
_importadas = _history.import_gateway_logs(_ruta(settings.telephony_logs_dir))
if _importadas:
    logging.getLogger("vva").info("Historial: importadas %d transcripciones de telephony/logs.", _importadas)
_telephony = TelephonyService(
    AsteriskControl(settings.telephony_docker_bin, settings.telephony_container),
    TelephonyStore(settings.telephony_ticket_ttl_sec, history=_history),
    settings.telephony_sip_user,
)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "Error interno al procesar la llamada o la agenda."},
    )


@app.get("/health")
async def health_check() -> dict[str, str]:
    return {"status": "ok", "model": settings.llm_default_model, "sip_bridge": str(settings.sip_bridge_enabled)}


@app.get("/api/v1/calendar/events")
async def list_calendar_events() -> list[dict]:
    """Citas en memoria (reales y simuladas) para revisar desde la UI o /docs."""
    return [
        {
            "event_id": ev.event_id,
            "title": ev.title,
            "start_iso": ev.start_iso.isoformat(),
            "end_iso": ev.end_iso.isoformat(),
            "attendee_or_location": ev.attendee_or_location,
            "purpose": ev.purpose.value,
            "dry_run": ev.dry_run,
        }
        for ev in _orchestrator.calendar.list_events()
    ]


@app.post("/api/v1/calls/step", response_model=TurnExecutionResponse)
async def step_call(request: StartCallRequest) -> TurnExecutionResponse:
    async with _call_semaphore:
        try:
            return await _orchestrator.start_or_step_call(request)
        except CallFinishedError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
        except CallInputError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
        except httpx.TimeoutException:
            raise HTTPException(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                detail="Tiempo de espera agotado consultando el modelo local.",
            )
        except httpx.HTTPError:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Error de comunicación con el servidor de inferencia local (¿LM Studio encendido en el 1234?).",
            )


# ------------------------------------------------------------------------------------------------ telefonía real
# UI: estado de la línea, lanzar una llamada real, seguirla en vivo e historial.
@app.get("/api/v1/telephony/status")
async def telephony_status() -> dict:
    return await _telephony.status()


@app.post("/api/v1/telephony/calls")
async def telephony_start_call(request: RealCallRequest) -> dict:
    try:
        ticket = await _telephony.start_call(request)
    except LineNotReady as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except TelephonyError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Asterisk no pudo marcar: {exc}")
    return {"ticket_id": ticket.ticket_id, "estado": "llamando"}


@app.get("/api/v1/telephony/tickets/{ticket_id}")
async def telephony_ticket(ticket_id: str) -> dict:
    data = _telephony.store.ticket_status(ticket_id)
    if data is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Esa llamada no existe.")
    return data


@app.get("/api/v1/telephony/calls")
async def telephony_calls(limit: int = 10, offset: int = 0, q: str = "") -> dict:
    """Historial de llamadas reales (permanente), las más nuevas primero. q busca en clínica, resultado y frases."""
    return _telephony.store.list_calls(max(1, min(limit, 100)), max(0, offset), q[:100])


@app.get("/api/v1/telephony/calls/{voice_id}")
async def telephony_call_detail(voice_id: str) -> dict:
    data = _telephony.store.get_call(voice_id)
    if data is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Esa llamada no está en el historial.")
    return data


@app.delete("/api/v1/telephony/calls/{voice_id}")
async def telephony_delete_call(voice_id: str) -> dict:
    try:
        ok = _telephony.store.delete_call(voice_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    if not ok:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Esa llamada no está en el historial.")
    return {"ok": True}


@app.delete("/api/v1/telephony/calls")
async def telephony_delete_all_calls(confirm: bool = False) -> dict:
    """Borra todo el historial (menos lo que esté en curso). Exige ?confirm=true para no borrarlo por accidente."""
    if not confirm:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Falta confirm=true.")
    return {"ok": True, "borradas": _telephony.store.delete_all_calls()}


# Pasarela de voz (telephony/gateway): pide la ficha, reporta cada frase y el final, y avisa de que sigue viva.
@app.post("/api/v1/telephony/claim")
async def telephony_claim(request: ClaimRequest) -> dict:
    fallback = request.fallback_target.model_dump(mode="json") if request.fallback_target else None
    ticket, call = _telephony.store.claim(request.voice_id, fallback)
    return {
        "ticket_id": ticket.ticket_id if ticket else None,
        "target": call.target or None,
        "model_name": ticket.model_name if ticket else None,
    }


@app.post("/api/v1/telephony/calls/{voice_id}/events")
async def telephony_event(voice_id: str, event: VoiceEvent) -> dict:
    if not _telephony.store.add_event(voice_id, event):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Llamada desconocida.")
    return {"ok": True}


@app.post("/api/v1/telephony/calls/{voice_id}/end")
async def telephony_end(voice_id: str, end: VoiceEnd) -> dict:
    if not _telephony.store.finish(end, voice_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Llamada desconocida.")
    return {"ok": True}


@app.post("/api/v1/telephony/heartbeat")
async def telephony_heartbeat() -> dict:
    _telephony.store.heartbeat()
    return {"ok": True}
