from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from app.providers.base import IncomingMessage, MessagingClient

logger = logging.getLogger(__name__)


@dataclass
class MockMessagingClient(MessagingClient):
    """Guarda mensajes salientes en memoria para demos y tests."""

    sent: list[dict] = field(default_factory=list)

    async def send_text(
        self,
        to_id: str,
        text: str,
        *,
        keyboard: list[list[str]] | None = None,
        remove_keyboard: bool = False,
    ) -> None:
        entry = {
            "to": to_id,
            "text": text,
            "type": "text",
            "keyboard": keyboard,
            "remove_keyboard": remove_keyboard,
        }
        self.sent.append(entry)
        logger.info("[MOCK messaging] → %s: %s", to_id, text[:200])

    async def send_photo(self, to_id: str, file_path: str, caption: str = "") -> None:
        entry = {"to": to_id, "text": caption, "type": "photo", "file_path": file_path}
        self.sent.append(entry)
        logger.info("[MOCK photo] → %s: %s", to_id, file_path)

    async def send_document(self, to_id: str, file_path: str, caption: str = "") -> None:
        entry = {
            "to": to_id,
            "text": caption,
            "type": "document",
            "file_path": file_path,
        }
        self.sent.append(entry)
        logger.info("[MOCK document] → %s: %s", to_id, file_path)

    async def download_media(self, media_ref: str, dest_path: Path) -> Path | None:
        src = Path(media_ref)
        if not src.is_file():
            return None
        dest_path = Path(dest_path)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        if dest_path.suffix == "":
            dest_path = dest_path.with_suffix(src.suffix or ".jpg")
        shutil.copyfile(src, dest_path)
        return dest_path

    def parse_webhook(self, payload: dict) -> list[IncomingMessage]:
        """Formato simple de simulación:

        {"from": "51999...", "body": "hola", "message_id": "optional"}
        o {"messages": [{"from": "...", "body": "..."}]}
        """
        if "messages" in payload:
            return [
                IncomingMessage(
                    external_id=str(m["from"]).lstrip("+"),
                    body=str(m.get("body", "")),
                    message_id=str(m.get("message_id", "")),
                    media_url=m.get("media_url"),
                    raw=m,
                )
                for m in payload["messages"]
            ]
        if "from" in payload:
            return [
                IncomingMessage(
                    external_id=str(payload["from"]).lstrip("+"),
                    body=str(payload.get("body", "")),
                    message_id=str(payload.get("message_id", "")),
                    media_url=payload.get("media_url"),
                    raw=payload,
                )
            ]
        return []

    def clear(self) -> None:
        self.sent.clear()


MockWhatsAppClient = MockMessagingClient
