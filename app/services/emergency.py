from __future__ import annotations

import re
import unicodedata

EMERGENCY_PATTERNS = [
    r"\bparo\b",
    r"\binconsciente\b",
    r"\bno\s+respira\b",
    r"\bsangrado\s+grave\b",
    r"\bhemorragia\b",
    r"\bconvulsion",
    r"\bdolor\s+de\s+pecho\b",
    r"\binfarto\b",
    r"\bacv\b",
    r"\bderrame\b",
    r"\bsuicid",
    r"\basfixia\b",
    r"\bahogo\b",
    r"\bemergencia\b",
    r"\burgencia\s+vital\b",
]

EMERGENCY_MESSAGE = (
    "⚠️ *Posible emergencia detectada*\n\n"
    "Doctor Bot es un servicio de *teleorientación*, no de emergencias.\n\n"
    "Por favor:\n"
    "• Llama al *106* (SAMU)\n"
    "• O acude de inmediato a un hospital / emergencia\n\n"
    "Si la situación no es de vida o muerte, escribe *menu* para volver al inicio."
)


def _normalize(text: str) -> str:
    text = text.lower().strip()
    text = unicodedata.normalize("NFD", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return text


# Frases que niegan emergencia (evita "no es emergencia" → falso positivo).
_NEGATION_PATTERNS = (
    r"\bno\s+es(\s+una)?\s+emergencia\b",
    r"\bsin\s+emergencia\b",
    r"\bno\s+hay\s+emergencia\b",
    r"\bno\s+es(\s+una)?\s+urgencia\b",
)


def is_emergency(text: str) -> bool:
    normalized = _normalize(text)
    if any(re.search(pat, normalized) for pat in _NEGATION_PATTERNS):
        return False
    return any(re.search(pat, normalized) for pat in EMERGENCY_PATTERNS)
