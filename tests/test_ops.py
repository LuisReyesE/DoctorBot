from __future__ import annotations

from datetime import datetime
from pathlib import Path

from app.models import Consultation, Doctor, DoctorShift, Message, User
from app.services.pdf_recipe import generate_orientation_pdf
from app.providers import get_mock_client
from app.services.routing import find_available_doctor
from app.services.shifts import is_on_shift, parse_hhmm
from app.services.video import jitsi_room_url


def _sim(client, phone: str, body: str):
    return client.post("/dev/simulate", json={"from_phone": phone, "body": body})


def _login(client):
    r = client.post("/panel/login", data={"token": "admin"}, follow_redirects=False)
    assert r.status_code == 303


def test_parse_hhmm_and_shift_window():
    assert parse_hhmm("8:00") == "08:00"
    assert parse_hhmm("08:00:00") == "08:00"
    assert parse_hhmm("25:00") is None
    shift = DoctorShift(doctor_id=1, weekday=0, start_hm="08:00", end_hm="14:00")
    monday_10 = datetime(2026, 8, 17, 15, 0, 0)  # 10:00 Perú (UTC-5)
    monday_10 = monday_10.replace(tzinfo=__import__("datetime").timezone.utc)
    # 2026-08-17 is Monday; 15:00 UTC = 10:00 Lima
    assert is_on_shift([shift], monday_10)
    monday_20utc = datetime(2026, 8, 17, 20, 0, 0, tzinfo=__import__("datetime").timezone.utc)
    assert not is_on_shift([shift], monday_20utc)
    assert is_on_shift([], monday_20utc)


def test_menu_keyboard_and_button_label(client):
    r = _sim(client, "51988880101", "menu")
    replies = r.json()["replies"]
    assert replies
    assert replies[0].get("keyboard")
    r = _sim(client, "51988880101", "1 Nueva consulta")
    texts = " ".join(m["text"] for m in r.json()["replies"]).lower()
    assert "triaje" in texts or "nombre" in texts


def test_pdf_sent_to_patient(client, db_session):
    doctor = "51999900001"
    patient = "51988880102"
    _sim(client, doctor, "/disponible")
    _sim(client, patient, "1")
    _sim(client, patient, "Rosa")
    _sim(client, patient, "25")
    _sim(client, patient, "tos leve")
    _sim(client, patient, "1 dia")
    _sim(client, patient, "omitir")
    _sim(client, doctor, "Reposo.")
    mock = get_mock_client()
    _sim(client, doctor, "/cerrar")
    docs = [m for m in mock.sent if m.get("type") == "document" and m["to"] == patient]
    assert docs
    close_notes = [m["text"] for m in mock.sent if m["to"] == doctor and "cerrada" in (m.get("text") or "")]
    assert close_notes
    assert "storage" not in close_notes[-1].lower()
    assert "panel" in close_notes[-1].lower()


def test_doctor_pdf_command_own_case_only(client, db_session):
    doctor = "51999900001"
    other = Doctor(
        external_id="51999900077",
        name="Dr. Otro",
        specialty="general",
        status="offline",
    )
    db_session.add(other)
    db_session.commit()
    db_session.refresh(other)

    patient = "51988880122"
    _sim(client, doctor, "/disponible")
    _sim(client, patient, "1")
    _sim(client, patient, "Nora")
    _sim(client, patient, "41")
    _sim(client, patient, "tos")
    _sim(client, patient, "2 dias")
    _sim(client, patient, "omitir")
    _sim(client, doctor, "Reposo.")
    _sim(client, doctor, "/cerrar")
    c = (
        db_session.query(Consultation)
        .filter(Consultation.status == "closed")
        .order_by(Consultation.id.desc())
        .first()
    )
    assert c is not None

    mock = get_mock_client()
    before = len(mock.sent)
    _sim(client, doctor, f"/pdf {c.id}")
    sent = mock.sent[before:]
    assert any(m.get("type") == "document" and m["to"] == doctor for m in sent)

    before = len(mock.sent)
    _sim(client, other.external_id, f"/pdf {c.id}")
    texts = " ".join(m.get("text") or "" for m in mock.sent[before:]).lower()
    assert "no encuentro" in texts or "tuyas" in texts
    assert not any(m.get("type") == "document" and m["to"] == other.external_id for m in mock.sent[before:])


def test_shift_blocks_auto_assign(db_session):
    seed = db_session.query(Doctor).filter(Doctor.external_id == "51999900001").first()
    seed.status = "available"
    seed.active_consultation_id = None
    db_session.add(
        DoctorShift(doctor_id=seed.id, weekday=0, start_hm="08:00", end_hm="14:00")
    )
    db_session.commit()
    # Martes 10:00 Perú = 15:00 UTC 18 ago 2026
    tuesday = datetime(2026, 8, 18, 15, 0, 0, tzinfo=__import__("datetime").timezone.utc)
    assert find_available_doctor(db_session, now=tuesday) is None
    monday = datetime(2026, 8, 17, 15, 0, 0, tzinfo=__import__("datetime").timezone.utc)
    chosen = find_available_doctor(db_session, now=monday)
    assert chosen is not None
    assert chosen.id == seed.id


def test_panel_force_close_and_offline(client, db_session):
    _login(client)
    doctor = "51999900001"
    patient = "51988880103"
    _sim(client, doctor, "/disponible")
    _sim(client, patient, "1")
    _sim(client, patient, "Eva")
    _sim(client, patient, "33")
    _sim(client, patient, "tos")
    _sim(client, patient, "2 dias")
    _sim(client, patient, "omitir")
    c = (
        db_session.query(Consultation)
        .filter(Consultation.status.in_(["assigned", "active"]))
        .order_by(Consultation.id.desc())
        .first()
    )
    assert c is not None
    r = client.post(f"/panel/consultations/{c.id}/force-close", follow_redirects=False)
    assert r.status_code == 303
    db_session.expire_all()
    c = db_session.query(Consultation).filter(Consultation.id == c.id).first()
    assert c.status == "closed"

    seed = db_session.query(Doctor).filter(Doctor.external_id == doctor).first()
    r = client.post(f"/panel/doctors/{seed.id}/offline", follow_redirects=False)
    assert r.status_code == 303
    db_session.expire_all()
    seed = db_session.query(Doctor).filter(Doctor.id == seed.id).first()
    assert seed.status == "offline"


def test_panel_doctors_page_assigns_queued_when_on_shift(client, db_session):
    _login(client)
    seed = db_session.query(Doctor).filter(Doctor.external_id == "51999900001").first()
    seed.status = "available"
    seed.active_consultation_id = None
    db_session.add(seed)
    user = User(external_id="51988880910", name="En cola")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    c = Consultation(user_id=user.id, status="queued", triage_json='{"name":"En cola"}')
    db_session.add(c)
    db_session.commit()
    db_session.refresh(c)

    r = client.get("/panel/doctors")
    assert r.status_code == 200
    db_session.expire_all()
    c = db_session.query(Consultation).filter(Consultation.id == c.id).first()
    seed = db_session.query(Doctor).filter(Doctor.id == seed.id).first()
    assert c.status == "assigned"
    assert c.doctor_id == seed.id
    assert seed.status == "busy"


def test_panel_add_shift_and_edit_doctor(client, db_session):
    _login(client)
    seed = db_session.query(Doctor).filter(Doctor.external_id == "51999900001").first()
    r = client.get(f"/panel/doctors/{seed.id}")
    assert r.status_code == 200
    assert "Turnos horarios" in r.text
    r = client.post(
        f"/panel/doctors/{seed.id}/shifts",
        data={"weekday": "0", "start_hm": "08:00", "end_hm": "14:00"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    db_session.expire_all()
    n = db_session.query(DoctorShift).filter(DoctorShift.doctor_id == seed.id).count()
    assert n == 1
    r = client.post(
        f"/panel/doctors/{seed.id}/edit",
        data={
            "name": "Dr. Demo Editado",
            "external_id": "51999900001",
            "license_code": "CMP-X",
            "specialty": "pediatria",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    db_session.expire_all()
    seed = db_session.query(Doctor).filter(Doctor.id == seed.id).first()
    assert seed.name == "Dr. Demo Editado"
    assert seed.specialty == "pediatria"


def test_jitsi_url_stable_per_consultation(db_session):
    user = User(external_id="51988880901", name="Vid")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    c = Consultation(user_id=user.id, status="assigned", triage_json="{}")
    db_session.add(c)
    db_session.commit()
    db_session.refresh(c)
    a = jitsi_room_url(c)
    b = jitsi_room_url(c)
    assert a == b
    assert "meet.jit.si" in a
    assert f"DoctorBot-{c.id}-" in a


def test_video_only_doctor_can_start(client, db_session):
    doctor = "51999900001"
    patient = "51988880911"
    _sim(client, doctor, "/disponible")
    _sim(client, patient, "1")
    _sim(client, patient, "Violeta")
    _sim(client, patient, "29")
    _sim(client, patient, "tos")
    _sim(client, patient, "1 dia")
    _sim(client, patient, "omitir")

    mock = get_mock_client()
    before = len(mock.sent)
    _sim(client, patient, "Videollamada")
    after_patient = mock.sent[before:]
    assert not any("meet.jit.si" in (m.get("text") or "") and m["to"] == patient for m in after_patient)

    before = len(mock.sent)
    _sim(client, doctor, "/video")
    after = mock.sent[before:]
    assert any(m["to"] == patient and "meet.jit.si" in (m.get("text") or "") for m in after)
    assert any(m["to"] == doctor and "meet.jit.si" in (m.get("text") or "") for m in after)


_MIN_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)


def test_doctor_photo_goes_to_patient_and_pdf(client, db_session, tmp_path):
    doctor = "51999900001"
    patient = "51988880921"
    photo = tmp_path / "rx.png"
    photo.write_bytes(_MIN_PNG)

    _sim(client, doctor, "/disponible")
    _sim(client, patient, "1")
    _sim(client, patient, "Hugo")
    _sim(client, patient, "50")
    _sim(client, patient, "dolor de cabeza")
    _sim(client, patient, "2 dias")
    _sim(client, patient, "omitir")
    _sim(client, doctor, "Indicaciones en la foto.")

    mock = get_mock_client()
    before = len(mock.sent)
    client.post(
        "/dev/simulate",
        json={"from_phone": doctor, "body": "[photo]", "media_url": str(photo)},
    )
    sent = mock.sent[before:]
    assert any(m.get("type") == "photo" and m["to"] == patient for m in sent)

    _sim(client, doctor, "/cerrar")
    c = (
        db_session.query(Consultation)
        .filter(Consultation.status == "closed")
        .order_by(Consultation.id.desc())
        .first()
    )
    assert c is not None
    assert c.pdf_path
    assert Path(c.pdf_path).is_file()
    rx = (
        db_session.query(Message)
        .filter(Message.consultation_id == c.id, Message.from_role == "doctor", Message.media_url.isnot(None))
        .first()
    )
    assert rx is not None
    assert Path(rx.media_url).is_file()
    # El PDF se regenera con las mismas fotos
    out = generate_orientation_pdf(c, c.user, c.doctor, [rx])
    assert out.is_file()
    assert out.stat().st_size > 0
