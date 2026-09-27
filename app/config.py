from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    app_env: str = "development"
    database_url: str = "sqlite:///./doctorbot.db"

    # mock | telegram | meta  (WHATSAPP_PROVIDER sigue aceptado como alias)
    messaging_provider: str = Field(
        default="mock",
        validation_alias=AliasChoices("MESSAGING_PROVIDER", "WHATSAPP_PROVIDER", "messaging_provider"),
    )

    meta_whatsapp_token: str = ""
    meta_phone_number_id: str = ""
    meta_verify_token: str = "Doctor Bot_verify"
    meta_app_secret: str = ""

    telegram_bot_token: str = ""
    # Opcional: secreto corto para validar webhooks (NUNCA pongas aquí la URL de ngrok)
    telegram_webhook_secret: str = ""
    # URL pública del túnel (ej. https://xxxx.ngrok-free.app) — sin /webhooks/...
    # Si está vacío, al arrancar se intenta leer de ngrok automáticamente
    telegram_webhook_url: str = Field(
        default="",
        validation_alias=AliasChoices(
            "TELEGRAM_WEBHOOK_URL",
            "PUBLIC_BASE_URL",
            "telegram_webhook_url",
        ),
    )
    public_base_url: str = ""
    # Registrar setWebhook al iniciar (default: sí en telegram)
    telegram_auto_webhook: bool = True
    ngrok_api_url: str = "http://127.0.0.1:4040"

    # ID del médico en el canal activo (teléfono WA o chat_id Telegram)
    seed_doctor_id: str = Field(
        default="51999900001",
        validation_alias=AliasChoices("SEED_DOCTOR_ID", "SEED_DOCTOR_PHONE", "seed_doctor_id"),
    )
    seed_doctor_name: str = "Dr. Demo"

    panel_token: str = "admin"
    panel_enabled: bool = True

    require_subscription: bool = True
    trial_days: int = 3
    price_monthly: float = 8.0
    price_quarterly: float = 22.0
    currency: str = "PEN"
    # Destino del cobro (un Yape/Plin del proyecto). Las cajas solo verifican.
    yape_phone: str = ""
    yape_name: str = ""
    plin_phone: str = ""
    plin_name: str = ""
    payment_qr_path: str = ""

    storage_dir: str = str(BASE_DIR / "storage")
    reminder_hours: int = 48
    reminder_auto: bool = True
    reminder_poll_seconds: int = 900
    jitsi_base_url: str = "https://meet.jit.si"

    @model_validator(mode="after")
    def _normalize_provider(self) -> "Settings":
        p = (self.messaging_provider or "mock").lower().strip()
        if p in {"whatsapp", "wa"}:
            p = "meta"
        if p not in {"mock", "telegram", "meta"}:
            p = "mock"
        self.messaging_provider = p
        return self

    @property
    def is_dev(self) -> bool:
        return self.app_env.lower() in {"development", "dev", "test"}

    @property
    def pdf_dir(self) -> Path:
        return Path(self.storage_dir) / "pdfs"

    # Compat
    @property
    def whatsapp_provider(self) -> str:
        return self.messaging_provider

    @property
    def seed_doctor_phone(self) -> str:
        return self.seed_doctor_id


@lru_cache
def get_settings() -> Settings:
    return Settings()
