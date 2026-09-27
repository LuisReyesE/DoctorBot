# Opcional: re-registra el webhook sin reiniciar (usa .env + ngrok :4040).
# Normalmente no hace falta: uvicorn lo hace al arrancar.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

.\.venv\Scripts\python.exe -c @"
import asyncio
from app.config import get_settings
from app.services.telegram_webhook import register_telegram_webhook

get_settings.cache_clear()
s = get_settings()
r = asyncio.run(register_telegram_webhook(s))
print(r)
"@
