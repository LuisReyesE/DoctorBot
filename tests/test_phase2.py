from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.models import Consultation, User
from app.providers import get_mock_client
from app.services.reminders import process_followup_reminders
from app.services.subscription import activate_subscription, start_opening_trial


def _sim(client, phone: str, body: str):
    return client.post("/dev/simulate", json={"from_phone": phone, "body": body})


def test_panel_login_and_dashboard(client):
    r = client.get("/panel", follow_redirects=False)
    assert r.status_code in {303, 307}
    r = client.post("/panel/login", data={"token": "wrong"}, follow_redirects=False)
    assert r.status_code == 401
    r = client.post("/panel/login", data={"token": "admin"}, follow_redirects=False)
    assert r.status_code == 303
    assert client.cookies.get("sf_panel_auth") == "admin"
    r = client.get("/panel")
    assert r.status_code == 200
    assert "Dashboard" in r.text or "Consultas" in r.text
    r = client.get("/panel/doctors")
    assert r.status_code == 200
    assert "Especialidad" in r.text
    assert "Medicina general" in r.text
    assert "Orientación farmacéutica" in r.text
    r = client.get("/panel/users")
    assert r.status_code == 200
    assert "Doc. titular" in r.text
    assert "Consentimiento" in r.text


def test_opening_trial_on_first_message(client, db_session):
    patient = "51955550001"
    _sim(client, patient, "menu")
    user = db_session.query(User).filter(User.external_id == patient).first()
    assert user is not None
    assert user.subscription_status == "trial"
    assert user.subscription_plan == "trial"
    assert user.subscription_expires_at is not None


def test_trial_consulta_does_not_ask_dni_or_consent(client):
    patient = "51955550003"
    _sim(client, patient, "menu")
    r = _sim(client, patient, "1")
    texts = " ".join(m["text"] for m in r.json()["replies"]).lower()
    assert "triaje" in texts or "nombre" in texts
    assert "dni" not in texts
    assert "acepto" not in texts
    assert "consentimiento" not in texts


def test_subscription_offer_has_no_fake_trial_button(client):
    patient = "51955550002"
    _sim(client, patient, "menu")
    r = _sim(client, patient, "3")
    texts = " ".join(m["text"] for m in r.json()["replies"])
    assert "Mensual" in texts or "Suscripción" in texts
    assert "Prueba 7" not in texts
    assert "pagar" not in texts.lower() or "Yape" in texts


def test_activate_monthly_unit(db_session):
    user2 = User(external_id="51944440000", name="Unit")
    db_session.add(user2)
    db_session.commit()
    db_session.refresh(user2)
    start_opening_trial(db_session, user2)
    assert user2.subscription_status == "trial"
    p = activate_subscription(db_session, user2, "monthly", method="yape")
    assert p.amount == 8.0
    assert user2.subscription_status == "active"


def test_pdf_on_close(client, db_session):
    patient = "51966666666"
    doctor = "51999900001"
    _sim(client, doctor, "/disponible")
    _sim(client, patient, "1")
    _sim(client, patient, "Rosa")
    _sim(client, patient, "25")
    _sim(client, patient, "tos leve")
    _sim(client, patient, "1 dia")
    _sim(client, patient, "omitir")
    _sim(client, doctor, "Reposo e hidratación. Control en 48h si persiste.")
    r = _sim(client, doctor, "/cerrar")
    assert r.status_code == 200
    c = (
        db_session.query(Consultation)
        .filter(Consultation.status == "closed")
        .order_by(Consultation.id.desc())
        .first()
    )
    assert c is not None
    assert c.orientation_text
    assert c.pdf_path
    assert Path(c.pdf_path).is_file()


def test_reminders_48h(db_session):
    patient = "51977777777"
    user = User(external_id=patient, name="Seguimiento")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    closed_at = datetime.now(timezone.utc) - timedelta(hours=49)
    c = Consultation(
        user_id=user.id,
        status="closed",
        triage_json="{}",
        closed_at=closed_at,
        orientation_text="ok",
    )
    db_session.add(c)
    db_session.commit()
    db_session.refresh(c)

    mock = get_mock_client()
    mock.clear()
    sent_ids = asyncio.run(process_followup_reminders(db_session, mock))
    assert c.id in sent_ids
    db_session.refresh(c)
    assert c.reminder_sent_at is not None
    assert any(m["to"] == patient and "Seguimiento" in m["text"] for m in mock.sent)
