from app.config import Settings
from app.providers.base import MessagingClient, WhatsAppClient
from app.providers.meta_cloud import MetaCloudWhatsAppClient
from app.providers.mock import MockMessagingClient, MockWhatsAppClient
from app.providers.telegram import TelegramBotClient

_mock_singleton: MockMessagingClient | None = None


def get_messaging_client(settings: Settings) -> MessagingClient:
    """Devuelve el cliente del canal activo (un solo provider a la vez)."""
    global _mock_singleton
    provider = settings.messaging_provider.lower().strip()
    if provider == "meta":
        return MetaCloudWhatsAppClient(settings)
    if provider == "telegram":
        return TelegramBotClient(settings)
    if _mock_singleton is None:
        _mock_singleton = MockMessagingClient()
    return _mock_singleton


def get_whatsapp_client(settings: Settings) -> MessagingClient:
    """Alias histórico de get_messaging_client."""
    return get_messaging_client(settings)


def get_mock_client() -> MockMessagingClient:
    global _mock_singleton
    if _mock_singleton is None:
        _mock_singleton = MockMessagingClient()
    return _mock_singleton


__all__ = [
    "MessagingClient",
    "WhatsAppClient",
    "MockMessagingClient",
    "MockWhatsAppClient",
    "MetaCloudWhatsAppClient",
    "TelegramBotClient",
    "get_messaging_client",
    "get_whatsapp_client",
    "get_mock_client",
]
