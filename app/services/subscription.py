from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import Payment, User

PLAN_DAYS = {
    "monthly": 30,
    "quarterly": 90,
}

# Fácil de dictar y de escribir en el mensaje de Yape (sin 0/O/1/I).
_CODE_ALPH = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_PREFIX = "SALUD"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def plan_amount(plan: str, settings: Settings | None = None) -> float:
    settings = settings or get_settings()
    if plan == "monthly":
        return float(settings.price_monthly)
    if plan == "quarterly":
        return float(settings.price_quarterly)
    return 0.0


def plan_label(plan: str, settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    if plan == "monthly":
        return f"Mensual S/ {settings.price_monthly:.2f} (30 días)"
    if plan == "quarterly":
        return f"Trimestral S/ {settings.price_quarterly:.2f} (90 días)"
    if plan == "trial":
        days = int(settings.trial_days or 3)
        return f"Prueba {days} días"
    return plan or "—"


def format_pay_code(reference: str | None) -> str:
    if not reference:
        return "—"
    ref = reference.strip().upper()
    if ref.startswith(f"{CODE_PREFIX}-"):
        return ref
    return f"{CODE_PREFIX}-{ref}"


def normalize_pay_token(text: str | None) -> str | None:
    """Extrae el código corto (K7P2) o deja un id numérico."""
    if not text:
        return None
    raw = text.strip().upper()
    raw = raw.replace("SALUD-", "").replace("SALUD ", "")
    raw = re.sub(r"[^A-Z0-9]", "", raw)
    return raw or None


def is_subscription_active(user: User, settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    if not settings.require_subscription:
        return True
    if user.subscription_status not in {"active", "trial"}:
        return False
    exp = _as_utc(user.subscription_expires_at)
    if exp is None:
        return False
    return exp > _now()


def subscription_badge(user: User, settings: Settings | None = None) -> str:
    """Línea corta para poner al lado del nombre (menú, ficha médica)."""
    settings = settings or get_settings()
    exp = _as_utc(user.subscription_expires_at)
    exp_s = exp.strftime("%d/%m/%Y") if exp else "—"
    if is_subscription_active(user, settings):
        if user.subscription_status == "trial" or user.subscription_plan == "trial":
            return f"✅ Prueba hasta {exp_s}"
        if user.subscription_plan == "monthly":
            return f"✅ Suscrito · mensual hasta {exp_s}"
        if user.subscription_plan == "quarterly":
            return f"✅ Suscrito · trimestral hasta {exp_s}"
        return f"✅ Suscrito hasta {exp_s}"
    if exp is not None:
        return f"Prueba/plan vencido ({exp_s})"
    return "Sin suscripción"


def subscription_label(user: User, settings: Settings | None = None) -> str:
    return subscription_badge(user, settings)


def generate_pay_code(db: Session) -> str:
    for _ in range(24):
        code = "".join(secrets.choice(_CODE_ALPH) for _ in range(4))
        exists = db.query(Payment.id).filter(Payment.reference == code).first()
        if exists is None:
            return code
    return secrets.token_hex(3).upper()


def start_opening_trial(
    db: Session,
    user: User,
    settings: Settings | None = None,
) -> User:
    """Prueba de arranque (una vez). No crea fila de pago."""
    settings = settings or get_settings()
    days = int(settings.trial_days or 0)
    if days <= 0:
        return user
    if user.subscription_expires_at is not None:
        return user
    if user.subscription_status not in {"none", "", None}:
        return user
    now = _now()
    user.subscription_status = "trial"
    user.subscription_plan = "trial"
    user.subscription_expires_at = now + timedelta(days=days)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def get_pending_payment(db: Session, user: User) -> Payment | None:
    return (
        db.query(Payment)
        .filter(Payment.user_id == user.id, Payment.status == "pending")
        .order_by(Payment.id.desc())
        .first()
    )


def cancel_pending_payments(db: Session, user: User, *, reason: str = "cancelado") -> None:
    rows = (
        db.query(Payment)
        .filter(Payment.user_id == user.id, Payment.status == "pending")
        .all()
    )
    if not rows:
        return
    for p in rows:
        p.status = "failed"
        p.notes = (reason or "cancelado")[:500]
        db.add(p)
    db.commit()


def create_pending_payment(
    db: Session,
    user: User,
    plan: str,
    *,
    yape_name: str,
    yape_phone: str,
    method: str = "yape",
    settings: Settings | None = None,
) -> Payment:
    settings = settings or get_settings()
    if plan not in PLAN_DAYS:
        raise ValueError(f"Plan de cobro inválido: {plan}")
    cancel_pending_payments(db, user, reason="reemplazado por un plan nuevo")
    name = (yape_name or "").strip()[:120]
    phone = (yape_phone or "").strip()[:32]
    user.yape_name = name or user.yape_name
    user.yape_phone = phone or user.yape_phone
    if not user.name and name:
        user.name = name
    payment = Payment(
        user_id=user.id,
        status="pending",
        plan=plan,
        amount=plan_amount(plan, settings),
        currency=settings.currency,
        method=method,
        reference=generate_pay_code(db),
        yape_name=name or None,
        yape_phone=phone or None,
        consent_version=user.consent_version,
        paid_at=None,
    )
    db.add(payment)
    db.add(user)
    db.commit()
    db.refresh(payment)
    db.refresh(user)
    return payment


def attach_voucher(db: Session, payment: Payment, path: str) -> Payment:
    payment.voucher_path = path
    db.add(payment)
    db.commit()
    db.refresh(payment)
    return payment


def _grant_plan(user: User, plan: str, now: datetime, settings: Settings) -> None:
    if plan == "trial":
        days = int(settings.trial_days or 3)
    else:
        days = PLAN_DAYS[plan]
    base = now
    exp = _as_utc(user.subscription_expires_at)
    if exp is not None and exp > now:
        base = exp
    user.subscription_status = "trial" if plan == "trial" else "active"
    user.subscription_plan = plan
    user.subscription_expires_at = base + timedelta(days=days)


def confirm_pending_payment(
    db: Session,
    payment: Payment,
    *,
    verifier_id: int | None = None,
    settings: Settings | None = None,
) -> Payment:
    settings = settings or get_settings()
    if payment.status == "paid":
        return payment
    if payment.status not in {"pending", "failed"}:
        raise ValueError(f"No se puede confirmar un pago en estado {payment.status}")
    user = payment.user
    if user is None:
        user = db.query(User).filter(User.id == payment.user_id).first()
    if user is None:
        raise ValueError("Pago sin usuario")
    now = _now()
    payment.status = "paid"
    payment.paid_at = now
    if verifier_id:
        payment.verifier_id = verifier_id
    _grant_plan(user, payment.plan, now, settings)
    db.add(payment)
    db.add(user)
    db.commit()
    db.refresh(payment)
    db.refresh(user)
    return payment


def reject_pending_payment(
    db: Session,
    payment: Payment,
    *,
    reason: str = "No encontramos el Yape con ese código",
    verifier_id: int | None = None,
) -> Payment:
    if payment.status == "paid":
        raise ValueError("Un pago ya confirmado no se puede rechazar")
    payment.status = "failed"
    payment.notes = (reason or "rechazado")[:500]
    if verifier_id:
        payment.verifier_id = verifier_id
    db.add(payment)
    db.commit()
    db.refresh(payment)
    return payment


def activate_subscription(
    db: Session,
    user: User,
    plan: str,
    *,
    method: str = "yape",
    settings: Settings | None = None,
) -> Payment:
    """Activación inmediata (registro interno / tests). No es el flujo Yape."""
    settings = settings or get_settings()
    if plan not in PLAN_DAYS:
        raise ValueError(f"Plan inválido: {plan}")
    now = _now()
    payment = Payment(
        user_id=user.id,
        status="paid",
        plan=plan,
        amount=plan_amount(plan, settings),
        currency=settings.currency,
        method=method,
        reference=generate_pay_code(db),
        consent_version=user.consent_version,
        paid_at=now,
    )
    _grant_plan(user, plan, now, settings)
    db.add(payment)
    db.add(user)
    db.commit()
    db.refresh(payment)
    db.refresh(user)
    return payment


def yape_qr_path(settings: Settings | None = None) -> Path | None:
    settings = settings or get_settings()
    candidates: list[Path] = []
    if settings.payment_qr_path:
        candidates.append(Path(settings.payment_qr_path))
    root = Path(settings.storage_dir) / "pay"
    candidates.append(root / "yape-qr.png")
    candidates.append(root / "yape-qr.jpg")
    candidates.append(root / "yape-qr.jpeg")
    for path in candidates:
        if path.is_file():
            return path
    return None


def payment_offer_text(settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    trial = int(settings.trial_days or 0)
    trial_line = (
        f"Al entrar tienes *{trial} días de prueba*. Luego mensual o trimestral por Yape/Plin.\n\n"
        if trial > 0
        else ""
    )
    return (
        "💳 *Suscripción Doctor Bot*\n\n"
        f"{trial_line}"
        f"• *1* — {plan_label('monthly', settings)}\n"
        f"• *2* — {plan_label('quarterly', settings)}\n"
        "• *0* — Cancelar\n\n"
        "Pagas al Yape/Plin del proyecto. Una *caja* confirma tu captura "
        "(el médico no ve el voucher)."
    )


def payment_instructions_text(payment: Payment, settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    code = format_pay_code(payment.reference)
    lines = [
        "💸 *Paga por Yape o Plin*",
        "",
        f"Plan: *{plan_label(payment.plan, settings)}*",
        f"Monto: *S/ {payment.amount:.2f}*",
        f"Código: *{code}*",
        "",
        f"En el mensaje del Yape/Plin escribe *exactamente* `{code}`.",
        "",
    ]
    dest: list[str] = []
    if settings.yape_phone:
        who = f" ({settings.yape_name})" if settings.yape_name else ""
        dest.append(f"• Yape: `{settings.yape_phone}`{who}")
    if settings.plin_phone:
        who = f" ({settings.plin_name})" if settings.plin_name else ""
        dest.append(f"• Plin: `{settings.plin_phone}`{who}")
    if dest:
        lines.append("Destino:")
        lines.extend(dest)
        lines.append("")
    else:
        lines.append("_Configura YAPE_PHONE (y opcionalmente el QR) en el servidor._")
        lines.append("")
    lines.extend(
        [
            "Cuando hayas pagado, *envía la foto* del voucher aquí.",
            "Caja lo confirma y te avisamos por este chat.",
        ]
    )
    return "\n".join(lines)


def subscribed_ok_text(user: User, payment: Payment, settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    name = (user.yape_name or user.name or "").strip()
    hello = f"Hola *{name}* · " if name else ""
    exp = _as_utc(user.subscription_expires_at)
    exp_s = exp.strftime("%d/%m/%Y") if exp else "—"
    return (
        f"✅ *Ya estás suscrito*\n\n"
        f"{hello}{subscription_badge(user, settings)}\n"
        f"Plan: *{plan_label(payment.plan, settings)}*\n"
        f"Código de tu pago: *{format_pay_code(payment.reference)}*\n"
        f"Vigente hasta: *{exp_s}*\n\n"
        "Esto es *orientación primaria*, no reemplaza una cita presencial.\n"
        "Emergencias: llama al *106* (SAMU).\n\n"
        "Usa el menú: *1* nueva consulta · *2* estado · *4* ayuda."
    )


def normalize_yape_phone(text: str) -> str | None:
    digits = re.sub(r"\D", "", text or "")
    if not digits:
        return None
    if digits.startswith("51") and len(digits) == 11:
        return digits
    if len(digits) == 9 and digits.startswith("9"):
        return "51" + digits
    if 8 <= len(digits) <= 15:
        return digits
    return None
