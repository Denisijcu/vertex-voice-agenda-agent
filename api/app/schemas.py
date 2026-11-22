from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Literal, Union

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from .timeutil import ensure_aware

SafeText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=256),
]
NotesText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, max_length=2000),
]
# Toda fecha que entra queda con zona horaria (sin zona = hora local del PC). Evita comparar fechas con y sin zona.
AwareDatetime = Annotated[datetime, AfterValidator(ensure_aware)]


class CallPurpose(str, Enum):
    DENTIST_APPOINTMENT = "dentist_appointment"
    EMPLOYER_OUTREACH = "employer_outreach"
    CLIENT_MARKETING = "client_marketing"
    FOLLOW_UP = "follow_up"


class SipSignalState(str, Enum):
    IDLE = "idle"
    REGISTERING = "registering"
    INVITING = "inviting"
    RINGING = "ringing"
    ESTABLISHED_200_OK = "200_ok_ack"
    BYE_TERMINATING = "bye_terminating"
    TERMINATED = "terminated"
    FAILED = "failed"


class RtpMediaState(str, Enum):
    INACTIVE = "inactive"
    NEGOTIATING_SDP = "negotiating_sdp"
    STREAMING_PCMU = "streaming_pcmu"
    PAUSED_HOLD = "paused_hold"
    CLOSED = "closed"


class CallOutcome(str, Enum):
    PENDING = "pending"
    APPOINTMENT_BOOKED = "appointment_booked"
    INTERVIEW_OR_ROLE_CONFIRMED = "interview_or_role_confirmed"
    VOICEMAIL_LEFT = "voicemail_left"
    CALLBACK_REQUESTED = "callback_requested"
    NO_ANSWER = "no_answer"
    REJECTED_OR_UNAVAILABLE = "rejected_or_unavailable"


class VoiceTargetInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contact_name: SafeText = Field(..., description="Nombre del consultorio, clínica o empleador.")
    phone_or_sip_uri: SafeText = Field(
        ...,
        max_length=160,
        description="Destino telefónico o SIP URI (ej. sip:clinic@pbx.local o +13055550199).",
    )
    purpose: CallPurpose
    preferred_window_start: AwareDatetime | None = Field(
        default=None,
        description="Inicio de ventana horaria deseada en ISO-8601.",
    )
    preferred_window_end: AwareDatetime | None = Field(
        default=None,
        description="Fin de ventana horaria deseada en ISO-8601.",
    )
    context_notes: NotesText = Field(
        default="",
        description="Contexto breve para la llamada (motivo dental, puesto o campaña).",
    )
    dry_run: bool = Field(
        default=True,
        description=(
            "Si es True, simula señalización/ejecución sin marcar ni mutar agenda real. "
            "Con SIP_BRIDGE_ENABLED=false siempre se simula, aunque llegue False."
        ),
    )

    @field_validator("phone_or_sip_uri")
    @classmethod
    def validate_destination(cls, value: str) -> str:
        if any(ch in value for ch in ("\r", "\n", "\x00", " ", "\t")):
            raise ValueError("Destino SIP/telefónico inválido o contiene caracteres de control.")
        allowed_chars = set("0123456789+-.@:abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_")
        if not set(value).issubset(allowed_chars):
            raise ValueError("Destino SIP/telefónico contiene caracteres no permitidos.")
        return value

    @model_validator(mode="after")
    def validate_time_window(self) -> "VoiceTargetInput":
        if (
            self.preferred_window_start is not None
            and self.preferred_window_end is not None
            and self.preferred_window_end <= self.preferred_window_start
        ):
            raise ValueError("preferred_window_end debe ser posterior a preferred_window_start.")
        return self

class StartCallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    call_id: str | None = Field(default=None, min_length=6, max_length=64)
    model_name: str | None = Field(
        default=None,
        max_length=128,
        description="Identificador del modelo en LM Studio. Vacío = LLM_DEFAULT_MODEL.",
    )
    target: VoiceTargetInput
    caller_utterance: str | None = Field(
        default=None,
        max_length=2000,
        description=(
            "Respuesta de la contraparte o evento del turno (ej. '[evento: buzón de voz] ...'). "
            "Obligatoria en los turnos de seguimiento; se ignora en el primer turno."
        ),
    )
    temperature: float = Field(default=0.2, ge=0.0, le=1.2)
    max_tokens: int = Field(default=384, ge=32, le=1500)


class SipRtpSessionStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(..., min_length=8, max_length=64)
    sip_state: SipSignalState = SipSignalState.IDLE
    rtp_state: RtpMediaState = RtpMediaState.INACTIVE
    codec: str = Field(default="PCMU/8000", max_length=32)
    transport: Literal["SIP/UDP", "SIPS/TLS", "RTP/AVP", "SRTP"] = "SIPS/TLS"
    rtp_packets_sent: int = Field(default=0, ge=0)
    rtp_packets_received: int = Field(default=0, ge=0)
    last_sip_method: str | None = Field(default=None, max_length=32)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# --- Contrato estructurado de Tool Calls de Agenda y Llamada ---


class CheckCalendarArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_iso: AwareDatetime
    end_iso: AwareDatetime
    calendar_label: SafeText = Field(default="primary", max_length=64)

    @model_validator(mode="after")
    def check_range(self) -> "CheckCalendarArgs":
        if self.end_iso <= self.start_iso:
            raise ValueError("end_iso debe ser mayor que start_iso.")
        return self


class BookAppointmentArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: SafeText = Field(..., max_length=140)
    start_iso: AwareDatetime
    end_iso: AwareDatetime
    attendee_or_location: SafeText = Field(..., max_length=160)
    purpose: CallPurpose
    notes: NotesText = ""

    @model_validator(mode="after")
    def check_slot(self) -> "BookAppointmentArgs":
        if self.end_iso <= self.start_iso:
            raise ValueError("end_iso debe ser mayor que start_iso.")
        return self


class LogCallOutcomeArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: CallOutcome
    summary: SafeText = Field(..., max_length=600)
    confirmed_slot_iso: AwareDatetime | None = None
    next_action: NotesText = ""


class HangupCallArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: SafeText = Field(..., max_length=240)
    send_sip_bye: bool = True


class CheckCalendarToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: Literal["check_calendar"]
    arguments: CheckCalendarArgs


class BookAppointmentToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: Literal["book_appointment"]
    arguments: BookAppointmentArgs


class LogCallOutcomeToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: Literal["log_call_outcome"]
    arguments: LogCallOutcomeArgs


class HangupCallToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: Literal["hangup_call"]
    arguments: HangupCallArgs


AgendaToolCall = Union[
    CheckCalendarToolCall,
    BookAppointmentToolCall,
    LogCallOutcomeToolCall,
    HangupCallToolCall,
]


class TurnExecutionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    call_id: str
    session: SipRtpSessionStatus
    assistant_reply: str = Field(..., description="Texto hablado o propuesto para TTS.")
    tool_calls: list[AgendaToolCall] = Field(default_factory=list)
    dry_run: bool = True
    outcome: CallOutcome = CallOutcome.PENDING
    terminated: bool = False
    warnings: list[str] = Field(
        default_factory=list,
        description="Avisos del turno: tool calls rechazadas, JSON inválido del modelo, acciones automáticas.",
    )