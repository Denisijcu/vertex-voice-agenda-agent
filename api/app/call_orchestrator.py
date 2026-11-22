"""
call_orchestrator.py — Coordina cada turno de la llamada: arma los mensajes (formato de policy.py), llama al modelo
local, valida sus tool calls con Pydantic y las ejecuta contra la agenda y el puente SIP.

Reglas de seguridad que aplica el orquestador, diga lo que diga el modelo:
  - Una cita solo se agenda si cae dentro de los huecos libres consultados en esta llamada. Nunca se agenda sola.
  - Una llamada colgada no acepta más turnos (CallFinishedError -> HTTP 409).
  - Si el modelo no devuelve JSON válido, no se lee su texto crudo por teléfono.
"""
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import json
import re
from typing import Any
from uuid import uuid4

import httpx
from pydantic import TypeAdapter, ValidationError

from .calendar_bridge import CalendarBridge, is_covered_by_slots
from .config import effective_dry_run, settings
from .policy import (
    ALLOWED_TOOLS,
    SYSTEM_CALL_POLICY,
    assistant_json,
    build_followup_message,
    build_opening_message,
)
from .schemas import (
    AgendaToolCall,
    CallOutcome,
    CheckCalendarArgs,
    CheckCalendarToolCall,
    HangupCallArgs,
    HangupCallToolCall,
    SipRtpSessionStatus,
    StartCallRequest,
    TurnExecutionResponse,
)
from .sip_rtp_bridge import SipRtpBridge
from .timeutil import at_local, local_now, next_business_day_window, today_label

_AGENDA_TOOL_CALL_ADAPTER = TypeAdapter(AgendaToolCall)
_MAX_CALLS_IN_MEMORY = 200
_FALLBACK_REPLY = "Disculpe, ¿me lo podría repetir, por favor?"
_GOODBYE_REPLY = "Muchas gracias a usted. Que tenga un buen día, hasta luego."
# Rechazo firme: ahí no se insiste nunca.
_FIRM_REFUSAL_MARKERS = (
    "no me interesa",
    "no nos interesa",
    "no estamos interesados",
    "no estoy interesad",
    "no llame",
    "no vuelva",
    "no vuelvan",
    "deje de llamar",
    "dejen de llamar",
    "quíteme",
    "quiteme",
    "no quiero",
)
_FRANJAS = ((9, 12, "mañana"), (13, 17, "tarde"))   # las mismas que en la política (policy.py)
_DIAS_INSISTENCIA = 5                               # días hábiles en los que se buscan otros horarios
_DIAS = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")

_CLOSING_MARKERS = (
    "hasta mañana",
    "hasta luego",
    "adiós",
    "adios",
    "nos vemos",
    "que tenga buen día",
    "que tenga un buen día",
    "gracias por llamar",
)


# Esquema del turno del modelo. Con la salida estructurada de LM Studio (gramática de llama.cpp) el modelo no puede
# escribir otra cosa: ni texto suelto ni el formato nativo de Qwen ("<tool_call>"). Los argumentos se dejan libres
# aquí; los valida Pydantic después, herramienta por herramienta.
TURN_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "assistant_reply": {"type": "string"},
        "tool_calls": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "tool": {"type": "string", "enum": list(ALLOWED_TOOLS)},
                    "arguments": {"type": "object"},
                },
                "required": ["tool", "arguments"],
            },
        },
    },
    "required": ["assistant_reply", "tool_calls"],
}


class CallFinishedError(Exception):
    """La llamada ya se colgó: no admite más turnos."""


class CallInputError(Exception):
    """La petición no sirve para este turno (p. ej. seguimiento sin respuesta de la contraparte)."""


@dataclass
class _CallState:
    call_id: str
    session: SipRtpSessionStatus
    window_start: datetime
    window_end: datetime
    messages: list[dict[str, str]] = field(default_factory=list)
    # None = sin consulta vigente (aún no se consultó o ya se agendó); [] = consultada y sin huecos.
    checked_slots: list[dict[str, str]] | None = None
    booked_count: int = 0
    outcome: CallOutcome = CallOutcome.PENDING
    terminated: bool = False
    insisted: bool = False          # ya se ofrecieron otros horarios una vez (no se insiste dos veces)
    booked_start: datetime | None = None   # inicio de la cita agendada en ESTE turno (para confirmarla bien)
    booked_event_id: str | None = None     # la última cita agendada (por si la clínica se echa atrás)
    booked_last_turn: bool = False         # se agendó en el turno anterior
    proposed: tuple[datetime, datetime] | None = None   # horario propuesto en este turno en vez de agendarlo
    last_utterance: str = ""
    rejected_at: datetime | None = None    # hora que pidió el modelo y que la agenda NO tiene libre (este turno)


def _strip_think_blocks(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE).strip()


def _extract_json_payload(raw_text: str) -> dict[str, Any] | None:
    cleaned = _strip_think_blocks(raw_text)
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE | re.DOTALL).strip()
    for candidate in (cleaned, *(m.group(0) for m in [re.search(r"\{.*\}", cleaned, flags=re.DOTALL)] if m)):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    # JSON completo seguido de basura (el modelo siguió escribiendo): se queda el primer objeto entero.
    inicio = cleaned.find("{")
    if inicio >= 0:
        try:
            parsed, _ = json.JSONDecoder().raw_decode(cleaned[inicio:])
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    return None


def _normalize_tool_call(item: Any) -> Any:
    """Acepta las formas habituales en que un modelo escribe una tool call y las lleva a {"tool", "arguments"}.

    Formas aceptadas: {"tool", "arguments"} (la de entrenamiento), {"name", "arguments"|"args"|"parameters"},
    {"function": {"name", "arguments"}} / {"type": "function", "function": ...} (estilo OpenAI), y "arguments" como
    texto JSON. Si no reconoce la forma, devuelve el item tal cual y la validación lo rechaza con aviso.
    """
    if not isinstance(item, dict):
        return item
    fn = item.get("function") if isinstance(item.get("function"), dict) else {}
    name = item.get("tool") or item.get("name") or item.get("tool_name") or fn.get("name")
    args = next(
        (src[k] for src in (item, fn) for k in ("arguments", "args", "parameters") if k in src),
        {},
    )
    if isinstance(args, str):
        try:
            args = json.loads(args) if args.strip() else {}
        except json.JSONDecodeError:
            pass
    if not isinstance(name, str):
        return item
    return {"tool": name, "arguments": args}


def _short(item: Any, limit: int = 160) -> str:
    try:
        text = json.dumps(item, ensure_ascii=False)
    except (TypeError, ValueError):
        text = repr(item)
    return text if len(text) <= limit else text[: limit - 1] + "…"


class CallOrchestrator:
    """Coordina la inferencia del modelo, valida tool calls y mantiene el estado por llamada."""

    def __init__(
        self,
        calendar_bridge: CalendarBridge | None = None,
        sip_bridge: SipRtpBridge | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.calendar = calendar_bridge or CalendarBridge()
        self.sip = sip_bridge or SipRtpBridge()
        self._transport = transport  # en los tests se inyecta un transporte falso en vez de LM Studio
        self._calls: "OrderedDict[str, _CallState]" = OrderedDict()

    # ------------------------------------------------------------------ estado
    def _prune(self) -> None:
        while len(self._calls) > _MAX_CALLS_IN_MEMORY:
            viejo = next((cid for cid, st in self._calls.items() if st.terminated), None)
            self._calls.pop(viejo if viejo else next(iter(self._calls)))

    def _open_call(self, request: StartCallRequest) -> _CallState:
        target = request.target
        now = local_now()
        if target.preferred_window_start and target.preferred_window_end:
            win_start, win_end = target.preferred_window_start, target.preferred_window_end
        else:
            win_start, win_end = next_business_day_window(
                now, settings.default_window_start_hour, settings.default_window_end_hour
            )
        opening = build_opening_message(
            purpose=target.purpose.value,
            contact_name=target.contact_name,
            destination=target.phone_or_sip_uri,
            principal_name=settings.principal_name,
            notes=target.context_notes,
            today_label=today_label(now),
            window_start_iso=win_start.isoformat(),
            window_end_iso=win_end.isoformat(),
        )
        call_id = request.call_id or f"call_{uuid4().hex[:12]}"
        state = _CallState(
            call_id=call_id,
            session=self.sip.open_session(target),
            window_start=win_start,
            window_end=win_end,
            messages=[
                {"role": "system", "content": SYSTEM_CALL_POLICY},
                {"role": "user", "content": opening},
            ],
        )
        self._calls[call_id] = state
        self._prune()
        return state

    # ------------------------------------------------------------------ modelo
    async def _ask_model(
        self, request: StartCallRequest, messages: list[dict[str, str]], max_tokens: int | None = None
    ) -> tuple[str, str | None]:
        payload = {
            "model": (request.model_name or "").strip() or settings.llm_default_model,
            "messages": messages,
            "temperature": request.temperature,
            "max_tokens": max_tokens or request.max_tokens,
            "top_p": settings.llm_top_p,
            "top_k": settings.llm_top_k,
            "repeat_penalty": settings.llm_repeat_penalty,
            # El formato acordado es JSON en el texto; si el modelo cae en el formato nativo de Qwen, se corta ya.
            "stop": ["<tool_call>"],
            "stream": False,
        }
        if settings.llm_structured_output:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "turno_agente", "strict": "true", "schema": TURN_JSON_SCHEMA},
            }
        url = f"{settings.llm_base_url.rstrip('/')}/chat/completions"
        timeout = httpx.Timeout(settings.llm_timeout_sec, connect=5.0)
        async with httpx.AsyncClient(timeout=timeout, transport=self._transport) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
        choice = (data.get("choices") or [{}])[0]
        content = ((choice.get("message") or {}).get("content") or "").strip()
        return content, choice.get("finish_reason")

    # ------------------------------------------------------------------ turno
    async def start_or_step_call(self, request: StartCallRequest) -> TurnExecutionResponse:
        existing = self._calls.get(request.call_id) if request.call_id else None
        is_followup = existing is not None
        utterance = (request.caller_utterance or "").strip()

        if existing and existing.terminated:
            raise CallFinishedError("La llamada ya terminó. Empieza una llamada nueva.")
        if is_followup and not utterance:
            raise CallInputError("En un turno de seguimiento hace falta la respuesta de la contraparte.")

        state = existing or self._open_call(request)
        state.booked_start = None
        state.proposed = None
        state.rejected_at = None
        state.last_utterance = utterance
        if is_followup and state.booked_last_turn and state.booked_event_id and _es_negativa(utterance):
            # La clínica dice que no justo después de que se agendara: la cita se anula (nunca queda una cita
            # que la clínica rechazó). El resto del turno sigue normal (insistir una vez, despedirse...).
            self.calendar.cancel(state.booked_event_id)
            state.booked_count = max(0, state.booked_count - 1)
            state.booked_event_id = None
            state.outcome = CallOutcome.PENDING
            state.checked_slots = None
            warnings_previos = ["La clínica rechazó el horario justo después de agendarlo: la cita se anuló."]
        else:
            warnings_previos = []
        state.booked_last_turn = False
        dry_run = effective_dry_run(request.target.dry_run)
        warnings: list[str] = list(warnings_previos)

        if is_followup:
            state.messages.append(
                {
                    "role": "user",
                    "content": build_followup_message(request.target.contact_name, utterance, state.checked_slots),
                }
            )

        guardia = _respuesta_de_guardia(utterance, settings.principal_name) if is_followup else None
        if guardia is not None:
            # Preguntas sobre quién llama o sobre datos personales de quien encarga la llamada: respuesta fija.
            # El modelo no la contesta porque podría inventar (p. ej. "es ingeniero de software").
            raw_content = assistant_json(guardia, [])
            warnings.append("Pregunta sobre identidad o datos personales: respuesta fija, sin el modelo.")
        else:
            raw_content, motivo = await self._ask_model(request, state.messages)
            parsed_try = _extract_json_payload(raw_content)
            _log_llm(state.call_id, utterance, raw_content, motivo, parsed_try is not None)
            if parsed_try is None:
                # Una sola vez: si se cortó por longitud, con el doble de tokens. Nunca se lee el JSON roto.
                mas = min(request.max_tokens * 2, 1500) if motivo == "length" else None
                raw_content, motivo = await self._ask_model(request, state.messages, max_tokens=mas)
                ok = _extract_json_payload(raw_content) is not None
                _log_llm(state.call_id, utterance, raw_content, motivo, ok, reintento=True)
                warnings.append(
                    f"El primer JSON del modelo no servía (fin: {motivo or '?'}); se le volvió a preguntar"
                    + (" y respondió bien." if ok else " y tampoco sirvió.")
                )
        clean_content = _strip_think_blocks(raw_content)
        parsed = _extract_json_payload(raw_content)

        validated_calls: list[AgendaToolCall] = []
        model_calls_raw: list[dict] = []
        insistencia = None

        if parsed is None:
            # Nunca se dice por teléfono lo que no es el JSON acordado: puede ser basura (p. ej. "<tool_call>"
            # repetido), un JSON roto o texto que el modelo no debía soltar.
            assistant_reply = _FALLBACK_REPLY
            warnings.append(
                "El modelo no devolvió un JSON válido; no se ejecutó ninguna herramienta ni se leyó su texto. "
                f"Llegó: {_short(clean_content)}"
            )
        else:
            reply = parsed.get("assistant_reply")
            assistant_reply = _strip_think_blocks(reply) if isinstance(reply, str) and reply.strip() else ""
            if not assistant_reply:
                assistant_reply = _FALLBACK_REPLY
                warnings.append("El JSON del modelo no traía assistant_reply.")
            raw_calls = parsed.get("tool_calls") or []
            if not isinstance(raw_calls, list):
                warnings.append("tool_calls no es una lista; se ignoró.")
                raw_calls = []
            insistencia = (
                self._insist_instead_of_closing(state, request, utterance, [_normalize_tool_call(c) for c in raw_calls])
                if is_followup
                else None
            )
            if insistencia is not None:
                # El modelo quería colgar sin cita: no se ejecuta su cierre; se ofrecen otros horarios una vez.
                assistant_reply, check_call = insistencia
                raw_calls = []
                validated_calls.append(check_call)
                model_calls_raw.append(check_call.model_dump(mode="json"))
                warnings.append(
                    "El modelo quiso colgar sin cita; se insistió una vez con otros horarios libres de la agenda."
                )
            for raw_item in raw_calls:
                item = _normalize_tool_call(raw_item)
                if isinstance(item, dict):
                    model_calls_raw.append(item)  # al historial va ya normalizada, en el formato de entrenamiento
                self._run_tool_call(item, state, request, dry_run, validated_calls, warnings)

        # Primer turno sin consulta de agenda válida: la consulta la hace el orquestador con la ventana de la llamada.
        if not is_followup and not any(c.tool == "check_calendar" for c in validated_calls):
            auto = CheckCalendarToolCall(
                tool="check_calendar",
                arguments=CheckCalendarArgs(
                    start_iso=state.window_start, end_iso=state.window_end, calendar_label="primary"
                ),
            )
            state.checked_slots = self.calendar.get_free_slots(auto.arguments)
            validated_calls.append(auto)
            warnings.append("El modelo no consultó la agenda en el primer turno; se consultó automáticamente.")

        # Despedida explícita de la contraparte y el modelo no colgó: se cuelga limpio (nunca se agenda solo).
        wants_hangup = any(marker in utterance.lower() for marker in _CLOSING_MARKERS)
        if is_followup and wants_hangup and not state.terminated:
            auto_hangup = HangupCallToolCall(
                tool="hangup_call",
                arguments=HangupCallArgs(reason="Despedida de la contraparte", send_sip_bye=True),
            )
            self._hangup(state)
            validated_calls.append(auto_hangup)
            warnings.append("La contraparte se despidió y el modelo no colgó; se colgó automáticamente.")
            if parsed is None or assistant_reply == _FALLBACK_REPLY:
                assistant_reply = _GOODBYE_REPLY      # nunca "¿me lo repite?" justo antes de colgar

        # Consultó la agenda a mitad de llamada: se ofrecen ya los huecos (sin "déjeme revisar" y silencio).
        consulto = is_followup and insistencia is None and any(c.tool == "check_calendar" for c in validated_calls)
        if consulto and state.booked_start is None and not state.terminated and state.checked_slots is not None:
            prefijo = ""
            if not state.checked_slots:
                # La ventana que miró el modelo estaba llena (p. ej. justo las 9:00): se mira el día entero y,
                # si tampoco hay nada, los siguientes días hábiles. Nunca "ese día no hay nada" sin comprobarlo.
                dia = [c for c in validated_calls if c.tool == "check_calendar"][-1].arguments.start_iso
                huecos = self._free_slots_in_hours(at_local(dia, 0), at_local(dia, 23, 59))
                if not huecos:
                    huecos = self._next_days_slots(dia)
                    if huecos:
                        prefijo = "Ese día no me queda nada libre, pero "
                state.checked_slots = huecos
                warnings.append("La consulta del modelo no tenía huecos; se buscó en el resto del día y siguientes.")
            assistant_reply = _ofrecer_huecos(state.checked_slots, prefijo)

        # La clínica dijo que no a ese día ("No, el lunes 23") y lo ofrecido es justo ese día: se pasa a otros días.
        if consulto and state.booked_start is None and not state.terminated and state.checked_slots and (
            _es_negativa(utterance)
        ):
            dia_dicho, _ = _dia_mencionado(utterance, local_now())
            if dia_dicho and datetime.fromisoformat(state.checked_slots[0]["start_iso"]).date() == dia_dicho:
                otros = self._next_days_slots(at_local(dia_dicho, 9))
                if otros:
                    state.checked_slots = otros
                    assistant_reply = _ofrecer_huecos(otros, "Entendido, ese día no. Entonces ")
                    warnings.append("Respuesta corregida por la API: la clínica rechazó ese día; se ofrecen otros.")

        # Cita rechazada (hora ocupada o fuera de horario): nunca solo "no"; se ofrecen huecos libres reales.
        if state.rejected_at is not None and state.booked_start is None and not state.terminated:
            huecos = self._free_slots_in_hours(at_local(state.rejected_at, 0), at_local(state.rejected_at, 23, 59))
            prefijo = "Perdone, a esa hora no lo tengo libre, pero "
            if not huecos:
                huecos = self._next_days_slots(state.rejected_at)
                prefijo = "Perdone, ese día ya no me queda nada libre, pero "
            state.checked_slots = huecos
            assistant_reply = _ofrecer_huecos(huecos, prefijo) if huecos else _ofrecer_huecos([])
            warnings.append("Respuesta corregida por la API: se ofrecen los huecos libres reales de la agenda.")

        # El modelo dice que no está libre SIN mirar la agenda: se mira el día que dijo la clínica.
        if (
            is_followup and insistencia is None and state.booked_start is None and state.rejected_at is None
            and not state.terminated and state.proposed is None
            and not any(c.tool in ("check_calendar", "book_appointment") for c in validated_calls)
            and _parece_negativa_del_asistente(assistant_reply)
        ):
            dia_dicho, franja = _dia_mencionado(utterance, local_now())
            if dia_dicho is not None:
                ini_h, fin_h = franja or (0, 23)
                huecos = self._free_slots_in_hours(at_local(dia_dicho, ini_h), at_local(dia_dicho, fin_h, 59))
                prefijo = ""
                if not huecos:
                    huecos = self._next_days_slots(at_local(dia_dicho, 9))
                    prefijo = "Ese día no me queda nada libre, pero "
                if huecos:
                    state.checked_slots = huecos
                    assistant_reply = _ofrecer_huecos(huecos, prefijo)
                    check = CheckCalendarToolCall(
                        tool="check_calendar",
                        arguments=CheckCalendarArgs(
                            start_iso=datetime.fromisoformat(huecos[0]["start_iso"]),
                            end_iso=datetime.fromisoformat(huecos[-1]["end_iso"]),
                            calendar_label="primary",
                        ),
                    )
                    validated_calls.append(check)
                    model_calls_raw.append(check.model_dump(mode="json"))
                    warnings.append(
                        "Respuesta corregida por la API: el modelo dijo que no sin mirar la agenda; se miró el día "
                        "que propuso la clínica."
                    )

        # Quiso agendar sin el sí de la clínica: se le propone esa hora exacta.
        if state.proposed is not None and state.booked_start is None and not state.terminated:
            assistant_reply = f"¿Entonces le vendría bien {_frase_hueco(state.proposed[0].isoformat())}?"

        # Cita agendada: se confirma con la fecha REAL de la agenda (el modelo a veces se equivoca de día).
        if state.booked_start is not None:
            confirmacion = f"Perfecto, entonces queda {_frase_hueco(state.booked_start.isoformat())}. Muchas gracias."
            if confirmacion.split(" queda ")[1].split(".")[0] not in assistant_reply:
                assistant_reply = confirmacion
        else:
            corregida = _corregir_dias(assistant_reply, local_now())
            if corregida != assistant_reply:
                warnings.append(f"Se corrigió el día de la semana dicho por el modelo: «{assistant_reply}».")
                assistant_reply = corregida

        # Historial: el turno del modelo se guarda en el MISMO formato JSON con el que se entrenó.
        state.messages.append(
            {
                "role": "assistant",
                "content": assistant_json(assistant_reply, model_calls_raw) if parsed is not None else clean_content,
            }
        )
        if not state.terminated:
            state.session = self.sip.record_turn_packets(state.session.session_id)

        return TurnExecutionResponse(
            call_id=state.call_id,
            session=state.session,
            assistant_reply=assistant_reply,
            tool_calls=validated_calls,
            dry_run=dry_run,
            outcome=state.outcome,
            terminated=state.terminated,
            warnings=warnings,
        )

    # ------------------------------------------------------------------ agenda en horario de trabajo
    def _free_slots_in_hours(self, start: datetime, end: datetime) -> list[dict[str, str]]:
        """Huecos libres de [start, end) solo en las franjas de trabajo (9-12 y 13-17, lunes a viernes).
        Si la ventana pedida cae entera fuera de horario (p. ej. "a las 8 de la noche"), se mira ese día completo."""

        def franjas(dia_ini: datetime, dia_fin: datetime, recortar: bool) -> list[tuple[datetime, datetime]]:
            out = []
            dia = dia_ini
            while dia.date() <= dia_fin.date() and len(out) < 12:
                if dia.weekday() < 5:
                    for h_ini, h_fin, _ in _FRANJAS:
                        a, b = at_local(dia, h_ini), at_local(dia, h_fin)
                        if recortar:
                            a, b = max(a, start), min(b, end)
                        if a < b:
                            out.append((a, b))
                dia += timedelta(days=1)
            return out

        tramos = franjas(start, end - timedelta(seconds=1), True) or franjas(start, start, False)
        huecos: list[dict[str, str]] = []
        for a, b in tramos:
            if b <= local_now():
                continue
            a = max(a, local_now().replace(second=0, microsecond=0)) if a < local_now() else a
            huecos.extend(self.calendar.get_free_slots(CheckCalendarArgs(start_iso=a, end_iso=b,
                                                                          calendar_label="primary")))
            if len(huecos) >= 6:
                break
        return huecos[:6]

    def _libre_en_horario(self, start: datetime, end: datetime) -> bool:
        """La agenda tiene libre [start, end), de lunes a viernes, dentro de UNA franja de trabajo, y es futuro."""
        if start <= local_now() or end <= start or start.weekday() >= 5:
            return False
        dentro = any(at_local(start, a) <= start and end <= at_local(start, b) for a, b, _ in _FRANJAS)
        return dentro and not self.calendar.has_conflict(start, end)

    def _next_days_slots(self, dia: datetime) -> list[dict[str, str]]:
        """Huecos del primer día hábil con sitio después de 'dia' (hasta 7 días)."""
        for n in range(1, 8):
            otro = dia + timedelta(days=n)
            if otro.weekday() >= 5:
                continue
            huecos = self._free_slots_in_hours(at_local(otro, 0), at_local(otro, 23, 59))
            if huecos:
                return huecos
        return []

    # ------------------------------------------------------------------ insistir una vez
    def _insist_instead_of_closing(
        self, state: _CallState, request: StartCallRequest, utterance: str, calls: list[Any]
    ) -> tuple[str, CheckCalendarToolCall] | None:
        """Si la contraparte dice que no tiene ese hueco y el modelo quiere colgar sin cita, se ofrecen otros
        horarios de la agenda UNA vez. Nunca con un rechazo firme, un buzón de voz, una despedida o en marketing."""
        texto = utterance.lower()
        if (
            state.insisted
            or state.booked_count > 0
            or request.target.purpose.value == "client_marketing"
            or texto.startswith("[evento")
            or any(m in texto for m in _FIRM_REFUSAL_MARKERS + _CLOSING_MARKERS)
        ):
            return None
        quiere_cerrar = False
        for c in calls:
            if not isinstance(c, dict):
                continue
            if c.get("tool") == "hangup_call":
                quiere_cerrar = True
            args = c.get("arguments") if isinstance(c.get("arguments"), dict) else {}
            if c.get("tool") == "log_call_outcome" and args.get("outcome") in (
                CallOutcome.VOICEMAIL_LEFT.value,
                CallOutcome.CALLBACK_REQUESTED.value,
                CallOutcome.APPOINTMENT_BOOKED.value,
                CallOutcome.INTERVIEW_OR_ROLE_CONFIRMED.value,
            ):
                return None             # cierre con otro motivo (buzón, que devuelvan la llamada...): se respeta
        if not quiere_cerrar:
            return None

        # Otros horarios: el resto del día de la llamada y los siguientes días hábiles, por franjas.
        grupos: list[list[dict[str, str]]] = []
        dia = state.window_start
        dias_vistos = 0
        while dias_vistos < _DIAS_INSISTENCIA + 1 and len(grupos) < 8:
            if dia.weekday() < 5:
                dias_vistos += 1
                for ini_h, fin_h, _ in _FRANJAS:
                    ini, fin = at_local(dia, ini_h), at_local(dia, fin_h)
                    if fin <= state.window_end and ini < state.window_end:
                        continue        # esa franja ya se ofreció (es la ventana original)
                    if ini <= local_now():
                        continue
                    huecos = self._free_slots_in_hours(ini, fin)
                    if huecos:
                        grupos.append(huecos)
            dia = dia + timedelta(days=1)
        if not grupos:
            return None

        ofrecidos = grupos[:2]
        state.insisted = True
        # Primero los dos huecos ofrecidos (los ve el modelo en el siguiente turno); el resto también vale para agendar.
        state.checked_slots = [h for g in ofrecidos for h in g[:2]] + [
            h for g in grupos for h in g if h not in [x for o in ofrecidos for x in o[:2]]
        ]
        opciones = " o ".join(_frase_hueco(g[0]["start_iso"]) for g in ofrecidos)
        reply = f"Entiendo. ¿Y le vendría bien otro momento? Por ejemplo, {opciones}."
        fin_busqueda = max(datetime.fromisoformat(g[-1]["end_iso"]) for g in grupos)
        check = CheckCalendarToolCall(
            tool="check_calendar",
            arguments=CheckCalendarArgs(
                start_iso=datetime.fromisoformat(grupos[0][0]["start_iso"]),
                end_iso=fin_busqueda,
                calendar_label="primary",
            ),
        )
        return reply, check

    # ------------------------------------------------------------------ herramientas
    def _hangup(self, state: _CallState) -> None:
        state.session = self.sip.terminate_session(state.session.session_id)
        state.terminated = True

    def _run_tool_call(
        self,
        item: Any,
        state: _CallState,
        request: StartCallRequest,
        dry_run: bool,
        validated: list[AgendaToolCall],
        warnings: list[str],
    ) -> None:
        name = item.get("tool") if isinstance(item, dict) else None
        if state.terminated:
            warnings.append(f"Se ignoró {name or 'una herramienta'}: la llamada ya estaba colgada.")
            return
        if name not in ALLOWED_TOOLS:
            warnings.append(f"Herramienta no permitida o sin nombre: {name!r}. Llegó: {_short(item)}")
            return
        try:
            call = _AGENDA_TOOL_CALL_ADAPTER.validate_python(item)
        except ValidationError as exc:
            campos = ", ".join(".".join(str(p) for p in e["loc"][1:]) or "?" for e in exc.errors()[:4])
            warnings.append(
                f"{name}: argumentos inválidos ({exc.error_count()} error/es en: {campos}); no se ejecutó. "
                f"Llegó: {_short(item)}"
            )
            return

        if call.tool == "check_calendar":
            state.checked_slots = self._free_slots_in_hours(call.arguments.start_iso, call.arguments.end_iso)
            if not state.checked_slots:
                warnings.append("check_calendar: no hay huecos libres en esa ventana.")
        elif call.tool == "book_appointment":
            args = call.arguments
            if state.booked_count > 0:
                warnings.append("book_appointment rechazado: ya hay una cita agendada en esta llamada.")
                return
            # La agenda manda: vale cualquier hora libre en horario de trabajo, aunque no saliera en la última
            # consulta (la clínica puede proponer otro día). Lo que no se puede es agendar encima de otra cita
            # ni fuera de horario.
            if not is_covered_by_slots(args.start_iso, args.end_iso, state.checked_slots or []) and not (
                self._libre_en_horario(args.start_iso, args.end_iso)
            ):
                huecos_dia = self._free_slots_in_hours(at_local(args.start_iso, 0), at_local(args.start_iso, 23, 59))
                arreglo = _arreglar_hueco(args.start_iso, args.end_iso, (state.checked_slots or []) + huecos_dia)
                if arreglo is None:
                    warnings.append(
                        "book_appointment rechazado: esa hora no está libre en la agenda o es fuera de horario "
                        f"(pidió {args.start_iso.isoformat()} a {args.end_iso.isoformat()})."
                    )
                    state.rejected_at = args.start_iso
                    return
                warnings.append(
                    f"book_appointment ajustado: pidió {args.start_iso.isoformat()} a {args.end_iso.isoformat()}; "
                    f"se agenda el hueco libre {arreglo[0].isoformat()} a {arreglo[1].isoformat()}."
                )
                args.start_iso, args.end_iso = arreglo
            if not _es_aceptacion(state.last_utterance):
                # El horario está libre pero la clínica no lo ha aceptado (p. ej. solo repitió "el lunes por la
                # mañana"): no se agenda; se le propone esa hora y se espera su sí.
                state.proposed = (args.start_iso, args.end_iso)
                warnings.append(
                    "book_appointment aplazado: la clínica todavía no aceptó ese horario; se le propone y se espera "
                    f"su confirmación (había dicho: «{state.last_utterance[:120]}»)."
                )
                return
            ok, msg, record = self.calendar.book_appointment(args, dry_run=dry_run)
            if not ok:
                warnings.append(f"book_appointment rechazado: {msg}")
                return
            state.booked_count += 1
            state.booked_start = args.start_iso
            state.booked_event_id = record.event_id if record else None
            state.booked_last_turn = True
            state.outcome = (
                CallOutcome.INTERVIEW_OR_ROLE_CONFIRMED
                if args.purpose.value == "employer_outreach"
                else CallOutcome.APPOINTMENT_BOOKED
            )
            state.checked_slots = None  # el hueco ya no está libre; para otra cita hay que volver a consultar
        elif call.tool == "log_call_outcome":
            if state.booked_count > 0 and call.arguments.outcome not in (
                CallOutcome.APPOINTMENT_BOOKED,
                CallOutcome.INTERVIEW_OR_ROLE_CONFIRMED,
            ):
                warnings.append(
                    f"log_call_outcome '{call.arguments.outcome.value}' ignorado: en esta llamada ya se agendó una cita."
                )
            else:
                state.outcome = call.arguments.outcome
        elif call.tool == "hangup_call":
            self._hangup(state)

        validated.append(call)


def _frase_hueco(start_iso: str) -> str:
    """'2026-11-24T09:30:00-05:00' -> 'el martes 24 a las 9 y media de la mañana' (así lo lee bien Piper)."""
    t = datetime.fromisoformat(start_iso)
    h12 = t.hour - 12 if t.hour > 12 else t.hour
    hora = f"a la {h12}" if h12 == 1 else f"a las {h12}"
    if t.minute == 30:
        hora += " y media"
    elif t.minute:
        hora += f" y {t.minute}"
    franja = "de la mañana" if t.hour < 12 else "del mediodía" if t.hour == 12 else "de la tarde"
    return f"el {_DIAS[t.weekday()]} {t.day} {hora} {franja}"


_DIA_RE = re.compile(r"\b(lunes|martes|miércoles|miercoles|jueves|viernes|sábado|sabado|domingo)(\s+)(\d{1,2})\b",
                     re.IGNORECASE)


def _corregir_dias(texto: str, hoy: datetime) -> str:
    """'el miércoles 27' cuando el 27 es viernes -> 'el viernes 27'. Toma la próxima fecha con ese número de día."""

    def arreglar(m: re.Match) -> str:
        numero = int(m.group(3))
        if not 1 <= numero <= 31:
            return m.group(0)
        dia = hoy.date()
        for _ in range(62):
            if dia.day == numero:
                break
            dia += timedelta(days=1)
        else:
            return m.group(0)
        nombre = _DIAS[dia.weekday()]
        if m.group(1)[0].isupper():
            nombre = nombre.capitalize()
        return f"{nombre}{m.group(2)}{m.group(3)}"

    return _DIA_RE.sub(arreglar, texto)


def _hora(start_iso: str) -> str:
    """'...T09:30' -> 'a las 9 y media de la mañana'."""
    return _frase_hueco(start_iso).split(" ", 3)[3]


def _ofrecer_huecos(huecos: list[dict[str, str]], prefijo: str = "") -> str:
    if prefijo:
        texto = _ofrecer_huecos(huecos)
        return prefijo + texto[0].lower() + texto[1:]
    if not huecos:
        return "Lo siento, esta semana no me queda ningún hueco libre. ¿Le puedo volver a llamar más adelante?"
    a = huecos[0]["start_iso"]
    b = next((h["start_iso"] for h in huecos[1:]), None)
    dia_a = _frase_hueco(a).rsplit(" a la", 1)[0]          # 'el martes 24'
    if b is None:
        return f"{dia_a.capitalize()} tengo libre {_hora(a)}. ¿Le viene bien?"
    if _frase_hueco(b).rsplit(" a la", 1)[0] == dia_a:
        ha, hb = _hora(a), _hora(b)
        for franja in (" de la mañana", " de la tarde", " del mediodía"):
            if ha.endswith(franja) and hb.endswith(franja):
                ha = ha[: -len(franja)]          # 'a las 9 o a las 9 y media de la mañana'
        return f"{dia_a.capitalize()} tengo libre {ha} o {hb}. ¿Cuál le viene mejor?"
    return f"Tengo libre {_frase_hueco(a)} o {_frase_hueco(b)}. ¿Cuál le viene mejor?"


# Preguntas a las que el modelo no debe contestar por su cuenta.
_PREGUNTA_IDENTIDAD = (
    "cómo se llama usted", "como se llama usted", "cómo dijo que se llama", "como dijo que se llama",
    "quién habla", "quien habla", "con quién hablo", "con quien hablo", "cuál es su nombre", "cual es su nombre",
    "usted quién es", "usted quien es",
)
_PREGUNTA_DATOS_PERSONALES = (
    "qué cosa es él", "que cosa es el", "qué cosa es el", "a qué se dedica", "a que se dedica", "en qué trabaja",
    "en que trabaja", "qué hace él", "que hace el", "qué edad", "que edad", "cuántos años tiene", "cuantos años tiene",
    "fecha de nacimiento", "dónde vive", "donde vive", "su dirección", "su direccion", "seguro social",
    "número de teléfono", "numero de telefono", "su teléfono", "su telefono", "su correo",
)


_PREGUNTA_PACIENTE = (
    "el paciente", "la paciente", "del paciente", "de la paciente", "a nombre de quién", "a nombre de quien",
    "para quién es la cita", "para quien es la cita", "cómo se llama él", "como se llama el", "su nombre completo",
    "nombre de la persona",
)


def _respuesta_de_guardia(utterance: str, principal: str) -> str | None:
    texto = utterance.lower()
    # Primero el paciente: "¿cómo dijo que se llamaba el paciente?" pregunta por Denis, no por el asistente.
    if any(m in texto for m in _PREGUNTA_PACIENTE) and ("llam" in texto or "nombre" in texto or "quién" in texto
                                                         or "quien" in texto):
        return f"El paciente se llama {principal}."
    if any(m in texto for m in _PREGUNTA_IDENTIDAD):
        return (f"Soy un asistente de inteligencia artificial y llamo de parte de {principal}, que es quien "
                "necesita la cita.")
    if any(m in texto for m in _PREGUNTA_DATOS_PERSONALES):
        return (f"Disculpe, no tengo más datos personales de {principal}; yo solo llamo para agendar la cita. "
                "Si necesitan algo más, él puede llamar directamente a la clínica.")
    return None


def _arreglar_hueco(start: datetime, end: datetime, huecos: list[dict[str, str]]) -> tuple[datetime, datetime] | None:
    """El modelo pidió un hueco que no cuadra con lo consultado. Solo se arregla si la HORA DE INICIO coincide con un
    hueco libre consultado (a veces pone una duración larga, o la hora en UTC con 'Z'): se agenda ese hueco de 30 min.
    Si el inicio no es un hueco libre, no se adivina nada."""
    inicios = {datetime.fromisoformat(h["start_iso"]): datetime.fromisoformat(h["end_iso"]) for h in huecos}
    candidatos = [start]
    if start.utcoffset() == timedelta(0):
        candidatos.append(start.replace(tzinfo=None).astimezone())      # "09:00Z" -> 09:00 hora local
    for c in candidatos:
        for ini, fin in inicios.items():
            if ini == c:
                return ini, fin
    return None


_LLM_LOG_DIR = None


def _log_llm(call_id: str, utterance: str, raw: str, finish: str | None, ok: bool, reintento: bool = False) -> None:
    """Guarda lo que devolvió el modelo en api/logs/llm-AAAAMMDD.jsonl: para diagnosticar y para el dataset v2.
    (.jsonl: el --reload de uvicorn solo vigila .py, así que escribir aquí no reinicia la API.)"""
    global _LLM_LOG_DIR
    try:
        from pathlib import Path

        if _LLM_LOG_DIR is None:
            _LLM_LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
            _LLM_LOG_DIR.mkdir(exist_ok=True)
        linea = {"t": local_now().isoformat(timespec="seconds"), "call_id": call_id, "contraparte": utterance,
                 "finish_reason": finish, "json_ok": ok, "reintento": reintento, "raw": raw}
        with open(_LLM_LOG_DIR / f"llm-{local_now():%Y%m%d}.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(linea, ensure_ascii=False) + "\n")
    except OSError:
        pass


_NEGATIVAS = (
    "no es posible", "no tengo", "no puedo", "no hay", "no se puede", "no me queda", "imposible", "ocupad",
    "a tope", "lleno", "no está libre", "no esta libre", "no nos sirve", "no le sirve",
)
_ACEPTACIONES = (
    "perfecto", "de acuerdo", "ok", "okay", "vale", "está bien", "esta bien",
    "le anoto", "lo anoto", "la anoto", "anotado", "queda", "me sirve", "nos sirve", "adelante", "correcto",
    "exacto", "listo", "tengo espacio", "tengo disponible", "hay espacio", "le puedo dar", "le doy", "perfect",
    "excelente", "muy bien", "confirmado",
    # la clínica ofrece ella misma la hora: aceptarla es correcto
    "tenemos", "tengo a la", "tengo el", "hay a la", "puede venir", "le sirve", "le viene", "le queda",
)


_PALABRA_SI = re.compile(r"\b(sí|si|ok|okay|vale|claro|bueno|listo)\b")


def _es_negativa(utterance: str) -> bool:
    t = utterance.strip().lower()
    return bool(re.match(r"^no\b", t)) or any(m in t for m in _NEGATIVAS)


def _es_aceptacion(utterance: str) -> bool:
    """La clínica acepta o da el horario. Una negativa nunca cuenta como aceptación."""
    t = utterance.strip().lower()
    if not t or _es_negativa(t):
        return False
    return bool(_PALABRA_SI.search(t)) or any(m in t for m in _ACEPTACIONES)


_NOMBRE_DIA = {"lunes": 0, "martes": 1, "miércoles": 2, "miercoles": 2, "jueves": 3, "viernes": 4,
               "sábado": 5, "sabado": 5, "domingo": 6}
_FRANJA_MANANA = re.compile(r"\b(por|de|en) la mañana\b")


def _dia_mencionado(texto: str, hoy: datetime):
    """Día y franja que dice la clínica: ("viernes en la tarde" -> (viernes próximo, (13, 17))).
    Devuelve (date | None, (hora_ini, hora_fin) | None)."""
    t = texto.lower()
    franja = None
    if "tarde" in t:
        franja = (13, 17)
    elif _FRANJA_MANANA.search(t):
        franja = (9, 12)
    t2 = _FRANJA_MANANA.sub(" ", t)
    hoy_d = hoy.date()
    m = re.search(r"\b(?:el|día|dia|lunes|martes|miércoles|miercoles|jueves|viernes)\s+(\d{1,2})\b", t2)
    if m and 1 <= int(m.group(1)) <= 31:
        d = hoy_d
        for _ in range(62):
            if d.day == int(m.group(1)):
                return d, franja
            d += timedelta(days=1)
    if "pasado mañana" in t2:
        return hoy_d + timedelta(days=2), franja
    if re.search(r"\bmañana\b", t2):
        return hoy_d + timedelta(days=1), franja
    for nombre, wd in _NOMBRE_DIA.items():
        if re.search(rf"\b{nombre}\b", t2):
            n = (wd - hoy_d.weekday()) % 7 or 7
            return hoy_d + timedelta(days=n), franja
    return None, franja


_NEGATIVA_ASISTENTE = ("no está libre", "no esta libre", "no lo tengo libre", "no tengo libre", "no está disponible",
                       "no esta disponible", "no tengo disponib", "no hay huecos", "no me queda")


def _parece_negativa_del_asistente(reply: str) -> bool:
    t = reply.lower()
    return any(m in t for m in _NEGATIVA_ASISTENTE)
