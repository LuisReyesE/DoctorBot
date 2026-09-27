from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import Consultation, User
from app.providers.base import MessagingClient
from app.services.bridge import send_and_log
from app.services.keyboards import KB_MENU


def _aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


async def process_followup_reminders(
    db: Session,
    client: MessagingClient,
    settings: Settings | None = None,
) -> list[int]:
    """Envía recordatorios a consultas cerradas hace ~reminder_hours sin reminder previo."""
    settings = settings or get_settings()
    now = datetime.now(timezone.utc)
    threshold = now - timedelta(hours=settings.reminder_hours)
    oldest = now - timedelta(hours=settings.reminder_hours + 24)

    closed = (
        db.query(Consultation)
        .filter(
            Consultation.status == "closed",
            Consultation.closed_at.isnot(None),
            Consultation.reminder_sent_at.is_(None),
        )
        .all()
    )

    sent_ids: list[int] = []
    for c in closed:
        closed_at = _aware(c.closed_at)  # type: ignore[arg-type]
        if closed_at > threshold or closed_at < oldest:
            continue
        user = db.query(User).filter(User.id == c.user_id).first()
        if not user:
            continue
        await send_and_log(
            db,
            client,
            to_id=user.external_id,
            text=(
                f"👋 *Seguimiento Doctor Bot* (consulta #{c.id})\n\n"
                "¿Cómo evolucionaron tus síntomas desde la orientación?\n"
                "• Si mejoraste: ¡excelente!\n"
                "• Si igual o peor: escribe *1* para una nueva consulta o acude a un centro de salud.\n\n"
                "_Mensaje automático a las ~48 h. No es spam: solo un control breve._"
            ),
            consultation_id=c.id,
            keyboard=KB_MENU,
        )
        c.reminder_sent_at = now
        db.add(c)
        sent_ids.append(c.id)

    if sent_ids:
        db.commit()
    return sent_ids
