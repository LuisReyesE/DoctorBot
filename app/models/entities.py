from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # ID en el canal activo (teléfono WA o chat_id Telegram)
    external_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # Nombre y celular como figuran en Yape (para conciliar el voucher)
    yape_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    yape_phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Titular de la cuenta (solo al suscribirse; no se pide en el triaje)
    document_type: Mapped[str | None] = mapped_column(String(8), nullable=True)  # dni | ce
    document_number: Mapped[str | None] = mapped_column(String(20), nullable=True)
    consent_version: Mapped[str | None] = mapped_column(String(16), nullable=True)
    consent_accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # none | active | expired | trial
    subscription_status: Mapped[str] = mapped_column(String(20), default="none", index=True)
    subscription_plan: Mapped[str | None] = mapped_column(String(20), nullable=True)  # monthly | quarterly | trial
    subscription_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    consultations: Mapped[list[Consultation]] = relationship(back_populates="user")
    payments: Mapped[list[Payment]] = relationship(back_populates="user")


class Doctor(Base):
    __tablename__ = "doctors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    external_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    license_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # Pista de especialidad (blanda). Nunca bloquea asignación.
    # general | dermatologia | pediatria | ginecologia | traumatologia | ...
    specialty: Mapped[str] = mapped_column(String(40), default="general", index=True)
    # available | busy | offline
    status: Mapped[str] = mapped_column(String(20), default="offline", index=True)
    active_consultation_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_assigned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    consultations: Mapped[list[Consultation]] = relationship(back_populates="doctor")
    shifts: Mapped[list[DoctorShift]] = relationship(back_populates="doctor")


class Consultation(Base):
    __tablename__ = "consultations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    doctor_id: Mapped[int | None] = mapped_column(ForeignKey("doctors.id"), nullable=True, index=True)
    # triaging | queued | assigned | active | closed | emergency
    status: Mapped[str] = mapped_column(String(20), default="triaging", index=True)
    triage_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    orientation_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    pdf_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    reminder_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    assigned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship(back_populates="consultations")
    doctor: Mapped[Doctor | None] = relationship(back_populates="consultations")
    messages: Mapped[list[Message]] = relationship(back_populates="consultation")


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    consultation_id: Mapped[int | None] = mapped_column(
        ForeignKey("consultations.id"), nullable=True, index=True
    )
    direction: Mapped[str] = mapped_column(String(10))  # in | out
    from_role: Mapped[str] = mapped_column(String(20))  # patient | doctor | system
    body: Mapped[str] = mapped_column(Text)
    media_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    consultation: Mapped[Consultation | None] = relationship(back_populates="messages")


class SessionState(Base):
    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    external_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    role: Mapped[str] = mapped_column(String(20), default="patient")  # patient | doctor | caja
    state: Mapped[str] = mapped_column(String(40), default="idle")
    context_json: Mapped[str] = mapped_column(Text, default="{}")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Verifier(Base):
    """Caja: confirma Yapes. El dinero va al Yape del proyecto, no a este número."""

    __tablename__ = "verifiers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    external_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    payments: Mapped[list["Payment"]] = relationship(back_populates="verifier")


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    verifier_id: Mapped[int | None] = mapped_column(ForeignKey("verifiers.id"), nullable=True, index=True)
    # pending | paid | failed | simulated
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    plan: Mapped[str] = mapped_column(String(20))  # monthly | quarterly
    amount: Mapped[float] = mapped_column(Float, default=0.0)
    currency: Mapped[str] = mapped_column(String(8), default="PEN")
    method: Mapped[str] = mapped_column(String(20), default="yape")  # yape | plin | mock
    reference: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    yape_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    yape_phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    voucher_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    notes: Mapped[str | None] = mapped_column(String(500), nullable=True)
    consent_version: Mapped[str | None] = mapped_column(String(16), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship(back_populates="payments")
    verifier: Mapped[Verifier | None] = relationship(back_populates="payments")


class DoctorShift(Base):
    """Franja horaria de un médico (día + hora Perú). Sin filas = sin restricción."""

    __tablename__ = "doctor_shifts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    doctor_id: Mapped[int] = mapped_column(ForeignKey("doctors.id"), index=True)
    # 0=lunes … 6=domingo (datetime.weekday en America/Lima)
    weekday: Mapped[int] = mapped_column(Integer, index=True)
    start_hm: Mapped[str] = mapped_column(String(5))  # HH:MM
    end_hm: Mapped[str] = mapped_column(String(5))

    doctor: Mapped[Doctor] = relationship(back_populates="shifts")
