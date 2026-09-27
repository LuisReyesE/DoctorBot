from __future__ import annotations

import logging

import httpx

from app.config import Settings
from app.providers.base import IncomingMessage, MessagingClient

logger = logging.getLogger(__name__)


class MetaCloudWhatsAppClient(MessagingClient):
    """Cliente mínimo para Meta WhatsApp Cloud API."""

    def __init__(self, settings: Settings) -> None:
        self._token = settings.meta_whatsapp_token
        self._phone_number_id = settings.meta_phone_number_id
        self._base = f"https://graph.facebook.com/v21.0/{self._phone_number_id}"

    async def send_text(
        self,
        to_id: str,
        text: str,
        *,
        keyboard: list[list[str]] | None = None,
        remove_keyboard: bool = False,
    ) -> None:
        if not self._token or not self._phone_number_id:
            logger.warning("Meta Cloud API no configurada; mensaje no enviado a %s", to_id)
            return

        url = f"{self._base}/messages"
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }
        payload = {
            "messaging_product": "whatsapp",
            "to": to_id.lstrip("+"),
            "type": "text",
            "text": {"body": text},
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, headers=headers, json=payload)
            if resp.status_code >= 400:
                logger.error("Meta API error %s: %s", resp.status_code, resp.text)
            resp.raise_for_status()

    def parse_webhook(self, payload: dict) -> list[IncomingMessage]:
        messages: list[IncomingMessage] = []
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                for msg in value.get("messages", []):
                    body = ""
                    media_url = None
                    if msg.get("type") == "text":
                        body = msg.get("text", {}).get("body", "")
                    elif msg.get("type") == "button":
                        body = msg.get("button", {}).get("text", "")
                    elif msg.get("type") == "interactive":
                        interactive = msg.get("interactive", {})
                        if interactive.get("type") == "button_reply":
                            body = interactive.get("button_reply", {}).get("title", "")
                        elif interactive.get("type") == "list_reply":
                            body = interactive.get("list_reply", {}).get("title", "")
                    else:
                        body = f"[{msg.get('type', 'media')}]"
                    messages.append(
                        IncomingMessage(
                            external_id=str(msg.get("from", "")).lstrip("+"),
                            body=body,
                            message_id=str(msg.get("id", "")),
                            media_url=media_url,
                            raw=msg,
                        )
                    )
        return messages
