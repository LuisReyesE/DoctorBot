from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Consultation, Doctor, Message, User
from app.providers.base import MessagingClient
from app.services.doctor_display import format_doctor_credentials
from app.services.keyboards import KB_CHAT, KB_DOCTOR, KB_MENU
from app.services.pdf_recipe import generate_orientation_pdf
from app.services.routing import format_triage_card, try_assign_queued


async def notify_doctor_assignment(
    db: Session,
    client: MessagingClient,
    consultation: Consultation,
    user: User,
    doctor: Doctor,
) -> None:
    """Envía ficha de triaje y, si hay, la foto en un segundo mensaje (sin límite de caption)."""
    card = format_triage_card(consultation, user)
    await send_and_log(
        db,
        client,
        to_id=doctor.external_id,
        text=card,
        consultation_id=consultation.id,
        keyboard=KB_DOCTOR,
    )
    photo_path = None
    if consultation.triage_json:
        try:
            data = json.loads(consultation.triage_json)
            photo_path = data.get("photo_path")
        except json.JSONDecodeError:
            photo_path = None
    if photo_path and Path(photo_path).is_file():
        try:
            await client.send_photo(
                doctor.external_id,
                photo_path,
                caption=f"Foto consulta #{consultation.id} — {user.name or 'paciente'}",
            )
            await log_message(
                db,
                consultation_id=consultation.id,
                direction="out",
                from_role="system",
                body=f"[foto enviada: {photo_path}]",
            )
        except Exception:
            await send_and_log(
                db,
                client,
                to_id=doctor.external_id,
                text=f"⚠️ No se pudo reenviar la foto de la consulta #{consultation.id}.",
                consultation_id=consultation.id,
            )


async def notify_patient_assigned(
    db: Session,
    client: MessagingClient,
    consultation: Consultation,
    user: User,
    doctor: Doctor,
) -> None:
    await send_and_log(
        db,
        client,
        to_id=user.external_id,
        text=(
            f"👨‍⚕️ {format_doctor_credentials(doctor)} ya tiene tu consulta #{consultation.id}.\n"
            "Tiempo estimado de respuesta: *15–30 minutos*."
        ),
        consultation_id=consultation.id,
        keyboard=KB_CHAT,
    )


async def deliver_queued_to_doctor(
    db: Session,
    client: MessagingClient,
    doctor: Doctor,
    *,
    require_shift: bool = True,
) -> Consultation | None:
    """Si el médico puede tomar un caso, asigna el más antiguo en cola y avisa a ambos."""
    assigned = try_assign_queued(db, doctor, require_shift=require_shift)
    if assigned is None:
        return None
    user = assigned.user or db.query(User).filter(User.id == assigned.user_id).first()
    if user:
        await notify_doctor_assignment(db, client, assigned, user, doctor)
        await notify_patient_assigned(db, client, assigned, user, doctor)
    return assigned


async def persist_consultation_media(
    client: MessagingClient,
    media_url: str | None,
    consultation_id: int | None,
    *,
    suffix: str = "photo",
) -> str | None:
    if not media_url or not consultation_id:
        return None
    settings = get_settings()
    media_dir = Path(settings.storage_dir) / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    dest = media_dir / f"consulta_{consultation_id}_{suffix}"
    saved = await client.download_media(media_url, dest)
    if saved is None:
        return None
    return str(saved)


async def log_message(
    db: Session,
    *,
    consultation_id: int | None,
    direction: str,
    from_role: str,
    body: str,
    media_url: str | None = None,
) -> Message:
    msg = Message(
        consultation_id=consultation_id,
        direction=direction,
        from_role=from_role,
        body=body,
        media_url=media_url,
    )
    db.add(msg)
    db.commit()
    db.refresh(msg)
    return msg


async def send_and_log(
    db: Session,
    client: MessagingClient,
    *,
    to_id: str,
    text: str,
    consultation_id: int | None,
    from_role: str = "system",
    keyboard: list[list[str]] | None = None,
    remove_keyboard: bool = False,
) -> None:
    await client.send_text(
        to_id, text, keyboard=keyboard, remove_keyboard=remove_keyboard
    )
    await log_message(
        db,
        consultation_id=consultation_id,
        direction="out",
        from_role=from_role,
        body=text,
    )


async def forward_doctor_to_patient(
    db: Session,
    client: MessagingClient,
    doctor: Doctor,
    body: str,
) -> bool:
    if doctor.active_consultation_id is None:
        return False
    consultation = db.query(Consultation).filter(Consultation.id == doctor.active_consultation_id).first()
    if consultation is None or consultation.status in {"closed", "emergency"}:
        return False
    user = db.query(User).filter(User.id == consultation.user_id).first()
    if user is None:
        return False

    if consultation.status == "assigned":
        consultation.status = "active"
        db.add(consultation)
        db.commit()

    await log_message(
        db,
        consultation_id=consultation.id,
        direction="in",
        from_role="doctor",
        body=body,
    )
    doctor_label = (doctor.name or "Médico").strip()
    text = f"👨‍⚕️ *{doctor_label}:*\n{body}"
    await send_and_log(
        db,
        client,
        to_id=user.external_id,
        text=text,
        consultation_id=consultation.id,
        from_role="system",
    )
    return True


async def forward_doctor_photo_to_patient(
    db: Session,
    client: MessagingClient,
    doctor: Doctor,
    media_url: str,
    caption: str = "",
) -> bool:
    """Reenvía una foto del médico (p. ej. receta manuscrita) y la deja para el PDF."""
    if doctor.active_consultation_id is None:
        return False
    consultation = db.query(Consultation).filter(Consultation.id == doctor.active_consultation_id).first()
    if consultation is None or consultation.status in {"closed", "emergency"}:
        return False
    user = db.query(User).filter(User.id == consultation.user_id).first()
    if user is None:
        return False

    if consultation.status == "assigned":
        consultation.status = "active"
        db.add(consultation)
        db.commit()

    stamp = datetime.now(timezone.utc).strftime("%H%M%S")
    path = await persist_consultation_media(
        client, media_url, consultation.id, suffix=f"rx_{stamp}"
    )
    note = (caption or "").strip()
    if note.lower() in {"[photo]", "[document]"}:
        note = ""
    body = note or "[foto receta]"
    await log_message(
        db,
        consultation_id=consultation.id,
        direction="in",
        from_role="doctor",
        body=body,
        media_url=path,
    )
    cap = f"👨‍⚕️ {doctor.name}: receta / indicación en foto"
    if note:
        cap = f"{cap}\n{note}"[:1024]
    if path and Path(path).is_file():
        try:
            await client.send_photo(user.external_id, path, caption=cap)
            await log_message(
                db,
                consultation_id=consultation.id,
                direction="out",
                from_role="system",
                body=f"[foto receta enviada al paciente: {path}]",
            )
            return True
        except Exception:
            pass
    await send_and_log(
        db,
        client,
        to_id=user.external_id,
        text=f"👨‍⚕️ *{doctor.name}:*\n{note or '(envió una foto; no se pudo reenviar)'}",
        consultation_id=consultation.id,
    )
    return True


async def forward_patient_to_doctor(
    db: Session,
    client: MessagingClient,
    consultation: Consultation,
    body: str,
    photo_path: str | None = None,
) -> bool:
    if consultation.doctor_id is None:
        return False
    doctor = db.query(Doctor).filter(Doctor.id == consultation.doctor_id).first()
    if doctor is None:
        return False
    await log_message(
        db,
        consultation_id=consultation.id,
        direction="in",
        from_role="patient",
        body=body,
        media_url=photo_path,
    )
    caption_body = body if body and body not in {"[photo]", "[document]"} else ""
    if photo_path and Path(photo_path).is_file():
        caption = f"💬 Paciente (consulta #{consultation.id})"
        if caption_body:
            caption = f"{caption}\n{caption_body}"[:1024]
        try:
            await client.send_photo(doctor.external_id, photo_path, caption=caption)
            await log_message(
                db,
                consultation_id=consultation.id,
                direction="out",
                from_role="system",
                body=f"[foto reenviada al médico: {photo_path}]",
            )
        except Exception:
            await send_and_log(
                db,
                client,
                to_id=doctor.external_id,
                text=f"💬 *Paciente (consulta #{consultation.id}):*\n{body or '(foto; no se pudo reenviar)'}",
                consultation_id=consultation.id,
                from_role="system",
            )
        return True

    text = f"💬 *Paciente (consulta #{consultation.id}):*\n{body}"
    await send_and_log(
        db,
        client,
        to_id=doctor.external_id,
        text=text,
        consultation_id=consultation.id,
        from_role="system",
    )
    return True


async def close_consultation(
    db: Session,
    client: MessagingClient,
    doctor: Doctor,
) -> Consultation | None:
    if doctor.active_consultation_id is None:
        return None
    consultation = db.query(Consultation).filter(Consultation.id == doctor.active_consultation_id).first()
    if consultation is None:
        doctor.active_consultation_id = None
        doctor.status = "available"
        db.add(doctor)
        db.commit()
        return None

    doctor_msgs = (
        db.query(Message)
        .filter(
            Message.consultation_id == consultation.id,
            Message.from_role == "doctor",
        )
        .order_by(Message.created_at.asc())
        .all()
    )
    orientation = "\n".join(
        m.body
        for m in doctor_msgs
        if (m.body or "").strip().lower() not in {"[photo]", "[document]", "[foto receta]"}
    ).strip()
    consultation.orientation_text = orientation or consultation.orientation_text
    consultation.status = "closed"
    consultation.closed_at = datetime.now(timezone.utc)

    user = db.query(User).filter(User.id == consultation.user_id).first()
    pdf_note = ""
    try:
        path = generate_orientation_pdf(consultation, user, doctor, doctor_msgs)  # type: ignore[arg-type]
        consultation.pdf_path = str(path)
        pdf_note = (
            f"\n\n📄 Te enviamos tu *orientación digital* (PDF) "
            f"— consulta #{consultation.id}. "
            "También queda en el panel de gestión."
        )
    except Exception:
        path = None
        pdf_note = "\n\n_(No se pudo generar el PDF en este entorno; la orientación quedó en el chat.)_"

    doctor.active_consultation_id = None
    doctor.status = "available"
    db.add(consultation)
    db.add(doctor)
    db.commit()
    db.refresh(consultation)

    if user:
        await send_and_log(
            db,
            client,
            to_id=user.external_id,
            text=(
                f"✅ *Consulta #{consultation.id} finalizada*\n\n"
                "Esta fue una *teleorientación médica primaria*. "
                "No reemplaza una consulta presencial ni atención de emergencia.\n"
                f"{pdf_note}\n\n"
                "Si los síntomas empeoran, acude a un centro de salud.\n"
                "En ~48 h te escribiremos un breve seguimiento.\n"
                "Usa los botones o escribe *menu*."
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
                await log_message(
                    db,
                    consultation_id=consultation.id,
                    direction="out",
                    from_role="system",
                    body=f"[pdf enviado: {consultation.pdf_path}]",
                )
            except Exception:
                await send_and_log(
                    db,
                    client,
                    to_id=user.external_id,
                    text="⚠️ No se pudo adjuntar el PDF en el chat; está en el panel.",
                    consultation_id=consultation.id,
                    keyboard=KB_MENU,
                )

    next_case = try_assign_queued(db, doctor)
    if next_case is not None:
        next_user = db.query(User).filter(User.id == next_case.user_id).first()
        await send_and_log(
            db,
            client,
            to_id=doctor.external_id,
            text=_doctor_closed_notice(consultation, next_id=next_case.id),
            consultation_id=consultation.id,
            keyboard=KB_DOCTOR,
        )
        if next_user:
            await notify_doctor_assignment(db, client, next_case, next_user, doctor)
            await notify_patient_assigned(db, client, next_case, next_user, doctor)
    else:
        await send_and_log(
            db,
            client,
            to_id=doctor.external_id,
            text=_doctor_closed_notice(consultation),
            consultation_id=consultation.id,
            keyboard=KB_DOCTOR,
        )
    return consultation


def _doctor_closed_notice(consultation: Consultation, *, next_id: int | None = None) -> str:
    if consultation.pdf_path:
        pdf_line = (
            "El paciente ya recibió el PDF. El equipo lo tiene en el panel de gestión.\n"
            f"Si más adelante lo necesitas: */pdf {consultation.id}*."
        )
    else:
        pdf_line = "No se generó PDF (la orientación quedó en el chat / panel)."
    if next_id is not None:
        return (
            f"Consulta #{consultation.id} cerrada.\n{pdf_line}\n\n"
            f"➡️ Siguiente en cola: consulta #{next_id}."
        )
    return (
        f"Consulta #{consultation.id} cerrada.\n{pdf_line}\n"
        "Estado: *disponible*. Usa */fuera* si terminas tu turno."
    )
