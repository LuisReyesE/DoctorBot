from __future__ import annotations

from datetime import datetime, timezone

from app.models import Consultation, Doctor, User
from app.providers import get_mock_client
from app.services.emergency import is_emergency
from app.services.routing import find_available_doctor, infer_soft_specialty


def _sim(client, phone: str, body: str):
    return client.post("/dev/simulate", json={"from_phone": phone, "body": body})


def _triage(client, patient: str, *, name: str, age: str, symptom: str):
    _sim(client, patient, "1")
    _sim(client, patient, name)
    _sim(client, patient, age)
    _sim(client, patient, symptom)
    _sim(client, patient, "1 dia")
    return _sim(client, patient, "omitir")


def test_infer_soft_specialty_and_emergency_negation():
    assert infer_soft_specialty({"age": "8", "symptom": "tos"}) == "pediatria"
    assert infer_soft_specialty({"age": "30", "symptom": "erupcion en el brazo"}) == "dermatologia"
    assert infer_soft_specialty({"age": "30", "symptom": "dolor de garganta"}) == "general"
    assert infer_soft_specialty({"age": "30", "symptom": "dosis de paracetamol"}) == "farmacia"
    assert is_emergency("sangrado grave")
    assert not is_emergency("no es emergencia, solo un raspon")


def test_age_must_be_numeric(client, db_session):
    patient = "51988880001"
    _sim(client, patient, "1")
    _sim(client, patient, "Lucia")
    r = _sim(client, patient, "veintiocho")
    texts = " ".join(m["text"] for m in r.json()["replies"]).lower()
    assert "edad" in texts
    c = db_session.query(Consultation).order_by(Consultation.id.desc()).first()
    assert c is not None
    assert c.status == "triaging"


def test_close_assigns_next_queued(client, db_session):
    doctor = "51999900001"
    _sim(client, doctor, "/disponible")
    _triage(client, "51988880011", name="Ana", age="30", symptom="tos leve")
    first = (
        db_session.query(Consultation)
        .filter(Consultation.status.in_(["assigned", "active"]))
        .order_by(Consultation.id.desc())
        .first()
    )
    assert first is not None

    r = _triage(client, "51988880012", name="Beto", age="40", symptom="dolor de cabeza")
    texts = " ".join(m["text"] for m in r.json()["replies"]).lower()
    assert "cola" in texts
    queued = (
        db_session.query(Consultation)
        .filter(Consultation.status == "queued")
        .order_by(Consultation.id.desc())
        .first()
    )
    assert queued is not None
    assert "posici" in texts

    _sim(client, doctor, "Reposar e hidratarse.")
    _sim(client, doctor, "/cerrar")
    db_session.expire_all()
    queued = db_session.query(Consultation).filter(Consultation.id == queued.id).first()
    assert queued.status == "assigned"
    doc = db_session.query(Doctor).filter(Doctor.external_id == doctor).first()
    assert doc.active_consultation_id == queued.id
    assert doc.status == "busy"


def test_least_load_then_turn(client, db_session):
    seed = db_session.query(Doctor).filter(Doctor.external_id == "51999900001").first()
    assert seed is not None
    seed.status = "available"
    seed.active_consultation_id = None
    other = Doctor(
        external_id="51999900002",
        name="Dra. Menor Carga",
        license_code="CMP-2",
        specialty="general",
        status="available",
    )
    db_session.add(other)
    db_session.commit()
    db_session.refresh(other)

    now = datetime.now(timezone.utc)
    prior_user = User(external_id="51988880999", name="Carga previa")
    db_session.add(prior_user)
    db_session.commit()
    db_session.refresh(prior_user)
    prior = Consultation(
        user_id=prior_user.id,
        doctor_id=seed.id,
        status="closed",
        triage_json="{}",
        assigned_at=now,
        closed_at=now,
    )
    db_session.add(prior)
    db_session.commit()

    chosen = find_available_doctor(db_session, preferred_specialty="general")
    assert chosen is not None
    assert chosen.id == other.id


def test_soft_specialty_prefers_match_else_general(client, db_session):
    seed = db_session.query(Doctor).filter(Doctor.external_id == "51999900001").first()
    seed.status = "available"
    seed.active_consultation_id = None
    seed.specialty = "general"
    derm = Doctor(
        external_id="51999900003",
        name="Dra. Piel",
        license_code="CMP-3",
        specialty="dermatologia",
        status="available",
    )
    db_session.add(derm)
    db_session.commit()
    db_session.refresh(derm)

    chosen = find_available_doctor(db_session, preferred_specialty="dermatologia")
    assert chosen is not None
    assert chosen.id == derm.id

    derm.status = "offline"
    db_session.add(derm)
    db_session.commit()
    fallback = find_available_doctor(db_session, preferred_specialty="dermatologia")
    assert fallback is not None
    assert fallback.id == seed.id


def test_farmacia_preferred_over_general(db_session):
    seed = db_session.query(Doctor).filter(Doctor.external_id == "51999900001").first()
    seed.status = "available"
    seed.active_consultation_id = None
    seed.specialty = "general"
    qf = Doctor(
        external_id="51999900004",
        name="QF. Ana Pérez",
        license_code="CQFP 12345",
        specialty="farmacia",
        status="available",
    )
    db_session.add(qf)
    db_session.commit()
    db_session.refresh(qf)

    chosen = find_available_doctor(db_session, preferred_specialty="farmacia")
    assert chosen is not None
    assert chosen.id == qf.id

    qf.status = "offline"
    db_session.add(qf)
    db_session.commit()
    fallback = find_available_doctor(db_session, preferred_specialty="farmacia")
    assert fallback is not None
    assert fallback.id == seed.id


def test_new_triage_does_not_skip_existing_queue(client, db_session):
    """Si ya hay alguien en cola, el siguiente triaje no se salta esa espera."""
    seed = db_session.query(Doctor).filter(Doctor.external_id == "51999900001").first()
    seed.status = "offline"
    seed.active_consultation_id = None
    db_session.add(seed)
    db_session.commit()

    _triage(client, "51988880031", name="Ana", age="30", symptom="tos leve")
    first = (
        db_session.query(Consultation)
        .filter(Consultation.status == "queued")
        .order_by(Consultation.id.asc())
        .first()
    )
    assert first is not None

    seed.status = "available"
    db_session.add(seed)
    db_session.commit()

    _triage(client, "51988880032", name="Beto", age="40", symptom="dolor de cabeza")
    db_session.expire_all()
    first = db_session.query(Consultation).filter(Consultation.id == first.id).first()
    second = (
        db_session.query(Consultation)
        .filter(Consultation.user.has(User.external_id == "51988880032"))
        .order_by(Consultation.id.desc())
        .first()
    )
    assert first.status == "assigned"
    assert first.doctor_id == seed.id
    assert second is not None
    assert second.status == "queued"


def test_queue_position_in_status(client, db_session):
    _triage(client, "51988880021", name="Carla", age="22", symptom="tos")
    r = _sim(client, "51988880021", "2")
    texts = " ".join(m["text"] for m in r.json()["replies"]).lower()
    assert "cola" in texts
    assert "posici" in texts
