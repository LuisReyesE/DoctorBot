from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from app.config import Settings, get_settings
from app.db import get_db
from app.models import Consultation, Doctor, DoctorShift, Message, Payment, User, Verifier
from app.providers import get_messaging_client
from app.services.caja import confirm_and_notify, reject_and_notify
from app.services.bridge import deliver_queued_to_doctor
from app.services.panel_ops import force_close_consultation, mark_doctor_offline, reassign_consultation
from app.services.consent import format_holder_document
from app.services.doctor_display import format_doctor_credentials, specialty_label
from app.services.subscription import format_pay_code
from app.services.reminders import process_followup_reminders
from app.services.routing import SOFT_SPECIALTIES, doctor_load_today
from app.services.shifts import WEEKDAYS, doctor_on_shift, is_on_shift, load_shifts, parse_hhmm, shifts_by_doctor
from app.services.timeutil import format_lima

router = APIRouter(prefix="/panel", tags=["panel"])

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

COOKIE_NAME = "sf_panel_auth"


def patient_name_for_consultation(c: Consultation) -> str:
    """Nombre declarado en el triaje de ESA consulta (no el último del User)."""
    try:
        data = json.loads(c.triage_json or "{}")
        name = (data.get("name") or "").strip()
        if name:
            return name
    except (json.JSONDecodeError, TypeError):
        pass
    if c.user and c.user.name:
        return c.user.name
    return "—"


def _lima_filter(value: datetime | None) -> str:
    return format_lima(value)


def triage_photo_path(c: Consultation) -> str | None:
    try:
        data = json.loads(c.triage_json or "{}")
    except (json.JSONDecodeError, TypeError):
        return None
    path = data.get("photo_path")
    if path and Path(path).is_file():
        return path
    return None


templates.env.globals["patient_name"] = patient_name_for_consultation
templates.env.globals["pay_code"] = format_pay_code
templates.env.globals["holder_document"] = format_holder_document
templates.env.globals["doctor_credentials"] = (
    lambda d: format_doctor_credentials(d, markdown=False) if d else "—"
)
templates.env.globals["specialty_label"] = specialty_label
templates.env.filters["lima"] = _lima_filter


def _safe_storage_file(path: Path, settings: Settings) -> Path | None:
    try:
        resolved = path.resolve()
        root = Path(settings.storage_dir).resolve()
        if root not in resolved.parents and resolved != root:
            return None
        if resolved.is_file():
            return resolved
    except OSError:
        return None
    return None


def _image_media_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".png":
        return "image/png"
    if suffix == ".webp":
        return "image/webp"
    if suffix == ".gif":
        return "image/gif"
    return "image/jpeg"


def _flash(url: str, message: str) -> RedirectResponse:
    resp = RedirectResponse(url, status_code=303)
    resp.set_cookie("sf_flash", message, max_age=8, httponly=True)
    return resp


def _render_doctors(request: Request, db: Session, *, form_error: str | None, status_code: int = 200):
    items = db.query(Doctor).order_by(Doctor.id.asc()).all()
    loads = {d.id: doctor_load_today(db, d.id) for d in items}
    by_shifts = shifts_by_doctor(db, [d.id for d in items])
    on_shift = {d.id: is_on_shift(by_shifts.get(d.id, [])) for d in items}
    return _render(
        request,
        "panel/doctors.html",
        items=items,
        form_error=form_error,
        specialties=SOFT_SPECIALTIES,
        loads=loads,
        shifts_map=by_shifts,
        on_shift=on_shift,
        weekdays=WEEKDAYS,
        status_code=status_code,
    )


def _authed(request: Request, settings: Settings) -> bool:
    return request.cookies.get(COOKIE_NAME) == settings.panel_token


def _ctx(request: Request, **extra) -> dict:
    flash = request.cookies.get("sf_flash")
    data = {
        "path": request.url.path.replace("/panel", "") or "/",
        "flash": flash,
    }
    data.update(extra)
    return data


def _render(request: Request, name: str, status_code: int = 200, **extra):
    return templates.TemplateResponse(
        request,
        name,
        _ctx(request, **extra),
        status_code=status_code,
    )


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, settings: Settings = Depends(get_settings)):
    if not settings.panel_enabled:
        raise HTTPException(status_code=404)
    if _authed(request, settings):
        return RedirectResponse("/panel", status_code=303)
    return _render(request, "panel/login.html", error=None)


@router.post("/login")
async def login_submit(
    request: Request,
    token: str = Form(...),
    settings: Settings = Depends(get_settings),
):
    if token != settings.panel_token:
        return _render(request, "panel/login.html", status_code=401, error="Token incorrecto")
    resp = RedirectResponse("/panel", status_code=303)
    resp.set_cookie(COOKIE_NAME, token, httponly=True, samesite="lax")
    return resp


@router.get("/logout")
async def logout():
    resp = RedirectResponse("/panel/login", status_code=303)
    resp.delete_cookie(COOKIE_NAME)
    return resp


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    if not _authed(request, settings):
        return RedirectResponse("/panel/login", status_code=303)

    open_statuses = ["triaging", "queued", "assigned", "active"]
    stats = {
        "open": db.query(Consultation).filter(Consultation.status.in_(open_statuses)).count(),
        "queued": db.query(Consultation).filter(Consultation.status == "queued").count(),
        "closed": db.query(Consultation).filter(Consultation.status == "closed").count(),
        "emergency": db.query(Consultation).filter(Consultation.status == "emergency").count(),
        "doctors_available": db.query(Doctor).filter(Doctor.status == "available").count(),
        "subs_active": db.query(User)
        .filter(User.subscription_status.in_(["active", "trial"]))
        .count(),
        "payments": db.query(Payment).count(),
        "payments_pending": db.query(Payment).filter(Payment.status == "pending").count(),
        "cajas": db.query(Verifier).count(),
    }
    recent = (
        db.query(Consultation)
        .options(joinedload(Consultation.user), joinedload(Consultation.doctor))
        .order_by(Consultation.id.desc())
        .limit(15)
        .all()
    )
    resp = _render(request, "panel/dashboard.html", stats=stats, recent=recent)
    if request.cookies.get("sf_flash"):
        resp.delete_cookie("sf_flash")
    return resp


@router.get("/api/live")
async def panel_live_snapshot(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """Versión liviana para auto-refresco del panel (polling)."""
    if not _authed(request, settings):
        raise HTTPException(status_code=401, detail="No autenticado")
    last_c = db.query(Consultation.id).order_by(Consultation.id.desc()).first()
    last_m = db.query(Message.id).order_by(Message.id.desc()).first()
    last_p = db.query(Payment.id).order_by(Payment.id.desc()).first()
    open_n = (
        db.query(Consultation)
        .filter(Consultation.status.in_(["triaging", "queued", "assigned", "active"]))
        .count()
    )
    pending_p = db.query(Payment).filter(Payment.status == "pending").count()
    voucher_n = (
        db.query(Payment)
        .filter(Payment.status == "pending", Payment.voucher_path.isnot(None))
        .count()
    )
    # Incluye estados de médicos para ver /disponible sin F5
    docs = db.query(Doctor.id, Doctor.status, Doctor.active_consultation_id).all()
    doc_sig = "|".join(f"{d.id}:{d.status}:{d.active_consultation_id or 0}" for d in docs)
    return {
        "v": (
            f"c{last_c[0] if last_c else 0}-m{last_m[0] if last_m else 0}"
            f"-o{open_n}-p{last_p[0] if last_p else 0}-q{pending_p}-v{voucher_n}-{doc_sig}"
        ),
        "open": open_n,
    }


@router.get("/consultations", response_class=HTMLResponse)
async def consultations_list(
    request: Request,
    status: str | None = Query(None),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    if not _authed(request, settings):
        return RedirectResponse("/panel/login", status_code=303)
    q = db.query(Consultation).options(
        joinedload(Consultation.user), joinedload(Consultation.doctor)
    )
    if status:
        q = q.filter(Consultation.status == status)
    items = q.order_by(Consultation.id.desc()).limit(200).all()
    statuses = ["triaging", "queued", "assigned", "active", "closed", "emergency"]
    return _render(
        request,
        "panel/consultations.html",
        items=items,
        status=status,
        statuses=statuses,
    )


@router.get("/consultations/{consultation_id}", response_class=HTMLResponse)
async def consultation_detail(
    consultation_id: int,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    if not _authed(request, settings):
        return RedirectResponse("/panel/login", status_code=303)
    c = (
        db.query(Consultation)
        .options(joinedload(Consultation.user), joinedload(Consultation.doctor))
        .filter(Consultation.id == consultation_id)
        .first()
    )
    if not c:
        raise HTTPException(status_code=404, detail="Consulta no encontrada")
    messages = (
        db.query(Message)
        .filter(Message.consultation_id == c.id)
        .order_by(Message.created_at.asc())
        .all()
    )
    try:
        triage_pretty = json.dumps(json.loads(c.triage_json or "{}"), indent=2, ensure_ascii=False)
    except json.JSONDecodeError:
        triage_pretty = c.triage_json or "{}"
    candidates = (
        db.query(Doctor)
        .filter(Doctor.active_consultation_id.is_(None))
        .order_by(Doctor.id.asc())
        .all()
    )
    return _render(
        request,
        "panel/consultation_detail.html",
        c=c,
        messages=messages,
        triage_pretty=triage_pretty,
        has_photo=bool(triage_photo_path(c)),
        candidates=candidates,
        can_reassign=c.status in {"queued", "assigned", "active"},
        can_force_close=c.status in {"triaging", "queued", "assigned", "active"},
    )


@router.get("/consultations/{consultation_id}/pdf")
async def consultation_pdf(
    consultation_id: int,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    if not _authed(request, settings):
        return RedirectResponse("/panel/login", status_code=303)
    c = db.query(Consultation).filter(Consultation.id == consultation_id).first()
    if not c or not c.pdf_path:
        raise HTTPException(status_code=404, detail="PDF no disponible")
    path = Path(c.pdf_path)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Archivo PDF no encontrado")
    return FileResponse(path, media_type="application/pdf", filename=path.name)


@router.get("/consultations/{consultation_id}/photo")
async def consultation_photo(
    consultation_id: int,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    if not _authed(request, settings):
        return RedirectResponse("/panel/login", status_code=303)
    c = db.query(Consultation).filter(Consultation.id == consultation_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="Consulta no encontrada")
    path_s = triage_photo_path(c)
    if not path_s:
        raise HTTPException(status_code=404, detail="Foto no disponible")
    path = Path(path_s)
    media_type = "image/jpeg"
    suffix = path.suffix.lower()
    if suffix == ".png":
        media_type = "image/png"
    elif suffix == ".webp":
        media_type = "image/webp"
    elif suffix == ".gif":
        media_type = "image/gif"
    return FileResponse(path, media_type=media_type, filename=path.name)


@router.get("/doctors", response_class=HTMLResponse)
async def doctors_list(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    if not _authed(request, settings):
        return RedirectResponse("/panel/login", status_code=303)
    client = get_messaging_client(settings)
    for d in db.query(Doctor).filter(Doctor.status == "available").all():
        await deliver_queued_to_doctor(db, client, d, require_shift=True)
    return _render_doctors(request, db, form_error=None)


@router.post("/doctors")
async def doctors_create(
    request: Request,
    name: str = Form(...),
    external_id: str = Form(...),
    license_code: str = Form(""),
    specialty: str = Form("general"),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """Alta de médico en SQLite (no en .env). El external_id es chat_id TG o teléfono WA."""
    if not _authed(request, settings):
        return RedirectResponse("/panel/login", status_code=303)

    name = (name or "").strip()
    ext = str(external_id or "").strip().lstrip("+")
    lic = (license_code or "").strip() or None
    spec = (specialty or "general").strip().lower()
    if spec not in SOFT_SPECIALTIES:
        spec = "general"
    if not name or not ext:
        return _render_doctors(
            request, db, form_error="Nombre e ID de canal son obligatorios.", status_code=400
        )
    if db.query(Doctor).filter(Doctor.external_id == ext).first():
        return _render_doctors(
            request,
            db,
            form_error=f"Ya existe un médico con ID de canal «{ext}».",
            status_code=400,
        )
    if db.query(Verifier).filter(Verifier.external_id == ext).first():
        return _render_doctors(
            request,
            db,
            form_error=f"Ese ID de canal ya es una caja. Usa otro número.",
            status_code=400,
        )

    doctor = Doctor(
        name=name[:120],
        external_id=ext[:64],
        license_code=(lic[:40] if lic else None),
        specialty=spec,
        status="offline",
    )
    db.add(doctor)
    db.commit()

    resp = RedirectResponse("/panel/doctors", status_code=303)
    resp.set_cookie(
        "sf_flash",
        f"Médico «{name}» agregado. Debe escribir /disponible en el bot para recibir casos.",
        max_age=8,
        httponly=True,
    )
    return resp


@router.get("/users", response_class=HTMLResponse)
async def users_list(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    if not _authed(request, settings):
        return RedirectResponse("/panel/login", status_code=303)
    items = (
        db.query(User)
        .options(joinedload(User.consultations))
        .order_by(User.id.desc())
        .limit(200)
        .all()
    )
    return _render(request, "panel/users.html", items=items)


@router.get("/cajas", response_class=HTMLResponse)
async def cajas_list(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    if not _authed(request, settings):
        return RedirectResponse("/panel/login", status_code=303)
    items = db.query(Verifier).order_by(Verifier.id.asc()).all()
    return _render(request, "panel/cajas.html", items=items, form_error=None)


@router.post("/cajas")
async def cajas_create(
    request: Request,
    name: str = Form(...),
    external_id: str = Form(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    if not _authed(request, settings):
        return RedirectResponse("/panel/login", status_code=303)
    name = (name or "").strip()
    ext = str(external_id or "").strip().lstrip("+")
    items = db.query(Verifier).order_by(Verifier.id.asc()).all()
    if not name or not ext:
        return _render(
            request,
            "panel/cajas.html",
            items=items,
            form_error="Nombre e ID de canal son obligatorios.",
            status_code=400,
        )
    if db.query(Verifier).filter(Verifier.external_id == ext).first():
        return _render(
            request,
            "panel/cajas.html",
            items=items,
            form_error=f"Ya existe una caja con ID «{ext}».",
            status_code=400,
        )
    if db.query(Doctor).filter(Doctor.external_id == ext).first():
        return _render(
            request,
            "panel/cajas.html",
            items=items,
            form_error="Ese ID de canal ya es un médico. Usa otro número.",
            status_code=400,
        )
    db.add(Verifier(name=name[:120], external_id=ext[:64]))
    db.commit()
    return _flash("/panel/cajas", f"Caja «{name}» agregada. Recibirá las fichas de Yape en el bot.")


@router.post("/cajas/{verifier_id}/delete")
async def cajas_delete(
    verifier_id: int,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    if not _authed(request, settings):
        return RedirectResponse("/panel/login", status_code=303)
    row = db.query(Verifier).filter(Verifier.id == verifier_id).first()
    if row:
        db.delete(row)
        db.commit()
    return _flash("/panel/cajas", "Caja eliminada.")


@router.get("/payments", response_class=HTMLResponse)
async def payments_list(
    request: Request,
    status: str | None = Query(None),
    q: str | None = Query(None),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    if not _authed(request, settings):
        return RedirectResponse("/panel/login", status_code=303)
    query = db.query(Payment).options(joinedload(Payment.user), joinedload(Payment.verifier))
    if status in {"pending", "paid", "failed", "simulated"}:
        query = query.filter(Payment.status == status)
    needle = (q or "").strip()
    if needle:
        like = f"%{needle}%"
        code = needle.upper().replace("SALUD-", "")
        query = query.filter(
            or_(
                Payment.reference.ilike(like),
                Payment.yape_name.ilike(like),
                Payment.yape_phone.ilike(like),
                Payment.reference == code,
            )
        )
    items = query.order_by(Payment.id.desc()).limit(200).all()
    pending_n = db.query(Payment).filter(Payment.status == "pending").count()
    return _render(
        request,
        "panel/payments.html",
        items=items,
        status=status,
        q=needle,
        pending_n=pending_n,
        yape_phone=settings.yape_phone,
        yape_name=settings.yape_name,
    )


@router.get("/payments/{payment_id}/voucher")
async def payment_voucher(
    payment_id: int,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    if not _authed(request, settings):
        return RedirectResponse("/panel/login", status_code=303)
    p = db.query(Payment).filter(Payment.id == payment_id).first()
    if not p or not p.voucher_path:
        raise HTTPException(status_code=404, detail="Voucher no disponible")
    path = _safe_storage_file(Path(p.voucher_path), settings)
    if path is None:
        raise HTTPException(status_code=404, detail="Archivo no encontrado")
    return FileResponse(path, media_type=_image_media_type(path), filename=path.name)


@router.post("/payments/{payment_id}/confirm")
async def payment_confirm(
    payment_id: int,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    if not _authed(request, settings):
        return RedirectResponse("/panel/login", status_code=303)
    p = (
        db.query(Payment)
        .options(joinedload(Payment.user))
        .filter(Payment.id == payment_id)
        .first()
    )
    if not p:
        raise HTTPException(status_code=404, detail="Pago no encontrado")
    if p.status == "paid":
        return _flash("/panel/payments?status=pending", "Ese pago ya estaba confirmado.")
    client = get_messaging_client(settings)
    try:
        await confirm_and_notify(db, client, p, actor_name="Panel")
    except ValueError as exc:
        return _flash("/panel/payments?status=pending", str(exc))
    return _flash(
        "/panel/payments?status=pending",
        f"Confirmado {format_pay_code(p.reference)}. El paciente fue avisado.",
    )


@router.post("/payments/{payment_id}/reject")
async def payment_reject(
    payment_id: int,
    request: Request,
    reason: str = Form(""),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    if not _authed(request, settings):
        return RedirectResponse("/panel/login", status_code=303)
    p = (
        db.query(Payment)
        .options(joinedload(Payment.user))
        .filter(Payment.id == payment_id)
        .first()
    )
    if not p:
        raise HTTPException(status_code=404, detail="Pago no encontrado")
    why = (reason or "").strip() or "No encontramos el Yape con ese código o el monto no cuadra"
    client = get_messaging_client(settings)
    try:
        await reject_and_notify(db, client, p, reason=why, actor_name="Panel")
    except ValueError as exc:
        return _flash("/panel/payments?status=pending", str(exc))
    return _flash("/panel/payments?status=pending", f"Rechazado {format_pay_code(p.reference)}.")


@router.post("/consultations/{consultation_id}/reassign")
async def consultation_reassign(
    consultation_id: int,
    request: Request,
    doctor_id: int = Form(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    if not _authed(request, settings):
        return RedirectResponse("/panel/login", status_code=303)
    c = (
        db.query(Consultation)
        .options(joinedload(Consultation.user), joinedload(Consultation.doctor))
        .filter(Consultation.id == consultation_id)
        .first()
    )
    if not c:
        raise HTTPException(status_code=404, detail="Consulta no encontrada")
    new_doc = db.query(Doctor).filter(Doctor.id == doctor_id).first()
    if not new_doc:
        return _flash(f"/panel/consultations/{consultation_id}", "Médico destino no existe.")
    client = get_messaging_client(settings)
    err = await reassign_consultation(db, client, c, new_doc)
    if err:
        return _flash(f"/panel/consultations/{consultation_id}", err)
    return _flash(f"/panel/consultations/{consultation_id}", f"Reasignada a {new_doc.name}.")


@router.post("/consultations/{consultation_id}/force-close")
async def consultation_force_close(
    consultation_id: int,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    if not _authed(request, settings):
        return RedirectResponse("/panel/login", status_code=303)
    c = (
        db.query(Consultation)
        .options(joinedload(Consultation.user), joinedload(Consultation.doctor))
        .filter(Consultation.id == consultation_id)
        .first()
    )
    if not c:
        raise HTTPException(status_code=404, detail="Consulta no encontrada")
    if c.status in {"closed", "emergency"}:
        return _flash(f"/panel/consultations/{consultation_id}", "La consulta ya está cerrada.")
    client = get_messaging_client(settings)
    await force_close_consultation(db, client, c)
    return _flash(f"/panel/consultations/{consultation_id}", "Consulta cerrada desde el panel.")


@router.get("/doctors/{doctor_id}", response_class=HTMLResponse)
async def doctor_detail(
    doctor_id: int,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    if not _authed(request, settings):
        return RedirectResponse("/panel/login", status_code=303)
    d = db.query(Doctor).filter(Doctor.id == doctor_id).first()
    if not d:
        raise HTTPException(status_code=404, detail="Médico no encontrado")
    shifts = load_shifts(db, d.id)
    return _render(
        request,
        "panel/doctor_detail.html",
        d=d,
        shifts=shifts,
        specialties=SOFT_SPECIALTIES,
        weekdays=WEEKDAYS,
        on_shift=doctor_on_shift(db, d),
        load_today=doctor_load_today(db, d.id),
        form_error=None,
    )


@router.post("/doctors/{doctor_id}/edit")
async def doctor_edit(
    doctor_id: int,
    request: Request,
    name: str = Form(...),
    external_id: str = Form(...),
    license_code: str = Form(""),
    specialty: str = Form("general"),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    if not _authed(request, settings):
        return RedirectResponse("/panel/login", status_code=303)
    d = db.query(Doctor).filter(Doctor.id == doctor_id).first()
    if not d:
        raise HTTPException(status_code=404, detail="Médico no encontrado")
    name = (name or "").strip()
    ext = str(external_id or "").strip().lstrip("+")
    spec = (specialty or "general").strip().lower()
    if spec not in SOFT_SPECIALTIES:
        spec = "general"
    if not name or not ext:
        return _flash(f"/panel/doctors/{doctor_id}", "Nombre e ID de canal son obligatorios.")
    other = db.query(Doctor).filter(Doctor.external_id == ext, Doctor.id != d.id).first()
    if other:
        return _flash(f"/panel/doctors/{doctor_id}", f"El ID de canal «{ext}» ya está en uso.")
    d.name = name[:120]
    d.external_id = ext[:64]
    d.license_code = ((license_code or "").strip()[:40] or None)
    d.specialty = spec
    db.add(d)
    db.commit()
    return _flash(f"/panel/doctors/{doctor_id}", "Médico actualizado.")


@router.post("/doctors/{doctor_id}/offline")
async def doctor_offline(
    doctor_id: int,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    if not _authed(request, settings):
        return RedirectResponse("/panel/login", status_code=303)
    d = db.query(Doctor).filter(Doctor.id == doctor_id).first()
    if not d:
        raise HTTPException(status_code=404, detail="Médico no encontrado")
    client = get_messaging_client(settings)
    note = await mark_doctor_offline(db, client, d)
    return _flash(f"/panel/doctors/{doctor_id}", note)


@router.post("/doctors/{doctor_id}/shifts")
async def doctor_shift_add(
    doctor_id: int,
    request: Request,
    weekday: int = Form(...),
    start_hm: str = Form(...),
    end_hm: str = Form(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    if not _authed(request, settings):
        return RedirectResponse("/panel/login", status_code=303)
    d = db.query(Doctor).filter(Doctor.id == doctor_id).first()
    if not d:
        raise HTTPException(status_code=404, detail="Médico no encontrado")
    if weekday < 0 or weekday > 6:
        return _flash(f"/panel/doctors/{doctor_id}", "Día inválido.")
    start = parse_hhmm(start_hm)
    end = parse_hhmm(end_hm)
    if not start or not end:
        return _flash(f"/panel/doctors/{doctor_id}", "Hora inválida. Usa HH:MM.")
    if start == end:
        return _flash(f"/panel/doctors/{doctor_id}", "Inicio y fin no pueden ser iguales.")
    db.add(DoctorShift(doctor_id=d.id, weekday=weekday, start_hm=start, end_hm=end))
    db.commit()
    db.refresh(d)
    client = get_messaging_client(settings)
    claimed = await deliver_queued_to_doctor(db, client, d, require_shift=True)
    extra = f" Se asignó la consulta #{claimed.id}." if claimed else ""
    return _flash(f"/panel/doctors/{doctor_id}", f"Turno {start}-{end} agregado.{extra}")


@router.post("/doctors/{doctor_id}/shifts/{shift_id}/delete")
async def doctor_shift_delete(
    doctor_id: int,
    shift_id: int,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    if not _authed(request, settings):
        return RedirectResponse("/panel/login", status_code=303)
    row = (
        db.query(DoctorShift)
        .filter(DoctorShift.id == shift_id, DoctorShift.doctor_id == doctor_id)
        .first()
    )
    if row:
        db.delete(row)
        db.commit()
    return _flash(f"/panel/doctors/{doctor_id}", "Turno eliminado.")


@router.post("/reminders/run")
async def run_reminders(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    if not _authed(request, settings):
        return RedirectResponse("/panel/login", status_code=303)
    client = get_messaging_client(settings)
    sent = await process_followup_reminders(db, client, settings)
    resp = RedirectResponse("/panel", status_code=303)
    resp.set_cookie(
        "sf_flash",
        f"Recordatorios enviados: {len(sent)} ({sent})",
        max_age=8,
        httponly=True,
    )
    return resp
