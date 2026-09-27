from __future__ import annotations

import re
from datetime import datetime

from sqlalchemy.orm import Session

from app.models import Doctor, DoctorShift
from app.services.timeutil import DISPLAY_TZ, to_lima

WEEKDAYS = (
    (0, "Lunes"),
    (1, "Martes"),
    (2, "Miércoles"),
    (3, "Jueves"),
    (4, "Viernes"),
    (5, "Sábado"),
    (6, "Domingo"),
)
WEEKDAY_LABEL = dict(WEEKDAYS)

_HM = re.compile(r"^(\d{1,2}):(\d{2})")


def parse_hhmm(raw: str) -> str | None:
    """Normaliza '8:00', '08:00' o '08:00:00' → '08:00'."""
    m = _HM.match((raw or "").strip())
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 23 or mi > 59:
        return None
    return f"{h:02d}:{mi:02d}"


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def is_on_shift(shifts: list[DoctorShift], when: datetime | None = None) -> bool:
    """Sin franjas = sin restricción. Si hay calendario, `when` (hora Perú) debe caer en una."""
    if not shifts:
        return True
    local = to_lima(when) if when is not None else datetime.now(DISPLAY_TZ)
    wd = local.weekday()
    mins = local.hour * 60 + local.minute
    for s in shifts:
        start = _minutes(s.start_hm)
        end = _minutes(s.end_hm)
        if start == end:
            continue
        if start < end:
            if s.weekday == wd and start <= mins < end:
                return True
        else:
            # Cruza medianoche: lun 22:00–06:00 = lun 22–24 + mar 00–06
            if s.weekday == wd and mins >= start:
                return True
            if s.weekday == (wd - 1) % 7 and mins < end:
                return True
    return False


def load_shifts(db: Session, doctor_id: int) -> list[DoctorShift]:
    return (
        db.query(DoctorShift)
        .filter(DoctorShift.doctor_id == doctor_id)
        .order_by(DoctorShift.weekday.asc(), DoctorShift.start_hm.asc())
        .all()
    )


def doctor_on_shift(db: Session, doctor: Doctor, when: datetime | None = None) -> bool:
    return is_on_shift(load_shifts(db, doctor.id), when)


def format_shifts(shifts: list[DoctorShift]) -> str:
    if not shifts:
        return "Sin calendario: puedes recibir casos a cualquier hora si estás */disponible*."
    lines = [f"• {WEEKDAY_LABEL.get(s.weekday, s.weekday)} {s.start_hm}–{s.end_hm}" for s in shifts]
    return "Tu horario (hora Perú):\n" + "\n".join(lines)


def shifts_by_doctor(db: Session, doctor_ids: list[int]) -> dict[int, list[DoctorShift]]:
    if not doctor_ids:
        return {}
    rows = (
        db.query(DoctorShift)
        .filter(DoctorShift.doctor_id.in_(doctor_ids))
        .order_by(DoctorShift.weekday.asc(), DoctorShift.start_hm.asc())
        .all()
    )
    out: dict[int, list[DoctorShift]] = {i: [] for i in doctor_ids}
    for row in rows:
        out.setdefault(row.doctor_id, []).append(row)
    return out
