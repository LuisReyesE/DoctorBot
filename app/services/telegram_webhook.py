from __future__ import annotations

import logging

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)


async def resolve_public_base_url(settings: Settings) -> str | None:
    """Base pública HTTPS para el webhook.

    Prioridad:
    1. TELEGRAM_WEBHOOK_URL o PUBLIC_BASE_URL en .env (si cambió ngrok, pegas ahí)
    2. API local de ngrok (http://127.0.0.1:4040) — no requiere pegar URL
    """
    for raw in (settings.telegram_webhook_url, settings.public_base_url):
        url = (raw or "").strip().rstrip("/")
        if url.startswith("https://"):
            return url

    ngrok_api = (settings.ngrok_api_url or "http://127.0.0.1:4040").rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"{ngrok_api}/api/tunnels")
            resp.raise_for_status()
            tunnels = resp.json().get("tunnels") or []
            for t in tunnels:
                public = (t.get("public_url") or "").rstrip("/")
                if public.startswith("https://"):
                    return public
    except Exception as exc:
        logger.warning("No se pudo leer ngrok en %s: %s", ngrok_api, exc)
    return None


async def register_telegram_webhook(settings: Settings) -> dict:
    """Registra setWebhook en Telegram. Devuelve un resumen para logs."""
    if settings.messaging_provider != "telegram":
        return {"ok": False, "skipped": True, "reason": "provider != telegram"}
    if not settings.telegram_bot_token:
        return {"ok": False, "skipped": True, "reason": "sin TELEGRAM_BOT_TOKEN"}

    base = await resolve_public_base_url(settings)
    if not base:
        return {
            "ok": False,
            "skipped": False,
            "reason": (
                "Sin URL pública. Arranca ngrok (ngrok http 8000) o define "
                "TELEGRAM_WEBHOOK_URL / PUBLIC_BASE_URL en .env"
            ),
        }

    webhook_url = f"{base}/webhooks/telegram"
    api = f"https://api.telegram.org/bot{settings.telegram_bot_token}/setWebhook"
    payload: dict = {"url": webhook_url}
    # Solo si hay un secreto real (NO una URL). Telegram lo envía en el header.
    secret = (settings.telegram_webhook_secret or "").strip()
    if secret and not secret.startswith("http"):
        payload["secret_token"] = secret

    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(api, json=payload)
        data = resp.json() if resp.content else {}
        if resp.status_code >= 400 or not data.get("ok"):
            logger.error("setWebhook falló %s: %s", resp.status_code, data)
            return {"ok": False, "webhook_url": webhook_url, "response": data}
        logger.info("Telegram webhook registrado: %s", webhook_url)
        return {"ok": True, "webhook_url": webhook_url, "response": data}
