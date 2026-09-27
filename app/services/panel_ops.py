from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from app.models import Consultation, Doctor, Message, User
from app.providers.base import MessagingClient
from app.services.bridge import log_message, notify_doctor_assignment, send_and_log
from app.services.doctor_display import format_doctor_credentials
from app.services.keyboards import KB_CHAT, KB_DOCTOR, KB_MENU
from app.services.pdf_recipe import generate_orientation_pdf
from app.services.routing import assign_consultation, queue_consultation


async def force_close_consultation(
    db: Session,
    client: MessagingClient,
    consultation: Consultation,
    *,
    reason: str = "Cerrada desde el panel",
) -> Consultation:
    doctor = consultation.doctor
    doctor_msgs = (
        db.query(Message)
        .filter(
            Message.consultation_id == consultation.id,
            Message.from_role == "doctor",
        )
        .order_by(Message.created_at.asc())
        .all()
    )
    orientation = "\n".join(m.body for m in doctor_msgs).strip()
    consultation.orientation_text = (orientation or consultation.orientation_text or "") + f"\n[{reason}]"
    consultation.status = "closed"
    consultation.closed_at = datetime.now(timezone.utc)
    user = consultation.user or db.query(User).filter(User.id == consultation.user_id).first()
    if user:
        try:
            path = generate_orientation_pdf(consultation, user, doctor, doctor_msgs)
            consultation.pdf_path = str(path)
        except Exception:
            pass

    if doctor and doctor.active_consultation_id == consultation.id:
        doctor.active_consultation_id = None
        if doctor.status == "busy":
            doctor.status = "available"
        db.add(doctor)
    db.add(consultation)
    db.commit()
    db.refresh(consultation)

    if user:
        await send_and_log(
            db,
            client,
            to_id=user.external_id,
            text=(
                f"✅ *Consulta #{consultation.id} finalizada* (panel).\n"
                f"{reason}.\n\n"
                "Si los síntomas empeoran, acude a un centro de salud o escribe *1*."
            ),
            consultation_id=consultation.id,
            keyboard=KB_MENU,
        )
        if consultation.pdf_path and Path(consultation.pdf_path).is_file():
            try:
                await client.send_document(
                    user.external_id,
                    consultation.pdf_path,
                    caption=f"Orientación consulta #{consultation.id} (no es receta MINSA)",
                )
            except Exception:
                pass
    return consultation


async def reassign_consultation(
    db: Session,
    client: MessagingClient,
    consultation: Consultation,
    new_doctor: Doctor,
) -> str | None:
    """Devuelve un error en texto o None si ok."""
    if consultation.status not in {"queued", "assigned", "active"}:
        return f"No se puede reasignar una consulta en estado {consultation.status}."
    if new_doctor.active_consultation_id is not None:
        return f"{new_doctor.name} ya tiene la consulta #{new_doctor.active_consultation_id}."

    old = consultation.doctor
    if old and old.id == new_doctor.id:
        return "Ese médico ya tiene esta consulta."

    if old and old.active_consultation_id == consultation.id:
        old.active_consultation_id = None
        if old.status == "busy":
            old.status = "available"
        db.add(old)
        db.commit()

    assign_consultation(db, consultation, new_doctor)
    user = consultation.user or db.query(User).filter(User.id == consultation.user_id).first()
    if user:
        await notify_doctor_assignment(db, client, consultation, user, new_doctor)
        await send_and_log(
            db,
            client,
            to_id=user.external_id,
            text=(
                f"👨‍⚕️ Tu consulta #{consultation.id} pasa a {format_doctor_credentials(new_doctor)}."
            ),
            consultation_id=consultation.id,
            keyboard=KB_CHAT,
        )
    if old:
        patient = (user.name or "").strip() if user else ""
        who = f"*{patient}*" if patient else "tu paciente"
        await send_and_log(
            db,
            client,
            to_id=old.external_id,
            text=(
                f"↪️ Tu paciente {who} (consulta #{consultation.id}) "
                "fue reasignado a otro médico:\n"
                f"{format_doctor_credentials(new_doctor)}.\n\n"
                "Ya no recibirás sus mensajes. "
                "Si estás libre, usa */disponible* para tomar otro caso."
            ),
            consultation_id=consultation.id,
            keyboard=KB_DOCTOR,
        )
    return None


async def mark_doctor_offline(
    db: Session,
    client: MessagingClient,
    doctor: Doctor,
) -> str:
    """Pone offline. Si tenía caso activo, lo devuelve a la cola."""
    note = f"{doctor.name} marcado *offline*."
    if doctor.active_consultation_id:
        c = (
            db.query(Consultation)
            .filter(Consultation.id == doctor.active_consultation_id)
            .first()
        )
        doctor.active_consultation_id = None
        doctor.status = "offline"
        db.add(doctor)
        db.commit()
        if c and c.status in {"assigned", "active"}:
            queue_consultation(db, c)
            user = c.user or db.query(User).filter(User.id == c.user_id).first()
            if user:
                await send_and_log(
                    db,
                    client,
                    to_id=user.external_id,
                    text=(
                        f"Tu médico quedó fuera de servicio. "
                        f"La consulta #{c.id} volvió a la *cola*; te avisaremos al reasignarla."
                    ),
                    consultation_id=c.id,
                    keyboard=KB_CHAT,
                )
            await log_message(
                db,
                consultation_id=c.id,
                direction="out",
                from_role="system",
                body="[médico marcado offline desde el panel; consulta a cola]",
            )
            note += f" Consulta #{c.id} devuelta a la cola."
        return note

    doctor.status = "offline"
    db.add(doctor)
    db.commit()
    return note
