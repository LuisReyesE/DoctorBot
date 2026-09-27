from __future__ import annotations

from app.models import Consultation, Doctor
from app.providers import get_mock_client


def _sim(client, phone: str, body: str):
    return client.post("/dev/simulate", json={"from_phone": phone, "body": body})


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_emergency_blocks_routing(client, db_session):
    patient = "51911111111"
    _sim(client, patient, "hola")
    _sim(client, patient, "1")
    _sim(client, patient, "Ana")
    r = _sim(client, patient, "sangrado grave en la pierna")
    assert r.status_code == 200
    replies = r.json()["replies"]
    texts = " ".join(m["text"] for m in replies)
    assert "106" in texts or "emergencia" in texts.lower()
    c = db_session.query(Consultation).order_by(Consultation.id.desc()).first()
    assert c is not None
    assert c.status == "emergency"


def test_happy_path_patient_doctor(client, db_session):
    patient = "51922222222"
    doctor_phone = "51999900001"
    mock = get_mock_client()

    # Médico se pone disponible
    r = _sim(client, doctor_phone, "/disponible")
    assert r.status_code == 200

    # Paciente triaje completo
    _sim(client, patient, "menu")
    _sim(client, patient, "1")
    _sim(client, patient, "Luis")
    _sim(client, patient, "28")
    _sim(client, patient, "dolor de garganta")
    _sim(client, patient, "2 dias")
    r = _sim(client, patient, "omitir")
    assert r.status_code == 200
    assign_texts = " ".join(m["text"] for m in r.json()["replies"])
    assert "Medicina general" in assign_texts
    assert "CMP-DEMO" in assign_texts

    c = (
        db_session.query(Consultation)
        .filter(Consultation.status.in_(["assigned", "active"]))
        .order_by(Consultation.id.desc())
        .first()
    )
    assert c is not None
    assert c.doctor_id is not None

    # El médico debió recibir la ficha
    doctor_msgs = [m for m in mock.sent if m["to"] == doctor_phone]
    assert any("Nueva consulta" in m["text"] for m in doctor_msgs)

    # Médico responde
    before = len(mock.sent)
    _sim(client, doctor_phone, "Probable faringitis viral. Hidratación y reposo.")
    new = mock.sent[before:]
    patient_msgs = [m for m in new if m["to"] == patient]
    assert any("Médico" in m["text"] or "faringitis" in m["text"] for m in patient_msgs)

    # Cierre
    _sim(client, doctor_phone, "/cerrar")
    db_session.expire_all()
    c = db_session.query(Consultation).filter(Consultation.id == c.id).first()
    assert c.status == "closed"
    doc = db_session.query(Doctor).filter(Doctor.external_id == doctor_phone).first()
    assert doc.status == "available"
    assert doc.active_consultation_id is None


def test_queue_when_no_doctor(client, db_session):
    patient = "51933333333"
    # Médico offline por defecto
    _sim(client, patient, "1")
    _sim(client, patient, "Maria")
    _sim(client, patient, "40")
    _sim(client, patient, "erupcion cutanea")
    _sim(client, patient, "1 dia")
    r = _sim(client, patient, "omitir")
    assert r.status_code == 200
    texts = " ".join(m["text"] for m in r.json()["replies"])
    assert "cola" in texts.lower() or "disponibles" in texts.lower()
    c = db_session.query(Consultation).order_by(Consultation.id.desc()).first()
    assert c.status == "queued"


def test_resume_and_cancel_stuck_triage(client, db_session):
    patient = "51944441111"
    _sim(client, patient, "1")
    _sim(client, patient, "Pedro")
    # Sale al menú a mitad del triaje
    r = _sim(client, patient, "menu")
    assert r.status_code == 200
    assert any("incompleto" in m["text"].lower() or "continuar" in m["text"].lower() for m in r.json()["replies"])

    c = db_session.query(Consultation).order_by(Consultation.id.desc()).first()
    assert c is not None
    assert c.status == "triaging"

    # Opción 2: estado de CONSULTA (no solo suscripción)
    r = _sim(client, patient, "2")
    texts = " ".join(m["text"] for m in r.json()["replies"]).lower()
    assert "consulta" in texts
    assert "triaje" in texts or str(c.id) in texts

    # Retomar
    r = _sim(client, patient, "continuar")
    texts = " ".join(m["text"] for m in r.json()["replies"]).lower()
    assert "retom" in texts or "edad" in texts or "paso" in texts

    # Cancelar
    r = _sim(client, patient, "cancelar consulta")
    db_session.expire_all()
    c = db_session.query(Consultation).filter(Consultation.id == c.id).first()
    assert c.status == "closed"
    texts = " ".join(m["text"] for m in r.json()["replies"]).lower()
    assert "cancelad" in texts
