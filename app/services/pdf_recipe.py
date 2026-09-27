from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from reportlab.lib.colors import HexColor, white
from reportlab.lib.enums import TA_JUSTIFY, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.lib.utils import ImageReader
from reportlab.platypus import (
    Image,
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from app.config import Settings, get_settings
from app.models import Consultation, Doctor, Message, User
from app.services.consent import format_holder_document
from app.services.doctor_display import cmp_label, specialty_label
from app.services.timeutil import format_lima

NAVY = HexColor("#0f172a")
PRIMARY = HexColor("#0052cc")
ACCENT = HexColor("#00b8d9")
SURFACE = HexColor("#f1f5f9")
CARD = HexColor("#e8f4fc")
MUTED = HexColor("#64748b")
TEXT = HexColor("#0f172a")
LINE = HexColor("#cbd5e1")
WARN_BG = HexColor("#fff7ed")
WARN_FG = HexColor("#9a3412")


def _safe(text: str) -> str:
    return (
        (text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _format_local(dt: datetime) -> str:
    return format_lima(dt)


def _styles() -> dict[str, ParagraphStyle]:
    return {
        "label": ParagraphStyle(
            "PdfLabel",
            fontName="Helvetica",
            fontSize=7.5,
            textColor=MUTED,
            leading=10,
            spaceAfter=1,
        ),
        "value": ParagraphStyle(
            "PdfValue",
            fontName="Helvetica-Bold",
            fontSize=10.5,
            textColor=TEXT,
            leading=14,
        ),
        "value_normal": ParagraphStyle(
            "PdfValueN",
            fontName="Helvetica",
            fontSize=10.5,
            textColor=TEXT,
            leading=14,
        ),
        "h": ParagraphStyle(
            "PdfH",
            fontName="Helvetica-Bold",
            fontSize=11,
            textColor=PRIMARY,
            leading=14,
            spaceAfter=6,
        ),
        "body": ParagraphStyle(
            "PdfBody",
            fontName="Helvetica",
            fontSize=10.5,
            textColor=TEXT,
            leading=15,
            alignment=TA_JUSTIFY,
        ),
        "bullet": ParagraphStyle(
            "PdfBullet",
            fontName="Helvetica",
            fontSize=10.5,
            textColor=TEXT,
            leading=15,
            leftIndent=8,
            spaceAfter=4,
        ),
        "chip": ParagraphStyle(
            "PdfChip",
            fontName="Helvetica-Bold",
            fontSize=7.5,
            textColor=WARN_FG,
            leading=10,
            alignment=TA_LEFT,
        ),
        "muted": ParagraphStyle(
            "PdfMuted",
            fontName="Helvetica",
            fontSize=8.5,
            textColor=MUTED,
            leading=12,
        ),
        "right_muted": ParagraphStyle(
            "PdfRightMuted",
            fontName="Helvetica",
            fontSize=8,
            textColor=MUTED,
            alignment=TA_RIGHT,
            leading=11,
        ),
    }


def _kv_cell(label: str, value: str, styles: dict) -> list:
    return [
        Paragraph(label.upper(), styles["label"]),
        Paragraph(_safe(value) if value else "—", styles["value"]),
    ]


def _card(inner, *, bg=SURFACE, accent: HexColor | None = ACCENT) -> Table:
    t = Table([[inner]], colWidths=[17 * cm])
    cmds = [
        ("BACKGROUND", (0, 0), (-1, -1), bg),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]
    if accent is not None:
        cmds.append(("LINEBEFORE", (0, 0), (0, -1), 3, accent))
    t.setStyle(TableStyle(cmds))
    return t


def _image_flowable(path: Path) -> Image | None:
    try:
        reader = ImageReader(str(path))
        iw, ih = reader.getSize()
        if not iw or not ih:
            return None
        max_w, max_h = 15.2 * cm, 16 * cm
        scale = min(max_w / float(iw), max_h / float(ih), 1.0)
        img = Image(str(path), width=iw * scale, height=ih * scale)
        img.hAlign = "CENTER"
        return img
    except Exception:
        return None


def _draw_chrome(canvas, doc, consultation_id: int) -> None:
    canvas.saveState()
    w, h = A4

    canvas.setFillColor(NAVY)
    canvas.rect(0, h - 2.35 * cm, w, 2.35 * cm, fill=1, stroke=0)
    canvas.setFillColor(ACCENT)
    canvas.rect(0, h - 2.47 * cm, w, 0.12 * cm, fill=1, stroke=0)

    canvas.setFillColor(white)
    canvas.setFont("Helvetica-Bold", 15)
    canvas.drawString(1.8 * cm, h - 1.15 * cm, "Doctor Bot")
    canvas.setFillColor(HexColor("#94a3b8"))
    canvas.setFont("Helvetica", 8.5)
    canvas.drawString(1.8 * cm, h - 1.7 * cm, "Orientación médica digital  ·  Teleorientación primaria")

    canvas.setFillColor(ACCENT)
    canvas.setFont("Helvetica-Bold", 9)
    canvas.drawRightString(w - 1.8 * cm, h - 1.15 * cm, f"Consulta  #{consultation_id}")
    canvas.setFillColor(HexColor("#94a3b8"))
    canvas.setFont("Helvetica", 8)
    canvas.drawRightString(w - 1.8 * cm, h - 1.7 * cm, "Documento de orientación")

    canvas.setFillColor(NAVY)
    canvas.rect(0, 0, w, 1.45 * cm, fill=1, stroke=0)
    canvas.setFillColor(ACCENT)
    canvas.rect(0, 1.45 * cm, w, 0.08 * cm, fill=1, stroke=0)
    canvas.setFillColor(HexColor("#cbd5e1"))
    canvas.setFont("Helvetica", 7)
    canvas.drawString(
        1.8 * cm,
        0.82 * cm,
        "No es receta oficial del MINSA. No reemplaza consulta presencial, emergencia ni receta firmada.",
    )
    canvas.drawString(
        1.8 * cm,
        0.42 * cm,
        "Ante agravamiento: acuda a un centro de salud o llame al 106 (SAMU).",
    )
    canvas.setFont("Helvetica-Bold", 7.5)
    canvas.setFillColor(ACCENT)
    canvas.drawRightString(w - 1.8 * cm, 0.55 * cm, f"Pág. {doc.page}")
    canvas.restoreState()


def generate_orientation_pdf(
    consultation: Consultation,
    user: User,
    doctor: Doctor | None,
    doctor_messages: list[Message],
    settings: Settings | None = None,
) -> Path:
    """Genera PDF de orientación/receta digital y devuelve la ruta."""
    settings = settings or get_settings()
    pdf_dir = settings.pdf_dir
    pdf_dir.mkdir(parents=True, exist_ok=True)
    path = pdf_dir / f"consulta_{consultation.id}.pdf"

    triage: dict = {}
    if consultation.triage_json:
        try:
            triage = json.loads(consultation.triage_json)
        except json.JSONDecodeError:
            triage = {}

    styles = _styles()
    closed = consultation.closed_at or datetime.now(timezone.utc)
    patient = triage.get("name") or user.name or "—"
    age = str(triage.get("age") or "—")
    symptom = str(triage.get("symptom") or "—")
    duration = str(triage.get("duration") or "—")
    doctor_name = doctor.name if doctor else "—"
    spec = specialty_label(doctor.specialty if doctor else None)
    cmp = cmp_label(doctor.license_code if doctor else None)
    holder_doc = format_holder_document(user)

    doc = SimpleDocTemplate(
        str(path),
        pagesize=A4,
        leftMargin=1.8 * cm,
        rightMargin=1.8 * cm,
        topMargin=3.0 * cm,
        bottomMargin=1.9 * cm,
        title=f"Orientación consulta #{consultation.id}",
        author=doctor_name,
    )

    story: list = []

    chip = Table(
        [[Paragraph("NO ES RECETA OFICIAL DEL MINSA  ·  SOLO ORIENTACIÓN", styles["chip"])]],
        colWidths=[17 * cm],
    )
    chip.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), WARN_BG),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("LINEBEFORE", (0, 0), (0, -1), 3, HexColor("#f59e0b")),
            ]
        )
    )
    story.append(chip)
    story.append(Spacer(1, 0.45 * cm))

    meta_inner = Table(
        [
            [
                _kv_cell("Paciente", str(patient), styles),
                _kv_cell("Edad", f"{age} años" if age != "—" else "—", styles),
            ],
            [
                _kv_cell("Médico", doctor_name, styles),
                _kv_cell("Especialidad", spec, styles),
            ],
            [
                _kv_cell("Colegiatura", cmp, styles),
                _kv_cell("Doc. titular", holder_doc, styles),
            ],
            [
                _kv_cell("Cierre", _format_local(closed), styles),
                _kv_cell("Consulta", f"#{consultation.id}", styles),
            ],
        ],
        colWidths=[8.3 * cm, 8.3 * cm],
    )
    meta_inner.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 2),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ("LINEBELOW", (0, 0), (-1, -2), 0.4, LINE),
            ]
        )
    )
    story.append(_card(meta_inner, bg=SURFACE, accent=PRIMARY))
    story.append(Spacer(1, 0.4 * cm))

    story.append(Paragraph("Motivo de consulta", styles["h"]))
    motivo = Table(
        [
            [
                [
                    Paragraph("SÍNTOMA PRINCIPAL", styles["label"]),
                    Paragraph(_safe(symptom), styles["value_normal"]),
                ],
                [
                    Paragraph("TIEMPO DE EVOLUCIÓN", styles["label"]),
                    Paragraph(_safe(duration), styles["value_normal"]),
                ],
            ]
        ],
        colWidths=[11.2 * cm, 5.4 * cm],
    )
    motivo.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 2),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    story.append(_card(motivo, bg=CARD, accent=ACCENT))
    story.append(Spacer(1, 0.5 * cm))

    skip_bodies = {"[photo]", "[document]", "[foto receta]"}
    text_lines: list[str] = []
    photo_paths: list[str] = []
    for m in doctor_messages:
        raw = (m.body or "").strip()
        if raw and raw.lower() not in skip_bodies:
            text_lines.extend(ln for ln in raw.splitlines() if ln.strip())
        if m.media_url and Path(m.media_url).is_file():
            photo_paths.append(m.media_url)

    if not text_lines and consultation.orientation_text:
        text_lines = [
            ln
            for ln in consultation.orientation_text.splitlines()
            if ln.strip() and ln.strip().lower() not in skip_bodies
        ]

    story.append(Paragraph("Orientación e indicaciones", styles["h"]))
    if not text_lines and not photo_paths:
        inner = Paragraph("Sin indicaciones textuales registradas.", styles["muted"])
    else:
        blocks = []
        if text_lines:
            if len(text_lines) == 1:
                blocks.append(Paragraph(_safe(text_lines[0]), styles["body"]))
            else:
                for line in text_lines:
                    blocks.append(Paragraph(f"• {_safe(line.strip())}", styles["bullet"]))
        inner = blocks if len(blocks) > 1 else (blocks[0] if blocks else Paragraph("—", styles["muted"]))
    story.append(_card(inner, bg=HexColor("#f8fafc"), accent=PRIMARY))

    if photo_paths:
        story.append(Spacer(1, 0.45 * cm))
        story.append(Paragraph("Adjunto del médico", styles["h"]))
        story.append(
            Paragraph(
                "Imagen enviada en la consulta (p. ej. receta manuscrita). No constituye receta oficial.",
                styles["muted"],
            )
        )
        story.append(Spacer(1, 0.2 * cm))
        for p in photo_paths:
            flow = _image_flowable(Path(p))
            if flow is not None:
                story.append(KeepTogether([Spacer(1, 0.15 * cm), flow]))

    story.append(Spacer(1, 0.55 * cm))
    story.append(
        Paragraph(
            "Este documento resume una teleorientación primaria por mensajería. "
            "Las indicaciones son orientativas y deben contrastarse de forma presencial "
            "si los síntomas persisten, empeoran o aparecen signos de alarma.",
            styles["muted"],
        )
    )
    if user.consent_version and user.consent_accepted_at:
        story.append(Spacer(1, 0.2 * cm))
        story.append(
            Paragraph(
                f"Titular de la cuenta: consentimiento {user.consent_version} "
                f"el {_format_local(user.consent_accepted_at)}.",
                styles["muted"],
            )
        )

    def _on_page(canvas, doc_):
        _draw_chrome(canvas, doc_, consultation.id)

    doc.build(story, onFirstPage=_on_page, onLaterPages=_on_page)
    return path
