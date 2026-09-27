from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# Hora local del servicio (Perú). Los timestamps se guardan en UTC en BD.
DISPLAY_TZ = ZoneInfo("America/Lima")


def as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_lima(dt: datetime) -> datetime:
    return as_utc(dt).astimezone(DISPLAY_TZ)


def format_lima(dt: datetime | None, *, empty: str = "—") -> str:
    """Fecha/hora legible en hora Perú (para panel y PDF)."""
    if dt is None:
        return empty
    return to_lima(dt).strftime("%d/%m/%Y %H:%M") + " (hora Perú)"


def start_of_today_lima_utc() -> datetime:
    """Inicio del día civil en Perú, expresado en UTC (para carga diaria)."""
    now_local = datetime.now(DISPLAY_TZ)
    start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start_local.astimezone(timezone.utc)
