"""
timeutil.py — Fechas con zona horaria sin depender de tzdata.

Usa la zona horaria del sistema (Windows la da, con su horario de verano), así no hace falta instalar nada en el venv.
Toda fecha que entra sin zona (p. ej. el modelo escribe "2026-11-25T10:00:00") se interpreta como hora local:
así nunca se mezclan fechas con y sin zona (eso daba TypeError -> error 500).
"""
from datetime import datetime, timedelta

_DIAS = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")


def local_now() -> datetime:
    return datetime.now().astimezone()


def ensure_aware(value: datetime) -> datetime:
    """Fecha sin zona -> hora local del sistema. Fecha con zona -> tal cual."""
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        return value.astimezone()
    return value


def at_local(day: datetime, hour: int, minute: int = 0) -> datetime:
    """El día de 'day' a la hora indicada, en hora local (con el desfase correcto de ESE día)."""
    return datetime(day.year, day.month, day.day, hour, minute).astimezone()


def next_business_day_window(now: datetime, start_hour: int, end_hour: int) -> tuple[datetime, datetime]:
    """Ventana por defecto: el siguiente día hábil (lunes a viernes) de start_hour a end_hour."""
    day = now + timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return at_local(day, start_hour), at_local(day, end_hour)


def today_label(now: datetime) -> str:
    """Ej.: 'sábado 2026-11-21 09:30 (-05:00)'. Es el formato que el modelo ve al entrenar."""
    return f"{_DIAS[now.weekday()]} {now:%Y-%m-%d %H:%M} ({now.isoformat()[-6:]})"
