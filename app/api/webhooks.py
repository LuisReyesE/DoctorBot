from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db
from app.providers import get_messaging_client
from app.services.conversation import handle_incoming

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.get("/whatsapp")
async def verify_whatsapp_webhook(
    hub_mode: str | None = Query(None, alias="hub.mode"),
    hub_verify_token: str | None = Query(None, alias="hub.verify_token"),
    hub_challenge: str | None = Query(None, alias="hub.challenge"),
    settings: Settings = Depends(get_settings),
) -> str:
    """Verificación del webhook de Meta Cloud API."""
    if hub_mode == "subscribe" and hub_verify_token == settings.meta_verify_token:
        return hub_challenge or ""
    raise HTTPException(status_code=403, detail="Verification failed")


@router.post("/whatsapp")
async def whatsapp_webhook(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    if settings.messaging_provider != "meta":
        logger.warning("Webhook WhatsApp recibido con provider=%s", settings.messaging_provider)
    payload = await request.json()
    client = get_messaging_client(settings)
    messages = client.parse_webhook(payload)
    for msg in messages:
        try:
            await handle_incoming(db, client, msg)
        except Exception:
            logger.exception("Error procesando mensaje de %s", msg.external_id)
    return {"ok": True}


@router.post("/telegram")
async def telegram_webhook(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    x_telegram_bot_api_secret_token: str | None = Header(None),
) -> dict:
    """Webhook de Telegram Bot API (setWebhook → esta URL)."""
    if settings.messaging_provider != "telegram":
        logger.warning("Webhook Telegram recibido con provider=%s", settings.messaging_provider)
        # Aun así no fallamos duro si alguien prueba el endpoint
    # Solo validar si hay un secreto real (texto), no una URL mal pegada
    secret = (settings.telegram_webhook_secret or "").strip()
    if secret and not secret.startswith("http"):
        if x_telegram_bot_api_secret_token != secret:
            raise HTTPException(status_code=403, detail="Invalid Telegram secret token")

    payload = await request.json()
    client = get_messaging_client(settings)
    messages = client.parse_webhook(payload)
    for msg in messages:
        try:
            await handle_incoming(db, client, msg)
        except Exception:
            logger.exception("Error procesando mensaje Telegram de %s", msg.external_id)
    return {"ok": True}
