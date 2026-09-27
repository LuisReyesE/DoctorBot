from app.services.consent import mask_document, parse_document
from app.services.doctor_display import cmp_label, format_doctor_credentials, specialty_label
from app.models import Doctor


def test_parse_document_dni_and_ce():
    assert parse_document("12345678") == ("dni", "12345678")
    assert parse_document("12.345.678") == ("dni", "12345678")
    assert parse_document("1234567") is None
    assert parse_document("abc") is None
    assert parse_document("AB12345678") == ("ce", "AB12345678")
    assert parse_document("123456789") == ("ce", "123456789")


def test_mask_document():
    assert mask_document("12345678") == "****5678"
    assert mask_document("") == "—"


def test_doctor_credentials_seed_style():
    d = Doctor(name="Dr. Demo", license_code="CMP-DEMO", specialty="general")
    text = format_doctor_credentials(d)
    assert "*Dr. Demo*" in text
    assert "Medicina general" in text
    assert "CMP-DEMO" in text
    assert specialty_label("dermatologia") == "Dermatología"
    assert specialty_label("farmacia") == "Orientación farmacéutica"
    assert cmp_label(None) == "Colegiatura no registrada"
    assert cmp_label("12345") == "CMP 12345"
    assert cmp_label("CQFP 12345") == "CQFP 12345"
    qf = Doctor(name="QF. Ana Pérez", license_code="CQFP 12345", specialty="farmacia")
    qf_text = format_doctor_credentials(qf)
    assert "Orientación farmacéutica" in qf_text
    assert "CQFP 12345" in qf_text
    assert "CMP" not in qf_text
