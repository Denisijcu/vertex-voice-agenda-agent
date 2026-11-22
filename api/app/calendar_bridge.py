from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from .schemas import BookAppointmentArgs, CallPurpose, CheckCalendarArgs

SLOT_MINUTES = 30
MAX_SLOTS = 6


@dataclass
class CalendarEventRecord:
    event_id: str
    title: str
    start_iso: datetime
    end_iso: datetime
    attendee_or_location: str
    purpose: CallPurpose
    notes: str = ""
    dry_run: bool = True
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


def _overlaps(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    return a_end > b_start and a_start < b_end


def is_covered_by_slots(start: datetime, end: datetime, free_slots: list[dict[str, str]]) -> bool:
    """True si [start, end) cae entero dentro de los huecos libres (unidos si son contiguos)."""
    tramos = sorted(
        (datetime.fromisoformat(s["start_iso"]), datetime.fromisoformat(s["end_iso"])) for s in free_slots
    )
    unidos: list[list[datetime]] = []
    for ini, fin in tramos:
        if unidos and ini <= unidos[-1][1]:
            unidos[-1][1] = max(unidos[-1][1], fin)
        else:
            unidos.append([ini, fin])
    return any(ini <= start and end <= fin for ini, fin in unidos)


class CalendarBridge:
    """Agenda en memoria con modo simulación (dry-run) y prevención de solapes.

    Las citas reales van a `_events`. Las de dry-run van a `_simulated`: no son reales, pero también bloquean
    el hueco mientras la API está viva, para que dos llamadas simuladas no reserven la misma hora.
    """

    def __init__(self) -> None:
        self._events: list[CalendarEventRecord] = []
        self._simulated: list[CalendarEventRecord] = []

    def list_events(self, include_simulated: bool = True) -> list[CalendarEventRecord]:
        eventos = list(self._events) + (list(self._simulated) if include_simulated else [])
        return sorted(eventos, key=lambda ev: ev.start_iso)

    def _busy(self) -> list[CalendarEventRecord]:
        return self._events + self._simulated

    def get_free_slots(self, args: CheckCalendarArgs) -> list[dict[str, str]]:
        busy = sorted(
            (ev for ev in self._busy() if _overlaps(ev.start_iso, ev.end_iso, args.start_iso, args.end_iso)),
            key=lambda ev: ev.start_iso,
        )
        delta = timedelta(minutes=SLOT_MINUTES)
        slots: list[dict[str, str]] = []
        cursor = args.start_iso

        def agregar_hasta(limite: datetime) -> None:
            nonlocal cursor
            while cursor + delta <= limite and len(slots) < MAX_SLOTS:
                slots.append({"start_iso": cursor.isoformat(), "end_iso": (cursor + delta).isoformat()})
                cursor += delta

        for ev in busy:
            agregar_hasta(min(ev.start_iso, args.end_iso))
            if ev.end_iso > cursor:
                cursor = ev.end_iso
        agregar_hasta(args.end_iso)
        return slots

    def has_conflict(self, start_iso: datetime, end_iso: datetime) -> bool:
        return any(_overlaps(ev.start_iso, ev.end_iso, start_iso, end_iso) for ev in self._busy())

    def cancel(self, event_id: str) -> bool:
        """Anula una cita (la clínica se echó atrás justo después de agendarla)."""
        for lista in (self._simulated, self._events):
            for ev in lista:
                if ev.event_id == event_id:
                    lista.remove(ev)
                    return True
        return False

    def book_appointment(
        self,
        args: BookAppointmentArgs,
        *,
        dry_run: bool = True,
    ) -> tuple[bool, str, CalendarEventRecord | None]:
        if self.has_conflict(args.start_iso, args.end_iso):
            return False, "Existe un conflicto de horario en la ventana solicitada.", None

        record = CalendarEventRecord(
            event_id=f"evt_{uuid4().hex[:10]}",
            title=args.title,
            start_iso=args.start_iso,
            end_iso=args.end_iso,
            attendee_or_location=args.attendee_or_location,
            purpose=args.purpose,
            notes=args.notes,
            dry_run=dry_run,
        )
        (self._simulated if dry_run else self._events).append(record)
        status_msg = (
            "Cita validada en modo dry-run (no persistida en agenda real)."
            if dry_run
            else "Cita registrada en agenda."
        )
        return True, status_msg, record
