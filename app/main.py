from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.api import dev_simulate, health, panel, webhooks
from app.config import get_settings
from app.db import SessionLocal, init_db
from app.providers import get_messaging_client
from app.services.reminders import process_followup_reminders
from app.services.seed import seed_demo_doctor
from app.services.telegram_webhook import register_telegram_webhook

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("doctorbot")


async def _reminder_loop() -> None:
    settings = get_settings()
    interval = max(60, int(settings.reminder_poll_seconds or 900))
    await asyncio.sleep(min(30, interval))
    while True:
        try:
            db = SessionLocal()
            try:
                client = get_messaging_client(settings)
                sent = await process_followup_reminders(db, client, settings)
                if sent:
                    logger.info("Recordatorios automáticos enviados: %s", sent)
            finally:
                db.close()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Error en recordatorios automáticos")
        await asyncio.sleep(interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    Path(settings.storage_dir).mkdir(parents=True, exist_ok=True)
    settings.pdf_dir.mkdir(parents=True, exist_ok=True)
    init_db()
    db = SessionLocal()
    try:
        doctor = seed_demo_doctor(db, settings)
        logger.info(
            "DB lista. Médico demo: %s (%s) provider=%s status=%s | Panel: /panel",
            doctor.name,
            doctor.external_id,
            settings.messaging_provider,
            doctor.status,
        )
    finally:
        db.close()

    if settings.messaging_provider == "telegram" and settings.telegram_auto_webhook:
        result = await register_telegram_webhook(settings)
        if result.get("ok"):
            logger.info("Webhook Telegram OK → %s", result.get("webhook_url"))
        elif result.get("skipped"):
            logger.info("Webhook Telegram omitido: %s", result.get("reason"))
        else:
            logger.warning(
                "Webhook Telegram no registrado: %s | %s",
                result.get("reason") or result.get("response"),
                "Arranca ngrok primero (ngrok http 8000) o pon TELEGRAM_WEBHOOK_URL en .env y reinicia.",
            )

    reminder_task: asyncio.Task | None = None
    if settings.reminder_auto:
        reminder_task = asyncio.create_task(_reminder_loop())
        logger.info(
            "Recordatorios automáticos cada %ss (ventana %sh)",
            settings.reminder_poll_seconds,
            settings.reminder_hours,
        )
    yield
    if reminder_task:
        reminder_task.cancel()
        try:
            await reminder_task
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title="Doctor Bot API",
    description=(
        "Backend centralizado de teleorientación médica (WhatsApp / Telegram / mock). "
        "Pacientes y médicos operan por el canal configurado; el panel web (/panel) es solo para gestión."
    ),
    version=__version__,
    lifespan=lifespan,
)

static_dir = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

app.include_router(health.router)
app.include_router(webhooks.router)
app.include_router(panel.router)

settings = get_settings()
if settings.is_dev:
    app.include_router(dev_simulate.router)


@app.get("/")
def root() -> dict:
    return {
        "name": "Doctor Bot",
        "version": __version__,
        "docs": "/docs",
        "health": "/health",
        "panel": "/panel",
    }
