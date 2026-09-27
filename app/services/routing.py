from __future__ import annotations

import unicodedata
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import Consultation, Doctor, User
from app.services.shifts import is_on_shift, shifts_by_doctor
from app.services.subscription import start_opening_trial, subscription_badge
from app.services.timeutil import as_utc, start_of_today_lima_utc

# Especialidades blandas: pista, nunca un filtro rígido.
SOFT_SPECIALTIES = (
    "general",
    "dermatologia",
    "pediatria",
    "ginecologia",
    "traumatologia",
    "farmacia",
)

# Palabras en síntoma / nota que sugieren una especialidad.
_SPECIALTY_HINTS: dict[str, tuple[str, ...]] = {
    "dermatologia": (
        "piel",
        "erupcion",
        "sarpullido",
        "acne",
        "mancha",
        "urticaria",
        "comezon",
        "roncha",
        "dermatitis",
        "herpes",
        "micosis",
    ),
    "ginecologia": (
        "menstrua",
        "regla",
        "embarazo",
        "vaginal",
        "ovario",
        "papanicolau",
        "anticonceptiv",
    ),
    "traumatologia": (
        "fractura",
        "esguince",
        "luxacion",
        "torcedura",
        "hueso",
        "rodilla",
        "tobillo",
        "esguince",
    ),
    "farmacia": (
        "medicamento",
        "medicacion",
        "pastilla",
        "tableta",
        "dosis",
        "farmacia",
        "paracetamol",
        "ibuprofeno",
        "antibiotico",
        "jarabe",
        "vitamina",
        "efecto secundario",
        "interaccion",
    ),
}


def get_or_create_user(db: Session, external_id: str, name: str | None = None) -> User:
    ext = str(external_id).lstrip("+")
    user = db.query(User).filter(User.external_id == ext).first()
    if user is None:
        user = User(external_id=ext, name=name)
        db.add(user)
        db.commit()
        db.refresh(user)
        start_opening_trial(db, user)
    elif name and not user.name:
        user.name = name
        db.add(user)
        db.commit()
        db.refresh(user)
        start_opening_trial(db, user)
    else:
        start_opening_trial(db, user)
    return user


def _normalize_text(text: str) -> str:
    text = (text or "").lower().strip()
    text = unicodedata.normalize("NFD", text)
    return "".join(c for c in text if unicodedata.category(c) != "Mn")


def infer_soft_specialty(triage: dict | None) -> str:
    """Pista de especialidad a partir del triaje. Nunca obliga a esperar un especialista."""
    data = triage or {}
    age_raw = str(data.get("age") or "").strip()
    age_n: int | None = None
    if age_raw.isdigit():
        age_n = int(age_raw)
    if age_n is not None and age_n < 12:
        return "pediatria"

    blob = _normalize_text(
        f"{data.get('symptom', '')} {data.get('photo_note', '')} {data.get('duration', '')}"
    )
    for spec, words in _SPECIALTY_HINTS.items():
        if any(w in blob for w in words):
            return spec
    return "general"


def doctor_load_today(db: Session, doctor_id: int) -> int:
    """Consultas asignadas hoy (día civil Perú). Menor carga = menos casos del día."""
    start = start_of_today_lima_utc()
    rows = (
        db.query(Consultation.assigned_at)
        .filter(Consultation.doctor_id == doctor_id, Consultation.assigned_at.isnot(None))
        .all()
    )
    n = 0
    for (assigned_at,) in rows:
        if assigned_at is None:
            continue
        if as_utc(assigned_at) >= start:
            n += 1
    return n


def find_available_doctor(
    db: Session,
    preferred_specialty: str | None = None,
    *,
    now: datetime | None = None,
) -> Doctor | None:
    """Elige médico: horario → especialidad blanda → menor carga de hoy → rotación."""
    candidates = (
        db.query(Doctor)
        .filter(Doctor.status == "available", Doctor.active_consultation_id.is_(None))
        .all()
    )
    if not candidates:
        return None
    by_shifts = shifts_by_doctor(db, [d.id for d in candidates])
    candidates = [d for d in candidates if is_on_shift(by_shifts.get(d.id, []), now)]
    if not candidates:
        return None

    preferred = (preferred_specialty or "general").strip().lower() or "general"
    if preferred not in SOFT_SPECIALTIES:
        preferred = "general"

    def spec_of(d: Doctor) -> str:
        return ((d.specialty or "general").strip().lower() or "general")

    if preferred != "general":
        matched = [d for d in candidates if spec_of(d) == preferred]
        if matched:
            pool = matched
        else:
            generals = [d for d in candidates if spec_of(d) == "general"]
            pool = generals or candidates
    else:
        pool = candidates

    def sort_key(d: Doctor) -> tuple:
        load = doctor_load_today(db, d.id)
        last = d.last_assigned_at
        if last is None:
            last_key = datetime.min.replace(tzinfo=timezone.utc)
        else:
            last_key = as_utc(last)
        return (load, last_key, d.id)

    return sorted(pool, key=sort_key)[0]


def queue_position(db: Session, consultation: Consultation) -> tuple[int, int]:
    """Posición 1-based y total de la cola FIFO (por created_at, luego id)."""
    queued = (
        db.query(Consultation.id, Consultation.created_at)
        .filter(Consultation.status == "queued")
        .all()
    )

    def qkey(row: tuple) -> tuple:
        cid, created = row
        created_key = as_utc(created) if created is not None else datetime.min.replace(
            tzinfo=timezone.utc
        )
        return (created_key, cid)

    ordered = sorted(queued, key=qkey)
    total = len(ordered)
    pos = next((i for i, (cid, _) in enumerate(ordered, start=1) if cid == consultation.id), total)
    return pos, total


def get_doctor_by_external_id(db: Session, external_id: str) -> Doctor | None:
    ext = str(external_id).lstrip("+")
    return db.query(Doctor).filter(Doctor.external_id == ext).first()


def get_doctor_by_phone(db: Session, wa_phone: str) -> Doctor | None:
    """Alias histórico."""
    return get_doctor_by_external_id(db, wa_phone)


def assign_consultation(db: Session, consultation: Consultation, doctor: Doctor) -> Consultation:
    now = datetime.now(timezone.utc)
    consultation.doctor_id = doctor.id
    consultation.status = "assigned"
    consultation.assigned_at = now
    doctor.status = "busy"
    doctor.active_consultation_id = consultation.id
    doctor.last_assigned_at = now
    db.add(consultation)
    db.add(doctor)
    db.commit()
    db.refresh(consultation)
    return consultation


def queue_consultation(db: Session, consultation: Consultation) -> Consultation:
    consultation.status = "queued"
    consultation.doctor_id = None
    db.add(consultation)
    db.commit()
    db.refresh(consultation)
    return consultation


def try_assign_queued(
    db: Session,
    doctor: Doctor,
    *,
    require_shift: bool = True,
    now: datetime | None = None,
) -> Consultation | None:
    """Asigna la consulta en cola más antigua a un médico que acaba de ponerse disponible."""
    if doctor.status != "available" or doctor.active_consultation_id is not None:
        return None
    if require_shift and not is_on_shift(shifts_by_doctor(db, [doctor.id]).get(doctor.id, []), now):
        return None
    queued = (
        db.query(Consultation)
        .filter(Consultation.status == "queued")
        .order_by(Consultation.created_at.asc())
        .first()
    )
    if queued is None:
        return None
    return assign_consultation(db, queued, doctor)


def format_triage_card(consultation: Consultation, user: User) -> str:
    import json

    data: dict = {}
    if consultation.triage_json:
        try:
            data = json.loads(consultation.triage_json)
        except json.JSONDecodeError:
            data = {}
    name = data.get("name") or user.name or "Sin nombre"
    age = data.get("age", "—")
    symptom = data.get("symptom", "—")
    duration = data.get("duration", "—")
    photo = data.get("photo_note") or "No adjunta"
    has_file = bool(data.get("photo_path"))
    photo_line = (
        "📎 Foto: *adjunta* (se envía a continuación)"
        if has_file
        else f"📎 Foto/nota: {photo}"
    )
    hint = data.get("specialty_hint") or infer_soft_specialty(data)
    hint_line = (
        f"🏷️ Pista (no excluyente): {hint}\n"
        if hint and hint != "general"
        else ""
    )
    sub_line = f"🪪 {subscription_badge(user)}\n"
    return (
        f"📋 *Nueva consulta #{consultation.id}*\n\n"
        f"👤 Paciente: {name}\n"
        f"{sub_line}"
        f"🎂 Edad: {age}\n"
        f"🤒 Síntoma principal: {symptom}\n"
        f"⏱ Tiempo de evolución: {duration}\n"
        f"{photo_line}\n"
        f"{hint_line}\n"
        f"Responde con texto o una *foto de receta*. */video* si hace falta cámara. Al terminar, */cerrar*.\n"
        f"Comandos: */casos* */video* */pdf* */ocupado* */disponible* */fuera*"
    )
