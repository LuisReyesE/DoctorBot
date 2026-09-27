from __future__ import annotations

import re
from pathlib import Path

from sqlalchemy.orm import Session

from app.models import Consultation, Doctor
from app.providers.base import MessagingClient
from app.services.bridge import (
    close_consultation,
    deliver_queued_to_doctor,
    forward_doctor_photo_to_patient,
    forward_doctor_to_patient,
    log_message,
    send_and_log,
)
from app.services.keyboards import KB_DOCTOR
from app.services.shifts import doctor_on_shift, format_shifts, load_shifts
from app.services.video import share_video_link


HELP_DOCTOR = (
    "👨‍⚕️ *Comandos médico Doctor Bot*\n\n"
    "*/disponible* — recibir nuevas consultas\n"
    "*/ocupado* — no recibir nuevas (mantienes la activa)\n"
    "*/fuera* — salir de servicio\n"
    "*/casos* — ver consultas abiertas\n"
    "*/pdf 8* — reenviar el PDF de una consulta tuya ya cerrada\n"
    "*/video* — enlace Jitsi de la consulta activa (solo tú la inicias)\n"
    "*/horario* — ver tu calendario (hora Perú)\n"
    "*/cerrar* — cerrar consulta + PDF al paciente\n"
    "*/ayuda* — este mensaje\n\n"
    "Texto y *fotos* (p. ej. receta manuscrita) se reenvían al paciente; la foto entra al PDF.\n"
    "Panel web: /panel (gestión y turnos horarios)."
)


async def handle_doctor_message(
    db: Session,
    client: MessagingClient,
    doctor: Doctor,
    body: str,
    media_url: str | None = None,
) -> None:
    text = body.strip()
    lower = text.lower()

    if media_url:
        ok = await forward_doctor_photo_to_patient(db, client, doctor, media_url, caption=text)
        if not ok:
            await send_and_log(
                db,
                client,
                to_id=doctor.external_id,
                text="No tienes consulta activa para enviar una foto. Espera un caso o usa */ayuda*.",
                consultation_id=None,
                keyboard=KB_DOCTOR,
            )
            return
        if lower in {"", "[photo]", "[document]"}:
            return
        # Caption que es comando: ejecutar después de enviar la foto
        if not lower.startswith("/") and lower not in {
            "disponible",
            "ocupado",
            "fuera",
            "casos",
            "cerrar",
            "ayuda",
            "help",
            "horario",
            "video",
            "videollamada",
            "pdf",
        }:
            return

    if lower in {"/ayuda", "ayuda", "help", "/help"}:
        await send_and_log(
            db,
            client,
            to_id=doctor.external_id,
            text=HELP_DOCTOR,
            consultation_id=None,
            keyboard=KB_DOCTOR,
        )
        return

    if lower in {"/horario", "horario"}:
        shifts = load_shifts(db, doctor.id)
        on = doctor_on_shift(db, doctor)
        extra = "Ahora: *dentro de horario*." if on else "Ahora: *fuera de horario*."
        if not shifts:
            extra = "Ahora: *sin calendario* (sin restricción)."
        await send_and_log(
            db,
            client,
            to_id=doctor.external_id,
            text=f"{format_shifts(shifts)}\n\n{extra}",
            consultation_id=None,
            keyboard=KB_DOCTOR,
        )
        return

    if lower in {"/disponible", "disponible"}:
        if doctor.active_consultation_id is not None:
            await send_and_log(
                db,
                client,
                to_id=doctor.external_id,
                text=(
                    f"Sigues con la consulta *#{doctor.active_consultation_id}* activa. "
                    "Usa */cerrar* para terminar y tomar el siguiente caso de la cola."
                ),
                consultation_id=doctor.active_consultation_id,
            )
            return
        doctor.status = "available"
        db.add(doctor)
        db.commit()
        shifts = load_shifts(db, doctor.id)
        on = doctor_on_shift(db, doctor)
        note = "✅ Estado: *disponible*. Puedes recibir nuevas consultas."
        if shifts and not on:
            note = (
                "✅ Estado: *disponible* (hora extra).\n"
                "Estás *fuera de tu horario* configurado. "
                "Igual puedes atender: si hay cola, te asigno un caso.\n\n"
                f"{format_shifts(shifts)}"
            )
        await send_and_log(
            db,
            client,
            to_id=doctor.external_id,
            text=note,
            consultation_id=None,
            keyboard=KB_DOCTOR,
        )
        await deliver_queued_to_doctor(db, client, doctor, require_shift=False)
        return

    if lower in {"/ocupado", "ocupado"}:
        doctor.status = "busy"
        db.add(doctor)
        db.commit()
        await send_and_log(
            db,
            client,
            to_id=doctor.external_id,
            text="⏸ Estado: *ocupado*. No recibirás nuevas consultas.",
            consultation_id=doctor.active_consultation_id,
            keyboard=KB_DOCTOR,
        )
        return

    if lower in {"/fuera", "fuera", "/offline"}:
        if doctor.active_consultation_id is not None:
            await send_and_log(
                db,
                client,
                to_id=doctor.external_id,
                text="Tienes una consulta activa. Usa */cerrar* antes de salir de servicio.",
                consultation_id=doctor.active_consultation_id,
            )
            return
        doctor.status = "offline"
        db.add(doctor)
        db.commit()
        await send_and_log(
            db,
            client,
            to_id=doctor.external_id,
            text="📴 Estado: *fuera de servicio*. Envía */disponible* para volver.",
            consultation_id=None,
            keyboard=KB_DOCTOR,
        )
        return

    if lower in {"/casos", "casos"}:
        cases = (
            db.query(Consultation)
            .filter(
                Consultation.doctor_id == doctor.id,
                Consultation.status.in_(["assigned", "active"]),
            )
            .order_by(Consultation.id.desc())
            .limit(10)
            .all()
        )
        queued_count = (
            db.query(Consultation).filter(Consultation.status == "queued").count()
        )
        triaging_count = (
            db.query(Consultation).filter(Consultation.status == "triaging").count()
        )
        if not cases:
            msg = (
                "No tienes consultas *asignadas* a ti.\n\n"
                f"• En cola del sistema: *{queued_count}*\n"
                f"• Pacientes en triaje incompleto: *{triaging_count}*\n\n"
                "_Las de triaje no aparecen aquí: el paciente aún no terminó el formulario "
                "y no se te asignaron. Cuando completen el triaje y estés */disponible*, "
                "recibirás la ficha._"
            )
        else:
            lines = [f"• #{c.id} — {c.status}" for c in cases]
            active = doctor.active_consultation_id
            msg = "*Tus consultas asignadas:*\n" + "\n".join(lines)
            if active:
                msg += f"\n\nActiva actual: #{active}"
            if queued_count or triaging_count:
                msg += (
                    f"\n\n_Cola global: {queued_count} · Triaje incompleto: {triaging_count}_"
                )
        recent_pdf = (
            db.query(Consultation.id)
            .filter(
                Consultation.doctor_id == doctor.id,
                Consultation.status == "closed",
                Consultation.pdf_path.isnot(None),
            )
            .order_by(Consultation.id.desc())
            .limit(5)
            .all()
        )
        if recent_pdf:
            ids = ", ".join(f"#{row[0]}" for row in recent_pdf)
            msg += f"\n\nPDF recientes: {ids}. Escribe */pdf 8* para reenviar uno."
        await send_and_log(
            db,
            client,
            to_id=doctor.external_id,
            text=msg,
            consultation_id=None,
            keyboard=KB_DOCTOR,
        )
        return

    if lower in {"/video", "video", "videollamada"}:
        if doctor.active_consultation_id is None:
            await send_and_log(
                db,
                client,
                to_id=doctor.external_id,
                text="*/video* solo con una consulta activa. Espera un caso o usa */casos*.",
                consultation_id=None,
                keyboard=KB_DOCTOR,
            )
            return
        c = (
            db.query(Consultation)
            .filter(Consultation.id == doctor.active_consultation_id)
            .first()
        )
        if c is None or c.status not in {"assigned", "active"}:
            await send_and_log(
                db,
                client,
                to_id=doctor.external_id,
                text="No hay una consulta abierta para videollamada.",
                consultation_id=doctor.active_consultation_id,
                keyboard=KB_DOCTOR,
            )
            return
        await share_video_link(db, client, c, requested_by="doctor")
        return

    pdf_match = re.fullmatch(r"/?pdf\s*#?\s*(\d+)", lower)
    if lower in {"/pdf", "pdf"} or pdf_match:
        cid = int(pdf_match.group(1)) if pdf_match else None
        await _send_own_pdf(db, client, doctor, cid)
        return

    if lower in {"/cerrar", "cerrar"}:
        closed = await close_consultation(db, client, doctor)
        if closed is None:
            await send_and_log(
                db,
                client,
                to_id=doctor.external_id,
                text="No tienes una consulta activa para cerrar.",
                consultation_id=None,
                keyboard=KB_DOCTOR,
            )
        return

    ok = await forward_doctor_to_patient(db, client, doctor, text)
    if not ok:
        await send_and_log(
            db,
            client,
            to_id=doctor.external_id,
            text="No tienes consulta activa. Usa */disponible* y espera un caso, o */ayuda*.",
            consultation_id=None,
            keyboard=KB_DOCTOR,
        )


async def _send_own_pdf(
    db: Session,
    client: MessagingClient,
    doctor: Doctor,
    consultation_id: int | None,
) -> None:
    if consultation_id is None:
        await send_and_log(
            db,
            client,
            to_id=doctor.external_id,
            text="Indica el número: */pdf 8* (el paciente te dice el # de la consulta anterior).",
            consultation_id=None,
            keyboard=KB_DOCTOR,
        )
        return

    c = (
        db.query(Consultation)
        .filter(Consultation.id == consultation_id, Consultation.doctor_id == doctor.id)
        .first()
    )
    if c is None:
        await send_and_log(
            db,
            client,
            to_id=doctor.external_id,
            text=(
                f"No encuentro la consulta *#{consultation_id}* entre las tuyas. "
                "Solo puedes pedir PDF de casos que atendiste."
            ),
            consultation_id=None,
            keyboard=KB_DOCTOR,
        )
        return

    path = Path(c.pdf_path) if c.pdf_path else None
    if path is None or not path.is_file():
        await send_and_log(
            db,
            client,
            to_id=doctor.external_id,
            text=(
                f"La consulta #{c.id} no tiene PDF "
                f"(estado: *{c.status}*). Solo hay archivo si se cerró con orientación."
            ),
            consultation_id=c.id,
            keyboard=KB_DOCTOR,
        )
        return

    try:
        await client.send_document(
            doctor.external_id,
            str(path),
            caption=f"PDF consulta #{c.id} (triaje + orientación; no es receta MINSA)",
        )
        await log_message(
            db,
            consultation_id=c.id,
            direction="out",
            from_role="system",
            body=f"[pdf reenviado al médico: {path}]",
        )
    except Exception:
        await send_and_log(
            db,
            client,
            to_id=doctor.external_id,
            text=(
                f"No pude adjuntar el PDF de la #{c.id}. "
                "Está en el panel de gestión."
            ),
            consultation_id=c.id,
            keyboard=KB_DOCTOR,
        )
