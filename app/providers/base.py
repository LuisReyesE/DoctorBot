from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class IncomingMessage:
    """Mensaje normalizado desde cualquier canal (WhatsApp, Telegram, mock)."""

    external_id: str
    body: str
    message_id: str = ""
    media_url: str | None = None  # file_id Telegram, URL, o path local
    raw: dict = field(default_factory=dict)

    @property
    def from_phone(self) -> str:
        return self.external_id


class MessagingClient(ABC):
    """Transporte de mensajería: un canal activo a la vez (mock | telegram | meta)."""

    @abstractmethod
    async def send_text(
        self,
        to_id: str,
        text: str,
        *,
        keyboard: list[list[str]] | None = None,
        remove_keyboard: bool = False,
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    def parse_webhook(self, payload: dict) -> list[IncomingMessage]:
        raise NotImplementedError

    async def send_photo(self, to_id: str, file_path: str, caption: str = "") -> None:
        """Envía una imagen. Por defecto, avisa en texto si el canal no soporta foto."""
        note = caption or file_path
        await self.send_text(to_id, f"📎 [Foto adjunta: {note}]")

    async def send_document(self, to_id: str, file_path: str, caption: str = "") -> None:
        """Envía un archivo (p. ej. PDF). Por defecto avisa en texto."""
        note = caption or file_path
        await self.send_text(to_id, f"📄 [Documento: {note}]")

    async def download_media(self, media_ref: str, dest_path: Path) -> Path | None:
        """Descarga un adjunto del canal a dest_path. None si no aplica."""
        return None


WhatsAppClient = MessagingClient
