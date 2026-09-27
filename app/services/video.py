from __future__ import annotations

import hashlib

from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import Consultation, Doctor, User
from app.providers.base import MessagingClient
from app.services.bridge import log_message, send_and_log
from app.services.doctor_display import format_doctor_credentials
from app.services.keyboards import KB_CHAT, KB_DOCTOR


def jitsi_room_url(consultation: Consultation, settings: Settings | None = None) -> str:
    """Sala estable por consulta (mismo link cada vez). No se graba."""
    settings = settings or get_settings()
    base = (settings.jitsi_base_url or "https://meet.jit.si").rstrip("/")
    stamp = ""
    if consultation.created_at is not None:
        stamp = consultation.created_at.isoformat()
    token = hashlib.sha256(f"{consultation.id}|{stamp}".encode()).hexdigest()[:8]
    room = f"DoctorBot-{consultation.id}-{token}"
    return f"{base}/{room}"


async def share_video_link(
    db: Session,
    client: MessagingClient,
    consultation: Consultation,
    *,
    requested_by: str,
) -> bool:
    """Envía el mismo enlace Jitsi a paciente y médico. requested_by: patient | doctor."""
    if consultation.status not in {"assigned", "active"}:
        return False
    doctor = consultation.doctor or (
        db.query(Doctor).filter(Doctor.id == consultation.doctor_id).first()
        if consultation.doctor_id
        else None
    )
    user = consultation.user or db.query(User).filter(User.id == consultation.user_id).first()
    if doctor is None or user is None:
        return False

    url = jitsi_room_url(consultation)
    who = "El paciente pidió videollamada." if requested_by == "patient" else "Iniciaste una videollamada."
    common = (
        f"Consulta *#{consultation.id}*\n"
        f"{url}\n\n"
        "Misma sala para ambos. *No se graba.* "
        "Al terminar, siguen en este chat; el médico cierra con */cerrar*."
    )
    await send_and_log(
        db,
        client,
        to_id=user.external_id,
        text=(
            f"📹 *Videollamada*\n\n"
            f"Ábrela para hablar con {format_doctor_credentials(doctor)}:\n\n"
            f"{common}"
        ),
        consultation_id=consultation.id,
        keyboard=KB_CHAT,
    )
    await send_and_log(
        db,
        client,
        to_id=doctor.external_id,
        text=f"📹 *Videollamada*\n\n{who}\n\n{common}",
        consultation_id=consultation.id,
        keyboard=KB_DOCTOR,
    )
    await log_message(
        db,
        consultation_id=consultation.id,
        direction="out",
        from_role="system",
        body=f"[videollamada: {url}]",
    )
    return True
