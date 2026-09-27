from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session, joinedload

from app.config import get_settings
from app.models import Payment, User, Verifier
from app.providers.base import MessagingClient
from app.services.bridge import send_and_log
from app.services.keyboards import KB_CAJA, menu_keyboard
from app.services.session_store import get_or_create_session, set_state
from app.services.subscription import (
    attach_voucher,
    confirm_pending_payment,
    format_pay_code,
    normalize_pay_token,
    plan_label,
    reject_pending_payment,
    subscribed_ok_text,
)
from app.services.timeutil import format_lima


def get_verifier_by_external_id(db: Session, external_id: str) -> Verifier | None:
    ext = str(external_id).lstrip("+")
    return db.query(Verifier).filter(Verifier.external_id == ext).first()


def list_verifiers(db: Session) -> list[Verifier]:
    return db.query(Verifier).order_by(Verifier.id.asc()).all()


def find_payment_by_token(db: Session, token: str | None) -> Payment | None:
    raw = normalize_pay_token(token)
    if not raw:
        return None
    by_ref = db.query(Payment).filter(Payment.reference == raw).first()
    if by_ref is not None:
        return by_ref
    if raw.isdigit():
        return db.query(Payment).filter(Payment.id == int(raw)).first()
    return None


def format_payment_ficha(payment: Payment) -> str:
    user = payment.user
    code = format_pay_code(payment.reference)
    voucher = "sí (se envía a continuación)" if payment.voucher_path else "pendiente"
    who = payment.yape_name or (user.yape_name if user else None) or (user.name if user else None) or "—"
    phone = payment.yape_phone or (user.yape_phone if user else None) or "—"
    return (
        f"🧾 *Suscripción #{payment.id}*\n"
        f"Código: *{code}*\n\n"
        f"👤 Nombre Yape: {who}\n"
        f"📱 Celular Yape: `{phone}`\n"
        f"💳 Plan: {plan_label(payment.plan)} · S/ {payment.amount:.2f}\n"
        f"📸 Voucher: {voucher}\n"
        f"🕒 {format_lima(payment.created_at)}\n\n"
        f"Cruza con el Yape del proyecto y:\n"
        f"*/confirmar {payment.reference}*\n"
        f"*/rechazar {payment.reference} motivo*\n"
        f"*/otrafoto {payment.reference}* si está borrosa"
    )


async def persist_payment_voucher(
    client: MessagingClient,
    media_url: str | None,
    payment_id: int,
) -> str | None:
    if not media_url or not payment_id:
        return None
    settings = get_settings()
    media_dir = Path(settings.storage_dir) / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    dest = media_dir / f"pago_{payment_id}_voucher"
    saved = await client.download_media(media_url, dest)
    return str(saved) if saved else None


async def save_voucher_from_message(
    db: Session,
    client: MessagingClient,
    payment: Payment,
    media_url: str | None,
) -> Payment | None:
    path = await persist_payment_voucher(client, media_url, payment.id)
    if not path:
        return None
    return attach_voucher(db, payment, path)


async def notify_verifiers_new_payment(
    db: Session,
    client: MessagingClient,
    payment: Payment,
) -> int:
    """Deriva la ficha a todas las cajas. Devuelve cuántas fueron notificadas."""
    db.refresh(payment)
    if payment.user is None:
        payment = (
            db.query(Payment)
            .options(joinedload(Payment.user))
            .filter(Payment.id == payment.id)
            .first()
            or payment
        )
    verifiers = list_verifiers(db)
    card = format_payment_ficha(payment)
    n = 0
    for v in verifiers:
        await send_and_log(
            db,
            client,
            to_id=v.external_id,
            text=card,
            consultation_id=None,
            keyboard=KB_CAJA,
        )
        if payment.voucher_path and Path(payment.voucher_path).is_file():
            try:
                await client.send_photo(
                    v.external_id,
                    payment.voucher_path,
                    caption=f"Voucher {format_pay_code(payment.reference)}",
                )
            except Exception:
                pass
        n += 1
    return n


async def notify_verifiers_taken(
    db: Session,
    client: MessagingClient,
    payment: Payment,
    *,
    by_name: str,
    except_external_id: str | None = None,
    action: str = "confirmó",
) -> None:
    code = format_pay_code(payment.reference)
    for v in list_verifiers(db):
        if except_external_id and v.external_id == except_external_id:
            continue
        await send_and_log(
            db,
            client,
            to_id=v.external_id,
            text=f"{by_name} {action} *{code}* (#{payment.id}).",
            consultation_id=None,
            keyboard=KB_CAJA,
        )


async def confirm_and_notify(
    db: Session,
    client: MessagingClient,
    payment: Payment,
    *,
    actor_name: str,
    verifier: Verifier | None = None,
    except_external_id: str | None = None,
) -> Payment:
    payment = confirm_pending_payment(
        db, payment, verifier_id=verifier.id if verifier else None
    )
    user = payment.user or db.query(User).filter(User.id == payment.user_id).first()
    if user:
        sess = get_or_create_session(db, user.external_id, role="patient")
        set_state(db, sess, "menu", {})
        await send_and_log(
            db,
            client,
            to_id=user.external_id,
            text=subscribed_ok_text(user, payment),
            consultation_id=None,
            keyboard=menu_keyboard(),
        )
    await notify_verifiers_taken(
        db,
        client,
        payment,
        by_name=actor_name,
        except_external_id=except_external_id,
        action="confirmó",
    )
    return payment


async def reject_and_notify(
    db: Session,
    client: MessagingClient,
    payment: Payment,
    *,
    reason: str,
    actor_name: str,
    verifier: Verifier | None = None,
    except_external_id: str | None = None,
) -> Payment:
    payment = reject_pending_payment(
        db,
        payment,
        reason=reason,
        verifier_id=verifier.id if verifier else None,
    )
    user = payment.user or db.query(User).filter(User.id == payment.user_id).first()
    code = format_pay_code(payment.reference)
    if user:
        sess = get_or_create_session(db, user.external_id, role="patient")
        set_state(db, sess, "menu", {})
        await send_and_log(
            db,
            client,
            to_id=user.external_id,
            text=(
                f"No pudimos confirmar el pago *{code}*.\n"
                f"Motivo: {reason}\n\n"
                "Puedes intentar de nuevo con *3 Suscripción*."
            ),
            consultation_id=None,
            keyboard=menu_keyboard(),
        )
    await notify_verifiers_taken(
        db,
        client,
        payment,
        by_name=actor_name,
        except_external_id=except_external_id,
        action="rechazó",
    )
    return payment


async def request_another_photo(
    db: Session,
    client: MessagingClient,
    payment: Payment,
    *,
    actor_name: str,
) -> None:
    user = payment.user or db.query(User).filter(User.id == payment.user_id).first()
    if user is None:
        return
    code = format_pay_code(payment.reference)
    sess = get_or_create_session(db, user.external_id, role="patient")
    set_state(db, sess, "pay_wait", {"payment_id": payment.id})
    await send_and_log(
        db,
        client,
        to_id=user.external_id,
        text=(
            f"{actor_name} pide una *captura más nítida* del Yape *{code}*.\n"
            "Manda otra foto aquí (no hace falta volver a escribir tus datos)."
        ),
        consultation_id=None,
    )
