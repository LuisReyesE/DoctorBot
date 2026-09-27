from __future__ import annotations

import re

from sqlalchemy.orm import Session, joinedload

from app.models import Payment, Verifier
from app.providers.base import MessagingClient
from app.services.bridge import send_and_log
from app.services.caja import (
    confirm_and_notify,
    find_payment_by_token,
    format_payment_ficha,
    reject_and_notify,
    request_another_photo,
)
from app.services.keyboards import KB_CAJA
from app.services.subscription import format_pay_code
from app.services.timeutil import format_lima

HELP_CAJA = (
    "🧾 *Caja Doctor Bot*\n\n"
    "Recibes fichas de suscripción (Yape/Plin). El dinero llega al Yape del *proyecto*.\n\n"
    "*/casos* — pendientes de confirmar\n"
    "*/buscar K7P2* — ficha por código (también *SALUD-K7P2* o #id)\n"
    "*/confirmar K7P2* — activar suscripción\n"
    "*/rechazar K7P2 motivo* — avisar al paciente\n"
    "*/otrafoto K7P2* — pedir captura de nuevo (misma ficha)\n"
    "*/ayuda* — este mensaje\n\n"
    "Si dos cajas ven el mismo caso, la primera que confirma gana."
)


def _cmd_arg(text: str, names: tuple[str, ...]) -> str:
    t = text.strip()
    lower = t.lower()
    for name in names:
        for prefix in (f"/{name}", name):
            if lower == prefix or lower.startswith(prefix + " "):
                return t[len(prefix) :].strip()
    return ""


def _token_and_reason(rest: str) -> tuple[str, str]:
    parts = rest.strip().split(None, 1)
    if not parts:
        return "", ""
    token = parts[0]
    reason = parts[1].strip() if len(parts) > 1 else ""
    return token, reason


async def handle_verifier_message(
    db: Session,
    client: MessagingClient,
    verifier: Verifier,
    body: str,
    media_url: str | None = None,
) -> None:
    text = (body or "").strip()
    lower = text.lower()

    if media_url and lower in {"", "[photo]", "[document]"}:
        await send_and_log(
            db,
            client,
            to_id=verifier.external_id,
            text="Caja no reenvía fotos. Usa */casos* o */buscar CÓDIGO*.",
            consultation_id=None,
            keyboard=KB_CAJA,
        )
        return

    if lower in {"/ayuda", "ayuda", "help", "/help", "/start", "start"}:
        await send_and_log(
            db,
            client,
            to_id=verifier.external_id,
            text=HELP_CAJA,
            consultation_id=None,
            keyboard=KB_CAJA,
        )
        return

    if lower in {"/casos", "casos"}:
        await _send_pending_list(db, client, verifier)
        return

    if lower.startswith("/buscar") or lower.startswith("buscar "):
        rest = _cmd_arg(text, ("buscar",))
        await _send_search(db, client, verifier, rest)
        return

    if lower.startswith("/confirmar") or lower.startswith("confirmar "):
        rest = _cmd_arg(text, ("confirmar",))
        token, _ = _token_and_reason(rest)
        await _do_confirm(db, client, verifier, token)
        return

    if lower.startswith("/rechazar") or lower.startswith("rechazar "):
        rest = _cmd_arg(text, ("rechazar",))
        token, reason = _token_and_reason(rest)
        await _do_reject(db, client, verifier, token, reason)
        return

    if lower.startswith("/otrafoto") or lower.startswith("otrafoto "):
        rest = _cmd_arg(text, ("otrafoto",))
        token, _ = _token_and_reason(rest)
        await _do_otrafoto(db, client, verifier, token)
        return

    # Código suelto = búsqueda
    if re.search(r"(SALUD-)?[A-Z0-9]{4,8}", text, re.I) and " " not in text.strip():
        await _send_search(db, client, verifier, text.strip())
        return

    await send_and_log(
        db,
        client,
        to_id=verifier.external_id,
        text="No reconocí el comando. */ayuda* — */casos* — */confirmar CÓDIGO*.",
        consultation_id=None,
        keyboard=KB_CAJA,
    )


async def _send_pending_list(db: Session, client: MessagingClient, verifier: Verifier) -> None:
    rows = (
        db.query(Payment)
        .options(joinedload(Payment.user))
        .filter(Payment.status == "pending")
        .order_by(Payment.id.asc())
        .limit(20)
        .all()
    )
    if not rows:
        await send_and_log(
            db,
            client,
            to_id=verifier.external_id,
            text="No hay suscripciones pendientes.",
            consultation_id=None,
            keyboard=KB_CAJA,
        )
        return
    lines = [f"🧾 *Pendientes* ({len(rows)})\n"]
    for p in rows:
        who = p.yape_name or (p.user.name if p.user else None) or "—"
        flag = "📸" if p.voucher_path else "⏳"
        lines.append(
            f"{flag} #{p.id} *{format_pay_code(p.reference)}* · {who} · S/ {p.amount:.2f}"
        )
    lines.append("\n*/confirmar CÓDIGO*  ·  */buscar CÓDIGO*")
    await send_and_log(
        db,
        client,
        to_id=verifier.external_id,
        text="\n".join(lines),
        consultation_id=None,
        keyboard=KB_CAJA,
    )


async def _send_search(
    db: Session, client: MessagingClient, verifier: Verifier, token: str
) -> None:
    if not token:
        await send_and_log(
            db,
            client,
            to_id=verifier.external_id,
            text="Uso: */buscar K7P2* o */buscar SALUD-K7P2*.",
            consultation_id=None,
            keyboard=KB_CAJA,
        )
        return
    payment = find_payment_by_token(db, token)
    if payment is None:
        await send_and_log(
            db,
            client,
            to_id=verifier.external_id,
            text=f"No encontré un pago con *{token}*.",
            consultation_id=None,
            keyboard=KB_CAJA,
        )
        return
    payment = (
        db.query(Payment)
        .options(joinedload(Payment.user))
        .filter(Payment.id == payment.id)
        .first()
        or payment
    )
    extra = ""
    if payment.status == "paid":
        extra = f"\n\n✅ Ya está *pagado* ({format_lima(payment.paid_at)})."
    elif payment.status == "failed":
        extra = f"\n\n❌ Rechazado: {payment.notes or '—'}"
    await send_and_log(
        db,
        client,
        to_id=verifier.external_id,
        text=format_payment_ficha(payment) + extra,
        consultation_id=None,
        keyboard=KB_CAJA,
    )
    if payment.voucher_path:
        from pathlib import Path

        if Path(payment.voucher_path).is_file():
            try:
                await client.send_photo(
                    verifier.external_id,
                    payment.voucher_path,
                    caption=f"Voucher {format_pay_code(payment.reference)}",
                )
            except Exception:
                pass


async def _do_confirm(
    db: Session, client: MessagingClient, verifier: Verifier, token: str
) -> None:
    if not token:
        await send_and_log(
            db,
            client,
            to_id=verifier.external_id,
            text="Uso: */confirmar K7P2*",
            consultation_id=None,
            keyboard=KB_CAJA,
        )
        return
    payment = find_payment_by_token(db, token)
    if payment is None:
        await send_and_log(
            db,
            client,
            to_id=verifier.external_id,
            text=f"No encontré *{token}*. Prueba */casos*.",
            consultation_id=None,
            keyboard=KB_CAJA,
        )
        return
    if payment.status == "paid":
        await send_and_log(
            db,
            client,
            to_id=verifier.external_id,
            text=f"*{format_pay_code(payment.reference)}* ya estaba confirmado.",
            consultation_id=None,
            keyboard=KB_CAJA,
        )
        return
    try:
        await confirm_and_notify(
            db,
            client,
            payment,
            actor_name=verifier.name,
            verifier=verifier,
            except_external_id=verifier.external_id,
        )
    except ValueError as exc:
        await send_and_log(
            db,
            client,
            to_id=verifier.external_id,
            text=str(exc),
            consultation_id=None,
            keyboard=KB_CAJA,
        )
        return
    await send_and_log(
        db,
        client,
        to_id=verifier.external_id,
        text=f"✅ Confirmado *{format_pay_code(payment.reference)}*. El paciente ya puede usar el menú.",
        consultation_id=None,
        keyboard=KB_CAJA,
    )


async def _do_reject(
    db: Session,
    client: MessagingClient,
    verifier: Verifier,
    token: str,
    reason: str,
) -> None:
    if not token:
        await send_and_log(
            db,
            client,
            to_id=verifier.external_id,
            text="Uso: */rechazar K7P2 motivo*",
            consultation_id=None,
            keyboard=KB_CAJA,
        )
        return
    payment = find_payment_by_token(db, token)
    if payment is None:
        await send_and_log(
            db,
            client,
            to_id=verifier.external_id,
            text=f"No encontré *{token}*.",
            consultation_id=None,
            keyboard=KB_CAJA,
        )
        return
    if payment.status == "paid":
        await send_and_log(
            db,
            client,
            to_id=verifier.external_id,
            text="Ese pago ya está confirmado; no se puede rechazar.",
            consultation_id=None,
            keyboard=KB_CAJA,
        )
        return
    why = reason or "No encontramos el Yape con ese código o el monto no cuadra"
    try:
        await reject_and_notify(
            db,
            client,
            payment,
            reason=why,
            actor_name=verifier.name,
            verifier=verifier,
            except_external_id=verifier.external_id,
        )
    except ValueError as exc:
        await send_and_log(
            db,
            client,
            to_id=verifier.external_id,
            text=str(exc),
            consultation_id=None,
            keyboard=KB_CAJA,
        )
        return
    await send_and_log(
        db,
        client,
        to_id=verifier.external_id,
        text=f"❌ Rechazado *{format_pay_code(payment.reference)}*. El paciente fue avisado.",
        consultation_id=None,
        keyboard=KB_CAJA,
    )


async def _do_otrafoto(
    db: Session, client: MessagingClient, verifier: Verifier, token: str
) -> None:
    if not token:
        await send_and_log(
            db,
            client,
            to_id=verifier.external_id,
            text="Uso: */otrafoto K7P2*",
            consultation_id=None,
            keyboard=KB_CAJA,
        )
        return
    payment = find_payment_by_token(db, token)
    if payment is None or payment.status != "pending":
        await send_and_log(
            db,
            client,
            to_id=verifier.external_id,
            text="Solo se pide otra foto de un pago *pendiente*.",
            consultation_id=None,
            keyboard=KB_CAJA,
        )
        return
    await request_another_photo(db, client, payment, actor_name=verifier.name)
    await send_and_log(
        db,
        client,
        to_id=verifier.external_id,
        text=f"Le pedimos otra captura de *{format_pay_code(payment.reference)}*.",
        consultation_id=None,
        keyboard=KB_CAJA,
    )
