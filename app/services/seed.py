from __future__ import annotations

from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Doctor


def seed_demo_doctor(db: Session, settings: Settings) -> Doctor:
    ext_id = str(settings.seed_doctor_id).lstrip("+")
    doctor = db.query(Doctor).filter(Doctor.external_id == ext_id).first()
    if doctor is None:
        doctor = Doctor(
            external_id=ext_id,
            name=settings.seed_doctor_name,
            license_code="CMP-DEMO",
            specialty="general",
            status="offline",
        )
        db.add(doctor)
        db.commit()
        db.refresh(doctor)
    return doctor
