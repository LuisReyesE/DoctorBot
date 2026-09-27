from __future__ import annotations

from pathlib import Path

from app.models import Payment, User, Verifier
from app.providers import get_mock_client


def _sim(client, phone: str, body: str, media_url: str | None = None):
    payload = {"from_phone": phone, "body": body}
    if media_url:
        payload["media_url"] = media_url
    return client.post("/dev/simulate", json=payload)


def _dni_and_consent(client, phone: str, dni: str = "12345678"):
    _sim(client, phone, dni)
    return _sim(client, phone, "Acepto")


def _login(client):
    r = client.post("/panel/login", data={"token": "admin"}, follow_redirects=False)
    assert r.status_code == 303


_MIN_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)


def test_yape_ficha_pending_then_caja_confirms(client, db_session, tmp_path):
    caja_id = "51900001111"
    db_session.add(Verifier(external_id=caja_id, name="Caja 1"))
    db_session.commit()

    patient = "51955556666"
    _sim(client, patient, "menu")
    _sim(client, patient, "3")
    r = _sim(client, patient, "1")
    texts = " ".join(m["text"] for m in r.json()["replies"])
    assert "DNI" in texts or "dni" in texts.lower()

    r = _dni_and_consent(client, patient)
    texts = " ".join(m["text"] for m in r.json()["replies"])
    assert "Yape" in texts or "nombre" in texts.lower()

    _sim(client, patient, "Maria Perez")
    r = _sim(client, patient, "999888777")
    texts = " ".join(m["text"] for m in r.json()["replies"])
    assert "SALUD-" in texts
    pay = db_session.query(Payment).order_by(Payment.id.desc()).first()
    assert pay is not None
    assert pay.status == "pending"
    assert pay.plan == "monthly"
    assert pay.yape_name == "Maria Perez"
    assert pay.voucher_path is None
    code = pay.reference

    photo = tmp_path / "voucher.png"
    photo.write_bytes(_MIN_PNG)
    r = _sim(client, patient, "[photo]", media_url=str(photo))
    assert r.status_code == 200
    db_session.refresh(pay)
    assert pay.voucher_path
    assert Path(pay.voucher_path).is_file()

    mock = get_mock_client()
    caja_msgs = [m for m in mock.sent if m["to"] == caja_id]
    assert any("Suscripción" in (m.get("text") or "") for m in caja_msgs)

    r = _sim(client, caja_id, f"/confirmar {code}")
    assert r.status_code == 200
    db_session.expire_all()
    pay = db_session.query(Payment).filter(Payment.id == pay.id).first()
    assert pay.status == "paid"
    user = db_session.query(User).filter(User.external_id == patient).first()
    assert user.subscription_status == "active"
    assert user.subscription_plan == "monthly"
    assert user.document_type == "dni"
    assert user.document_number == "12345678"
    assert user.consent_version == "v1"
    assert user.consent_accepted_at is not None
    assert pay.consent_version == "v1"

    patient_msgs = [m["text"] for m in mock.sent if m["to"] == patient]
    assert any("suscrito" in (t or "").lower() for t in patient_msgs)


def test_second_caja_cannot_double_confirm(client, db_session, tmp_path):
    db_session.add(Verifier(external_id="51900001112", name="Caja 1"))
    db_session.add(Verifier(external_id="51900001113", name="Caja 2"))
    db_session.commit()

    patient = "51955556667"
    _sim(client, patient, "3")
    _sim(client, patient, "2")
    _dni_and_consent(client, patient, "87654321")
    _sim(client, patient, "Luis Gomez")
    _sim(client, patient, "988777666")
    pay = db_session.query(Payment).order_by(Payment.id.desc()).first()
    photo = tmp_path / "v.png"
    photo.write_bytes(_MIN_PNG)
    _sim(client, patient, "[photo]", media_url=str(photo))

    _sim(client, "51900001112", f"/confirmar {pay.reference}")
    r = _sim(client, "51900001113", f"/confirmar {pay.reference}")
    texts = " ".join(m["text"] for m in r.json()["replies"])
    assert "ya estaba" in texts.lower() or "confirmado" in texts.lower()
    db_session.refresh(pay)
    assert pay.status == "paid"
    assert pay.verifier_id is not None


def test_panel_caja_and_confirm(client, db_session):
    _login(client)
    r = client.get("/panel/cajas")
    assert r.status_code == 200
    assert "Agregar caja" in r.text

    r = client.post(
        "/panel/cajas",
        data={"name": "Caja 1", "external_id": "51900002222"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    v = db_session.query(Verifier).filter(Verifier.external_id == "51900002222").first()
    assert v is not None

    patient = "51955556668"
    _sim(client, patient, "3")
    _sim(client, patient, "1")
    _dni_and_consent(client, patient, "11223344")
    _sim(client, patient, "Ana Ruiz")
    _sim(client, patient, "911222333")
    pay = db_session.query(Payment).order_by(Payment.id.desc()).first()
    assert pay.status == "pending"

    r = client.get("/panel/payments?status=pending")
    assert r.status_code == 200
    assert pay.reference in r.text or "SALUD-" in r.text

    r = client.post(f"/panel/payments/{pay.id}/confirm", follow_redirects=False)
    assert r.status_code == 303
    db_session.expire_all()
    pay = db_session.query(Payment).filter(Payment.id == pay.id).first()
    assert pay.status == "paid"


def test_caja_search_by_code(client, db_session):
    db_session.add(Verifier(external_id="51900001114", name="Caja 1"))
    db_session.commit()
    patient = "51955556669"
    _sim(client, patient, "3")
    _sim(client, patient, "1")
    _dni_and_consent(client, patient, "44332211")
    _sim(client, patient, "Rosa Diaz")
    _sim(client, patient, "900111222")
    pay = db_session.query(Payment).order_by(Payment.id.desc()).first()
    r = _sim(client, "51900001114", f"/buscar {pay.reference}")
    texts = " ".join(m["text"] for m in r.json()["replies"])
    assert pay.reference in texts or "SALUD-" in texts
    assert "Rosa" in texts


def test_invalid_dni_does_not_advance(client, db_session):
    patient = "51955557770"
    _sim(client, patient, "3")
    _sim(client, patient, "1")
    r = _sim(client, patient, "abc")
    texts = " ".join(m["text"] for m in r.json()["replies"]).lower()
    assert "no reconocí" in texts or "dni" in texts
    user = db_session.query(User).filter(User.external_id == patient).first()
    assert user is not None
    assert user.document_number is None
    assert db_session.query(Payment).filter(Payment.user_id == user.id).count() == 0


def test_consent_reject_does_not_create_payment(client, db_session):
    patient = "51955557771"
    _sim(client, patient, "3")
    _sim(client, patient, "1")
    _sim(client, patient, "12345678")
    r = _sim(client, patient, "No acepto")
    texts = " ".join(m["text"] for m in r.json()["replies"]).lower()
    assert "no se creó" in texts or "prueba" in texts
    user = db_session.query(User).filter(User.external_id == patient).first()
    assert user.consent_version is None
    assert user.consent_accepted_at is None
    assert db_session.query(Payment).filter(Payment.user_id == user.id).count() == 0


def test_renewal_skips_consent_if_already_accepted(client, db_session):
    from datetime import datetime, timezone

    patient = "51955557772"
    _sim(client, patient, "menu")
    user = db_session.query(User).filter(User.external_id == patient).first()
    user.document_type = "dni"
    user.document_number = "12345678"
    user.consent_version = "v1"
    user.consent_accepted_at = datetime.now(timezone.utc)
    db_session.add(user)
    db_session.commit()

    _sim(client, patient, "3")
    r = _sim(client, patient, "1")
    texts = " ".join(m["text"] for m in r.json()["replies"]).lower()
    assert "dni" in texts
    r = _sim(client, patient, "ok")
    texts = " ".join(m["text"] for m in r.json()["replies"]).lower()
    assert "acepto" not in texts
    assert "consentimiento" not in texts
    assert "yape" in texts or "nombre" in texts
