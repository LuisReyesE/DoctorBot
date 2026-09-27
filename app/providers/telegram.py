from __future__ import annotations

import logging
import re
from pathlib import Path

import httpx

from app.config import Settings
from app.providers.base import IncomingMessage, MessagingClient

logger = logging.getLogger(__name__)


def _reply_markup(
    keyboard: list[list[str]] | None, remove_keyboard: bool
) -> dict | None:
    if remove_keyboard:
        return {"remove_keyboard": True}
    if keyboard:
        return {
            "keyboard": [[{"text": cell} for cell in row] for row in keyboard],
            "resize_keyboard": True,
        }
    return None


def _wa_markdown_to_html(text: str) -> str:
    """Convierte *negrita* estilo WhatsApp a HTML de Telegram (parse_mode=HTML)."""
    escaped = (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    return re.sub(r"\*(.+?)\*", r"<b>\1</b>", escaped)


class TelegramBotClient(MessagingClient):
    """Cliente Bot API de Telegram (un bot = canal de paciente y médico)."""

    def __init__(self, settings: Settings) -> None:
        self._token = settings.telegram_bot_token
        self._secret = settings.telegram_webhook_secret
        self._base = f"https://api.telegram.org/bot{self._token}" if self._token else ""
        self._file_base = (
            f"https://api.telegram.org/file/bot{self._token}" if self._token else ""
        )

    async def send_text(
        self,
        to_id: str,
        text: str,
        *,
        keyboard: list[list[str]] | None = None,
        remove_keyboard: bool = False,
    ) -> None:
        if not self._token:
            logger.warning("Telegram no configurado; mensaje no enviado a %s", to_id)
            return

        url = f"{self._base}/sendMessage"
        payload: dict = {
            "chat_id": to_id,
            "text": _wa_markdown_to_html(text),
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        markup = _reply_markup(keyboard, remove_keyboard)
        if markup is not None:
            payload["reply_markup"] = markup
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code >= 400:
                logger.warning(
                    "Telegram send HTML falló %s: %s — reintento texto plano",
                    resp.status_code,
                    resp.text[:300],
                )
                payload_plain: dict = {
                    "chat_id": to_id,
                    "text": text,
                    "disable_web_page_preview": True,
                }
                if markup is not None:
                    payload_plain["reply_markup"] = markup
                resp = await client.post(url, json=payload_plain)
                if resp.status_code >= 400:
                    logger.error("Telegram API error %s: %s", resp.status_code, resp.text)
                resp.raise_for_status()
            else:
                resp.raise_for_status()

    async def send_photo(self, to_id: str, file_path: str, caption: str = "") -> None:
        if not self._token:
            logger.warning("Telegram no configurado; foto no enviada a %s", to_id)
            return
        path = Path(file_path)
        if not path.is_file():
            logger.warning("Foto no encontrada: %s", file_path)
            await self.send_text(to_id, f"📎 (No se pudo adjuntar la foto: {caption or path.name})")
            return

        url = f"{self._base}/sendPhoto"
        data: dict = {"chat_id": to_id}
        if caption:
            data["caption"] = _wa_markdown_to_html(caption)[:1024]
            data["parse_mode"] = "HTML"
        async with httpx.AsyncClient(timeout=60.0) as client:
            with path.open("rb") as f:
                files = {"photo": (path.name, f, "image/jpeg")}
                resp = await client.post(url, data=data, files=files)
            if resp.status_code >= 400 and caption:
                # Reintento sin HTML si el caption falla
                logger.warning(
                    "sendPhoto HTML falló %s — reintento caption plano",
                    resp.status_code,
                )
                data_plain = {"chat_id": to_id, "caption": caption[:1024]}
                with path.open("rb") as f:
                    files = {"photo": (path.name, f, "image/jpeg")}
                    resp = await client.post(url, data=data_plain, files=files)
            if resp.status_code >= 400:
                logger.error("Telegram sendPhoto error %s: %s", resp.status_code, resp.text)
                resp.raise_for_status()

    async def send_document(self, to_id: str, file_path: str, caption: str = "") -> None:
        if not self._token:
            logger.warning("Telegram no configurado; documento no enviado a %s", to_id)
            return
        path = Path(file_path)
        if not path.is_file():
            logger.warning("Documento no encontrado: %s", file_path)
            await self.send_text(to_id, f"📄 (No se pudo adjuntar: {caption or path.name})")
            return
        url = f"{self._base}/sendDocument"
        data: dict = {"chat_id": to_id}
        if caption:
            data["caption"] = _wa_markdown_to_html(caption)[:1024]
            data["parse_mode"] = "HTML"
        mime = "application/pdf" if path.suffix.lower() == ".pdf" else "application/octet-stream"
        async with httpx.AsyncClient(timeout=60.0) as client:
            with path.open("rb") as f:
                files = {"document": (path.name, f, mime)}
                resp = await client.post(url, data=data, files=files)
            if resp.status_code >= 400 and caption:
                logger.warning("sendDocument HTML falló %s — reintento caption plano", resp.status_code)
                data_plain = {"chat_id": to_id, "caption": caption[:1024]}
                with path.open("rb") as f:
                    files = {"document": (path.name, f, mime)}
                    resp = await client.post(url, data=data_plain, files=files)
            if resp.status_code >= 400:
                logger.error("Telegram sendDocument error %s: %s", resp.status_code, resp.text)
                resp.raise_for_status()

    async def download_media(self, media_ref: str, dest_path: Path) -> Path | None:
        """media_ref = file_id de Telegram."""
        if not self._token or not media_ref:
            return None
        dest_path = Path(dest_path)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        async with httpx.AsyncClient(timeout=60.0) as client:
            meta = await client.get(f"{self._base}/getFile", params={"file_id": media_ref})
            if meta.status_code >= 400:
                logger.error("getFile falló %s: %s", meta.status_code, meta.text)
                return None
            data = meta.json()
            if not data.get("ok"):
                logger.error("getFile no ok: %s", data)
                return None
            file_path = data["result"].get("file_path")
            if not file_path:
                return None
            # Conservar extensión si viene en file_path
            if dest_path.suffix == "" and "." in file_path:
                dest_path = dest_path.with_suffix(Path(file_path).suffix)
            file_resp = await client.get(f"{self._file_base}/{file_path}")
            if file_resp.status_code >= 400:
                logger.error("download file falló %s", file_resp.status_code)
                return None
            dest_path.write_bytes(file_resp.content)
            return dest_path

    def parse_webhook(self, payload: dict) -> list[IncomingMessage]:
        msg = payload.get("message") or payload.get("edited_message")
        if not msg:
            return []
        chat = msg.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is None:
            return []

        body = msg.get("text") or msg.get("caption") or ""
        media_url: str | None = None

        photos = msg.get("photo") or []
        if photos:
            # La última es la de mayor resolución
            media_url = str(photos[-1].get("file_id") or "")
            if not body:
                body = "[photo]"
        elif msg.get("document"):
            doc = msg["document"]
            mime = (doc.get("mime_type") or "").lower()
            if mime.startswith("image/"):
                media_url = str(doc.get("file_id") or "")
                if not body:
                    body = "[photo]"
            elif not body:
                body = "[document]"

        return [
            IncomingMessage(
                external_id=str(chat_id),
                body=body,
                message_id=str(msg.get("message_id", "")),
                media_url=media_url or None,
                raw=msg,
            )
        ]
