"""Consentimiento y documento del titular: solo en la suscripción, no en el triaje."""

from __future__ import annotations

import re
from datetime import datetime, timezone

from app.models import User

CONSENT_VERSION = "v1"

CONSENT_TEXT = (
    "📋 *Consentimiento (versión v1)*\n\n"
    "Doctor Bot es *teleorientación* por mensajería: un médico colegiado te orienta. "
    "No reemplaza una consulta presencial ni un examen físico.\n\n"
    "• *No es emergencia.* Si hay riesgo vital, llama al *106* (SAMU) o al *113* (Infosalud).\n"
    "• Si con lo que cuentas no se puede orientar con seguridad, te derivamos a "
    "presencial, video, 113 o 106.\n"
    "• El PDF *no es receta oficial* del MINSA.\n"
    "• El canal es el *bot del proyecto*, no una cuenta personal del médico.\n"
    "• Tus datos de salud se usan para orientarte y quedan en la ficha de la consulta "
    "(Ley 29733). Puedes pedir acceso, corrección o baja al equipo del panel.\n"
    "• Puedes interrumpir cuando quieras.\n\n"
    "Al pulsar *Acepto* dejas constancia de fecha, hora y esta versión."
)

DOCUMENT_HELP = (
    "No reconocí el documento. *DNI* = 8 dígitos (ej. 12345678) "
    "o *CE* de 8 a 12 letras y números."
)

_DOC_STRIP = re.compile(r"[\s\-.]")
_DNI = re.compile(r"^\d{8}$")
_CE = re.compile(r"^[A-Z0-9]{8,12}$")

_ACCEPT = frozenset({"acepto", "si", "sí", "yes", "aceptar"})
_REJECT = frozenset({"no acepto", "noacepto", "no", "rechazo", "rechazar"})


def parse_document(raw: str) -> tuple[str, str] | None:
    """Devuelve (tipo, número) o None. DNI = 8 dígitos; CE = 8–12 alfanumérico."""
    token = _DOC_STRIP.sub("", (raw or "")).upper()
    if not token:
        return None
    if _DNI.match(token):
        return "dni", token
    if _CE.match(token) and not token.isdigit():
        return "ce", token
    if token.isdigit() and 9 <= len(token) <= 12:
        return "ce", token
    return None


def mask_document(number: str | None) -> str:
    n = (number or "").strip()
    if len(n) <= 4:
        return "*" * len(n) if n else "—"
    return "*" * (len(n) - 4) + n[-4:]


def format_holder_document(user: User | None) -> str:
    if user is None or not (user.document_number or "").strip():
        return "—"
    kind = "DNI" if (user.document_type or "dni").lower() == "dni" else "CE"
    return f"{kind} {user.document_number.strip()}"


def user_has_document(user: User) -> bool:
    return bool((user.document_number or "").strip() and parse_document(user.document_number or ""))


def user_has_current_consent(user: User) -> bool:
    return (user.consent_version or "") == CONSENT_VERSION and user.consent_accepted_at is not None


def is_consent_accept(text: str) -> bool:
    return (text or "").strip().lower() in _ACCEPT


def is_consent_reject(text: str) -> bool:
    t = (text or "").strip().lower()
    return t in _REJECT


def record_consent(user: User, *, at: datetime | None = None) -> None:
    user.consent_version = CONSENT_VERSION
    user.consent_accepted_at = at or datetime.now(timezone.utc)
