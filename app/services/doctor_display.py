"""Nombre, especialidad y colegiatura tal como los ve el paciente."""

from __future__ import annotations

from app.models import Doctor

SPECIALTY_LABELS: dict[str, str] = {
    "general": "Medicina general",
    "dermatologia": "Dermatología",
    "pediatria": "Pediatría",
    "ginecologia": "Ginecología",
    "traumatologia": "Traumatología",
    "farmacia": "Orientación farmacéutica",
}

# Colegios que no deben prefijarse con CMP.
_LICENSE_PREFIXES = ("CMP", "CQFP", "QF", "COP", "CQP")


def specialty_label(key: str | None) -> str:
    raw = (key or "general").strip().lower() or "general"
    return SPECIALTY_LABELS.get(raw, raw.replace("_", " ").title())


def cmp_label(code: str | None) -> str:
    c = (code or "").strip()
    if not c:
        return "Colegiatura no registrada"
    upper = c.upper()
    if any(upper.startswith(p) for p in _LICENSE_PREFIXES):
        return c
    return f"CMP {c}"


def format_doctor_credentials(doctor: Doctor | None, *, markdown: bool = True) -> str:
    if doctor is None:
        return "un médico"
    name = (doctor.name or "Médico").strip() or "Médico"
    spec = specialty_label(doctor.specialty)
    cmp = cmp_label(doctor.license_code)
    head = f"*{name}*" if markdown else name
    return f"{head} · {spec} · {cmp}"
