from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Consultation, Payment, User
from app.providers.base import IncomingMessage, MessagingClient
from app.services.bridge import (
    forward_patient_to_doctor,
    notify_doctor_assignment,
    notify_patient_assigned,
    persist_consultation_media,
    send_and_log,
)
from app.services.caja import (
    get_verifier_by_external_id,
    notify_verifiers_new_payment,
    save_voucher_from_message,
)
from app.services.doctor_commands import handle_doctor_message
from app.services.emergency import EMERGENCY_MESSAGE, is_emergency
from app.services.routing import (
    find_available_doctor,
    get_doctor_by_external_id,
    get_or_create_user,
    infer_soft_specialty,
    queue_consultation,
    queue_position,
    try_assign_queued,
)
from app.services.consent import (
    CONSENT_TEXT,
    CONSENT_VERSION,
    DOCUMENT_HELP,
    is_consent_accept,
    is_consent_reject,
    mask_document,
    parse_document,
    record_consent,
    user_has_current_consent,
    user_has_document,
)
from app.services.doctor_display import format_doctor_credentials
from app.services.keyboards import (
    KB_CHAT,
    KB_CONSENT,
    KB_PAY_FICHA,
    KB_PAY_PLAN,
    KB_PAY_WAIT,
    KB_TRIAGE,
    KB_TRIAGE_PHOTO,
    menu_keyboard,
)
from app.services.session_store import get_context, get_or_create_session, set_state
from app.services.subscription import (
    cancel_pending_payments,
    create_pending_payment,
    format_pay_code,
    get_pending_payment,
    is_subscription_active,
    normalize_yape_phone,
    payment_instructions_text,
    payment_offer_text,
    subscription_badge,
    subscription_label,
    yape_qr_path,
)
from app.services.verifier_commands import handle_verifier_message

_PAY_STATES = {
    "pay_plan",
    "pay_dni",
    "pay_consent",
    "pay_name",
    "pay_phone",
    "pay_wait",
    "pay_confirm",
}


def _welcome(user: User) -> str:
    settings = get_settings()
    name = (user.yape_name or user.name or "").strip()
    hello = f"Hola *{name}*" if name else "*Bienvenido a Doctor Bot*"
    badge = subscription_badge(user, settings)
    trial_hint = ""
    if user.subscription_status == "trial" or user.subscription_plan == "trial":
        trial = int(settings.trial_days or 0)
        if trial > 0 and is_subscription_active(user, settings):
            trial_hint = (
                f"Estás en *prueba ({trial} días)*. "
                "Cuando termine, suscríbete con la opción *3*.\n\n"
            )
    return (
        f"👋 {hello} · {badge}\n\n"
        f"{trial_hint}"
        "Teleorientación médica primaria.\n"
        "_No es un servicio de emergencias._\n\n"
        "Usa los *botones* o escribe el número:\n"
        "1️⃣ Nueva consulta\n"
        "2️⃣ Estado de mi consulta\n"
        "3️⃣ Suscripción / pago\n"
        "4️⃣ Ayuda\n\n"
        "También puedes escribir *menu* en cualquier momento."
    )


def _help_text() -> str:
    settings = get_settings()
    trial = int(settings.trial_days or 0)
    trial_line = (
        f"• Al empezar tienes *{trial} días de prueba*. "
        f"Luego mensual S/ {settings.price_monthly:.0f} o trimestral S/ {settings.price_quarterly:.0f} por Yape/Plin.\n"
        if trial > 0
        else (
            f"• Plan mensual S/ {settings.price_monthly:.0f} o trimestral "
            f"S/ {settings.price_quarterly:.0f} por Yape/Plin.\n"
        )
    )
    return (
        "ℹ️ *Ayuda Doctor Bot*\n\n"
        "• Completa el triaje en ~2–3 minutos.\n"
        "• Un médico colegiado revisa tu caso (meta: <30 min).\n"
        f"{trial_line}"
        "• Caja confirma tu captura; el médico no ve el voucher.\n"
        "• Esto es *orientación*, no reemplaza una cita presencial.\n"
        "• Si hay signos de emergencia: llama al *106* (SAMU).\n"
        "• Si dejaste un triaje a medias: botón *Continuar* o *2*.\n"
        "• Para abandonar un triaje: *Cancelar consulta*.\n\n"
        "Escribe *menu* o usa el botón *Menú*."
    )

TRIAGE_STEPS = [
    ("name", "¿Cuál es tu *nombre* (o del paciente)?"),
    ("age", "¿Qué *edad* tienes? (solo el número)"),
    ("symptom", "Describe el *síntoma principal* en una frase."),
    ("duration", "¿Hace *cuánto tiempo* empezó? (ej. 2 días, esta mañana)"),
    (
        "photo_note",
        "Si tienes una *foto* del problema (erupción, resultado, etc.), descríbela o escribe *omitir*.",
    ),
]

STATUS_LABELS = {
    "triaging": "En triaje (formulario incompleto — aún no llega al médico)",
    "queued": "En cola (esperando médico disponible)",
    "assigned": "Asignada a un médico",
    "active": "En conversación con el médico",
    "closed": "Cerrada",
    "emergency": "Derivada a emergencia",
}


def _menu_choice(text: str) -> str | None:
    t = text.strip().lower()
    if t in {"/start", "start"}:
        return "menu"
    if t in {"1", "1️⃣", "nueva", "nueva consulta", "consulta"} or t.startswith("1 nueva"):
        return "new"
    if t in {"2", "2️⃣", "estado", "estado de mi consulta", "status"} or t.startswith("2 estado"):
        return "status"
    if t in {"3", "3️⃣", "suscripcion", "suscripción", "pago", "pagar", "planes"} or t.startswith(
        "3 suscrip"
    ):
        return "subscription"
    if t in {"4", "4️⃣", "ayuda", "help"} or t.startswith("4 ayuda"):
        return "help"
    if t in {
        "continuar",
        "continúa",
        "continua",
        "seguir",
        "retomar",
        "reanudar",
    }:
        return "continue"
    if t in {
        "cancelar consulta",
        "cancelar triaje",
        "abandonar consulta",
        "anular consulta",
    }:
        return "cancel_consultation"
    if t in {"menu", "menú", "inicio", "hola", "hi", "buenas", "buenos dias", "buenos días"}:
        return "menu"
    return None


def _kb_menu(open_c: Consultation | None) -> list[list[str]]:
    resume = bool(open_c and open_c.status == "triaging")
    if open_c and open_c.status in {"assigned", "active"}:
        return KB_CHAT
    return menu_keyboard(resume=resume)


def _open_consultation(db: Session, user: User) -> Consultation | None:
    return (
        db.query(Consultation)
        .filter(
            Consultation.user_id == user.id,
            Consultation.status.in_(["triaging", "queued", "assigned", "active"]),
        )
        .order_by(Consultation.id.desc())
        .first()
    )


def _parse_age(text: str) -> int | None:
    raw = (text or "").strip()
    if raw.isdigit():
        n = int(raw)
    else:
        m = re.search(r"\d{1,3}", raw)
        if not m:
            return None
        n = int(m.group())
    if n < 0 or n > 120:
        return None
    return n


def _load_triage(consultation: Consultation) -> dict:
    if not consultation.triage_json:
        return {}
    try:
        data = json.loads(consultation.triage_json)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _next_triage_step(triage: dict) -> tuple[int, str, str]:
    """Devuelve (índice 0-based, key, prompt) del siguiente paso pendiente."""
    for idx, (key, prompt) in enumerate(TRIAGE_STEPS):
        if key not in triage or triage.get(key) in (None, ""):
            return idx, key, prompt
    # Completo: último paso (por si acaso)
    idx = len(TRIAGE_STEPS) - 1
    key, prompt = TRIAGE_STEPS[idx]
    return idx, key, prompt


def _save_partial_triage(db: Session, consultation: Consultation, triage: dict) -> None:
    consultation.triage_json = json.dumps(triage, ensure_ascii=False)
    db.add(consultation)
    db.commit()


async def handle_incoming(
    db: Session,
    client: MessagingClient,
    message: IncomingMessage,
) -> list[str]:
    ext_id = message.external_id.lstrip("+")
    body = (message.body or "").strip()
    settings = get_settings()

    doctor = get_doctor_by_external_id(db, ext_id)
    if doctor is not None:
        await handle_doctor_message(db, client, doctor, body, media_url=message.media_url)
        return []

    verifier = get_verifier_by_external_id(db, ext_id)
    if verifier is not None:
        await handle_verifier_message(db, client, verifier, body, media_url=message.media_url)
        return []

    user = get_or_create_user(db, ext_id)
    sess = get_or_create_session(db, ext_id, role="patient")

    if sess.state in _PAY_STATES:
        await _handle_payment_flow(
            db, client, user, sess, ext_id, body, media_url=message.media_url
        )
        return []

    choice = _menu_choice(body)
    open_c = _open_consultation(db, user)

    # Cancelar triaje / consulta incompleta (antes de otros menús)
    if choice == "cancel_consultation" or (
        body.strip().lower() == "cancelar" and open_c and open_c.status == "triaging"
    ):
        await _cancel_open_consultation(db, client, user, sess, ext_id)
        return []

    # Retomar triaje incompleto
    if choice == "continue":
        if open_c and open_c.status == "triaging":
            await _resume_triage(db, client, user, sess, ext_id, open_c)
        elif open_c:
            await _send_status(db, client, user, sess, ext_id)
        else:
            await send_and_log(
                db,
                client,
                to_id=ext_id,
                text="No hay un triaje pendiente. Usa *1 Nueva consulta*.",
                consultation_id=None,
                keyboard=menu_keyboard(),
            )
        return []

    # Chat activo con médico
    active = (
        open_c
        if open_c and open_c.status in {"assigned", "active"}
        else None
    )
    if active and choice is None and not sess.state.startswith("triage_"):
        if is_emergency(body):
            await send_and_log(
                db, client, to_id=ext_id, text=EMERGENCY_MESSAGE, consultation_id=active.id
            )
            return []
        photo_path = None
        if message.media_url:
            stamp = datetime.now(timezone.utc).strftime("%H%M%S")
            photo_path = await _persist_photo(
                client, message.media_url, active.id, suffix=f"chat_{stamp}"
            )
        await forward_patient_to_doctor(
            db, client, active, body, photo_path=photo_path
        )
        return []

    # En medio del triaje: respuestas van al paso (salvo menu/status/help/new)
    if sess.state.startswith("triage_"):
        if choice == "menu":
            set_state(db, sess, "menu", get_context(sess))
            note = ""
            if open_c and open_c.status == "triaging":
                note = (
                    f"\n\n⚠️ Tienes la consulta #{open_c.id} *en triaje incompleto*.\n"
                    "Escribe *continuar* o *1* para retomar, o *cancelar consulta* para abandonarla."
                )
            await send_and_log(
                db,
                client,
                to_id=ext_id,
                text=_welcome(user) + note,
                consultation_id=open_c.id if open_c else None,
                keyboard=_kb_menu(open_c),
            )
            return []
        if choice == "status":
            await _send_status(db, client, user, sess, ext_id)
            return []
        if choice == "help":
            await send_and_log(
                db,
                client,
                to_id=ext_id,
                text=_help_text(),
                consultation_id=None,
                keyboard=_kb_menu(open_c),
            )
            return []
        if choice == "new":
            # Reanuda en lugar de crear otra
            if open_c and open_c.status == "triaging":
                await _resume_triage(db, client, user, sess, ext_id, open_c)
                return []
        if choice is None or choice not in {"subscription"}:
            await _handle_triage_step(
                db, client, user, sess, ext_id, body, media_url=message.media_url
            )
            return []

    if choice == "menu" or (sess.state == "idle" and not body):
        set_state(db, sess, "menu", {})
        note = ""
        if open_c and open_c.status == "triaging":
            note = (
                f"\n\n⚠️ Consulta #{open_c.id} *en triaje incompleto*.\n"
                "Escribe *continuar* o *1* para retomar · *cancelar consulta* para abandonar."
            )
        elif open_c:
            note = (
                f"\n\n📋 Tienes la consulta #{open_c.id} "
                f"(*{STATUS_LABELS.get(open_c.status, open_c.status)}*). Escribe *2* para detalles."
            )
        await send_and_log(
            db,
            client,
            to_id=ext_id,
            text=_welcome(user) + note,
            consultation_id=open_c.id if open_c else None,
            keyboard=_kb_menu(open_c),
        )
        return []

    if choice == "help":
        await send_and_log(
            db, client, to_id=ext_id, text=_help_text(), consultation_id=None, keyboard=_kb_menu(open_c)
        )
        return []

    if choice == "status":
        await _send_status(db, client, user, sess, ext_id)
        return []

    if choice == "subscription":
        await _start_payment(db, client, user, sess, ext_id)
        return []

    if choice == "new":
        if not is_subscription_active(user, settings):
            expired_trial = user.subscription_plan == "trial" or user.subscription_status == "trial"
            head = (
                "Tu periodo de prueba terminó. Suscríbete por Yape para seguir consultando.\n\n"
                if expired_trial
                else "Para una nueva consulta necesitas suscripción activa.\n\n"
            )
            await _start_payment(db, client, user, sess, ext_id, intro=head)
            return []
        await _start_triage(db, client, user, sess, ext_id)
        return []

    if sess.state in {"idle", "menu"} and choice is None:
        if is_emergency(body):
            await send_and_log(db, client, to_id=ext_id, text=EMERGENCY_MESSAGE, consultation_id=None)
            set_state(db, sess, "idle", {})
            return []
        # Si hay triaje colgado y escribe texto libre, retomar
        if open_c and open_c.status == "triaging":
            await _resume_triage(db, client, user, sess, ext_id, open_c)
            return []
        set_state(db, sess, "menu", {})
        await send_and_log(
            db, client, to_id=ext_id, text=_welcome(user), consultation_id=None, keyboard=_kb_menu(open_c)
        )
        return []

    set_state(db, sess, "menu", {})
    await send_and_log(
        db, client, to_id=ext_id, text=_welcome(user), consultation_id=None, keyboard=_kb_menu(open_c)
    )
    return []


def _is_photo(body: str, media_url: str | None) -> bool:
    if media_url:
        return True
    return (body or "").strip().lower() in {"[photo]", "[document]"}


def _cancel_pay(t: str) -> bool:
    return t.startswith("0") or t in {"cancelar", "menu", "menú", "0 cancelar"}


async def _start_payment(
    db: Session,
    client: MessagingClient,
    user: User,
    sess,
    ext_id: str,
    *,
    intro: str = "",
) -> None:
    settings = get_settings()
    pending = get_pending_payment(db, user)
    if pending is not None:
        ctx = {"payment_id": pending.id}
        if pending.voucher_path:
            set_state(db, sess, "pay_wait", ctx)
            await send_and_log(
                db,
                client,
                to_id=ext_id,
                text=(
                    f"Tu pago *{format_pay_code(pending.reference)}* ya está en caja.\n"
                    "Te escribimos al confirmar. Si la captura no se veía, manda *otra foto*."
                ),
                consultation_id=None,
                keyboard=KB_PAY_WAIT,
            )
            return
        if pending.yape_name and pending.yape_phone:
            set_state(db, sess, "pay_wait", ctx)
            await _send_pay_instructions(db, client, ext_id, pending)
            return
        set_state(db, sess, "pay_name", {"selected_plan": pending.plan, "payment_id": pending.id})
        await _ask_yape_name(db, client, user, sess, ext_id)
        return

    label = subscription_label(user)
    set_state(db, sess, "pay_plan", {})
    await send_and_log(
        db,
        client,
        to_id=ext_id,
        text=f"{intro}Tu plan: *{label}*\n\n{payment_offer_text(settings)}",
        consultation_id=None,
        keyboard=KB_PAY_PLAN,
    )


async def _ask_dni(db: Session, client: MessagingClient, user: User, sess, ext_id: str) -> None:
    known = user_has_document(user)
    extra = ""
    if known:
        extra = (
            f"\n\nTenemos: `{mask_document(user.document_number)}`. "
            "Escribe *ok* para usarlo, o manda otro."
        )
    await send_and_log(
        db,
        client,
        to_id=ext_id,
        text=(
            "Para la ficha de tu *cuenta* (no se pide en cada consulta), "
            "¿cuál es tu *DNI* (8 dígitos) o *carné de extranjería*?"
            f"{extra}"
        ),
        consultation_id=None,
        keyboard=KB_PAY_FICHA if known else KB_PAY_WAIT,
    )


async def _ask_consent(db: Session, client: MessagingClient, ext_id: str) -> None:
    await send_and_log(
        db,
        client,
        to_id=ext_id,
        text=(
            f"{CONSENT_TEXT}\n\n"
            f"Versión *{CONSENT_VERSION}*. Pulsa *Acepto* para seguir "
            "o *No acepto* para volver al menú (tu prueba no se toca)."
        ),
        consultation_id=None,
        keyboard=KB_CONSENT,
    )


async def _continue_after_document(
    db: Session,
    client: MessagingClient,
    user: User,
    sess,
    ext_id: str,
    ctx: dict,
) -> None:
    if user_has_current_consent(user):
        set_state(db, sess, "pay_name", ctx)
        await _ask_yape_name(db, client, user, sess, ext_id)
        return
    set_state(db, sess, "pay_consent", ctx)
    await _ask_consent(db, client, ext_id)


async def _ask_yape_name(db: Session, client: MessagingClient, user: User, sess, ext_id: str) -> None:
    known = (user.yape_name or "").strip()
    extra = (
        f"\n\nTenemos: *{known}*. Escribe *ok* para usarlo, o manda el nombre otra vez."
        if known
        else ""
    )
    await send_and_log(
        db,
        client,
        to_id=ext_id,
        text=(
            "Para cruzar el Yape, ¿cuál es tu *nombre completo como figura en Yape*?"
            f"{extra}"
        ),
        consultation_id=None,
        keyboard=KB_PAY_FICHA if known else KB_PAY_WAIT,
    )


async def _ask_yape_phone(db: Session, client: MessagingClient, user: User, sess, ext_id: str) -> None:
    known = (user.yape_phone or "").strip()
    extra = (
        f"\n\nTenemos: `{known}`. Escribe *ok* para usarlo, o manda el número otra vez."
        if known
        else ""
    )
    await send_and_log(
        db,
        client,
        to_id=ext_id,
        text=(
            "¿Con qué *celular Yapeas*? (9 dígitos, el de la app)"
            f"{extra}"
        ),
        consultation_id=None,
        keyboard=KB_PAY_FICHA if known else KB_PAY_WAIT,
    )


async def _send_pay_instructions(
    db: Session, client: MessagingClient, ext_id: str, payment: Payment
) -> None:
    await send_and_log(
        db,
        client,
        to_id=ext_id,
        text=payment_instructions_text(payment),
        consultation_id=None,
        keyboard=KB_PAY_WAIT,
    )
    qr = yape_qr_path()
    if qr is not None:
        try:
            await client.send_photo(
                ext_id,
                str(qr),
                caption=f"QR Yape · pon *{format_pay_code(payment.reference)}* en el mensaje",
            )
        except Exception:
            pass


async def _handle_payment_flow(
    db: Session,
    client: MessagingClient,
    user: User,
    sess,
    ext_id: str,
    body: str,
    media_url: str | None = None,
) -> None:
    ctx = get_context(sess)
    t = body.strip().lower()

    if sess.state == "pay_confirm":
        set_state(db, sess, "pay_plan", ctx)
        sess.state = "pay_plan"

    if sess.state == "pay_plan":
        if _cancel_pay(t):
            set_state(db, sess, "menu", {})
            await send_and_log(
                db, client, to_id=ext_id, text=_welcome(user), consultation_id=None, keyboard=menu_keyboard()
            )
            return
        if t.startswith("1"):
            plan = "monthly"
        elif t.startswith("2"):
            plan = "quarterly"
        else:
            await send_and_log(
                db,
                client,
                to_id=ext_id,
                text="Elige *1* mensual, *2* trimestral o *0* para cancelar.\nLa prueba de 3 días se activa sola al entrar.",
                consultation_id=None,
                keyboard=KB_PAY_PLAN,
            )
            return
        ctx["selected_plan"] = plan
        set_state(db, sess, "pay_dni", ctx)
        await _ask_dni(db, client, user, sess, ext_id)
        return

    if sess.state == "pay_dni":
        if _cancel_pay(t):
            set_state(db, sess, "menu", {})
            await send_and_log(
                db, client, to_id=ext_id, text=_welcome(user), consultation_id=None, keyboard=menu_keyboard()
            )
            return
        if _is_photo(body, media_url):
            await send_and_log(
                db,
                client,
                to_id=ext_id,
                text="Primero el *DNI o CE* (texto). Luego te pedimos Yape y la captura.",
                consultation_id=None,
                keyboard=KB_PAY_WAIT,
            )
            return
        if t in {"ok", "sí", "si", "mismo"} and user_has_document(user):
            parsed = parse_document(user.document_number or "")
        else:
            parsed = parse_document(body)
        if parsed is None:
            await send_and_log(
                db,
                client,
                to_id=ext_id,
                text=DOCUMENT_HELP,
                consultation_id=None,
                keyboard=KB_PAY_FICHA if user_has_document(user) else KB_PAY_WAIT,
            )
            return
        doc_type, doc_number = parsed
        user.document_type = doc_type
        user.document_number = doc_number
        db.add(user)
        db.commit()
        await _continue_after_document(db, client, user, sess, ext_id, ctx)
        return

    if sess.state == "pay_consent":
        if _cancel_pay(t) or is_consent_reject(t):
            set_state(db, sess, "menu", {})
            settings = get_settings()
            trial_on = is_subscription_active(user, settings) and (
                user.subscription_status == "trial" or user.subscription_plan == "trial"
            )
            extra = (
                "Tu periodo de prueba sigue activo; puedes consultar con *1*."
                if trial_on
                else "Sin suscripción no hay consultas nuevas. Escribe *3* si cambias de opinión."
            )
            await send_and_log(
                db,
                client,
                to_id=ext_id,
                text=f"No se creó la suscripción. {extra}\n\n{_welcome(user)}",
                consultation_id=None,
                keyboard=menu_keyboard(),
            )
            return
        if not is_consent_accept(t):
            await send_and_log(
                db,
                client,
                to_id=ext_id,
                text="Pulsa *Acepto* para continuar o *No acepto* para volver al menú.",
                consultation_id=None,
                keyboard=KB_CONSENT,
            )
            return
        record_consent(user)
        db.add(user)
        db.commit()
        set_state(db, sess, "pay_name", ctx)
        await _ask_yape_name(db, client, user, sess, ext_id)
        return

    if sess.state == "pay_name":
        if _cancel_pay(t):
            set_state(db, sess, "menu", {})
            await send_and_log(
                db, client, to_id=ext_id, text=_welcome(user), consultation_id=None, keyboard=menu_keyboard()
            )
            return
        if _is_photo(body, media_url):
            await send_and_log(
                db,
                client,
                to_id=ext_id,
                text="Primero el *nombre como figura en Yape* (texto). Luego te pedimos la captura.",
                consultation_id=None,
                keyboard=KB_PAY_WAIT,
            )
            return
        name = (user.yape_name or "").strip() if t in {"ok", "sí", "si", "mismo"} else body.strip()
        if len(name) < 3:
            await send_and_log(
                db,
                client,
                to_id=ext_id,
                text="Escribe el nombre completo (mínimo 3 letras).",
                consultation_id=None,
                keyboard=KB_PAY_WAIT,
            )
            return
        ctx["yape_name"] = name[:120]
        set_state(db, sess, "pay_phone", ctx)
        await _ask_yape_phone(db, client, user, sess, ext_id)
        return

    if sess.state == "pay_phone":
        if _cancel_pay(t):
            set_state(db, sess, "menu", {})
            await send_and_log(
                db, client, to_id=ext_id, text=_welcome(user), consultation_id=None, keyboard=menu_keyboard()
            )
            return
        if _is_photo(body, media_url):
            await send_and_log(
                db,
                client,
                to_id=ext_id,
                text="Primero el *celular de Yape* (números). Después mandas la captura.",
                consultation_id=None,
                keyboard=KB_PAY_WAIT,
            )
            return
        if t in {"ok", "sí", "si", "mismo"} and user.yape_phone:
            phone = user.yape_phone
        else:
            phone = normalize_yape_phone(body)
        if not phone:
            await send_and_log(
                db,
                client,
                to_id=ext_id,
                text="No reconocí el número. Ejemplo: *999888777*.",
                consultation_id=None,
                keyboard=KB_PAY_WAIT,
            )
            return
        plan = ctx.get("selected_plan", "monthly")
        payment = create_pending_payment(
            db,
            user,
            plan,
            yape_name=ctx.get("yape_name") or user.yape_name or "",
            yape_phone=phone,
        )
        set_state(db, sess, "pay_wait", {"payment_id": payment.id})
        await _send_pay_instructions(db, client, ext_id, payment)
        return

    if sess.state == "pay_wait":
        if _cancel_pay(t):
            cancel_pending_payments(db, user, reason="cancelado por el paciente")
            set_state(db, sess, "menu", {})
            await send_and_log(
                db, client, to_id=ext_id, text=_welcome(user), consultation_id=None, keyboard=menu_keyboard()
            )
            return
        pid = ctx.get("payment_id")
        payment = db.query(Payment).filter(Payment.id == pid).first() if pid else get_pending_payment(db, user)
        if payment is None or payment.status != "pending":
            set_state(db, sess, "menu", {})
            await send_and_log(
                db,
                client,
                to_id=ext_id,
                text="No hay un pago pendiente. Usa *3 Suscripción* para empezar de nuevo.",
                consultation_id=None,
                keyboard=menu_keyboard(),
            )
            return
        if _is_photo(body, media_url):
            saved = await save_voucher_from_message(db, client, payment, media_url)
            if not saved:
                await send_and_log(
                    db,
                    client,
                    to_id=ext_id,
                    text="No pude guardar la foto. Intenta mandarla otra vez.",
                    consultation_id=None,
                    keyboard=KB_PAY_WAIT,
                )
                return
            n = await notify_verifiers_new_payment(db, client, saved)
            extra = (
                " Caja ya la está viendo."
                if n
                else " Quedó registrada; caja la confirma desde el panel o el chat."
            )
            await send_and_log(
                db,
                client,
                to_id=ext_id,
                text=(
                    f"Recibimos tu captura. Código *{format_pay_code(payment.reference)}*.\n"
                    f"Te escribimos al confirmar.{extra}"
                ),
                consultation_id=None,
                keyboard=menu_keyboard(),
            )
            set_state(db, sess, "menu", {"payment_id": payment.id})
            return
        await send_and_log(
            db,
            client,
            to_id=ext_id,
            text=(
                f"Manda la *foto del voucher* (código *{format_pay_code(payment.reference)}*).\n"
                "O *cancelar* para abortar."
            ),
            consultation_id=None,
            keyboard=KB_PAY_WAIT,
        )
        return

    set_state(db, sess, "menu", {})
    await send_and_log(
        db, client, to_id=ext_id, text=_welcome(user), consultation_id=None, keyboard=menu_keyboard()
    )


async def _send_status(
    db: Session,
    client: MessagingClient,
    user: User,
    sess,
    ext_id: str,
) -> None:
    c = _open_consultation(db, user)
    sub = subscription_label(user)
    pending = get_pending_payment(db, user)
    pay_line = ""
    if pending:
        flag = "captura recibida" if pending.voucher_path else "esperando captura"
        pay_line = f"\nPago en caja: *{format_pay_code(pending.reference)}* ({flag})."
    if c is None:
        text = (
            "📋 *Estado de tu consulta*\n\n"
            "No tienes consultas abiertas.\n"
            "Escribe *1* para iniciar una.\n\n"
            f"_Suscripción: {sub}_" + pay_line
        )
        await send_and_log(
            db, client, to_id=ext_id, text=text, consultation_id=None, keyboard=menu_keyboard()
        )
        return

    label = STATUS_LABELS.get(c.status, c.status)
    lines = [
        "📋 *Estado de tu consulta*",
        "",
        f"Consulta *#{c.id}*",
        f"Estado: *{label}*",
    ]
    if c.status == "triaging":
        triage = _load_triage(c)
        done = sum(1 for k, _ in TRIAGE_STEPS if triage.get(k))
        lines.append(f"Progreso del triaje: *{done}/{len(TRIAGE_STEPS)}* pasos")
        lines.append("")
        lines.append("➡️ Escribe *continuar* o *1* para retomar el formulario.")
        lines.append("🗑 Escribe *cancelar consulta* si la iniciaste por error.")
        lines.append("")
        lines.append(
            "_Mientras esté en triaje incompleto, el médico *no* ve tu caso "
            "(aún no terminaste el formulario)._"
        )
    elif c.status == "queued":
        pos, total = queue_position(db, c)
        lines.append("")
        lines.append(f"Estás en cola: posición *{pos}* de *{total}*.")
        lines.append("Te avisaremos cuando un médico tome tu caso.")
    elif c.status in {"assigned", "active"}:
        lines.append("")
        lines.append(
            f"Te atiende {format_doctor_credentials(c.doctor)}. "
            "Escribe aquí; se reenvía al médico."
        )
        lines.append("También puedes enviar una *foto*.")
    lines.append("")
    lines.append(f"_Suscripción: {sub}_" + pay_line)
    kb = KB_CHAT if c.status in {"assigned", "active"} else _kb_menu(c)
    await send_and_log(
        db, client, to_id=ext_id, text="\n".join(lines), consultation_id=c.id, keyboard=kb
    )


async def _cancel_open_consultation(
    db: Session,
    client: MessagingClient,
    user: User,
    sess,
    ext_id: str,
) -> None:
    c = _open_consultation(db, user)
    if c is None:
        await send_and_log(
            db,
            client,
            to_id=ext_id,
            text="No tienes una consulta abierta para cancelar.",
            consultation_id=None,
            keyboard=menu_keyboard(),
        )
        return
    if c.status not in {"triaging", "queued"}:
        await send_and_log(
            db,
            client,
            to_id=ext_id,
            text=(
                f"La consulta #{c.id} está *{STATUS_LABELS.get(c.status, c.status)}*. "
                "Si ya hay un médico asignado, no se puede cancelar desde aquí; "
                "pídele que use */cerrar*."
            ),
            consultation_id=c.id,
        )
        return
    cid = c.id
    c.status = "closed"
    c.closed_at = datetime.now(timezone.utc)
    c.orientation_text = (c.orientation_text or "") + "\n[Cancelada por el paciente]"
    db.add(c)
    db.commit()
    set_state(db, sess, "menu", {})
    await send_and_log(
        db,
        client,
        to_id=ext_id,
        text=(
            f"✅ Consulta #{cid} cancelada.\n"
            "Ya puedes usar *1 Nueva consulta* o *Menú*."
        ),
        consultation_id=cid,
        keyboard=menu_keyboard(),
    )


async def _resume_triage(
    db: Session,
    client: MessagingClient,
    user: User,
    sess,
    ext_id: str,
    consultation: Consultation,
) -> None:
    triage = _load_triage(consultation)
    idx, key, prompt = _next_triage_step(triage)
    set_state(
        db,
        sess,
        f"triage_{key}",
        {"consultation_id": consultation.id, "triage": triage},
    )
    await send_and_log(
        db,
        client,
        to_id=ext_id,
        text=(
            f"📝 *Retomamos el triaje* de la consulta #{consultation.id} "
            f"(paso {idx + 1}/{len(TRIAGE_STEPS)})\n\n"
            f"{prompt}\n\n"
            "_Pulsa *Cancelar consulta* si quieres abandonarla._"
        ),
        consultation_id=consultation.id,
        keyboard=KB_TRIAGE_PHOTO if key == "photo_note" else KB_TRIAGE,
    )


async def _start_triage(db: Session, client: MessagingClient, user: User, sess, ext_id: str) -> None:
    open_c = _open_consultation(db, user)
    if open_c is not None:
        if open_c.status == "triaging":
            await _resume_triage(db, client, user, sess, ext_id, open_c)
            return
        await send_and_log(
            db,
            client,
            to_id=ext_id,
            text=(
                f"Ya tienes la consulta #{open_c.id} "
                f"(*{STATUS_LABELS.get(open_c.status, open_c.status)}*).\n"
                "Pulsa *2 Estado* para ver el detalle. "
                "No puedes abrir otra hasta cerrar o cancelar esta."
            ),
            consultation_id=open_c.id,
            keyboard=_kb_menu(open_c),
        )
        return

    consultation = Consultation(user_id=user.id, status="triaging", triage_json="{}")
    db.add(consultation)
    db.commit()
    db.refresh(consultation)

    set_state(
        db,
        sess,
        "triage_name",
        {"consultation_id": consultation.id, "triage": {}},
    )
    await send_and_log(
        db,
        client,
        to_id=ext_id,
        text=(
            "📝 *Triaje Doctor Bot* (paso 1/5)\n\n"
            f"{TRIAGE_STEPS[0][1]}\n\n"
            "_Si es una emergencia (sangrado grave, inconsciente, no respira), "
            "llama al 106 ahora._\n"
            "_Si entraste por error: *Cancelar consulta*._"
        ),
        consultation_id=consultation.id,
        keyboard=KB_TRIAGE,
    )


async def _persist_photo(
    client: MessagingClient,
    media_url: str | None,
    consultation_id: int | None,
    *,
    suffix: str = "photo",
) -> str | None:
    return await persist_consultation_media(
        client, media_url, consultation_id, suffix=suffix
    )


async def _handle_triage_step(
    db: Session,
    client: MessagingClient,
    user: User,
    sess,
    ext_id: str,
    body: str,
    media_url: str | None = None,
) -> None:
    ctx = get_context(sess)
    triage: dict = ctx.get("triage", {})
    consultation_id = ctx.get("consultation_id")
    step_key = sess.state.replace("triage_", "")

    if body.strip().lower() in {"cancelar consulta", "cancelar triaje", "abandonar consulta"}:
        await _cancel_open_consultation(db, client, user, sess, ext_id)
        return

    if is_emergency(body):
        if consultation_id:
            c = db.query(Consultation).filter(Consultation.id == consultation_id).first()
            if c:
                c.status = "emergency"
                c.closed_at = datetime.now(timezone.utc)
                db.add(c)
                db.commit()
        set_state(db, sess, "idle", {})
        await send_and_log(
            db, client, to_id=ext_id, text=EMERGENCY_MESSAGE, consultation_id=consultation_id
        )
        return

    if step_key == "age":
        age_n = _parse_age(body)
        if age_n is None:
            await send_and_log(
                db,
                client,
                to_id=ext_id,
                text="La *edad* debe ser un número entre 0 y 120 (solo el número, ej. *28*).",
                consultation_id=consultation_id,
            )
            return
        triage[step_key] = str(age_n)
    elif step_key == "photo_note":
        if body.lower() in {"omitir", "skip", "no", "ninguna"}:
            triage[step_key] = "omitida"
            triage.pop("photo_path", None)
        elif media_url or body.strip().lower() in {"[photo]", "[document]"}:
            path = await _persist_photo(client, media_url, consultation_id)
            if path:
                triage[step_key] = "foto adjunta"
                triage["photo_path"] = path
            else:
                # Foto recibida pero no se pudo bajar: al menos no guardamos "[photo]"
                triage[step_key] = "foto enviada (no se pudo guardar en servidor)"
                if body and body not in {"[photo]", "[document]"}:
                    triage[step_key] = body
        else:
            # Descripción en texto (comportamiento anterior)
            triage[step_key] = body
    elif step_key != "age":
        triage[step_key] = body

    keys = [k for k, _ in TRIAGE_STEPS]
    try:
        idx = keys.index(step_key)
    except ValueError:
        set_state(db, sess, "menu", {})
        await send_and_log(
            db, client, to_id=ext_id, text=_welcome(user), consultation_id=None, keyboard=menu_keyboard()
        )
        return

    consultation = (
        db.query(Consultation).filter(Consultation.id == consultation_id).first()
        if consultation_id
        else None
    )
    if consultation:
        _save_partial_triage(db, consultation, triage)

    if idx < len(keys) - 1:
        next_key = keys[idx + 1]
        next_prompt = TRIAGE_STEPS[idx + 1][1]
        set_state(
            db,
            sess,
            f"triage_{next_key}",
            {"consultation_id": consultation_id, "triage": triage},
        )
        await send_and_log(
            db,
            client,
            to_id=ext_id,
            text=f"📝 *Triaje* (paso {idx + 2}/5)\n\n{next_prompt}",
            consultation_id=consultation_id,
            keyboard=KB_TRIAGE_PHOTO if next_key == "photo_note" else KB_TRIAGE,
        )
        return

    if consultation is None:
        set_state(db, sess, "idle", {})
        await send_and_log(
            db,
            client,
            to_id=ext_id,
            text="Hubo un error con la consulta. Escribe *menu* para reintentar.",
            consultation_id=None,
        )
        return

    hint = infer_soft_specialty(triage)
    triage["specialty_hint"] = hint
    consultation.triage_json = json.dumps(triage, ensure_ascii=False)
    if triage.get("name"):
        user.name = str(triage["name"])[:120]
        db.add(user)
    db.add(consultation)
    db.commit()

    combined = " ".join(str(v) for v in triage.values() if not str(v).startswith("/") and "\\" not in str(v)[:3])
    if is_emergency(combined):
        consultation.status = "emergency"
        consultation.closed_at = datetime.now(timezone.utc)
        db.add(consultation)
        db.commit()
        set_state(db, sess, "idle", {})
        await send_and_log(
            db, client, to_id=ext_id, text=EMERGENCY_MESSAGE, consultation_id=consultation.id
        )
        return

    # Siempre entra a cola y se toma el más antiguo: no se salta a quien ya esperaba.
    queue_consultation(db, consultation)
    doctor = find_available_doctor(db, preferred_specialty=hint)
    assigned = (
        try_assign_queued(db, doctor, require_shift=True) if doctor is not None else None
    )
    db.refresh(consultation)
    set_state(db, sess, "idle", {})

    if assigned is not None and assigned.id != consultation.id:
        assigned_user = assigned.user or db.query(User).filter(User.id == assigned.user_id).first()
        if assigned_user:
            await notify_doctor_assignment(db, client, assigned, assigned_user, doctor)
            await notify_patient_assigned(db, client, assigned, assigned_user, doctor)

    if consultation.status == "queued":
        pos, total = queue_position(db, consultation)
        await send_and_log(
            db,
            client,
            to_id=ext_id,
            text=(
                f"✅ Triaje de la consulta #{consultation.id} completo.\n\n"
                "En este momento no hay un médico libre en horario. "
                f"Quedaste en *cola* (posición *{pos}* de *{total}*). "
                "Te avisaremos cuando uno tome tu caso.\n\n"
                "Tiempo estimado habitual: *15–30 minutos* cuando hay médico en turno."
            ),
            consultation_id=consultation.id,
            keyboard=KB_CHAT,
        )
        return

    await send_and_log(
        db,
        client,
        to_id=ext_id,
        text=(
            f"✅ Triaje completo. Consulta #{consultation.id} asignada.\n\n"
            f"Te atenderá {format_doctor_credentials(doctor)}. "
            "Tiempo estimado de respuesta: *15–30 minutos*.\n"
            "Puedes escribir más detalles aquí y se reenviarán al médico."
        ),
        consultation_id=consultation.id,
        keyboard=KB_CHAT,
    )
    await notify_doctor_assignment(db, client, consultation, user, doctor)
